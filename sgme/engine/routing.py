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
- `route=wiki_candidate` → **wiki_pages 草稿**（T2，2026-11-14：status='draft'、
  检索天然排除、人工 promote 转正）+ `memory_pages` 互链；
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

#: 本轮处理的路由（T2 接通 wiki_candidate：落 wiki_pages 草稿 + memory_pages 互链）
_HANDLED_ROUTES = ("skill_candidate", "wiki_candidate")

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


def _open_wiki_conn(mem_conn: sqlite3.Connection) -> sqlite3.Connection | None:
    """T2：按 memory.db 推导 data_dir 后自建 wiki.db 连接（失败返回 None）。

    照 _open_skills_conn 同款（resolve_data_dir 推导 + 用完即关），原因相同：
    persist_memories 的调用方只持有 mem_conn，为分流改签名会波及全部落库路径。
    """
    try:
        from sgme.data import db as db_mod

        data_dir = db_mod.resolve_data_dir(mem_conn)
        return db_mod.connect_wiki(data_dir)
    except Exception as e:  # noqa: BLE001——wiki 库不可用不该影响记忆落库
        logger.warning("分流跳过：wiki.db 连接失败（%s）", e)
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
        `{enabled, candidates, adopted, duplicates, linked, skipped, errors,
        inbox, wiki_written}`
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
        # T2（三缺口补齐 2026-11-14）：wiki 草稿写入数（含幂等更新）
        "wiki_written": 0,
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

    # v0.3 §4 兜底矩阵：置信度不足 → 进 inbox 等人工裁决，绝不静默丢。
    # 缺 route_confidence（v023 及更早）按高置信处理——见 LOW_CONFIDENCE 注释。
    # 公共前置：对 skills / wiki 两条分支同样生效（T2：低置信不进 wiki 草稿）。
    pending: list[dict[str, Any]] = []
    for memory in targets:
        memory_id = memory["memory_id"]
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
        pending.append(memory)

    # 按 route 分组，两条分支各自独立（连接失败只 skip 本组，互不拖累）
    skill_targets = [m for m in pending if m.get("route") == "skill_candidate"]
    wiki_targets = [m for m in pending if m.get("route") == "wiki_candidate"]
    if skill_targets:
        _dispatch_skill_targets(skill_targets, mem_conn, stats, file_id)
    if wiki_targets:
        _dispatch_wiki_targets(wiki_targets, mem_conn, stats, file_id)
    return stats


def _dispatch_skill_targets(
    targets: list[dict[str, Any]],
    mem_conn: sqlite3.Connection,
    stats: dict[str, Any],
    file_id: str | None,
) -> None:
    """skill_candidate → skill_candidates 候选池（原 S2 逻辑，拆出与 wiki 分支并列）。"""
    skills_conn = _open_skills_conn(mem_conn)
    if skills_conn is None:
        stats["skipped"] += len(targets)
        return
    try:
        from sgme.data import memory_dao
        from sgme.operations import skill_candidates as sc_ops

        for memory in targets:
            memory_id = memory["memory_id"]
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
    except Exception as e:  # noqa: BLE001——分流整体是旁路
        logger.warning("三平面分流整体失败（不影响已落库记忆）file=%s: %s", file_id, e)
        stats["errors"] += 1
    finally:
        try:
            from sgme.data import db as db_mod

            db_mod.close(skills_conn)
        except Exception:  # noqa: BLE001
            pass


def _dispatch_wiki_targets(
    targets: list[dict[str, Any]],
    mem_conn: sqlite3.Connection,
    stats: dict[str, Any],
    file_id: str | None,
) -> None:
    """T2（三缺口补齐 2026-11-14）：wiki_candidate → wiki_pages 草稿 + memory_pages 互链。

    接收端前提（v0.3 S4 已就绪）：wiki_pages.status='draft'、检索侧 search_wiki_fts
    的 FTS/LIKE 两条路都只认 active（草稿天然不进检索）、promote_page 人工转正。
    page_id 确定性生成（``refine-{memory_id}``）：insert_page 按 page_id 幂等更新，
    同一条记忆重跑命中同一页，不产生第二页。

    旁路语义与 skills 分支一致：单条失败计 errors、连接失败整组计 skipped，
    绝不影响已落库记忆。
    """
    wiki_conn = _open_wiki_conn(mem_conn)
    if wiki_conn is None:
        stats["skipped"] += len(targets)
        return
    try:
        from sgme import wiki_classify
        from sgme.data import memory_dao, wiki_dao
        from sgme.wiki import fts as wiki_fts

        try:
            # 照 operations.wiki 先例：FTS 虚拟表/触发器先幂等就位，插入即索引
            # （草稿行也会进 FTS 表，但查询侧写死 status='active'，不影响检索口径）。
            wiki_fts.init_wiki_fts(wiki_conn)
        except Exception as e:  # noqa: BLE001——索引就位失败不拦写入
            logger.warning("分流 wiki：init_wiki_fts 失败（写入继续）file=%s: %s", file_id, e)

        for memory in targets:
            memory_id = memory["memory_id"]
            try:
                content = str(memory.get("content") or "")
                page_id = f"refine-{memory_id}"
                # 标题：正文首行 ≤30 字（headline 同口径），空回退 page_id；
                # 草稿不进检索，转正评审时人工可改，此处只求可辨认。
                title = wiki_classify.headline(content, page_id) or page_id
                wiki_dao.insert_page(
                    wiki_conn, page_id,
                    title=title,
                    content=content,
                    source_type="refine",
                    status="draft",
                    page_type="topic",
                    description=content[:60],
                    updated_by=f"agent_{memory.get('agent_tag') or 'unknown'}",
                )
                stats["wiki_written"] += 1
                # 互链：这条记忆的详情在该 wiki 页（与 skills 分支同款）
                stats["linked"] += memory_dao.link_memory_pages(
                    mem_conn, memory_id,
                    [{"kind": "wiki", "ref_id": page_id}],
                    relation="derived_from",
                )
            except Exception as e:  # noqa: BLE001——单条失败不影响其余
                stats["errors"] += 1
                logger.warning(
                    "分流异常（wiki 草稿）file=%s memory=%s: %s", file_id, memory_id, e,
                )
        try:
            wiki_conn.commit()
        except Exception:  # noqa: BLE001
            pass
        if stats["wiki_written"] or stats["errors"]:
            logger.info(
                "三平面分流 wiki file=%s：候选 %d / 草稿写入 %d / 互链 %d / 失败 %d",
                file_id, len(targets), stats["wiki_written"], stats["linked"], stats["errors"],
            )
    except Exception as e:  # noqa: BLE001——分流整体是旁路
        logger.warning("三平面分流 wiki 整体失败（不影响已落库记忆）file=%s: %s", file_id, e)
        stats["errors"] += 1
    finally:
        try:
            from sgme.data import db as db_mod

            db_mod.close(wiki_conn)
        except Exception:  # noqa: BLE001
            pass
