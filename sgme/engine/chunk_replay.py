"""engine/chunk_replay.py：块级失败补跑（D-80，2026-11-06）。

职责边界：只补跑 refine_chunk_failures 里的块，**不重提整份文件**。
与 2026-11-03 的「块失败不得静默丢内容」修复配套——那条修复用「整份不提交」实现，
代价是同一批块每轮追加都重发（实测平均 3.7 次、最坏 23 轮 / 835 次调用）。
本模块把兜底从「全量重跑」换成「逐块补跑」：

1. 提炼时：成功块照常落库并推进游标；失败块带最终块文本 + 沉淀清单入队；
2. 本模块：取队首若干块，按原上下文单块重提（走同一条 L1 → 归一化 → L1.5 落库路径）；
3. 成功即出队；累计失败达 refine.chunk_replay_max_attempts → 保留记录停止重试
   （缺口对运维可见，不再以「整份文件无限重发」为代价）。

补跑复用 l2_extraction（含块级缓存）与 refine.normalize_memories，保证与主路径同一口径。
"""

from __future__ import annotations

import logging
import re
import sqlite3

from sgme.data import chunk_failure_dao
from sgme.engine import l2_extraction
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod
from sgme.prompts import BucketCtx

logger = logging.getLogger("sgme.engine.chunk_replay")

#: 单轮补跑上限（防一批失败块挤占常规提炼额度）
DEFAULT_BATCH = 10

#: 格式化块里的消息头形如：[msg#123] 2026-11-06T10:12:29Z user:
_MSG_HEADER = re.compile(r"^\[msg#(\d+)\]\s+(\d{4}-\d{2}-\d{2}T[^\s]+)", re.MULTILINE)


def _seq_ts_from_chunk(text: str) -> dict[int, str]:
    """从块文本还原 seq → 时间戳映射（occurred_at 用）。

    refine._format_conversation 的格式是唯一来源；这样补跑不需要额外存全文件的
    消息表，也不会因为「按 seq 回查 L0」受剪枝/游标漂移影响。
    """
    out: dict[int, str] = {}
    for seq, ts in _MSG_HEADER.findall(text or ""):
        try:
            out[int(seq)] = ts
        except (TypeError, ValueError):
            continue
    return out


def _max_attempts(cfg: dict) -> int:
    try:
        value = int(((cfg or {}).get("refine") or {}).get("chunk_replay_max_attempts", 3))
    except (TypeError, ValueError):
        return 3
    return value if value >= 1 else 3


def _cache_enabled(cfg: dict) -> bool:
    return bool(((cfg or {}).get("refine") or {}).get("chunk_cache_enabled", True))


def _retry_cfg(cfg: dict) -> tuple[int, float]:
    refine_cfg = (cfg or {}).get("refine") or {}
    try:
        times = int(refine_cfg.get("chunk_retry_times", 2))
    except (TypeError, ValueError):
        times = 2
    try:
        backoff = float(refine_cfg.get("chunk_retry_backoff_s", 5.0))
    except (TypeError, ValueError):
        backoff = 5.0
    return max(0, times), max(0.0, backoff)


def replay_pending(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
    limit: int | None = None,
) -> dict:
    """补跑队列里的块（阻塞执行；调用方持提炼锁）。

    Returns:
        {"replayed": 尝试数, "resolved": 出队数, "failed": 仍失败数,
         "memories": 新增记忆数, "pending": 剩余积压数}
    """
    out = {"replayed": 0, "resolved": 0, "failed": 0, "memories": 0, "pending": 0}
    if mem_conn is None:
        return out
    chunk_failure_dao.ensure_table(mem_conn)

    # 首链熔断中不做补跑（否则会降级到付费备用模型）
    from sgme.llm import chain as llm_chain

    if llm_chain.is_primary_open("refinement"):
        logger.warning("块级补跑跳过：提炼首链熔断中")
        out["pending"] = chunk_failure_dao.count(mem_conn)
        return out

    rows = chunk_failure_dao.list_pending(
        mem_conn, limit=int(limit or DEFAULT_BATCH), max_attempts=_max_attempts(cfg),
    )
    dimensions = (cfg or {}).get("dimensions") or []
    retry_times, backoff = _retry_cfg(cfg)

    for row in rows:
        out["replayed"] += 1
        file_id = row.get("file_id") or ""
        chunk_text = row.get("chunk_text") or ""
        sunk_refs_text = row.get("sunk_refs_text") or ""
        try:
            raw_memories, _provider, _meta = l2_extraction.extract_memories(
                [chunk_text],
                dimensions,
                (cfg or {}).get("llm") or {},
                mem_conn=mem_conn,
                bucket_ctx=BucketCtx(bucket_key=file_id),
                sunk_refs_text=sunk_refs_text,
                chunk_retry_times=retry_times,
                chunk_retry_backoff_s=backoff,
                cache_enabled=_cache_enabled(cfg),
            )
        except Exception as e:  # noqa: BLE001——单块失败不影响其它块
            attempts = chunk_failure_dao.bump(mem_conn, row["failure_id"], str(e))
            out["failed"] += 1
            logger.warning(
                "块级补跑失败（file=%s chunk=%s，累计 %d/%d）: %s",
                file_id, row.get("chunk_sha"), attempts, _max_attempts(cfg), e,
            )
            continue

        from sgme.engine import sinking as sinking_mod

        sunk_refs = sinking_mod.extract_sunk_refs(chunk_text)
        normalized, _stats = refine_mod.normalize_memories(
            raw_memories, dimensions, mem_conn,
            file_id=file_id,
            seq_ts=_seq_ts_from_chunk(chunk_text),
            sunk_refs=sunk_refs,
        )
        if normalized:
            result = refine_mod.RefineResult(file_id=file_id, memories=normalized)
            try:
                pipeline_mod.persist_memories(
                    result, mem_conn, cfg,
                    agent_tag=pipeline_mod._resolve_file_agent(session_conn, file_id),
                )
            except Exception as e:  # noqa: BLE001
                chunk_failure_dao.bump(mem_conn, row["failure_id"], f"persist: {e}")
                out["failed"] += 1
                logger.warning("块级补跑落库失败（file=%s）: %s", file_id, e)
                continue
            out["memories"] += len(normalized)
        chunk_failure_dao.resolve(mem_conn, row["failure_id"])
        out["resolved"] += 1

    out["pending"] = chunk_failure_dao.count(mem_conn)
    if out["replayed"]:
        logger.info(
            "块级补跑: 尝试=%d 出队=%d 失败=%d 新增记忆=%d 积压=%d",
            out["replayed"], out["resolved"], out["failed"], out["memories"], out["pending"],
        )
    return out
