"""L2 提取·2.0 输出解析（SGME 2.0 D-23 / D-66 / D-67 / D-68）。

2.0 的输出结构（一个 memories 单元）：

    {
      "main_event": {"content": "...", "occurred_at": "...", "feeling": "..."},
      "facts": [{"type": "偏好", "content": "...", "subject": "self", "certainty": "confirmed"}],
      "causes": [{"from": "...", "to": "..."}],
      "trait": "只给原文不给域",
      "dimensions": {"primary": "preferences", "secondary": ["habits"]},
      "route": "memory|skill_candidate|wiki_candidate|discard"
    }

本模块负责：
1. **形状归一**：2.0 结构 → 内部记忆记录列表（每个拆出的侧面各一条）；
2. **拆条**：按 D-21/D-22 规则（`l2_split.decide_split`）决定侧面拆不拆——
   **不交给 LLM**；
3. **路由分流**：`discard` 整条丢弃；`wiki_candidate` / `skill_candidate` 保留但带
   路由标记，由落库侧决定去处（D-31：route 承担价值判断，不另设分数）；
4. **降级**：字段缺失/结构不明的单元不静默丢——主干仍入库并打 `needs_review=1`
   （D-23 二轮修订 P1-2：宁可漏注入不可错断言）。

向后兼容：输入若不是 2.0 结构（无 `main_event`），整段委托给 1.x 的
`sgme.engine.l1.parse_l1_output`，行为不变。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sgme.engine import l1
from sgme.engine.l2_split import decide_split

# D-43：愿望类断言的有效窗口（天）
WISHED_WINDOW_DAYS = 90

logger = logging.getLogger(__name__)

VALID_ROUTES = {"memory", "skill_candidate", "wiki_candidate", "discard"}
# D-23 长度上限（提示词层声明，解析层兜底截断；超长只告警不丢条）
MAIN_MAX_CHARS = 200
FACT_MAX_CHARS = 100
TRAIT_MAX_CHARS = 50
# 2.0 不引入模型自评分数（D-31），priority 统一给中性值
DEFAULT_PRIORITY = 70

# 长期属性类事实拆出后按 persona 落库；其余按 fact
_PERSONA_TYPES = {"约束", "偏好", "习惯"}


def _clip(text: Any, limit: int, what: str) -> str:
    s = str(text or "").strip()
    if len(s) > limit:
        logger.warning("L2 %s 超长截断: %d → %d 字", what, len(s), limit)
        return s[:limit]
    return s


def _dimensions_of(unit: dict[str, Any], dimensions: list[dict]) -> list[str]:
    """归一维度：兼容 {primary, secondary} 与列表两种形态；只保留注册表内的 id。"""
    dims, _dropped = l1._normalize_dimensions(unit.get("dimensions"), dimensions)
    valid = {d["id"] for d in dimensions}
    return [d for d in dims if d in valid]


def _valid_to_for(
    modality: str | None, occurred_at: str | None
) -> str | None:
    """D-43 闸门 A 的输入：给 planned / wished 断言算有效期（``valid_to``）。

    - **planned**：取计划时间点。模型给的 ``occurred_at`` 解析为**未来**时刻才认，
      否则返回 None——不编造一个假的计划日期（宁可不设有效期，也不给错误断言）。
    - **wished**：一律 ``now + 90 天``（D-43 定义的可执行窗口）。
    - 其余 modality（done / quoted / unknown）→ None：已发生/引用类事实无断言有效期。

    此前 ``valid_to`` 全仓无写入点，导致闸门 A 永远无输入——「明年三月体检」这类
    计划记忆会像普通事实一样按维度 TTL 到期，而不是活到计划日。
    """
    m = str(modality or "").strip().lower()
    if m == "wished":
        return (_utcnow() + timedelta(days=WISHED_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if m != "planned":
        return None
    raw = str(occurred_at or "").strip()
    if not raw:
        return None
    try:
        # 兼容 'YYYY-MM-DD' 与完整 ISO（含 Z）
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts <= _utcnow():
        return None  # 计划时间点已过 → 不是未来的断言
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _base_record(
    *,
    content: str,
    dims: list[str],
    memory_type: str,
    route: str | None,
    occurred_at: str | None,
    extra: dict[str, Any],
    valid_to: str | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "dimensions": dims,
        "memory_type": memory_type,
        "priority": DEFAULT_PRIORITY,
        "route": route if route in VALID_ROUTES else None,
        "occurred_at": occurred_at,
        # D-43：planned/wished 的断言有效期（闸门 A 载体）
        "valid_to": valid_to,
        "extra": extra,
    }


def _flatten_unit(unit: dict[str, Any], dimensions: list[dict]) -> list[dict[str, Any]]:
    """把一个 2.0 单元摊成若干条记忆记录（主干 + 需拆的侧面）。"""
    route = str(unit.get("route") or "").strip()
    if route == "discard":
        return []  # D-67：按来源判定的降噪结果，直接分流

    main = unit.get("main_event") or {}
    # 模型偶尔把 main_event 写成字符串（"main_event": "用户做了某事"）而不是对象，
    # 直接 .get 会 AttributeError 把整个文件判失败（2026-10-31 实测：320 个文件里 21 个因此丢）。
    if isinstance(main, str):
        main = {"content": main}
    if not isinstance(main, dict):
        main = {}
    facts = [f for f in (unit.get("facts") or []) if isinstance(f, dict)]
    causes = [c for c in (unit.get("causes") or []) if isinstance(c, dict)]
    dims = _dimensions_of(unit, dimensions)

    content = _clip(main.get("content"), MAIN_MAX_CHARS, "主事件")
    if not content and facts:
        # 主干缺失但侧面有内容 → 用第一条事实顶主干，并打待审标
        content = _clip(facts[0].get("content"), MAIN_MAX_CHARS, "主事件(回退)")
        need_review = 1
    else:
        need_review = 0
    if not content:
        logger.warning("L2 单元无任何可入库内容，跳过")
        return []
    if not dims:
        # 维度缺失 → 主干仍入库但进待审队列（D-23 降级路径）
        need_review = 1

    trait = _clip(unit.get("trait"), TRAIT_MAX_CHARS, "特质")
    kept_facts: list[dict[str, Any]] = []
    side_records: list[dict[str, Any]] = []

    for fact in facts:
        decision = decide_split(fact, main)
        fcontent = _clip(fact.get("content"), FACT_MAX_CHARS, "事实")
        if not fcontent:
            continue
        if not decision.split:
            kept_facts.append({
                "type": str(fact.get("type") or "").strip(),
                "content": fcontent,
                "subject": str(fact.get("subject") or "").strip() or None,
                "reason": decision.reason,
            })
            continue
        ftype = str(fact.get("type") or "").strip()
        side_records.append(_base_record(
            content=fcontent,
            dims=dims,
            memory_type="persona" if ftype in _PERSONA_TYPES else "fact",
            route=decision.route or route or None,
            occurred_at=(fact.get("occurred_at") if decision.route is None else None),
            valid_to=_valid_to_for(
                fact.get("modality"),
                fact.get("occurred_at") if decision.route is None else None,
            ),
            extra={
                "type": ftype or None,
                "subject": l1._normalize_subject(fact.get("subject")),
                "certainty": l1._normalize_enum(
                    fact.get("certainty"), l1.VALID_CERTAINTY, "confirmed"),
                # 2026-11-02 修复：原先写死 fact/done，从不读模型输出——导致
                # 「计划下周出差」被记成已发生（modality=done），闸门 A 永远无输入。
                # 现在逐 fact 取模型值，缺失/非法才回落 fact/unknown（D-43 双轴正交）。
                "derivation": l1._normalize_enum(
                    fact.get("derivation"), l1.VALID_DERIVATION, "fact"),
                "modality": l1._normalize_enum(
                    fact.get("modality"), l1.VALID_MODALITY, "unknown"),
                "needs_review": 0,
                "split_reason": decision.reason,
            },
        ))

    # 主干类型：含长期属性类事实（约束/习惯/偏好）→ persona，否则 fact。
    # 不用 episodic：1.x 规则「episodic 仅限动态维度」会把主干的静态维度统统剥掉，
    # 而 2.0 的主干恰恰要带静态维度（架构约束、偏好都属主干）。
    _has_static = any(str(f.get("type") or "").strip() in _PERSONA_TYPES for f in facts)
    main_record = _base_record(
        content=content,
        dims=dims,
        memory_type="persona" if _has_static else "fact",
        route=route or None,
        occurred_at=str(main.get("occurred_at") or "").strip() or None,
        # 主干按 unit 内 facts 的众数 modality 判有效期（与下面 extra.modality 同源）
        valid_to=_valid_to_for(
            _majority([f.get("modality") for f in facts], l1.VALID_MODALITY, "unknown"),
            str(main.get("occurred_at") or "").strip() or None,
        ),
        extra={
            "type": None,
            "subject": "self",
            "certainty": "confirmed",
            # 主干两轴取 unit 内 facts 的众数（主干是整段会话的摘要，两轴应代表
            # 这批事实的整体性质）；无 facts 时回落 fact/unknown。
            # need_review=1（降级待审）一律标 inferred——宁可漏注入不可错断言；
            # 正常项才取 facts 众数。
            "derivation": "inferred" if need_review else _majority(
                [f.get("derivation") for f in facts], l1.VALID_DERIVATION, "fact"),
            "modality": _majority([f.get("modality") for f in facts],
                                  l1.VALID_MODALITY, "unknown"),
            "feeling": str(main.get("feeling") or "").strip() or None,
            "trait": trait or None,
            "needs_review": need_review,
            "l2_facts_json": json.dumps(
                {"facts": kept_facts, "causes": causes}, ensure_ascii=False
            ) if (kept_facts or causes) else None,
        },
    )
    return [main_record, *side_records]


def _majority(values: list, valid: set[str], default: str) -> str:
    """取一组轴值的众数（非法值忽略）；全空/全非法时回落 default。

    主干记录没有自己的 derivation/modality——它是对整个 unit 的概括，
    所以按该 unit 内 facts 的多数意见定，避免「一条计划事实把整段标成 done」。
    """
    from collections import Counter

    norm = [v for v in (l1._normalize_enum(x, valid, "") if x else "" for x in values) if v]
    if not norm:
        return default
    return Counter(norm).most_common(1)[0][0]

def parse_l2_output(text: str, dimensions: list[dict]) -> list[dict[str, Any]]:
    """解析 2.0 L2 输出为记忆记录列表。

    Args:
        text: 模型原始输出（JSON 数组）
        dimensions: 维度注册表（用于归一与合法性校验）

    Returns:
        记录列表，每条形如
        ``{content, dimensions, memory_type, priority, route, occurred_at, extra}``，
        ``extra`` 内含 2.0 元数据（type/subject/certainty/derivation/modality/
        feeling/trait/needs_review/l2_facts_json 等）。

    Raises:
        RefineError: JSON 无法解析（与 1.x 一致，由调用方走重试/降级）。
    """
    try:
        data = l1._extract_json_array(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise l1.RefineError(f"JSON 解析失败: {e}") from e
    if not data:
        return []
    if not (isinstance(data[0], dict) and "main_event" in data[0]):
        # 1.x 结构 → 走旧解析，行为完全不变（向后兼容）
        return l1.parse_l1_output(text, dimensions)

    records: list[dict[str, Any]] = []
    for unit in data:
        if not isinstance(unit, dict):
            continue
        records.extend(_flatten_unit(unit, dimensions))
    return records

# ---------- 记录 → 1.x 校验项（供 pipeline 复用既有落库路径） ----------

def records_to_items(
    records: list[dict[str, Any]], dimensions: list[dict]
) -> list[dict[str, Any]]:
    """把 `parse_l2_output` 的记录转成 `l1._validate_item` 的输入项并校验。

    保留 2.0 元数据（type/subject/certainty/derivation/modality/route 直接进 item，
    feeling/trait/needs_review/l2_facts_json 在校验后回填），其余走既有校验逻辑，
    保证落库路径与 1.x 完全一致。

    Args:
        records: `parse_l2_output` 的产物
        dimensions: 维度注册表

    Returns:
        校验通过的 item 列表（不合法项按 1.x 语义丢弃）。

    维度缺失的处理：D-23 要求「主干仍入库 + needs_review=1」，而 `_validate_item`
    在无有效维度时返回 None。这里给待审项一个兜底维度（优先 `status`，否则第一个
    动态维度），使它能落库、能被待审候选页查到，同时**不会进主注入块**。
    """
    fallback_dim = next(
        (d["id"] for d in dimensions if d["id"] == "status"), None
    ) or next(
        (d["id"] for d in dimensions if d.get("time_velocity") == "dynamic"), None
    ) or (dimensions[0]["id"] if dimensions else None)

    out: list[dict[str, Any]] = []
    for rec in records:
        extra = dict(rec.get("extra") or {})
        dims = list(rec.get("dimensions") or [])
        if not dims and fallback_dim:
            dims = [fallback_dim]
            extra["needs_review"] = 1
        item: dict[str, Any] = {
            "content": rec.get("content"),
            "dimensions": dims,
            "memory_type": rec.get("memory_type"),
            "priority": rec.get("priority"),
            "occurred_at": rec.get("occurred_at"),
            # 2026-11-03 修复：原这里硬编码 ``facts: []`` 且不带 supersedes，
            # 导致 2.0 解析链（默认开启）把 L1 解析出的原子事实三元组与替代意图
            # **全部丢掉**——supersession 联动与 facts 落库在 2.0 路径上静默失效
            # （1.x 委托路径的记录本来就带这两个键，白白被这里清空）。
            "facts": rec.get("facts") or [],
            "supersedes": rec.get("supersedes") or [],
        }
        # 路由优先取记录级（parse 时已判来源），其次 extra 里的拆分路由
        route = rec.get("route") or extra.get("route")
        if route is not None:
            item["route"] = route
        for key in ("type", "subject", "certainty", "derivation", "modality"):
            if extra.get(key) is not None:
                item[key] = extra[key]
        validated = l1._validate_item(item, dimensions)
        if validated is None:
            continue
        validated["feeling"] = extra.get("feeling")
        validated["trait"] = extra.get("trait")
        validated["needs_review"] = int(extra.get("needs_review") or 0)
        validated["l2_facts_json"] = extra.get("l2_facts_json")
        out.append(validated)
    return out
