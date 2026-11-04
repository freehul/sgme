# -*- coding: utf-8 -*-
"""scripts/raw_redact_locate.py：D-44 ③ L0 原件段落定位（只读）。

背景：彻底删除（purge）只清记忆层，L0 raw 是整会话文件、不做段落删除；
D-44 要求结果页诚实列出剩余副本，raw 段落 redact 一直是人工步骤。
本脚本把「人工找段落」变成只读定位：

- 输入：--memory-id（读 deletion_audit.snapshot_json 里的原文与来源）或 --keyword；
- 扫描：<data_dir>/raw/**/*.md，按空行切段；
- 打分：记忆原文 vs 段落的 2-gram Jaccard（默认阈值 0.2，top 5）；keyword 模式直接命中；
- 输出：文件 / 段落序号 / 行范围 / 分数 / 120 字预览；
- 边界：绝不写文件、不改库、不删任何内容；redact 仍需人工确认后执行。

用法：
  python scripts/raw_redact_locate.py --memory-id mem_xxx
  python scripts/raw_redact_locate.py --keyword "某段敏感内容" --top 10
  python scripts/raw_redact_locate.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_THRESHOLD = 0.2
DEFAULT_TOP = 5
PREVIEW_CHARS = 120


def _bigrams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", str(text or "")).lower()
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _raw_dir(data_dir: str | None) -> Path:
    if data_dir:
        return Path(data_dir) / "raw"
    try:
        from sgme.config import load_config

        cfg = load_config()
        return Path(cfg["paths"]["raw_dir"])
    except Exception:  # noqa: BLE001
        return ROOT / "data" / "raw"


def _purged_snapshots(memory_ids: list[str], data_dir: str | None) -> list[dict]:
    """读 deletion_audit（mode='purge'）快照；无表/无记录返回空。"""
    if data_dir:
        db = Path(data_dir) / "memory.db"
    else:
        try:
            from sgme.config import load_config

            db = Path(load_config()["paths"]["data_dir"]) / "memory.db"
        except Exception:  # noqa: BLE001
            db = ROOT / "data" / "memory.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT memory_id, snapshot_json, created_at FROM deletion_audit WHERE mode='purge'"
        params: tuple = ()
        if memory_ids:
            marks = ",".join("?" * len(memory_ids))
            sql += f" AND memory_id IN ({marks})"
            params = tuple(memory_ids)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            snap = json.loads(r["snapshot_json"] or "{}")
        except json.JSONDecodeError:
            snap = {}
        out.append({
            "memory_id": r["memory_id"],
            "content": str((snap.get("memory") or {}).get("content") or ""),
            "sources": snap.get("sources") or [],
            "created_at": r["created_at"],
        })
    return out


def _body(text: str) -> str:
    """去掉 L0 段内的 # 时间戳/角色头，减少对相似度的干扰。"""
    return "\n".join(
        line for line in str(text or "").splitlines() if not line.startswith("# ")
    )


def _paragraphs(path: Path) -> list[dict]:
    """按空行切段，保留 1-based 行范围。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks: list[dict] = []
    buf: list[str] = []
    start = 1
    line_no = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not buf:
                start = line_no
            buf.append(line)
        elif buf:
            blocks.append({"text": "\n".join(buf), "start": start, "end": line_no - 1})
            buf = []
    if buf:
        blocks.append({"text": "\n".join(buf), "start": start, "end": line_no})
    return blocks


def locate(memory_ids: list[str], keywords: list[str], data_dir: str | None,
           threshold: float = DEFAULT_THRESHOLD, top: int = DEFAULT_TOP) -> dict:
    raw_dir = _raw_dir(data_dir)
    files = sorted(raw_dir.rglob("*.md")) if raw_dir.is_dir() else []
    targets = _purged_snapshots(memory_ids, data_dir) if memory_ids else []
    if not targets:
        targets = [{"memory_id": None, "content": "", "sources": []}]
    matches: list[dict] = []
    scanned_paras = 0
    for path in files:
        try:
            paras = _paragraphs(path)
        except OSError:
            continue
        scanned_paras += len(paras)
        for para in paras:
            text = _body(para["text"])
            for kw in keywords:
                if kw and kw.lower() in text.lower():
                    matches.append({
                        "memory_id": None, "file": str(path), "paragraph": None,
                        "line_start": para["start"], "line_end": para["end"],
                        "score": 1.0, "keyword": kw,
                        "preview": text[:PREVIEW_CHARS],
                    })
            for t in targets:
                content = t.get("content") or ""
                if not content:
                    continue
                score = _similarity(content, text)
                if score >= threshold:
                    matches.append({
                        "memory_id": t.get("memory_id"),
                        "file": str(path), "paragraph": None,
                        "line_start": para["start"], "line_end": para["end"],
                        "score": round(score, 4), "keyword": None,
                        "preview": text[:PREVIEW_CHARS],
                    })
    matches.sort(key=lambda m: (-m["score"], m["file"], m["line_start"]))
    # 每个目标（memory_id / keyword）最多 top 条
    limited: list[dict] = []
    seen: dict = {}
    for m in matches:
        key = m.get("memory_id") or m.get("keyword") or "<all>"
        if seen.get(key, 0) >= top:
            continue
        seen[key] = seen.get(key, 0) + 1
        limited.append(m)
    return {
        "read_only": True,
        "raw_dir": str(raw_dir),
        "files_scanned": len(files),
        "paragraphs_scanned": scanned_paras,
        "targets": [
            {"memory_id": t.get("memory_id"), "has_snapshot_text": bool(t.get("content"))}
            for t in targets
        ],
        "matches": limited,
        "summary": {
            "targets": len(targets),
            "matches": len(limited),
        },
        "warnings": [
            "L0 raw 为整会话文件，本脚本只定位候选段落，不执行 redact；"
            "确认后用编辑器人工删除并记录（D-44 剩余副本清单）",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L0 raw 段落定位（只读，D-44）")
    ap.add_argument("--memory-id", action="append", default=[], help="已彻底删除的记忆 id（可重复）")
    ap.add_argument("--keyword", action="append", default=[], help="关键词（可重复；不依赖审计快照）")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not args.memory_id and not args.keyword:
        ap.error("至少给一个 --memory-id 或 --keyword")
    out = locate(args.memory_id, args.keyword, args.data_dir,
                 threshold=args.threshold, top=max(1, args.top))
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"raw 目录：{out['raw_dir']}（{out['files_scanned']} 文件 / {out['paragraphs_scanned']} 段）")
        print(f"目标 {out['summary']['targets']} 个；命中 {out['summary']['matches']} 条")
        for m in out["matches"]:
            who = m["memory_id"] or f"keyword={m['keyword']}"
            print(f"  [{who}] {m['file']}:{m['line_start']}-{m['line_end']} score={m['score']}")
            print(f"      {m['preview']}…")
        for w in out["warnings"]:
            print(f"  ⚠️ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
