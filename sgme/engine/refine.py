"""engine/refine.py：提炼调度入口。

- refine_file(file_id): Session 级增量提炼
  - 读 raw 文件 → 增量段提取 → L1 提取 → 归一化 → 返回记忆列表
  - 失败路径递增 raw_files.fail_count / last_error（E3 失败熔断；达
    refine.max_file_attempts → status='error'）；体积闸超限（E4）直接标 error
  - 成功路径由 commit_refine 推进 last_refined_seq/refined_at/status 并清零失败计数
- refine_batch(): 扫 status=new 的文件批量提炼
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from sgme import config
from sgme.engine import l2_extraction, scene_aggregate, normalize
from sgme.llm import provider as llm_provider
from sgme.prompts import BucketCtx
from sgme.raw import store
from sgme.data import memory_dao, session_dao

logger = logging.getLogger("sgme.engine.refine")


def apply_llm_override(cfg: dict) -> dict:
    """返回带 refine.llm_override 动态链的 cfg（幂等；未配置时原样返回）。

    修复（2026-11-06）：此前 llm_override 只在 refine_file 内、且仅当文件带
    agent_model 时才被 build_refinement_cfg 使用——没有 agent_model 的文件
    （如 hermes-* 历史会话）里 sgme.yaml 的 llm_override 被静默忽略（B121 同类）。
    现统一在入口层应用；已应用过（首链 provider 与 override 相同）则跳过，避免重复。
    """
    override = (cfg.get("refine") or {}).get("llm_override") or {}
    if not override:
        return cfg
    chain = (((cfg.get("llm") or {}).get("chains") or {}).get("refinement")) or [{}]
    head = (chain[0] or {}).get("provider")
    if head == override.get("provider"):
        return cfg
    from sgme.llm.resolve import build_refinement_cfg

    return build_refinement_cfg(cfg)


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
    # T-223：本次提炼时 raw 文件的 SHA-256（含未变场景），供落库成功后的
    # commit_refine 同步到 raw_files.content_hash；哈希计算失败为 None。
    content_hash: str | None = None
    # 块级失败计数（2026-11-03）：>0 表示该文件内容有缺口，不得标 refined。
    failed_chunks: int = 0
    chunk_total: int = 0
    # E3 失败熔断（2026-11-05）：本次失败是否已计入 raw_files.fail_count——
    # batch_scan 据此避免对同一文件重复计数（只有 refine 入口之外的异常才由它记账）。
    failure_recorded: bool = False
    # 首链熔断延后（2026-11-05）：True = 本次没有真正提炼（LLM 首链熔断中，
    # 主动跳过以免降级烧付费模型）；文件保持 status=new、不计 fail_count，冷却后再试。
    deferred: bool = False
    # E5 截断计量（2026-11-05）：本次 L1 有多块输出被 max_tokens 截断
    # （finish_reason="length"）；**仅计量**，不参与提交闸/重试判定。
    # 明细随逐块 refine_runs.action_counts["l1_truncated_chunks"] 落库。
    truncated_chunks: int = 0


#: 自注入画像块（dsh/hermes 把 Tier0 画像当 user 消息写进原件时的标记）
_PROFILE_START = "--- SGME 用户画像 ---"


def _strip_injected_profile(text: str, limit: int = 30000) -> str:
    """剥掉原件里的「自注入画像」块。

    2026-10-31 实测：186 个失败文件里 27/40 在开头带 `--- SGME 用户画像 --- [Tier0 摘要]`
    ——那是我们自己注入给 agent 的画像，被当成 user 消息记进了原件。模型读到后认为
    「这些我都知道」直接返回 []，整份会话（后面还有真实对话，如报价单、方案）被判空。
    剥掉它既止血，也符合数据卫生：注入内容不该回流成源数据。
    """
    if not text or _PROFILE_START not in text:
        return text
    i = text.find(_PROFILE_START)
    tail = text[i:]
    m = re.search(r"\n---\s*\n", tail[10:])
    end = i + 10 + m.start() if m else min(len(text), i + limit)
    return (text[:i] + "\n" + text[end:]).strip() or text[:i]


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


# ======================================================================
# E3/E4（2026-11-05）：失败熔断 + 体积闸
# ======================================================================
# 背景（B312 待办 1/2）：块级失败的文件此前保持 status=new 无限重扫（实测同一批
# 超大文件空转 5 天 / 4.9 万次提炼）；>10MB 文件反复超时且 92% 落在 3 个超大文件。
# 口径：不新增 status 取值（保持 new/refined/error/archived），失败原因写 last_error，
# 连续失败次数写 fail_count，达 refine.max_file_attempts → status='error' 终止重试。

def _refine_section(cfg: dict) -> dict:
    """取 cfg 的 refine 段（缺失/非 dict → 空 dict）。"""
    section = (cfg or {}).get("refine")
    return section if isinstance(section, dict) else {}


def max_file_attempts(cfg: dict) -> int:
    """refine.max_file_attempts（失败熔断阈值；默认 5，非法 / <1 回退默认）。"""
    default = int(config.DEFAULT_REFINE_CONFIG.get("max_file_attempts", 5))
    try:
        value = int(_refine_section(cfg).get("max_file_attempts", default))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def max_file_bytes(cfg: dict) -> int:
    """refine.max_file_bytes（单文件体积闸；默认 10 MiB，0 / 负数 = 关闭）。"""
    default = int(config.DEFAULT_REFINE_CONFIG.get("max_file_bytes", 10 * 1024 * 1024))
    try:
        return int(_refine_section(cfg).get("max_file_bytes", default))
    except (TypeError, ValueError):
        return default


def record_refine_failure(
    session_conn: sqlite3.Connection,
    file_id: str,
    error: str | None,
    cfg: dict,
    *,
    terminal: bool = False,
) -> None:
    """失败熔断统一入口（E3）：递增 fail_count / last_error，达上限转 status='error'。

    记账失败只告警不抛出——记账是旁路，绝不能反过来打断提炼主流程。终态失败
    （terminal=True）在记账失败时兜底 mark_status('error')，保持修复前语义。
    """
    attempts = max_file_attempts(cfg)
    try:
        state = session_dao.record_refine_failure(
            session_conn, file_id, error, max_attempts=attempts, terminal=terminal,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("失败熔断计数写入异常（不阻塞）: file=%s err=%s", file_id, e)
        state = None
    if state is None:
        if terminal:
            try:
                session_dao.mark_status(session_conn, file_id, "error")
            except Exception as e:  # noqa: BLE001
                logger.warning("终态 error 标记失败（不阻塞）: file=%s err=%s", file_id, e)
        return
    if state.get("status") == "error" and not terminal:
        logger.warning(
            "失败熔断触发：file=%s 连续失败 %s 次（阈值 %s），已标 status=error 终止重试；"
            "last_error=%s",
            file_id, state.get("fail_count"), attempts, str(error or "")[:200],
        )


def _gate_refine_size(rf: dict, cfg: dict) -> int | None:
    """E4 体积闸：返回超限字节数；未超限 / 闸关闭 / 无法判定 → None。

    size 优先用 raw_files.size（append 时写入）；缺失时读磁盘实际大小兜底。
    读盘失败不拦（交给正常链路，失败也会被 E3 熔断兜住）。
    """
    limit = max_file_bytes(cfg)
    if limit <= 0:
        return None
    size = rf.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        size = None
    if size is None:
        try:
            size = store.file_path(
                rf["file_id"], source_type=rf.get("source_type") or "session"
            ).stat().st_size
        except Exception:  # noqa: BLE001
            size = None
    if size is None or size <= limit:
        return None
    return int(size)


#: refine_cost 告警节流窗口（秒）：同进程同一 source 最多每窗口发布 1 条
#: （F2，2026-11-05：生产 10 小时 82 条属噪声；日志仍逐次输出，不丢线索）
REFINE_COST_ALERT_INTERVAL_S = 3600.0
#: 上次实际发布 refine_cost 告警的 monotonic 时刻（None = 本进程尚未发布）
_refine_cost_alert_last_at: float | None = None


def _throttle_refine_cost_alert(now: float | None = None) -> bool:
    """refine_cost 告警节流判定（F2）：窗口内返回 False（不发布），放行时记账。"""
    global _refine_cost_alert_last_at
    ts = time.monotonic() if now is None else now
    last = _refine_cost_alert_last_at
    if last is not None and ts - last < REFINE_COST_ALERT_INTERVAL_S:
        return False
    _refine_cost_alert_last_at = ts
    return True


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

    游标语义（T-223，2026-09-30 深度审查 P0-2）：
    本函数**不再推进游标/内容哈希**——原实现在此先 ``update_refine_cursor(
    status="refined")`` 并写内容哈希，而 L1.5 落库在其后由调用方执行；落库崩溃
    （并发错误/磁盘满）后文件已 refined、批扫不再拾起、重试走「无增量」分支
    → 该文件记忆永久丢失（探针实测复现）。现改为两阶段：调用方落库成功后显式调
    ``commit_refine(result, session_conn)`` 推进游标 + 哈希；失败时文件保持
    status=new 可重扫重试。
    error 路径（raw_files 无记录/L0 解析失败/L1 失败）语义不变：直接
    ``mark_status("error")``（error 是终态型业务结果，不是「可丢弃的中间态」）。

    E3 失败熔断（2026-11-05）：所有失败路径同时递增 raw_files.fail_count 并写
    last_error；可重试失败（块级缺口）累计达 refine.max_file_attempts 后转
    status='error' 终止重扫（见 record_refine_failure）。成功路径在 commit_refine 清零。
    E4 体积闸（2026-11-05）：文件超过 refine.max_file_bytes 时**在解析/LLM 之前**
    直接标 error + last_error='file_too_large'，不消耗模型额度。
    """
    result = RefineResult(file_id=file_id)

    rf = session_dao.get_raw_file(session_conn, file_id)
    if rf is None:
        result.status = "error"
        result.error = f"raw_files 表无记录: {file_id}"
        return result

    # E4 体积闸（2026-11-05，B312 待办 2）：超限文件不进解析 / LLM——历史上
    # 16.7 / 25.4 / 12.4 MB 三个超大文件占失败风暴 92%，反复超时且耗尽重试预算。
    # 终态：status='error' + last_error='file_too_large'；不计入 E3 失败次数
    # （体积是静态事实，重试无意义；append 追加后 status 回 new 会重新过闸）。
    oversized = _gate_refine_size(rf, cfg)
    if oversized is not None:
        result.status = "error"
        result.error = "file_too_large"
        result.failure_recorded = True
        session_dao.mark_file_too_large(session_conn, file_id, size=oversized)
        logger.warning(
            "文件超过体积闸（file=%s size=%d > max_file_bytes=%d），不调 LLM 直接标 error",
            file_id, oversized, max_file_bytes(cfg),
        )
        return result

    # 首链熔断（2026-11-05）：熔断期内直接延后——不解析/不分块/不调 LLM，
    # 文件保持 status=new、不计失败；冷却后自动恢复。
    from sgme.llm import chain as llm_chain
    if llm_chain.is_primary_open("refinement"):
        result.status = "deferred"
        result.deferred = True
        result.error = "primary_circuit_open"
        logger.warning("首链熔断中，跳过提炼（file=%s，保持 status=new）", file_id)
        return result

    # 动态提炼链（T-43 + 2026-11-06 修复）：llm_override 显式指定优先于
    # agent_model；无 agent_model 也必须生效（此前被静默忽略）。
    cfg = apply_llm_override(cfg)
    agent_model = rf.get("agent_model")
    override = (cfg.get("refine") or {}).get("llm_override") or {}
    if agent_model and not override:
        from sgme.llm.resolve import build_refinement_cfg

        cfg = build_refinement_cfg(cfg, agent_model=agent_model)
        logger.info("提炼动态链: agent_model=%s (file=%s)", agent_model, file_id)

    try:
        parsed = store.parse_file(file_id, source_type=rf.get("source_type") or "session")
    except Exception as e:
        result.status = "error"
        result.error = f"L0 解析失败: {e}"
        result.failure_recorded = True
        # E3：终态失败也记账（fail_count/last_error 供运维追溯），status 仍直接 error
        record_refine_failure(session_conn, file_id, result.error, cfg, terminal=True)
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
        # 无增量：不调 L1、不写库；游标/哈希推进交由调用方落库成功后的
        # commit_refine（T-223）——「无增量」同样收敛为 refined（设计选择：
        # 文件已全部提炼过，重复重扫无意义；且本分支无落库动作，不存在
        # 「先标记后落库」的丢失窗口）。
        result.new_last_refined_seq = last_seq
        result.content_hash = current_hash
        return result

    # 沉淀清单（2026-10-31 用户定）：**必须在剪枝之前抽**——知识库 page_id 绝大多数
    # 出现在 tool 回执里，剪枝会把 tool 输出整段抹掉，剪枝后再抽永远是空清单
    # （实测教训：端到端用例把 page_id 放在 assistant 消息里能过，真实会话全是 tool 回执 → 挂 0 条）。
    from sgme.engine import sinking as sinking_mod

    _raw_text = "\n".join(
        f"{getattr(m, 'role', '')}: {getattr(m, 'content', '') or ''}" for m in incremental
    )
    sunk_refs = sinking_mod.extract_sunk_refs(_raw_text)
    sunk_refs_text = sinking_mod.render_sunk_refs(sunk_refs)
    if sunk_refs:
        logger.info("沉淀清单: 本会话命中 %d 条知识页/技能（%s）", len(sunk_refs),
                    ", ".join(f"{r.kind}:{r.ref_id[:24]}" for r in sunk_refs[:5]))

    # 剪枝（先剪枝后分块）：tool 输出/系统注入/超长消息压缩，
    # 避免 95% 工具输出噪音撑爆分块并稀释 LLM 信噪比
    from sgme.engine import prune as prune_mod

    prune_cfg = (cfg.get("refine", {}) or {}).get("prune")
    incremental = prune_mod.prune_messages(incremental, prune_cfg)

    # 剥自注入画像（见 _strip_injected_profile 注释）：不剥会让模型直接返回 []，
    # 整份会话被判「全部分块失败」（实测 186 个文件栽在这上面）
    for _m in incremental:
        try:
            _m.content = _strip_injected_profile(_m.content or "")
        except Exception:  # noqa: BLE001
            pass

    # 回合感知分块：剪枝后消息按「user+回答」语义单元贪心填充分块，
    # 不拆散问答对（固定字符切块会切碎上下文导致提取无效）
    l1_cfg = cfg.get("l2_extraction", {}) or {}
    chunk_size = l1_cfg.get("chunk_size", 6000)
    msg_chunks = l2_extraction.chunk_messages_by_turn(incremental, chunk_size=chunk_size)
    conversations = [
        _format_conversation(chunk_msgs, file_id) for chunk_msgs in msg_chunks
    ]

    dimensions = cfg["dimensions"]

    # L1 提取（多块逐块提炼 + 按 content 去重合并）
    # #33：bucket_ctx 携带 file_id（A/B 确定性分流）；mem_conn 记录 refine_run
    bucket_ctx = BucketCtx(bucket_key=file_id)
    try:
        raw_memories, provider, prompt_meta = l2_extraction.extract_memories(
            conversations, dimensions, cfg["llm"], client=client,
            chunk_size=chunk_size,
            overlap=l1_cfg.get("overlap", 1200),
            bucket_ctx=bucket_ctx,
            mem_conn=mem_conn,
            sunk_refs_text=sunk_refs_text,
            chunk_retry_times=int((cfg.get("refine") or {}).get("chunk_retry_times", 2)),
            chunk_retry_backoff_s=float((cfg.get("refine") or {}).get("chunk_retry_backoff_s", 5.0)),
        )
        result.prompt_versions["l1_extraction"] = prompt_meta
        # 块级失败（2026-11-03）：单块失败被 continue 吞掉，整份仍会标 refined →
        # 静默缺内容。这里把失败数报到结果上，由 pipeline 的提交闸拦下（不推进游标）。
        result.failed_chunks = int(prompt_meta.get("chunk_failures") or 0)
        result.chunk_total = int(prompt_meta.get("chunk_total") or 0)
        # E5 截断计量（2026-11-05）：只计数（逐块明细已写 refine_runs.action_counts），
        # 不改提交/重试/判定——max_tokens 是否偏紧靠这批数据回算。
        result.truncated_chunks = int(prompt_meta.get("l1_truncated_chunks") or 0)
        if result.truncated_chunks:
            logger.warning(
                "L1 输出截断 %d/%d 块（finish_reason=length，file=%s）——仅计量，"
                "不改重试/判定", result.truncated_chunks, result.chunk_total or 0, file_id,
            )
        if result.failed_chunks:
            result.anomaly_warn = True
            logger.warning(
                "L1 块级失败 %d/%d（file=%s）：内容有缺口，本次不推进游标，文件保持待重跑",
                result.failed_chunks, result.chunk_total, file_id,
            )
    except llm_provider.PrimaryUnavailable as e:
        # 首链熔断（2026-11-05）：主动延后——不降级、不调备用模型、不计失败次数，
        # 文件保持 status=new（batch_scan/append 联动在冷却期后自动重试）。
        result.status = "deferred"
        result.deferred = True
        result.error = str(e)
        logger.warning(
            "首链熔断，本次提炼延后（file=%s，保持 status=new，不计失败）: %s",
            file_id, e,
        )
        # 每次「熔断打开」发布一条 anomaly_warn（source=llm_primary）；已打开期间不重复发。
        try:
            from sgme.llm import chain as llm_chain
            from sgme.signal import engine as signal_engine
            alert = llm_chain.take_primary_alert("refinement")
            if alert:
                signal_engine.publish("anomaly_warn", "llm_primary", alert, mem_conn)
        except Exception as e2:  # noqa: BLE001——告警是旁路，不影响延后语义
            logger.warning("首链熔断告警发布失败（不阻塞）: %s", e2)
        return result
    except l2_extraction.RefineError as e:
        result.status = "error"
        result.error = str(e)
        result.anomaly_warn = True
        result.failure_recorded = True
        # E3：LLM 全链失败属终态结果（与修复前 mark_status("error") 一致）
        record_refine_failure(session_conn, file_id, result.error, cfg, terminal=True)
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
            # T-136 facts 透传（2026-09-03 修复）：此前归一化白名单漏 facts →
            # L1 产出的三元组在落库前被丢弃，生产 facts_json 全 NULL（冒烟实证）
            "facts": rm.get("facts"),
            # 2.0（2026-10-31）：同一个坑再犯一次——归一化的白名单同样会吃掉
            # 三轴/路由/七值类型/feeling/trait/needs_review。此处显式透传，
            # 否则解析层算得再对，落库前也会归零（实测：22 条落库后字段全 NULL）。
            **{k: rm[k] for k in (
                "type", "subject", "certainty", "derivation", "modality", "route",
                "feeling", "trait", "scene_key", "needs_review", "l2_facts_json",
            ) if rm.get(k) is not None},
            # 记忆 → 知识页/技能 关联（2026-10-31）：按标题重合度挂，落库写 memory_pages
            "_sunk_refs": [
                {"kind": r.kind, "ref_id": r.ref_id}
                for r in sinking_mod.link_targets(rm.get("content") or "", sunk_refs)
            ],
        })

    result.memories = normalized_memories
    result.stats = batch_stats
    result.anomaly_warn = normalize.should_warn(batch_stats)

    # 2026-08-18（T-53 免费托底）：提炼走了付费备用模型（deepseek）→ 记 anomaly_warn，
    # 供用户发现「免费主链被跳过/降级在烧钱」——降级成功不报错，此前无任何告警
    # （refine_runs 5279 次 deepseek 消耗无人知晓的根因）。
    # F2（2026-11-05）：①文案对齐现行降级链 agnes→deepseek→rule（zhipu 已于
    # 2026-08-29 B121 移出，旧文案仍指向已删除 provider）；②signal 事件按进程内
    # 3600s 窗口节流（生产实测 10 小时 82 条属噪声），但**日志每次照常输出**，
    # 线索不丢——只有事件面收敛。
    if provider == "deepseek":
        logger.warning("提炼走 deepseek（付费备用）: file=%s memories=%d", file_id, len(normalized_memories))
        if _throttle_refine_cost_alert():
            try:
                from sgme.signal import engine as signal_engine
                signal_engine.publish(
                    event_type="anomaly_warn",
                    source="refine_cost",
                    payload={
                        "message": "提炼使用了付费备用模型 deepseek（预期 agnes 免费；"
                                   "链：agnes→deepseek→rule）——检查 llm_override / "
                                   "agent_model 是否被劫持",
                        "provider": provider,
                        "file_id": file_id,
                        "memories_count": len(normalized_memories),
                    },
                    mem_conn=mem_conn,
                )
            except Exception as e:
                logger.warning("deepseek 使用告警发布失败（不阻塞）: %s", e)

    # 提炼完成：不在本函数推进游标/哈希（T-223 两阶段——见 docstring 游标语义）。
    # 调用方必须在 L1.5 落库成功后执行 commit_refine(result, session_conn)。
    new_last_seq = max(m.seq for m in incremental) if incremental else last_seq
    result.new_last_refined_seq = new_last_seq
    result.content_hash = current_hash
    logger.info(
        "提炼完成 file=%s 增量=%d 条 记忆=%d 条 drops=%d warn=%s prompt=%s",
        file_id, len(incremental), len(normalized_memories),
        batch_stats.drops, result.anomaly_warn,
        result.prompt_versions.get("l1_extraction"),
    )

    # E3 失败熔断（2026-11-05）：块级缺口是可重试失败——未达 max_file_attempts 时保持
    # status=new 让下轮重扫；累计达上限 → status='error' 终止重试（历史教训：无上限
    # 重扫空转 5 天 / 4.9 万次）。放在归一化全部完成之后记账：此前若中途抛异常，
    # 由批扫侧兜底记账一次，避免同一轮被重复计数。成功提交时 commit_refine 清零。
    if result.failed_chunks:
        result.failure_recorded = True
        record_refine_failure(
            session_conn, file_id,
            f"块级失败 {result.failed_chunks}/{result.chunk_total}: 内容有缺口",
            cfg,
        )

    return result


def commit_refine(
    result: RefineResult,
    session_conn: sqlite3.Connection,
) -> bool:
    """落库成功后的显式收尾：推进提炼游标 + 同步内容哈希（T-223）。

    两阶段提炼的关键第二步：调用方在 L1.5 落库成功（``persist_memories`` 正常
    返回、未抛异常）**之后**调用本函数。落库崩溃/异常时本函数不被调用 → 文件
    保持 status=new → 批扫/重试可重新提取并落库（修复「先推游标后落库、
    崩溃即永久丢记忆」的 P0-2）。

    仅对 ``status="refined"`` 的结果生效（error 等非成功态返回 False 不推进——
    error 路径的状态流转在 refine_file 内已完成）。「无增量」结果同样走本函数
    （new_last_refined_seq=原游标值），语义上收敛为 refined。
    E3（2026-11-05）：成功推进游标的同时清零 raw_files.fail_count / last_error——
    失败计数只反映「连续失败」，一次成功即归零。

    Returns:
        True 表示已推进游标；False 表示未推进（非 refined 结果）。
    """
    if result.status != "refined":
        return False
    # refined 结果必带 new_last_refined_seq（refine_file 两条路径均赋值）；
    # 防御性回退 None → 0（宁可下次全量重提炼，也不把 None 写进游标列）。
    seq = result.new_last_refined_seq if result.new_last_refined_seq is not None else 0
    session_dao.update_refine_cursor(
        session_conn, result.file_id, seq, status="refined",
    )
    if result.content_hash:
        session_dao.update_content_hash(session_conn, result.file_id, result.content_hash)
    # E3（2026-11-05）：成功路径清零失败计数——避免历史失败残留让「一次成功之后的
    # 偶发失败」过早触发熔断。归零只在真正落库成功（本函数）做，refine_file 不碰。
    session_dao.clear_refine_failure(session_conn, result.file_id)
    return True


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
    # D-53 冻结开关（2026-11-03）：2.0 取消场景层，四表冻结只读；A1 收尾（2026-11-05）
    # 起配置默认 enabled=false 停写，存量场景仅供图谱页/管理页只读展示。
    # ⚠️ 2026-11-05 修正：旧实现误读 scene_aggregate.scene_aggregate.enabled（双嵌套，
    # 恒为默认 True，导致开关形同虚设）；配置键口径统一为 scene_aggregate.enabled。
    # ⚠️ 2026-11-05（E1 复核修复）：冻结场景层只能跳过**场景聚合本身**——旧实现用
    # `return` 提前退出，把后面的 embedding 回填、source='refine' 信号发布、人格特质抽取
    # 一并跳过（场景默认关=这三个副作用全死）。现改为只包场景块。
    scene_enabled = bool((((cfg or {}).get("scene_aggregate") or {}).get("enabled", True)))
    if not scene_enabled:
        logger.debug("L2 场景聚合已冻结（scene_aggregate.enabled=false），跳过: %s", file_id)
    else:
        # #33：bucket_ctx 携带 file_id（A/B 分流）；l2_result.prompt_meta 透传
        try:
            l2_result = scene_aggregate.aggregate(
                result.memories, mem_conn, cfg, client=client,
                bucket_ctx=BucketCtx(bucket_key=file_id),
            )
            result.prompt_versions["l2_scene"] = l2_result.prompt_meta
            if l2_result.error:
                logger.warning("L2 聚合失败（不阻塞）: %s", l2_result.error)
            # 场景阈值预警（软策略，仅告警）
            level, count = scene_aggregate.check_scene_threshold(mem_conn, cfg)
            if level is not None:
                result.anomaly_warn = True
                logger.warning("场景数预警 level=%s count=%s", level, count)
            # 信号发布：L2 场景事件
            try:
                from sgme.signal import engine as signal_engine
                signal_engine.publish(
                    event_type="memory_updated",
                    source="scene_aggregate",
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

    # ST-35 T-99：人格特质实时抽取（零 LLM 规则，失败不阻塞）
    try:
        from sgme.engine import persona_extract
        persona_extract.extract_and_store(result, mem_conn, cfg)
    except Exception as e:
        logger.warning("特质抽取异常（不阻塞）: %s", e)


def refine_batch(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict,
    limit: int = 100,
    client=None,
) -> Iterator[RefineResult]:
    """批量扫描 status=new 的文件提炼（F-2 修复，2026-09-24：逐文件容错生成器）。

    2026-08-06 修复（崩溃丢数据）说明：调用方必须逐文件即时落库。
    2026-09-24 深度审查（F-2）：本函数原为「先全部提炼、返回列表」，
    唯一调用方 refine_many 再统一落库——中途异常导致前序文件已被标记 refined
    但记忆未落库（永久丢失；08-06 修复只覆盖了异步路径）。现改为**生成器**：
    逐文件 yield，单文件异常收成 status=error 的结果项并继续，批次不再中断。

    T-223（2026-09-30 深度审查 P0-2）：游标推进已从 refine_file 拆出为
    **落库成功后的 commit_refine**。本生成器只产结果、不碰 raw_files 状态；
    调用方必须「逐文件落库 → commit_refine」——落库失败的文件游标不推进，
    保持 status=new → 下批扫描自动重试（不丢）。

    Yields:
        RefineResult：每个 status=new 文件一个（正常=refined；异常=error）。
    """
    new_files = session_dao.list_by_status(session_conn, "new", limit=limit)
    for rf in new_files:
        file_id = rf["file_id"]
        try:
            yield refine_file(file_id, mem_conn, session_conn, apply_llm_override(cfg), client=client)
        except Exception as e:
            logger.warning("提炼异常（逐文件容错，继续下一文件）: file=%s error=%s", file_id, e)
            result = RefineResult(file_id=file_id)
            result.status = "error"
            result.error = str(e)
            yield result
