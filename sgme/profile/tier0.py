"""profile/tier0.py：Tier0 画像摘要生成（§8.2）。

每日 1 次 LLM 生成 ~200 tokens persona 摘要，48h 过期降级为静态维度直出。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sgme import config
from sgme.llm import chain as llm_chain
from sgme.llm import provider as llm_provider
from sgme.prompts import PromptStore
from sgme.data import memory_dao

logger = logging.getLogger("sgme.profile.tier0")

SUMMARY_PATH = config.DATA_DIR / "tier0_summary.json"

# 静态维度（降级时使用）
STATIC_DIMENSIONS = ["identity", "family", "social", "values"]
FALLBACK_PRIORITY_MIN = 70
FALLBACK_LIMIT = 10

# generate_summary 拉取上限（priority 过滤前的召回量）
RECALL_LIMIT = 20
RECALL_PRIORITY_MIN = 70

# 48 小时过期阈值
EXPIRY_HOURS = 48


def generate_summary(mem_conn, cfg, client=None) -> str | None:
    """生成 Tier0 摘要：拉取静态维度高优先级记忆 → 渲染 prompt → LLM 生成。

    模板经 PromptStore 读取（tier0_summary，A/B 默认不启用）；
    每次生成记录一条 refine_run（file_id="tier0"，观测 #33）。
    失败不抛异常，返回 None + 日志告警。
    """
    from sgme.data.refine_dao import RefineRunRecorder

    pv = None
    try:
        # 提示词版本（manifest 缺失/坏配置 → 失败返回 None，不抛异常）
        pv = PromptStore().get("tier0_summary", None)
        # 拉取静态维度记忆（priority>=70，limit 20）
        memories = memory_dao.list_memories_by_dimension(
            mem_conn, STATIC_DIMENSIONS, match="any", limit=RECALL_LIMIT,
            include_expired=False,
        )
        # priority 过滤
        memories = [m for m in memories if m.get("priority", 0) >= RECALL_PRIORITY_MIN]

        if not memories:
            logger.warning("Tier0 生成：静态维度无可用记忆（priority>=%s）", RECALL_PRIORITY_MIN)
            # 仍尝试生成（LLM 会基于空输入给出泛化摘要）
            memories_text = "（暂无静态维度高优先级记忆）"
        else:
            # 格式化为 LLM 易读文本
            lines = []
            for m in memories:
                lines.append(f"- {m.get('content', '')}")
            memories_text = "\n".join(lines)

        # 渲染 prompt
        prompt = pv.text.replace("{{memories}}", memories_text)

        # 调 LLM
        text, provider_name, _usage = llm_chain.call_with_fallback(
            cfg["llm"], prompt, chain_name="refinement", client=client,
        )
        if mem_conn is not None:
            run_id = RefineRunRecorder.start(
                mem_conn, file_id="tier0", stage="tier0_summary",
                version=pv.version, variant=pv.variant,
                provider=provider_name, bucket_key="tier0",
            )
            RefineRunRecorder.finish(mem_conn, run_id, memories_count=0, action_counts={}, status="ok")
        return text
    except llm_provider.LLMUnavailable as e:
        logger.warning("Tier0 生成失败（LLM 全挂）: %s", e)
        _record_tier0_error(mem_conn, pv, str(e))
        return None
    except Exception as e:
        logger.warning("Tier0 生成异常: %s", e)
        _record_tier0_error(mem_conn, pv, str(e))
        return None


def _record_tier0_error(mem_conn, pv, error: str) -> None:
    """Tier0 失败时记录一条 error refine_run（观测 #33）。"""
    if mem_conn is None:
        return
    from sgme.data.refine_dao import RefineRunRecorder
    version = pv.version if pv else "unknown"
    variant = pv.variant if pv else None
    run_id = RefineRunRecorder.start(
        mem_conn, file_id="tier0", stage="tier0_summary",
        version=version, variant=variant,
        provider="unavailable", bucket_key="tier0",
    )
    RefineRunRecorder.finish(mem_conn, run_id, memories_count=0, action_counts={},
                             status="error", error=error)


def save_summary(summary: str, path: Path | None = None) -> Path:
    """写 tier0_summary.json（含 generated_at ISO 时间戳）。"""
    p = path or SUMMARY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "summary": summary,
        "generated_at": _now_iso(),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_summary(path: Path | None = None) -> str | None:
    """读取 + 48h 过期检测。返回 None 表示需降级。"""
    p = path or SUMMARY_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Tier0 摘要文件读取失败: %s", e)
        return None
    generated_at = data.get("generated_at")
    summary = data.get("summary")
    if not generated_at or not summary:
        return None
    # 48h 过期检测
    try:
        gen_t = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        logger.warning("Tier0 摘要 generated_at 格式无效: %s", generated_at)
        return None
    now = datetime.now(timezone.utc)
    if (now - gen_t) > timedelta(hours=EXPIRY_HOURS):
        logger.info("Tier0 摘要已过期（>=%sh），需降级", EXPIRY_HOURS)
        return None
    return summary


def fallback_static(mem_conn, cfg) -> list[dict]:
    """降级：静态维度 priority>=70 top 10 直出。"""
    memories = memory_dao.list_memories_by_dimension(
        mem_conn, STATIC_DIMENSIONS, match="any", limit=FALLBACK_LIMIT,
        include_expired=False,
    )
    return [m for m in memories if m.get("priority", 0) >= FALLBACK_PRIORITY_MIN]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
