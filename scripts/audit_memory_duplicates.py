#!/usr/bin/env python
"""审计记忆池内的语义重复（只读）。

用途：提示词改版（如 v023 → v025）后，判断新版本是否"变啰嗦"——同一结论在
同一批 / 相邻时间窗里被抽成多条。前缀去重测不出这类重复（表述不同、字面前缀
全不一样），只能走向量余弦。

数据来源：memories.route/content/prompt_version/created_at + memory_vectors.embedding
（float32 BLOB，与 scene_gc.py 同一解码口径）。**全程只读**——库以 mode=ro 打开。

用法::

    python scripts/audit_memory_duplicates.py --db /data/data/memory.db --since 2026-11-19
    python scripts/audit_memory_duplicates.py --since 2026-11-19 --threshold 0.92 --json

判读：
- `dup_pairs` 高 = 同义条目多；`dup_rate`（涉及条数/总数）用来跨版本比较；
- 报告按"重复簇"聚合，簇内每条都会被人工复核，不是自动删除；
- 阈值 0.92 偏严（同义改写通常 0.85~0.95），0.85 更宽。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("需要 numpy（pip install numpy）", file=sys.stderr)
    raise SystemExit(2)


def _default_db() -> Path:
    home = os.environ.get("SGME_HOME") or ""
    if home:
        return Path(home) / "data" / "memory.db"
    return Path("data") / "memory.db"


def connect_ro(db: Path) -> sqlite3.Connection:
    """只读连接（不改库、不加锁写）。"""
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(
    conn: sqlite3.Connection,
    since: str | None,
    versions: list[str] | None,
) -> tuple[list[dict], int]:
    """取目标记忆 + 向量。返回 (带向量的行, 缺向量条数)。"""
    where = ["1=1"]
    params: list = []
    if since:
        where.append("m.created_at >= ?")
        params.append(since)
    if versions:
        placeholders = ",".join("?" * len(versions))
        where.append(f"m.prompt_version IN ({placeholders})")
        params.extend(versions)
    where.append("COALESCE(m.status,'active') != 'rejected'")
    sql = f"""
        SELECT m.memory_id, m.content, m.prompt_version, m.created_at, m.route,
               v.embedding, v.dims
          FROM memories m
          JOIN memory_vectors v ON v.memory_id = m.memory_id
         WHERE {' AND '.join(where)}
         ORDER BY m.created_at
    """
    rows = [dict(r) for r in conn.execute(sql, params)]
    total_sql = f"SELECT COUNT(*) FROM memories m WHERE {' AND '.join(where)}"
    total = conn.execute(total_sql, params).fetchone()[0]
    return rows, total - len(rows)


def to_matrix(rows: list[dict]):
    """float32 BLOB → 归一化矩阵（余弦=点积）。"""
    vecs = []
    keep = []
    for r in rows:
        blob = r["embedding"]
        if not blob:
            continue
        v = np.frombuffer(blob, dtype=np.float32)
        if v.size == 0:
            continue
        n = float(np.linalg.norm(v))
        if n == 0:
            continue
        vecs.append(v / n)
        keep.append(r)
    if not vecs:
        return keep, np.zeros((0, 0), dtype=np.float32)
    return keep, np.vstack(vecs)


def find_pairs(mat, threshold: float, block: int = 1024) -> list[tuple[int, int, float]]:
    """分块算余弦，返回 (i, j, score) 且 i < j。"""
    n = mat.shape[0]
    out: list[tuple[int, int, float]] = []
    for start in range(0, n, block):
        end = min(start + block, n)
        sim = mat[start:end] @ mat.T  # (b, n)
        for bi in range(end - start):
            i = start + bi
            row = sim[bi]
            idx = np.where(row >= threshold)[0]
            for j in idx:
                j = int(j)
                if j <= i:
                    continue
                out.append((i, j, float(row[j])))
    out.sort(key=lambda x: -x[2])
    return out


def cluster(pairs: list[tuple[int, int, float]], n: int) -> list[list[int]]:
    """并查集：把互相重复的条目聚成簇。"""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, j, _ in pairs:
        union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def build_report(rows: list[dict], pairs, clusters, threshold: float, total: int, missing: int) -> dict:
    involved = set()
    for i, j, _ in pairs:
        involved.add(i)
        involved.add(j)
    by_version: dict[str, dict] = {}
    for i, j, _ in pairs:
        for idx in (i, j):
            ver = rows[idx].get("prompt_version") or "?"
            by_version.setdefault(ver, {"pairs": 0, "involved": set()})
        by_version[rows[i].get("prompt_version") or "?"]["pairs"] += 1
        by_version[rows[i].get("prompt_version") or "?"]["involved"].update((i, j))
    version_stats = {
        ver: {"pairs": d["pairs"], "involved": len(d["involved"])}
        for ver, d in by_version.items()
    }
    samples = []
    for g in sorted(clusters, key=len, reverse=True)[:10]:
        samples.append([
            {
                "memory_id": rows[i]["memory_id"],
                "version": rows[i].get("prompt_version"),
                "route": rows[i].get("route"),
                "content": (rows[i].get("content") or "")[:120],
            }
            for i in g[:5]
        ])
    return {
        "threshold": threshold,
        "rows_total": total,
        "rows_with_vector": len(rows),
        "rows_missing_vector": missing,
        "dup_pairs": len(pairs),
        "dup_involved": len(involved),
        "dup_rate": round(len(involved) / len(rows) * 100, 1) if rows else 0.0,
        "clusters": len(clusters),
        "by_version": version_stats,
        "top_clusters": samples,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="审计记忆池语义重复（只读）")
    ap.add_argument("--db", default=str(_default_db()), help="memory.db 路径")
    ap.add_argument("--since", default=None, help="只看该时间之后的记忆（ISO，如 2026-11-19）")
    ap.add_argument("--prompt-version", action="append", dest="versions", help="限定提示词版本（可多次）")
    ap.add_argument("--threshold", type=float, default=0.92, help="余弦阈值（默认 0.92）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"库不存在: {db}", file=sys.stderr)
        return 2
    conn = connect_ro(db)
    try:
        rows, missing = load_rows(conn, args.since, args.versions)
    finally:
        conn.close()
    keep, mat = to_matrix(rows)
    pairs = find_pairs(mat, args.threshold) if len(keep) else []
    clusters = cluster(pairs, len(keep)) if pairs else []
    report = build_report(keep, pairs, clusters, args.threshold, len(rows) + missing, missing)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("记忆语义重复审计（只读）")
    print("=" * 72)
    print(f"库: {db}")
    print(f"范围: since={args.since or '-'} versions={args.versions or '-'} threshold={args.threshold}")
    print(f"记忆: 总 {report['rows_total']} | 有向量 {report['rows_with_vector']} | 缺向量 {report['rows_missing_vector']}")
    print(f"重复: {report['dup_pairs']} 对 / 涉及 {report['dup_involved']} 条 / 重复率 {report['dup_rate']}% / {report['clusters']} 个簇")
    if report["by_version"]:
        print("\n按提示词版本:")
        for ver, st in sorted(report["by_version"].items()):
            print(f"  {ver:24} 重复对 {st['pairs']:4} | 涉及 {st['involved']:4} 条")
    if report["top_clusters"]:
        print("\n最大重复簇（前 3）:")
        for idx, g in enumerate(report["top_clusters"][:3], 1):
            print(f"  簇{idx}（{len(g)} 条示例）:")
            for it in g:
                print(f"    [{it['version']}|{it['route']}] {it['content']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
