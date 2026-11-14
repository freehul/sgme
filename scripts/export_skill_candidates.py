# -*- coding: utf-8 -*-
"""scripts/export_skill_candidates.py：T5a 技能候选导出分片（纯本地脚本，零 LLM）。

用途
----
把 skills.db 候选池里 ``status='accepted'`` 且尚未命名（suggested_name IS NULL）
的候选按 content_sha256 去重后切片导出，供命名阶段（T5b）逐片生成语义名：

    python scripts/export_skill_candidates.py --out tmp/naming --part-size 20 [--db <path>]
    python scripts/export_skill_candidates.py --out tmp/naming --only-unexported

设计要点
----
- **只读**打开 skills.db（``?mode=ro``），导出过程零写入；老库没有 suggested_name
  列时视为「全部未命名」（只读连接做不了迁移，列缺失必须容忍）；
- 同指纹（content_sha256）只出一次，代表取 candidate_id 最小的一条——一份正文
  一个名字，导入时同指纹候选共用（apply_skill_names.py 按 sha 广播）；
- 已存在的分片文件**不覆盖**（断点续跑）：默认模式从 part-001 起重算，编号稳定
  的前缀会全部命中已有文件被跳过；apply 之后候选集合会变化（已命名的退出查询、
  新候选追加在尾部），续导请用 ``--only-unexported``——排除已出现在既有分片里的
  memory_id，编号接在最大分片号之后；
- 命名本体（T5b）是 zcode 会话内用 GLM 逐片生成的步骤，本脚本不调任何 LLM。

分片格式（**契约，不许改**）
----
    {"part": 1, "total_parts": 52, "part_size": 20,
     "items": [{"i": 0, "memory_id": "...", "sha": "...",
                "description": "...", "content_head": "...(正文前 300 字)"}]}

命名输出（part-XXX.names.json）由 T5b 生成、apply_skill_names.py 校验落库。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

# 允许直接 `python scripts/xxx.py` 时从仓库根导入 sgme
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PART_PREFIX = "part-"
CONTENT_HEAD_CHARS = 300
DEFAULT_PART_SIZE = 20


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """只读打开 skills.db（导出零写入；文件不存在由调用方先拦）。"""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return col in cols


def _collect_candidates(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    """取 accepted 且未命名的候选，按 content_sha256 去重（代表取 candidate_id 最小）。

    Returns:
        ``(去重后的候选列表, 去重前的总行数)``——摘要要分别报两个口径。
    """
    has_col = _has_column(conn, "skill_candidates", "suggested_name")
    sql = (
        "SELECT candidate_id, memory_id, content_sha256, description, content"
        " FROM skill_candidates WHERE status='accepted'"
    )
    if has_col:
        sql += " AND suggested_name IS NULL"
    sql += " ORDER BY candidate_id"
    items: list[dict] = []
    seen: set[str] = set()
    raw_count = 0
    for _cid, memory_id, sha, description, content in conn.execute(sql).fetchall():
        raw_count += 1
        # NULL 指纹（异常行）按 memory_id 自成一组，不参与广播
        key = str(sha) if sha else f"__noshaf__:{memory_id}"
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "memory_id": str(memory_id),
            "sha": str(sha) if sha else "",
            "description": str(description or ""),
            "content_head": str(content or "")[:CONTENT_HEAD_CHARS],
        })
    return items, raw_count


def _iter_part_files(out_dir: Path):
    """目录下既有分片文件（排除 *.names.json）。"""
    for p in sorted(out_dir.glob(f"{PART_PREFIX}*.json")):
        if p.name.endswith(".names.json"):
            continue
        yield p


def _load_exported_ids(out_dir: Path) -> set[str]:
    """已出现在既有分片里的 memory_id 全集（--only-unexported 用）。"""
    ids: set[str] = set()
    for p in _iter_part_files(out_dir):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001——损坏文件不算已导出，重导由覆盖守卫兜底
            continue
        for item in data.get("items") or []:
            mid = (item or {}).get("memory_id")
            if mid:
                ids.add(str(mid))
    return ids


def _next_part_number(out_dir: Path) -> int:
    nums = []
    for p in _iter_part_files(out_dir):
        stem = p.stem[len(PART_PREFIX):]
        if stem.isdigit():
            nums.append(int(stem))
    return (max(nums) + 1) if nums else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出技能候选分片（T5a，纯本地零 LLM）")
    parser.add_argument("--out", required=True, help="分片输出目录（如 tmp/naming）")
    parser.add_argument("--part-size", type=int, default=DEFAULT_PART_SIZE,
                        help=f"每片候选数（默认 {DEFAULT_PART_SIZE}）")
    parser.add_argument("--db", default=None,
                        help="skills.db 路径（缺省取 sgme 配置的 DATA_DIR/skills.db）")
    parser.add_argument("--only-unexported", action="store_true",
                        help="排除已出现在既有分片里的候选，编号接续（apply 后续导用）")
    args = parser.parse_args(argv)
    if args.part_size < 1:
        parser.error("--part-size 必须 >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.db:
        db_path = Path(args.db)
    else:
        from sgme import config as sgme_config

        db_path = Path(sgme_config.DATA_DIR) / "skills.db"
    if not db_path.exists():
        print(f"skills.db 不存在: {db_path}（用 --db 显式指定）", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = _open_readonly(db_path)
    try:
        candidates, raw_count = _collect_candidates(conn)
    finally:
        conn.close()

    if not candidates:
        print("导出摘要：总候选 0 / 唯一指纹 0 / 分片 0 / 跳过 0（候选池为空）")
        return 0

    pending = candidates
    if args.only_unexported:
        exported = _load_exported_ids(out_dir)
        pending = [c for c in candidates if c["memory_id"] not in exported]

    part_size = args.part_size
    total_new = math.ceil(len(pending) / part_size) if pending else 0
    start_num = 1 if not args.only_unexported else _next_part_number(out_dir)
    # total_parts = 本轮写完后全批的最大分片号（跨运行接续时随编号增长）
    total_parts = start_num + total_new - 1 if total_new else 0

    written = 0
    skipped = 0
    for offset in range(0, len(pending), part_size):
        num = start_num + offset // part_size
        path = out_dir / f"{PART_PREFIX}{num:03d}.json"
        if path.exists():
            skipped += 1
            continue
        chunk = pending[offset:offset + part_size]
        payload = {
            "part": num,
            "total_parts": total_parts,
            "part_size": part_size,
            "items": [
                {"i": i, **item} for i, item in enumerate(chunk)
            ],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        written += 1

    print(
        f"导出摘要：总候选 {raw_count} / 唯一指纹 {len(candidates)}"
        f"（同 sha 只出一次）/ 待导 {len(pending)} / 写出分片 {written} / 跳过已有分片 {skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
