# -*- coding: utf-8 -*-
"""scripts/scene_merge_candidates.py：9.4 #10 场景重复合并候选（只读 dry-run）。

背景：9.4 验收要求「scenes 范围核对 + 重复合并需人工确认」。此前没有候选清单，
只能靠肉眼翻 470 个场景。本脚本给**只读**候选清单：

- 读 memory.db（mode=ro）的 scenes（active）与 scene_vectors；
- 候选阻塞：标题 difflib 相似度 ≥ --title-ratio（默认 0.6）；
- 向量复核：两场景 embedding 余弦相似度 ≥ --threshold（默认 0.92）；
- 输出：a/b scene_id、标题、余弦、标题相似度、basis，按余弦降序取 top N；
- 边界：只读、不合并、不写库；合并仍需人工确认后走管理端点/脚本。

用法：
  python scripts/scene_merge_candidates.py --json
  python scripts/scene_merge_candidates.py --threshold 0.9 --top 20
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_THRESHOLD = 0.92
DEFAULT_TITLE_RATIO = 0.6
DEFAULT_TOP = 50


def _data_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    try:
        from sgme.config import load_config

        return Path(load_config()["paths"]["data_dir"])
    except Exception:  # noqa: BLE001
        return ROOT / "data"


def _load_vectors(conn: sqlite3.Connection) -> dict[str, tuple[list[float], str, int]]:
    out: dict[str, tuple[list[float], str, int]] = {}
    try:
        rows = conn.execute(
            "SELECT scene_id, embedding, model, dims FROM scene_vectors"
        ).fetchall()
    except sqlite3.Error:
        return out
    for scene_id, blob, model, dims in rows:
        if not blob:
            continue
        n = int(dims or 0)
        if n <= 0:
            n = len(blob) // 4
        try:
            import numpy as np

            vec = np.frombuffer(blob, dtype=np.float32)[:n].astype(float).tolist()
        except Exception:  # noqa: BLE001
            vec = [
                float(int.from_bytes(blob[i : i + 4], "little", signed=True)) / 1e9
                for i in range(0, min(len(blob), n * 4), 4)
            ]
        out[str(scene_id)] = (vec, str(model or ""), n)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def candidates(data_dir: str | None, threshold: float = DEFAULT_THRESHOLD,
               title_ratio: float = DEFAULT_TITLE_RATIO, top: int = DEFAULT_TOP) -> dict:
    db = _data_dir(data_dir) / "memory.db"
    if not db.is_file():
        return {"read_only": True, "available": False, "db": str(db),
                "scenes": 0, "vectors": 0, "candidates": [],
                "warnings": ["memory.db 不存在（数据目录未初始化）"]}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT scene_id, title FROM scenes WHERE status='active' ORDER BY scene_id"
            ).fetchall()
        except sqlite3.Error as exc:
            return {"read_only": True, "available": False, "db": str(db),
                    "scenes": 0, "vectors": 0, "candidates": [],
                    "warnings": [f"scenes 表不可读：{exc}"]}
        titles = {str(r["scene_id"]): str(r["title"] or "") for r in rows}
        vectors = _load_vectors(conn)
    finally:
        conn.close()

    ids = [sid for sid in titles if sid in vectors]
    out: list[dict] = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ratio = difflib.SequenceMatcher(None, titles[a], titles[b]).ratio()
            if ratio < title_ratio:
                continue
            sim = _cosine(vectors[a][0], vectors[b][0])
            if sim < threshold:
                continue
            out.append({
                "a": a, "b": b,
                "title_a": titles[a], "title_b": titles[b],
                "cosine": round(sim, 4),
                "title_ratio": round(ratio, 4),
                "basis": "title_block + vector",
            })
    out.sort(key=lambda x: (-x["cosine"], -x["title_ratio"]))
    return {
        "read_only": True,
        "available": True,
        "db": str(db),
        "scenes": len(titles),
        "vectors": len(vectors),
        "scanned_pairs": len(ids) * (len(ids) - 1) // 2,
        "threshold": threshold,
        "title_ratio": title_ratio,
        "candidates": out[: max(1, top)],
        "summary": {"candidates": len(out), "limited_to": min(len(out), max(1, top))},
        "warnings": [
            "只读候选：不合并、不写库；合并必须人工确认后走管理端点/脚本",
            "标题阻塞可能漏掉「标题不像但语义重复」的场景；需要全量语义比对时另跑（成本高）",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="9.4 #10 场景重复合并候选（只读）")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--title-ratio", type=float, default=DEFAULT_TITLE_RATIO)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    out = candidates(args.data_dir, args.threshold, args.title_ratio, args.top)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"场景合并候选（只读）：{out['scenes']} 场景 / {out['vectors']} 向量")
        if not out.get("available"):
            for w in out["warnings"]:
                print(f"  ⚠️ {w}")
            return 0
        print(f"候选 {out['summary']['candidates']} 条（展示前 {out['summary']['limited_to']}）")
        for c in out["candidates"]:
            print(f"  {c['cosine']:.3f}  {c['a']} {c['title_a']}  ↔  {c['b']} {c['title_b']}")
        for w in out["warnings"]:
            print(f"  ⚠️ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
