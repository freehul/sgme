"""engine/routing.py：三平面分流分发（v0.3 S2，2026-11-14）。

定位
----
L1.5 落库之后、下游消费之前的**旁路分发**：把已经落了 memories 的记忆按 route
送去各自该去的地方，而不是让它们停在记忆池里等人工捞。

为什么是「落库后」而不是「落库前」
----------------------------------
v0.2 §11.2 定下的顺序是「先分流落库 → 成功 → 才允许 memory 不写」。落地时发现
这个顺序在实现上必须倒过来走一步：

- `skill_candidates.memory_id` 是**指向 memories 表的外键式幂等锚**（UNIQUE）——
  记忆不先落库就没有 memory_id 可挂；
- 分发失败时要有退路：记忆还在 memories 里，随时可以重试；先拦截再分发则一旦
  分发失败就两头空。

所以真实顺序是：**先落 memories（带 route 标记）→ 旁路分发 → 分发成功才允许归档**。
本模块只做中间那一步；归档（`lifecycle='archived'`）留给存量治理批次（S7），
且必须 dry-run 先行——分发通道和生产可见性是两件事，不能一步到位。

当前范围（灰度）
----------------
- `route=skill_candidate` → `skill_candidates` 候选池（复用 operations 层已验证的
  幂等/去重/置信度逻辑）+ `memory_pages` 互链；
- `route=wiki_candidate` → **本轮不处理**（wiki 草稿通道在 S4），只计数；
- 总开关 `refine.routing_enabled` **默认关**。

失败语义
--------
整条链路是旁路：任何异常只记日志 + 返回统计，**绝不回滚已落库的记忆**，
也绝不抛出打断提炼主流程（记忆已经安全落库，分发是「让它更好找」，不是「让它存在」）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger("sgme.engine.routing")

#: 本轮处理的路由（wiki_candidate 的自动写入通道未接，仍留在 memories）
_HANDLED_ROUTES = ("skill_candidate",)

#: 分流置信度阈值（v0.3 §4）：低于此值不进候选池，改进 inbox 等人工裁决
LOW_CONFIDENCE = 0.6


def is_enabled(cfg: dict) -> bool:
    """refine.routing_enabled（灰度开关，默认关）。"""
    section = (cfg or {}).get("refine")
    if not isinstance(section, dict):
        return False
    return bool(section.get("routing_enabled", False))


def _open_skills_conn(mem_conn: sqlite3.Connection) -> sqlite3.Connection | None:
    """按 memory.db 推导 data_dir 后自建 skills.db 连接（失败返回 None）。

    不自建会污染调用方连接：persist_memories 的五个调用方（同步/异步/补跑）
    都只持有 mem_conn，为分流改签名会波及全部落库路径。连接隔离照
    batch_scan / dream 的既有先例：用 resolve_data_dir 推导 + 用完即关。
    """
    try:
        from sgme.data import db as db_mod

        data_dir = db_mod.resolve_data_dir(mem_conn)
        return db_mod.connect_skills(data_dir)
    except Exception as e:  # noqa: BLE001——技能库不可用不该影响记忆落库
        logger.warning("分流跳过：skills.db 连接失败（%s）", e)
        return None


def dispatch_after_persist(
    memories: list[dict[str, Any]],
    mem_conn: sqlite3.Connection,
    cfg: dict,
    *,
    file_id: str | None = None,
) -> dict[str, Any]:
    """落库后按 route 分发（旁路，绝不抛出）。

    Args:
        memories: 已落库、**带 memory_id** 的记忆列表（persist_memories 的
            refine_result.memories）。
        mem_conn: memory.db 连接。
        cfg: 运行时配置（读 refine.routing_enabled）。
        file_id: 来源文件（仅日志/统计用）。

    Returns:
        `{enabled, candidates, adopted, duplicates, linked, skipped, errors}`
    """
    stats: dict[str, Any] = {
        "enabled": False,
        "candidates": 0,
        "adopted": 0,
        "duplicates": 0,
        "linked": 0,
        "skipped": 0,
        "errors": 0,
        "inbox": 0,
    }
    if not memories or not is_enabled(cfg):
        return stats
    stats["enabled"] = True

    targets = [
        m for m in memories
        if isinstance(m, dict) and m.get("route") in _HANDLED_ROUTES and m.get("memory_id")
    ]
    stats["candidates"] = len(targets)
    if not targets:
        return stats

    skills_conn = _open_skills_conn(mem_conn)
    if skills_conn is None:
        stats["skipped"] = len(targets)
        return stats

    try:
        from sgme.data import memory_dao
        from sgme.operations import skill_candidates as sc_ops

        for memory in targets:
            memory_id = memory["memory_id"]
            # v0.3 §4 兜底矩阵：置信度不足 → 进 inbox 等人工裁决，绝不静默丢。
            # 缺 route_confidence（v023 及更早）按高置信处理——见 _route_confidence()。
            conf = memory.get("route_confidence")
            if conf is not None:
                try:
                    low = float(conf) < LOW_CONFIDENCE
                except (TypeError, ValueError):
                    low = False
                if low:
                    try:
                        from sgme.data import routing_inbox_dao

                        routing_inbox_dao.create(
                            mem_conn,
                            source_ref=memory_id,
                            source_type="memory",
                            content=str(memory.get("content") or ""),
                            route_guess=memory.get("route"),
                            confidence=float(conf),
                            reason=memory.get("route_reason"),
                        )
                        stats["inbox"] += 1
                    except Exception as e:  # noqa: BLE001
                        stats["errors"] += 1
                        logger.warning("低置信入 inbox 失败 memory=%s: %s", memory_id, e)
                    continue
            try:
                # 复用运维侧已验证的口径：幂等（memory_id UNIQUE）+ 正文指纹去重 +
                # 高置信直接 accepted、其余 pending。commit=False 由本函数按批提交。
                res = sc_ops.adopt_skill_candidate(
                    mem_conn, skills_conn, memory_id, dedupe=True, commit=False,
                )
                if not res.ok:
                    stats["errors"] += 1
                    logger.warning(
                        "分流失败（转候选池）file=%s memory=%s: %s",
                        file_id, memory_id, res.message,
                    )
                    continue
                data = res.data or {}
                if data.get("created"):
                    stats["adopted"] += 1
                else:
                    stats["duplicates"] += 1
                # 互链：这条记忆的详情在该技能（v0.3 §3.5，memory_pages 是现成载体）
                name = data.get("name")
                if name:
                    stats["linked"] += memory_dao.link_memory_pages(
                        mem_conn, memory_id,
                        [{"kind": "skill", "ref_id": name}],
                        relation="derived_from",
                    )
            except Exception as e:  # noqa: BLE001——单条失败不影响其余
                stats["errors"] += 1
                logger.warning(
                    "分流异常 file=%s memory=%s: %s", file_id, memory_id, e,
                )
        try:
            skills_conn.commit()
        except Exception:  # noqa: BLE001
            pass
        if stats["adopted"] or stats["errors"]:
            logger.info(
                "三平面分流 file=%s：候选 %d / 新入池 %d / 重复 %d / 互链 %d / 失败 %d",
                file_id, stats["candidates"], stats["adopted"],
                stats["duplicates"], stats["linked"], stats["errors"],
            )
        return stats
    except Exception as e:  # noqa: BLE001——分流整体是旁路
        logger.warning("三平面分流整体失败（不影响已落库记忆）file=%s: %s", file_id, e)
        stats["errors"] += 1
        return stats
    finally:
        try:
            from sgme.data import db as db_mod

            db_mod.close(skills_conn)
        except Exception:  # noqa: BLE001
            pass
