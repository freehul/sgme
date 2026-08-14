"""search/rrf.py：Reciprocal Rank Fusion（RRF）。

标准 RRF 算法：score = Σ 1/(k + rank + 1)，多路结果归一合并。
"""
from __future__ import annotations


def rrf_merge(
    bm25_results: list[dict],
    vector_results: list[dict],
    k: int = 60,
    id_key: str = "memory_id",
) -> list[dict]:
    """RRF 融合 BM25 + 向量两路结果。

    - 每路结果按原顺序（rank=0 为第一条）
    - score = Σ 1/(k + rank + 1)
    - 同一 id（id_key 指定聚合键，场景检索传 "scene_id"）多路命中 → score 累加
    - 返回按 score DESC 排序的合并结果
    - 每条结果含 {<id_key>, content, score, sources: [bm25|vector]}
    """
    # <id_key> → 聚合条目
    merged: dict[str, dict] = {}

    def _ingest(results: list[dict], source_name: str) -> None:
        for rank, r in enumerate(results):
            mid = r.get(id_key)
            if not mid:
                continue
            if mid not in merged:
                merged[mid] = {
                    id_key: mid,
                    "content": r.get("content"),
                    "priority": r.get("priority"),
                    "updated_at": r.get("updated_at"),
                    "score": 0.0,
                    "sources": [],
                }
            entry = merged[mid]
            entry["score"] += 1.0 / (k + rank + 1)
            if source_name not in entry["sources"]:
                entry["sources"].append(source_name)

    _ingest(bm25_results, "bm25")
    _ingest(vector_results, "vector")

    # 按 score DESC 排序
    out = list(merged.values())
    out.sort(key=lambda x: x["score"], reverse=True)
    return out
