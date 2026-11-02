#!/usr/bin/env python
"""T-404：把技能附属文件回填进 skill_files 表（全存库的最后一块）。

背景
----
2.0 定案「技能全存数据库」：主文件正文已随 `skills.content` 入库，但
references/ scripts/ assets/ 等**附属文件**一直只存在于文件系统——
技能包接口（`GET /v1/skills/{name}/package`）因此只能从目录现打包。
本脚本把附属文件也写进 `skill_files`，让库成为唯一真源。

用法
----
    # 默认试运行，只统计不写库
    python scripts/backfill_skill_files.py --skills-root /app/cache/skills --db /data/data/skills.db

    # 确认后落库
    python scripts/backfill_skill_files.py --skills-root /app/cache/skills --db /data/data/skills.db --apply

规则
----
- 只处理**库里存在**的技能（skills 表的 name 集合），目录里有但库没有的技能跳过并计数
- SKILL.md 本身不写 skill_files（正文在 skills.content，避免双份）
- 逐文件 sha256 + size；二进制按 BLOB 原样存
- 幂等：同 rel_path 覆盖（sha 相同则跳过，减少写放大）
- 路径一律相对技能目录、正斜杠；拒绝 `..` 与绝对路径（防穿越）
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

SKIP_NAMES = {"SKILL.md"}
# 明显不该入库的噪音
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _iter_attachments(skill_dir: Path):
    """产出 (rel_path, abs_path)，跳过 SKILL.md 与噪音文件。"""
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(skill_dir)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name in SKIP_NAMES or p.suffix in SKIP_SUFFIXES:
            continue
        yield rel.as_posix(), p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills-root", required=True, help="技能根目录（其下有 <name>/SKILL.md）")
    ap.add_argument("--db", required=True, help="skills.db 路径")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认试运行）")
    args = ap.parse_args()

    root = Path(args.skills_root)
    if not root.is_dir():
        print(f"技能根目录不存在: {root}")
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    # 兜底建表：老库可能没有 skill_files（T-401 之前），不依赖服务已初始化
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS skill_files (
          skill_name TEXT NOT NULL,
          rel_path   TEXT NOT NULL,
          content    BLOB NOT NULL,
          size       INTEGER NOT NULL,
          sha256     TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (skill_name, rel_path)
        );
        CREATE TABLE IF NOT EXISTS skill_revisions (
          revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
          skill_name  TEXT NOT NULL,
          version     TEXT NOT NULL,
          package_sha256 TEXT NOT NULL,
          file_count  INTEGER NOT NULL,
          total_bytes INTEGER NOT NULL,
          note        TEXT,
          created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_skill_rev_name ON skill_revisions(skill_name, created_at);
        """
    )
    conn.commit()
    known = {r["name"] for r in conn.execute("SELECT name FROM skills")}
    print(f"库里技能数: {len(known)} | 目录: {root}")

    # 已有附属文件（用于跳过未变文件）
    existing: dict[tuple[str, str], str] = {}
    try:
        for r in conn.execute("SELECT skill_name, rel_path, sha256 FROM skill_files"):
            existing[(r["skill_name"], r["rel_path"])] = r["sha256"]
    except sqlite3.OperationalError:
        print("skill_files 表不存在——请先在服务里跑一次（或 --ensure 前先 init）")
        return 2
    print(f"已有附属文件记录: {len(existing)}")

    now = _now_iso()
    inserted = updated = unchanged = skipped_skills = 0
    total_bytes = 0
    rows: list[tuple] = []
    per_skill: dict[str, int] = {}

    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        name = skill_dir.name
        if name not in known:
            skipped_skills += 1
            continue
        for rel, abs_path in _iter_attachments(skill_dir):
            if rel.startswith("/") or ".." in rel.split("/"):
                print(f"  跳过可疑路径: {name}/{rel}")
                continue
            data = abs_path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            total_bytes += len(data)
            per_skill[name] = per_skill.get(name, 0) + 1
            prev = existing.get((name, rel))
            if prev == sha:
                unchanged += 1
                continue
            rows.append((name, rel, data, len(data), sha, now))
            if prev is None:
                inserted += 1
            else:
                updated += 1

    print(f"\n统计：新增 {inserted} / 覆盖 {updated} / 未变 {unchanged} | 字节 {total_bytes:,}")
    print(f"涉及技能 {len(per_skill)} 个 | 目录里有但库无（跳过）{skipped_skills} 个")
    top = sorted(per_skill.items(), key=lambda kv: kv[1], reverse=True)[:5]
    print("附属文件最多的技能:", top)

    if not args.apply:
        print("\n[试运行] 未写库。确认后加 --apply")
        conn.close()
        return 0

    conn.executemany(
        "INSERT INTO skill_files (skill_name, rel_path, content, size, sha256, updated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(skill_name, rel_path) DO UPDATE SET "
        "content=excluded.content, size=excluded.size, sha256=excluded.sha256, "
        "updated_at=excluded.updated_at",
        rows,
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM skill_files").fetchone()[0]
    skills_with_files = conn.execute(
        "SELECT COUNT(DISTINCT skill_name) FROM skill_files"
    ).fetchone()[0]
    print(f"\n已写库：{len(rows)} 行 | skill_files 总计 {total} 行 | 覆盖 {skills_with_files} 个技能")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
