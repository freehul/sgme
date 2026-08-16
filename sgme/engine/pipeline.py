"""engine/pipeline.py：提炼管线编排——L1 提取 → L1.5 落库 → L2 聚合的串联器。

背景（2026-08-07 模块化重构 B30）：L1.5 落库编排（原 routes_admin._persist_memories）
与 trigger 双链路（同步/异步）曾住在 server 路由层，mcp_server 反向 import 复用——
业务逻辑归属错位、入口互相依赖（server ↔ mcp_server 环）。
本模块收编全部管线编排；HTTP/MCP 入口只做鉴权、参数解析与响应组装。

职责边界（单一职责）：
- refine.py：单文件提炼（L0→L1）+ finalize_refinement（L2 聚合 + embedding + 信号）
- l15.py：冲突裁决（候选池 + LLM，含降级直存）
- pipeline.py：串联 refine_file → L1.5 落库 → finalize，含批量逐文件容错

conn 参数模式与 refine.refine_file 一致（不持有连接生命周期）。
"""
from __future__ import annotations

import logging
import sqlite3
import uuid

from sgme.engine import l15 as l15_mod
from sgme.engine import refine as refine_mod
from sgme.prompts import BucketCtx
from sgme.data import memory_dao
from sgme.data import session_dao

logger = logging.getLogger("sgme.engine.pipeline")

# 零值统计（无记忆可落库时返回，与 L1.5 正常路径同构）
_ZERO_STATS = {
    "stored": 0, "skipped": 0, "updated": 0, "merged": 0, "archived": 0,
    "l15_error": None, "fallback": False, "supersession_rejected": 0,
}


def _apply_supersession_linkage_safe(
    mem_conn: sqlite3.Connection,
    memories: list[dict],
) -> list[str]:
    """替代联动（ST-18）安全包装：失败只记日志，不影响主落库路径。

    联动是落库后的增强步骤（标记旧主体记忆），任何异常都不应回滚/阻断已完成的落库。
    """
    try:
        return l15_mod.apply_supersession_linkage(mem_conn, memories)
    except Exception as e:
        logger.warning("替代联动失败（不影响落库）: %s", e)
        return []


def persist_memories(
    refine_result: refine_mod.RefineResult,
    mem_conn: sqlite3.Connection,
    cfg: dict,
) -> dict:
    """把 refine_file 产出的 L1 记忆经 L1.5 落库。

    L1.5 LLM 不可用时降级直存（store 全部新记忆，不丢数据）。
    落库后调用 finalize_refinement（L2 聚合 + embedding + 信号）。
    #33：L1 版本经 prompt_version 透传写入 memories；L1.5/L2 版本元信息回填 RefineResult。
    返回 L1.5 统计 dict。

    v0.7 三库拆分：L1.5 与 L2 全部落在 memory.db，本函数不再需要第二个连接，
    原 conn 形参（wiki_conn）随之移除——纯连接来源调整，业务逻辑一行未改。
    """
    if not refine_result.memories:
        return dict(_ZERO_STATS)

    file_id = refine_result.file_id
    # source_ref: {file_id}:{首个 msg seq}（溯源用）
    src_refs: list[str] = []
    for m in refine_result.memories:
        msg_ids = m.get("source_message_ids") or []
        seq = msg_ids[0] if msg_ids else 0
        src_refs.append(f"{file_id}:{seq}")

    # #33：L1 版本 → memories.prompt_version（分块混合版本用 "chunked"，精确信息在 refine_runs）
    prompt_version: str | None = None
    pm = refine_result.prompt_versions.get("l1_extraction") or {}
    if pm.get("version") and pm.get("version") != "chunked":
        prompt_version = f"{pm.get('stage', 'l1_extraction')}:{pm['version']}"

    # 尝试 L1.5 冲突提炼（带候选池 + LLM 裁决）
    try:
        l15_res = l15_mod.resolve_conflicts(
            refine_result.memories, mem_conn, cfg, source_ref=src_refs[0] if src_refs else None,
            bucket_ctx=BucketCtx(bucket_key=file_id), prompt_version=prompt_version,
        )
        if l15_res.error:
            # LLM 失败 → 降级直存
            logger.warning("L1.5 失败降级直存: %s", l15_res.error)
            stats = _fallback_direct_store(refine_result, mem_conn, cfg, src_refs,
                                           prompt_version=prompt_version)
        else:
            refine_result.prompt_versions["l1_conflict"] = l15_res.prompt_meta
            stats = {
                "stored": len(l15_res.stored),
                "skipped": len(l15_res.skipped),
                "updated": len(l15_res.updated),
                "merged": len(l15_res.merged),
                "archived": len(l15_res.archived),
                "l15_error": None,
                "fallback": False,
                "supersession_rejected": 0,
            }
        # ST-18 替代联动：L1 supersedes 声明（「X 已被 Y 替代」）→ 旧主体 X 记忆标记
        # L1.5 成功与降级直存两条路径都会写回 memory_id，联动统一在落库后执行
        superseded = _apply_supersession_linkage_safe(mem_conn, refine_result.memories)
        stats["supersession_rejected"] = len(superseded)
        if superseded:
            logger.info(
                "替代联动: %d 条旧主体记忆标记 rejected: %s",
                len(superseded), superseded,
            )
        # L1.5 落库完成后：L2 场景聚合 + embedding + 信号（记忆已带 memory_id）
        refine_mod.finalize_refinement(refine_result, mem_conn, cfg)
        return stats
    except Exception as e:
        # L1.5 异常 → 降级直存
        logger.warning("L1.5 异常降级直存: %s", e)
        stats = _fallback_direct_store(refine_result, mem_conn, cfg, src_refs,
                                       prompt_version=prompt_version)
        # 降级路径同样执行替代联动（新记忆已直存带 memory_id）
        superseded = _apply_supersession_linkage_safe(mem_conn, refine_result.memories)
        stats["supersession_rejected"] = len(superseded)
        refine_mod.finalize_refinement(refine_result, mem_conn, cfg)
        return stats


def _fallback_direct_store(
    refine_result: refine_mod.RefineResult,
    mem_conn: sqlite3.Connection,
    cfg: dict,
    src_refs: list[str],
    prompt_version: str | None = None,
) -> dict:
    """L1.5 不可用时直接 store 每条记忆。"""
    dimensions = cfg["dimensions"]
    stored = 0
    for m, src_ref in zip(refine_result.memories, src_refs):
        ttl = l15_mod._backfill_ttl(
            m.get("ttl_days"),
            m.get("dimension_ids", m.get("dimensions", [])),
            dimensions,
        )
        mid = memory_dao.insert_memory(
            mem_conn,
            content=m["content"],
            memory_type=m.get("memory_type", "persona"),
            priority=m.get("priority", 50),
            time_velocity=m.get("time_velocity", "static"),
            ttl_days=ttl,
            dimension_ids=m.get("dimension_ids", m.get("dimensions", [])),
            sources=[(src_ref, "session")],
            prompt_version=prompt_version,
        )
        m["memory_id"] = mid  # 写回，供 L2 场景关联
        stored += 1
    return {"stored": stored, "skipped": 0, "updated": 0, "merged": 0, "archived": 0,
            "l15_error": "fallback_direct_store", "fallback": True}


def refine_one(
    file_id: str,
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
) -> tuple[refine_mod.RefineResult, dict]:
    """单文件提炼：refine_file → L1.5 落库。返回 (result, l15_stats)。"""
    result = refine_mod.refine_file(file_id, mem_conn, session_conn, cfg)
    l15_stats = (
        persist_memories(result, mem_conn, cfg)
        if result.memories else dict(_ZERO_STATS)
    )
    return result, l15_stats


def refine_many(
    limit: int,
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
) -> list[tuple[refine_mod.RefineResult, dict]]:
    """批量提炼（同步）：refine_batch 收集全部 L1 结果 → 逐个 L1.5 落库。"""
    results = refine_mod.refine_batch(mem_conn, session_conn, cfg, limit=limit)
    return [
        (r, persist_memories(r, mem_conn, cfg) if r.memories else dict(_ZERO_STATS))
        for r in results
    ]


def async_refine_worker(
    file_id: str | None,
    limit: int,
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
) -> None:
    """异步提炼后台执行体（线程内运行，异常不抛出——由批扫兜底）。

    逐文件 L1→L1.5→L2 立即落库（2026-08-06 修复）：
    原实现先 refine_batch 收集全部 L1 结果再统一落库，中途异常
    （如 Model is unloaded）导致已处理文件的记忆全部丢失。
    现在每个文件独立 try/except + 立即落库，崩溃只丢当前文件。
    """
    try:
        if file_id:
            result = refine_mod.refine_file(file_id, mem_conn, session_conn, cfg)
            if result.memories:
                persist_memories(result, mem_conn, cfg)
            logger.info("async refine file=%s status=%s", file_id, result.status)
        else:
            new_files = session_dao.list_by_status(session_conn, "new", limit=limit)
            processed = 0
            for rf in new_files:
                try:
                    r = refine_mod.refine_file(rf["file_id"], mem_conn, session_conn, cfg)
                    if r.memories:
                        persist_memories(r, mem_conn, cfg)
                    processed += 1
                except Exception as e:
                    logger.warning("async refine 文件 %s 失败（继续下一文件）: %s", rf.get("file_id"), e)
            logger.info("async refine batch processed=%d", processed)
    except Exception as e:
        logger.warning("async refine 异常（后台）: %s", e)


def append_l0(
    session_key: str,
    started_at: str,
    content: str,
    source_type: str,
    ended_at: str | None,
    agent_id: str | None,
    metadata: dict | None,
    cfg: dict,
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    agent_model: str | None = None,
) -> dict:
    """L0 捕获：写 raw 文件 + raw_files 索引（HTTP append 与 MCP append 共用）。

    模块化重构 B30：原实现住在 routes_memory.append_session，MCP 侧另写一套
    简化版——重复实现、行为漂移（缺 hash/ended_at/联动提炼）。现收编为本函数。

    幂等：同 session_key + 同 started_at → 不重复生成文件段，返回既有 file_id。
    同 session_key + 不同 started_at → 追加到既有文件（status 重置为 new）。
    refine_on_append 开启时后台触发该文件提炼（不阻塞 append）。

    异常语义：解析失败抛 ValueError；raw 文件丢失抛 FileNotFoundError；
    其余写盘异常由调用方兜底（HTTP 转 api_error，MCP 转 error JSON）。
    """
    from sgme.raw import store as raw_store
    from sgme.data import session_dao

    # 写 L0 前擦除明文密钥（防工具输出带 key 进原始层，2026-08-17 安全加固）
    content = raw_store.redact_secrets(content)
    messages = raw_store.parse_body_messages(content)
    if not messages:
        raise ValueError("content 解析出 0 条消息（需 # {ISO} {role} 格式）")
    msg_dicts = [
        {"timestamp": m.timestamp, "role": m.role, "content": m.content, "tool_name": m.tool_name}
        for m in messages
    ]

    # 查既有文件（按 session_key）
    existing = session_dao.get_raw_file_by_session(session_conn, session_key)

    if existing and existing.get("started_at") == started_at:
        # 幂等：同 session_key + 同 started_at → 不重复写
        return {
            "file_id": existing["file_id"],
            "path": existing["path"],
            "status": existing["status"],
            "idempotent": True,
        }

    if existing:
        # 同 session_key 不同 started_at → 追加
        file_id = existing["file_id"]
        st = existing.get("source_type") or source_type
        try:
            raw_store.append_messages(file_id, msg_dicts, source_type=st)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"raw 文件丢失: {e}") from e
        # 重置 status=new 触发重新提炼增量段；更新文件哈希（内容已变）
        size = raw_store.file_size(file_id, source_type=st)
        new_hash = _file_content_hash(file_id, st)
        session_dao.mark_status(
            session_conn, file_id, status="new",
            ended_at=ended_at, size=size,
        )
        session_dao.update_content_hash(session_conn, file_id, new_hash)
        _maybe_refine_on_append(cfg, mem_conn, session_conn, file_id)
        return {
            "file_id": file_id,
            "path": existing["path"],
            "status": "new",
            "appended": True,
        }

    # 新文件
    file_id = str(uuid.uuid4())
    raw_store.write_new_file(
        file_id=file_id,
        session_key=session_key,
        started_at=started_at,
        agent_id=agent_id,
        source_type=source_type,
        first_messages=msg_dicts,
        metadata=metadata,
    )
    rel_path = raw_store.relative_path(file_id, source_type=source_type)
    size = raw_store.file_size(file_id, source_type=source_type)
    session_dao.insert_raw_file(
        session_conn,
        file_id=file_id,
        path=rel_path,
        session_key=session_key,
        started_at=started_at,
        agent_id=agent_id,
        agent_model=agent_model,
        ended_at=ended_at,
        status="new",
        size=size,
        content_hash=_file_content_hash(file_id, source_type),
    )
    _maybe_refine_on_append(cfg, mem_conn, session_conn, file_id)
    return {
        "file_id": file_id,
        "path": rel_path,
        "status": "new",
    }


def _file_content_hash(file_id: str, source_type: str = "session") -> str:
    """计算 raw 文件内容 SHA-256（§9.1 Dedup：内容哈希对比识别未变）。"""
    import hashlib

    from sgme.raw import store as raw_store

    path = raw_store.file_path(file_id, source_type=source_type)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _maybe_refine_on_append(
    cfg: dict,
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    file_id: str,
) -> None:
    """文件到达联动：配置 refine_on_append 开启时，append 后后台触发该文件提炼。

    默认关闭（高频写入场景每轮提炼浪费）；低频场景（如会话级写入）可开启。
    提炼走后台线程（async_refine_worker 语义），立即返回不阻塞 append。
    """
    import threading

    refine_cfg = cfg.get("refine", {}) or {}
    if not refine_cfg.get("refine_on_append", False):
        return

    def _run() -> None:
        try:
            result, _ = refine_one(file_id, mem_conn, session_conn, cfg)
            logger.info("append 联动提炼 file=%s status=%s", file_id, result.status)
        except Exception as e:
            logger.warning("append 联动提炼异常: %s", e)

    threading.Thread(target=_run, daemon=True).start()
