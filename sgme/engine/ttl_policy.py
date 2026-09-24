"""TTL 回填策略（T-205 C2 / v0.4 案 A 变体）—— 全项目单一实现。

历史包袱：此前 l15.py 与 l2.py 各有一份 ``_backfill_ttl``（同签名同语义），
改一处漏一处即口径漂移（T-203 F1 同款教训）。现收敛为本模块，两处均委托。

C2 规则（2026-09-25 用户批，v0.4 决策①）：
- **episodic = min(维度默认 TTL, 30d)**——不可一律 30d（会覆盖 status 7d
  反而延长事件流水污染）；打在无 TTL 静态维度时取 30d；
- **fact 取案 A 变体**：动态维度随维度默认 TTL（fact 不享受特权，TTL 由
  所属维度语义决定）；静态维度 90d（知识资产保留一个季度，防被 TTL 清掉）；
- **persona / instruction 不设**（维持旧行为：仅随动态维度默认，静态=NULL
  永不过期）——人格画像与技术偏好是长期资产；
- 记忆级显式 ttl_days 永远优先（调用方覆盖语义不变）；
- 一条记忆命中多个维度时取**最小** TTL（最严约束优先）。

expired 后仍可被 /v1/search 召回（检索只排除 rejected + valid_to 过滤），
退出注入即可满足「注入清爽」，知识不丢（v0.3 主线：TTL 降权不删）。
"""
from __future__ import annotations

from typing import Any

EPISODIC_MAX_TTL_DAYS = 30   # episodic 事件流水上限（天）
FACT_STATIC_TTL_DAYS = 90    # fact 打静态维度的保底 TTL（天）
_DYNAMIC_TYPES = ("persona", "instruction")


def backfill_ttl(
    ttl_days: int | None,
    dimension_ids: list[str],
    dimensions: list[dict],
    memory_type: str | None = None,
) -> int | None:
    """TTL 回填（记忆级显式值优先 → 按 memory_type + 维度默认推导）。

    Args:
        ttl_days: L1 输出的记忆级 TTL（非 None 直接返回，语义不变）。
        dimension_ids: 该记忆的维度标签 id 列表。
        dimensions: 注册表维度列表（含 ttl_days 默认值）。
        memory_type: 记忆类型（episodic/fact/persona/instruction；None 按旧行为）。
    """
    # 创意池铁律（T-26，2026-08-13 强化，优先级最高——在显式值之前判定，与原
    # l15 实现逐行为一致）：含 ideas 维度 → 强制 None——创意长期保存，覆盖
    # 其他维度 TTL 与记忆级显式值（否则 ideas+goals 共存的创意会取 90d，
    # 90 天后过期退出注入，违背创意池「ideas + ttl_days=NULL」定义）
    if "ideas" in dimension_ids:
        return None

    if ttl_days is not None:
        return ttl_days

    dim_map: dict[str, dict[str, Any]] = {d["id"]: d for d in dimensions}
    dim_ttls = [
        int(dim_map[d]["ttl_days"])
        for d in dimension_ids
        if d in dim_map and dim_map[d].get("ttl_days")
    ]

    if memory_type == "episodic":
        # min(维度默认, 30)：status 7d 维持语义；纯静态维度给 30d 上限
        base = min(dim_ttls) if dim_ttls else None
        return min(base, EPISODIC_MAX_TTL_DAYS) if base else EPISODIC_MAX_TTL_DAYS

    if memory_type == "fact":
        # 案 A 变体：动态维随维度 TTL；静态维 90d
        return min(dim_ttls) if dim_ttls else FACT_STATIC_TTL_DAYS

    # persona / instruction / 未知类型：旧行为——取任一动态维度默认（首个命中）
    for dim_id in dimension_ids:
        d = dim_map.get(dim_id)
        if d and d.get("ttl_days"):
            return d["ttl_days"]
    return None
