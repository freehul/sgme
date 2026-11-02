"""sgme/operations/role_metrics.py：D-74 角色真实感指标埋点（T-308）。

五项指标：盲读误判率（外部脚本/真人盲读**写入**）/ 首句长度 / 感叹号率 / 复述率 /
AI 味句式率。四项确定性指标由本模块**服务端计算**（纯正则零 LLM，定义的唯一真源，
不信任调用方上传的派生数字）；盲读类（逐题 like_human 代理分 / 整轮 blind_misjudge_rate
真人误判率）只能由外部写入——服务端不做 LLM 裁判。

落点：角色卡 ``data.extensions.sgme_metrics``（不新增表、不写 memory.db）：

- ``baseline``：首次基线快照，只设一次（``set_as_baseline=True`` 且尚无 baseline 才写）
- ``latest``：每次 POST 覆盖，含逐题 items（回复原文截断 300 字，供区分度脚本读回）
- ``runs``：聚合历史（无 items 原文），最多保留 20 条

指标定义与测量脚本 ``measure_role_realism.py``（D-74 基线工具）逐字对齐。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_NOT_FOUND, OperationResult

logger = logging.getLogger("sgme.operations.role_metrics")

# 句末标点集（首句长度 = 首个句末标点前的字符数）
SENT_END = "。！？!?.\n"

# AI 味句式：命中任一即算（与测量脚本 AI_TASTE 同清单，此处为唯一真源）
AI_TASTE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (
        r"作为(一个|一名)?\s*(AI|人工智能|语言模型|助手)",
        r"我(是|作为)(一(个|名))?\s*(AI|人工智能)",
        r"让我(来)?为您",
        r"首先[，,].{0,40}其次",
        r"综上(所述)?",
        r"希望对(你|您)有(所)?帮助",
        r"还有(什么|其他)(可以|需要)(帮|为)",
        r"如有(其他|任何)问题",
        r"很高兴(为|能)(你|您)服务",
    )
)

# 复述率判定阈值：回复覆盖提问二字片段比例 ≥ 0.6 记为复述
RESTATE_THRESHOLD = 0.6


def _roles_dir() -> Path:
    return Path(sgme_config.ROLES_DIR)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 四项确定性指标（纯函数，纯正则零 LLM） ----------

def first_sentence_len(text: str) -> int:
    """首句长度：首个句末标点前的字符数（无标点则全文长度）。"""
    t = (text or "").strip()
    for i, ch in enumerate(t):
        if ch in SENT_END and i > 0:
            return i
    return len(t)


def exclaim_rate(text: str) -> float:
    """感叹号率：``！``/``!`` 个数 ÷ 总字数 × 100（保留两位小数）。"""
    t = text or ""
    n = t.count("！") + t.count("!")
    return round(n / max(1, len(t)) * 100, 2)


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def is_restating(question: str, reply: str) -> bool:
    """复述判定：回复覆盖提问实词（二字片段）比例 ≥ 0.6 记为复述。"""
    q = _bigrams(question)
    if not q:
        return False
    return len(q & _bigrams(reply)) / len(q) >= RESTATE_THRESHOLD


def ai_taste_hits(text: str) -> list[int]:
    """AI 味句式命中：返回命中模式的下标列表（空列表 = 干净）。"""
    t = text or ""
    return [i for i, p in enumerate(AI_TASTE_PATTERNS) if p.search(t)]


# ---------- 聚合 ----------

def _aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    """逐题指标 → 整轮聚合值（与测量脚本 summary 同口径）。"""
    n = len(items)
    scores = [it["like_human"] for it in items if it.get("like_human") is not None]
    return {
        "n": n,
        "avg_chars": round(sum(it["chars"] for it in items) / n, 1),
        "first_sentence_median": sorted(it["first_sentence_len"] for it in items)[n // 2],
        "exclaim_rate": round(sum(it["exclaim_rate"] for it in items) / n, 2),
        "restate_rate": round(sum(1 for it in items if it["restating"]) / n, 3),
        "ai_taste_rate": round(sum(1 for it in items if it["ai_taste"]) / n, 3),
        "like_human_avg": round(sum(scores) / len(scores), 1) if scores else None,
    }


def _compute_item(index: int, raw: dict[str, Any]) -> dict[str, Any] | OperationResult:
    """校验单条并服务端重算四项指标；非法返回 OperationResult 失败体。"""
    q = str((raw or {}).get("q") or "")
    reply = str((raw or {}).get("reply") or "")
    if not reply.strip():
        return OperationResult.fail(ERR_INVALID_ARGS, f"items[{index}].reply 不能为空")
    like_human = (raw or {}).get("like_human")
    if like_human is not None:
        try:
            like_human = int(like_human)
        except (TypeError, ValueError):
            return OperationResult.fail(
                ERR_INVALID_ARGS, f"items[{index}].like_human 必须是 0-100 整数")
        if not (0 <= like_human <= 100):
            return OperationResult.fail(
                ERR_INVALID_ARGS, f"items[{index}].like_human 必须落在 [0, 100]")
    return {
        "q": q,
        # 回复原文截断：最新快照供区分度脚本读回算相似度，300 字足够
        "reply": reply[:roles_mod.METRICS_REPLY_MAX],
        "chars": len(reply),
        "first_sentence_len": first_sentence_len(reply),
        "exclaim_rate": exclaim_rate(reply),
        "restating": is_restating(q, reply),
        "ai_taste": ai_taste_hits(reply),
        "like_human": like_human,
    }


# ---------- 记录 / 读取 ----------

def metrics_record(
    role_id: str,
    items: list[dict[str, Any]],
    label: str = "",
    blind_misjudge_rate: float | None = None,
    set_as_baseline: bool = False,
    meta: dict[str, Any] | None = None,
    now: str | None = None,
) -> OperationResult:
    """记录一轮测量：服务端重算四项确定性指标 → 聚合 → 写角色卡。

    - items：``[{q, reply, like_human?}]``；逐题 like_human 为盲读代理分（外部裁判）
    - blind_misjudge_rate：真人盲读误判率（0-1，外部脚本写入，可选）
    - set_as_baseline=True 且尚无 baseline → 固化为基线快照；已有 baseline 不再覆盖
    - 角色不存在 → ERR_NOT_FOUND；参数非法 → ERR_INVALID_ARGS
    - 只写角色卡 extensions.sgme_metrics，**不碰 memory.db**
    """
    card = roles_mod.get_role(_roles_dir(), role_id)
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    if not items:
        return OperationResult.fail(ERR_INVALID_ARGS, "items 不能为空")

    computed: list[dict[str, Any]] = []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            return OperationResult.fail(ERR_INVALID_ARGS, f"items[{i}] 必须是对象")
        got = _compute_item(i, raw)
        if isinstance(got, OperationResult):
            return got
        computed.append(got)

    agg = _aggregate(computed)
    if blind_misjudge_rate is not None:
        try:
            blind_misjudge_rate = float(blind_misjudge_rate)
        except (TypeError, ValueError):
            return OperationResult.fail(
                ERR_INVALID_ARGS, "blind_misjudge_rate 必须是 0-1 小数")
        if not (0.0 <= blind_misjudge_rate <= 1.0):
            return OperationResult.fail(
                ERR_INVALID_ARGS, "blind_misjudge_rate 必须落在 [0, 1]")
        agg["blind_misjudge_rate"] = round(blind_misjudge_rate, 4)

    at = now or _now_iso()
    run: dict[str, Any] = {"label": str(label or ""), "at": at, **agg}
    if meta:
        run["meta"] = dict(meta)

    box = roles_mod.load_metrics(card)
    runs = [r for r in (box.get("runs") or []) if isinstance(r, dict)]
    runs.append(dict(run))
    if len(runs) > roles_mod.METRICS_RUNS_KEEP:
        runs = runs[-roles_mod.METRICS_RUNS_KEEP:]

    new_box: dict[str, Any] = {
        "runs": runs,
        "latest": {**run, "items": computed},
        "updated_at": at,
    }
    if isinstance(box.get("baseline"), dict):
        new_box["baseline"] = box["baseline"]  # 基线永不覆盖
    elif set_as_baseline:
        new_box["baseline"] = dict(run)

    if not roles_mod.save_metrics(_roles_dir(), role_id, new_box):
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    logger.info("真实感指标已记录: %s（label=%s, n=%d）", role_id, run["label"], len(computed))
    return OperationResult.succeed({
        "role_id": role_id,
        "label": run["label"],
        "at": at,
        **agg,
        "baseline_set": "baseline" in new_box,
        "runs_total": len(runs),
    })


def metrics_get(role_id: str, now: str | None = None) -> OperationResult:
    """读取指标快照：baseline / latest（含 items）/ runs + 越界只读聚合（D-77 同表看）。

    越界聚合从 extensions.sgme_violations 现算（total / 今日 / by_kind），不落盘。
    角色不存在 → ERR_NOT_FOUND。
    """
    card = roles_mod.get_role(_roles_dir(), role_id)
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    box = roles_mod.load_metrics(card)

    violations = roles_mod.load_violations(card)
    today = (now or _now_iso())[:10]
    by_kind: dict[str, int] = {}
    today_count = 0
    for e in violations:
        k = str(e.get("kind") or "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
        if str(e.get("at") or "").startswith(today):
            today_count += 1

    return OperationResult.succeed({
        "role_id": role_id,
        "baseline": box.get("baseline"),
        "latest": box.get("latest"),
        "runs": box.get("runs") or [],
        "updated_at": box.get("updated_at"),
        "violations": {
            "total": len(violations),
            "today": today_count,
            "by_kind": by_kind,
        },
    })
