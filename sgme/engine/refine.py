"""engine/refine.py：提炼调度入口。

- refine_file(file_id): Session 级增量提炼
  - 读 raw 文件 → 增量段提取 → L1 提取 → 归一化 → 返回记忆列表
  - 更新 raw_files.last_refined_seq/refined_at/status
- refine_batch(): 扫 status=new 的文件批量提炼
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from sgme import config
from sgme.engine import l1, l2, normalize
from sgme.prompts import BucketCtx
from sgme.raw import store
from sgme.data import memory_dao, session_dao

logger = logging.getLogger("sgme.engine.refine")


@dataclass
class RefineResult:
    """单次提炼结果。"""
    file_id: str
    memories: list[dict] = field(default_factory=list)  # 归一化后的记忆（含 dimension_ids）
    stats: normalize.NormalizeStats | None = None
    new_last_refined_seq: int | None = None
    status: str = "refined"
    anomaly_warn: bool = False
    error: str | None = None
    prompt_versions: dict = field(default_factory=dict)  # #33：{stage: {version, variant, ...}} 透传


def _format_conversation(messages: list, file_id: str) -> str:
    """把消息列表格式化为 L1 prompt 的会话文本。"""
    lines = []
    for m in messages:
        lines.append(f"[msg#{m.seq}] {m.timestamp} {m.role}:")
        if m.role == "tool" and m.tool_name:
            lines.append(f"  (tool={m.tool_name})")
        lines.append(f"  {m.content}")
        lines.append("")
    return "\n".join(lines)


def _build_registry_names(dimensions: list[dict]) -> dict[str, str]:
    """{dimension_id: display_name}。"""
    return {d["id"]: d["display_name"] for d in dimensions}


def refine_file(
    file_id: str,
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
    source_type: str = "session",
    client=None,
) -> RefineResult:
    """Session 级增量提炼单个文件。

    流程：
    1. 读 raw_files 行（取 last_refined_seq）
    2. 解析 L0 文件 → 提取增量段（seq > last_refined_seq）
    3. L1 提取（render + LLM + parse）
    4. 维度归一化（每条记忆的 dimensions → dimension_ids）
    5. 返回结果（不直接写 memories 表，由调用方决定落库时机）
    6. 更新 raw_files.last_refined_seq/refined_at/status
    """
    result = RefineResult(file_id=file_id)

    rf = session_dao.get_raw_file(session_conn, file_id)
    if rf is None:
        result.status = "error"
        result.error = f"raw_files 表无记录: {file_id}"
        return result

    # T-43：动态提炼链——session 声明的 agent_model（provider/model）→
    # 未指定 refine.llm_override 时跟随 agent 当前 LLM（复制 providers 连接参数）
    agent_model = rf.get("agent_model")
    if agent_model:
        from sgme.llm.resolve import build_refinement_cfg

        cfg = build_refinement_cfg(cfg, agent_model=agent_model)
        logger.info("提炼动态链: agent_model=%s (file=%s)", agent_model, file_id)

    try:
        parsed = store.parse_file(file_id, source_type=rf.get("source_type") or "session")
    except Exception as e:
        result.status = "error"
        result.error = f"L0 解析失败: {e}"
        session_dao.mark_status(session_conn, file_id, "error")
        return result

    # 内容哈希对比（§9.1 Dedup）：文件被修改 → 全量重提炼（游标视为 0）
    import hashlib
    from pathlib import Path

    try:
        raw_path = store.file_path(file_id, source_type=rf.get("source_type") or "session")
        current_hash = hashlib.sha256(Path(raw_path).read_bytes()).hexdigest()
    except Exception as e:
        logger.warning("raw 文件哈希计算失败（按未变处理）: %s", e)
        current_hash = None

    stored_hash = rf.get("content_hash")
    full_refine = False
    if stored_hash and current_hash and stored_hash != current_hash:
        logger.info("文件内容变化（哈希 %s → %s），全量重提炼 file=%s",
                    stored_hash[:8], current_hash[:8], file_id)
        full_refine = True

    last_seq = 0 if full_refine else (rf.get("last_refined_seq") or 0)
    incremental = store.extract_incremental(parsed, last_seq)
    if not incremental:
        # 无增量，直接标记 refined（哈希不变时更新存储哈希）
        result.new_last_refined_seq = last_seq
        session_dao.update_refine_cursor(session_conn, file_id, last_seq, status="refined")
        if current_hash:
            session_dao.update_content_hash(session_conn, file_id, current_hash)
        return result

    # 剪枝（先剪枝后分块）：tool 输出/系统注入/超长消息压缩，
    # 避免 95% 工具输出噪音撑爆分块并稀释 LLM 信噪比
    from sgme.engine import prune as prune_mod

    prune_cfg = (cfg.get("refine", {}) or {}).get("prune")
    incremental = prune_mod.prune_messages(incremental, prune_cfg)

    # 回合感知分块：剪枝后消息按「user+回答」语义单元贪心填充分块，
    # 不拆散问答对（固定字符切块会切碎上下文导致提取无效）
    l1_cfg = cfg.get("l1", {}) or {}
    chunk_size = l1_cfg.get("chunk_size", 6000)
    msg_chunks = l1.chunk_messages_by_turn(incremental, chunk_size=chunk_size)
    conversations = [
        _format_conversation(chunk_msgs, file_id) for chunk_msgs in msg_chunks
    ]

    dimensions = cfg["dimensions"]

    # L1 提取（多块逐块提炼 + 按 content 去重合并）
    # #33：bucket_ctx 携带 file_id（A/B 确定性分流）；mem_conn 记录 refine_run
    bucket_ctx = BucketCtx(bucket_key=file_id)
    try:
        raw_memories, provider, prompt_meta = l1.extract_l1(
            conversations, dimensions, cfg["llm"], client=client,
            chunk_size=chunk_size,
            overlap=l1_cfg.get("overlap", 1200),
            bucket_ctx=bucket_ctx,
            mem_conn=mem_conn,
        )
        result.prompt_versions["l1_extraction"] = prompt_meta
    except l1.RefineError as e:
        result.status = "error"
        result.error = str(e)
        result.anomaly_warn = True
        session_dao.mark_status(session_conn, file_id, "error")
        return result

    # 归一化（每条记忆的 dimensions 标签）
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = _build_registry_names(dimensions)

    # 消息 seq → 真实时间戳映射（v0.5：occurred_at = 来源消息的最大发生时刻；
    # 注意剪枝后的 incremental 已保留原 seq，映射对得上）
    seq_ts: dict[int, str] = {}
    for m in incremental:
        if getattr(m, "timestamp", None):
            seq_ts[m.seq] = m.timestamp

    normalized_memories: list[dict] = []
    batch_stats = normalize.NormalizeStats()
    for rm in raw_memories:
        dim_ids, stats = normalize.normalize_batch(
            rm["dimensions"], alias_map, registry_names,
        )
        # 累加统计
        batch_stats.total += stats.total
        batch_stats.alias_hits += stats.alias_hits
        batch_stats.fuzzy_hits += stats.fuzzy_hits
        batch_stats.drops += stats.drops
        batch_stats.fuzzy_audit.extend(stats.fuzzy_audit)
        batch_stats.dropped_names.extend(stats.dropped_names)
        if not dim_ids:
            # 全部标签丢弃 → 跳过该记忆
            continue
        # occurred_at：来源消息里最新的 timestamp（无来源消息时 None → 落库回退 created_at）
        src_ids = rm.get("source_message_ids", [])
        occ = None
        if src_ids:
            ts_list = [seq_ts[s] for s in src_ids if s in seq_ts]
            if ts_list:
                occ = max(ts_list)
        normalized_memories.append({
            "content": rm["content"],
            "memory_type": rm["memory_type"],
            "priority": rm["priority"],
            "time_velocity": rm["time_velocity"],
            "dimension_ids": dim_ids,
            "source_message_ids": src_ids,
            "file_id": file_id,
            "occurred_at": occ,
            # ST-18 替代联动：L1 supersedes 声明透传（pipeline 落库后触发旧主体记忆标记）
            "supersedes": rm.get("supersedes", []),
        })

    result.memories = normalized_memories
    result.stats = batch_stats
    result.anomaly_warn = normalize.should_warn(batch_stats)

    # 2026-08-18（T-53 免费托底）：提炼走了付费备用模型（deepseek）→ 记 anomaly_warn，
    # 供用户发现「免费主链被跳过/降级在烧钱」——降级成功不报错，此前无任何告警
    # （refine_runs 5279 次 deepseek 消耗无人知晓的根因）。
    if provider == "deepseek":
        try:
            from sgme.signal import engine as signal_engine
            signal_engine.publish(
                event_type="anomaly_warn",
                source="refine_cost",
                payload={
                    "message": "提炼使用了付费备用模型 deepseek（预期 zhipu 免费）——"
                               "检查 llm_override / agent_model 是否被劫持",
                    "provider": provider,
                    "file_id": file_id,
                    "memories_count": len(normalized_memories),
                },
                mem_conn=mem_conn,
            )
            logger.warning("提炼走 deepseek（付费备用）: file=%s memories=%d", file_id, len(normalized_memories))
        except Exception as e:
            logger.warning("deepseek 使用告警发布失败（不阻塞）: %s", e)

    # 更新提炼游标（增量段最后一行 seq）+ 同步内容哈希
    new_last_seq = max(m.seq for m in incremental) if incremental else last_seq
    result.new_last_refined_seq = new_last_seq
    session_dao.update_refine_cursor(session_conn, file_id, new_last_seq, status="refined")
    if current_hash:
        session_dao.update_content_hash(session_conn, file_id, current_hash)
    logger.info(
        "提炼完成 file=%s 增量=%d 条 记忆=%d 条 drops=%d warn=%s prompt=%s",
        file_id, len(incremental), len(normalized_memories),
        batch_stats.drops, result.anomaly_warn,
        result.prompt_versions.get("l1_extraction"),
    )

    return result


def finalize_refinement(
    result: RefineResult,
    mem_conn: sqlite3.Connection,
    cfg: dict,
    client=None,
) -> None:
    """L1.5 落库后的收尾：L2 场景聚合 + embedding + 信号发布。

    必须在 L1.5 落库（memory_id 已写回 result.memories）之后调用，
    否则 _ensure_persisted 会兜底重复落库（双写 bug，2026-08-04 修复）。
    失败不阻塞主路径，仅记日志。

    v0.7 三库拆分：L2 场景系列已迁入 memory.db，本函数不再需要独立的场景库连接，
    原第三个 conn 形参（wiki_conn）随之移除——纯连接来源调整，业务逻辑一行未改。
    """
    file_id = result.file_id
    if not result.memories or result.status != "refined":
        return

    # L2 场景聚合（记忆已带 memory_id，_ensure_persisted 直接复用）
    # #33：bucket_ctx 携带 file_id（A/B 分流）；l2_result.prompt_meta 透传
    try:
        l2_result = l2.aggregate(
            result.memories, mem_conn, cfg, client=client,
            bucket_ctx=BucketCtx(bucket_key=file_id),
        )
        result.prompt_versions["l2_scene"] = l2_result.prompt_meta
        if l2_result.error:
            logger.warning("L2 聚合失败（不阻塞）: %s", l2_result.error)
        # 场景阈值预警（软策略，仅告警）
        level, count = l2.check_scene_threshold(mem_conn, cfg)
        if level is not None:
            result.anomaly_warn = True
            logger.warning("场景数预警 level=%s count=%s", level, count)
        # 信号发布：L2 场景事件
        try:
            from sgme.signal import engine as signal_engine
            signal_engine.publish(
                event_type="memory_updated",
                source="l2",
                payload={
                    "file_id": file_id,
                    "created": l2_result.created,
                    "updated": l2_result.updated,
                    "merged": l2_result.merged,
                    "archived": l2_result.archived,
                    "error": l2_result.error,
                },
                mem_conn=mem_conn,
            )
        except Exception as e:
            logger.warning("L2 信号发布失败（不阻塞）: %s", e)
    except Exception as e:
        logger.warning("L2 聚合异常（不阻塞）: %s", e)

    # 为新记忆生成 embedding（失败不阻塞，向量检索层降级纯 BM25）
    try:
        from sgme.data.search import vector as vector_mod
        for m in result.memories:
            if m.get("memory_id"):
                vector_mod.upsert_memory_vector(
                    mem_conn, m["memory_id"], m["content"], cfg, client=client,
                )
    except Exception as e:
        logger.warning("embedding 生成失败（不阻塞）: %s", e)

    # 信号发布：提炼成功后发布 memory_updated
    try:
        from sgme.signal import engine as signal_engine
        signal_engine.publish(
            event_type="memory_updated",
            source="refine",
            payload={"file_id": file_id, "memories_count": len(result.memories)},
            mem_conn=mem_conn,
        )
    except Exception as e:
        logger.warning("信号发布失败（不阻塞）: %s", e)


def refine_batch(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
    limit: int = 100,
    client=None,
) -> list[RefineResult]:
    """批量扫描 status=new 的文件提炼。

    2026-08-06 修复（崩溃丢数据）：原实现先全部文件 L1 再统一落库，
    中途异常（如 LM Studio Model is unloaded）会导致已完成的 L1 成果
    全部丢失（raw_files 已标记 refined 但记忆未落库）。
    现改为：调用方在循环内逐文件 refine_file → _persist_memories
    （L1.5/L2/embedding 每文件立即落库），本函数只做单文件提炼，
    不持有跨文件状态。返回值语义不变（每个 RefineResult 对应一个文件）。
    """
    new_files = session_dao.list_by_status(session_conn, "new", limit=limit)
    results = []
    for rf in new_files:
        r = refine_file(rf["file_id"], mem_conn, session_conn, cfg, client=client)
        results.append(r)
    return results
