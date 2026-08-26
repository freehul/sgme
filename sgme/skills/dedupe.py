"""sgme/skills/dedupe.py：三层查重（ST-36 M3，设计 §四）。

查重位置前移到回写之前（「先搜后写」纪律）：

    同名冲突      目录名唯一性        → reject_same_name
    同内容异名    归一化 SHA256       → reject_same_sha
    语义近亲      embedding 相似度    → ("warn_similar", score)  警告+人工裁决
    无冲突                            → None

语义近亲层用 sgme/skills/vectors.cosine_topk 做余弦比对；调用方不传向量
（向量不可用/嵌入服务离线）时**跳过该层返回 None——宁缺勿误报**。
分层重叠合法：相似度阈值 0.85 只警告不自动拦。
"""
from __future__ import annotations

from sgme.skills.vectors import cosine_topk

# 语义近亲判定阈值（≥ 判为近亲，只警告）
SIMILAR_THRESHOLD = 0.85


def check_duplicate(
    record,
    existing_records,
    query_vec: list[float] | None = None,
    existing_vectors: dict[str, list[float]] | None = None,
):
    """三层查重：同名 → 同 SHA → 语义近亲（可选）。

    Args:
        record: 待写入的 SkillRecord（indexer 产出，含 name/sha256）。
        existing_records: 库内已有记录列表（SkillRecord；不含自身更新场景由调用方过滤）。
        query_vec: 待写记录的 embedding 向量；None=跳过语义层（宁缺勿误报）。
        existing_vectors: {name: vec}；与 query_vec 配合使用。

    Returns:
        "reject_same_name" | "reject_same_sha" | ("warn_similar", score) | None
    """
    # 第一层：同名冲突（目录名唯一性）
    for ex in existing_records or ():
        if getattr(ex, "name", None) == record.name:
            return "reject_same_name"

    # 第二层：同内容异名（归一化 SHA256 指纹一致）
    for ex in existing_records or ():
        if (
            getattr(ex, "sha256", "")
            and getattr(record, "sha256", "")
            and ex.sha256 == record.sha256
            and ex.name != record.name
        ):
            return "reject_same_sha"

    # 第三层：语义近亲（embedding 余弦 ≥ 阈值 → 警告+人工裁决，不自动拦）
    if not query_vec or not existing_vectors:
        return None  # 向量不可用 → 跳过语义层（宁缺勿误报）
    candidates = {
        n: v for n, v in (existing_vectors or {}).items()
        if n != getattr(record, "name", None)
    }
    ranked = cosine_topk(query_vec, candidates, top_k=1)
    if not ranked:
        return None
    best_name, best_score = next(iter(ranked.items()))
    if best_score >= SIMILAR_THRESHOLD:
        return ("warn_similar", best_score)
    return None
