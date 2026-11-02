#!/usr/bin/env python3
"""scripts/lint_all_skills.py：批量扫描技能格式违规（T-403，只读）。

数据源（可混用）：
- 目录：``--source-dir <技能工作区>``（可重复；递归找 ``<name>/SKILL.md``）
- 数据库：``--db <skills.db>``（可重复；读 skills 表的 name/description/content）
- 都不传：读当前配置 ``skills.source_dirs``（``python scripts/lint_all_skills.py``）

只调用 ``sgme.operations.skill_lint.lint_skill``，**不写库、不落盘、不改技能**。
有 error 时退出码 1（便于挂 CI / 门禁），仅 warn 或全通过时退出码 0。

用法::

    python scripts/lint_all_skills.py
    python scripts/lint_all_skills.py --source-dir ./skills --json
    python scripts/lint_all_skills.py --db data/skills.db --json > lint-report.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

# 允许从任意 cwd 以 ``python scripts/lint_all_skills.py`` 运行
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sgme.operations.skill_lint import lint_skill
from sgme.skills.indexer import parse_skill_md


def _row(name: str, origin: str, results: list[dict]) -> dict:
    """统一扫描结果行：errors/warnings 分列，原样保留 rule/message。"""
    return {
        "name": name,
        "origin": origin,
        "errors": [r for r in results if r["level"] == "error"],
        "warnings": [r for r in results if r["level"] == "warn"],
    }


def scan_source_dirs(source_dirs: list[str]) -> list[dict]:
    """扫描目录树中的全部 SKILL.md（只读）。"""
    rows: list[dict] = []
    for raw_dir in source_dirs or []:
        root = Path(raw_dir)
        if not root.is_dir():
            print(f"[warn] 跳过不存在的技能目录: {raw_dir}", file=sys.stderr)
            continue
        for skill_file in sorted(root.rglob("SKILL.md")):
            name = skill_file.parent.name
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError as e:
                rows.append(_row(name, str(skill_file), [{
                    "level": "error",
                    "rule": "read_error",
                    "message": f"读取 SKILL.md 失败: {e}",
                }]))
                continue
            meta = parse_skill_md(text)["meta"]
            description = str(meta.get("description") or "")
            rows.append(_row(name, str(skill_file), lint_skill(name, description, text)))
    return rows


def scan_db(db_path: str) -> list[dict]:
    """扫描 skills.db 的全部行（只读；content 为正文，重建 frontmatter 后校验）。"""
    rows: list[dict] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = conn.execute("SELECT name, description, content FROM skills")
        for name, description, content in cursor:
            name = str(name or "")
            description = str(description or "")
            body = str(content or "")
            skill_md = _render_skill_md(name, description, body)
            rows.append(_row(name, f"{db_path}#skills", lint_skill(name, description, skill_md)))
    finally:
        conn.close()
    return rows


def _render_skill_md(name: str, description: str, body: str) -> str:
    """把库行重建为 SKILL.md 全文（仅用于格式校验，不落盘）。"""
    fm = yaml.safe_dump(
        {"name": name, "description": description},
        allow_unicode=True,
        sort_keys=False,
    )
    if not fm.endswith("\n"):
        fm += "\n"
    return f"---\n{fm}---\n\n{body}"


def summarize(rows: list[dict]) -> dict:
    """汇总计数（技能数 / 有 error / 有 warn / 违规条数）。"""
    return {
        "total": len(rows),
        "with_errors": sum(1 for r in rows if r["errors"]),
        "with_warnings": sum(1 for r in rows if r["warnings"] and not r["errors"]),
        "errors": sum(len(r["errors"]) for r in rows),
        "warnings": sum(len(r["warnings"]) for r in rows),
    }


def _load_config_source_dirs() -> list[str]:
    """读当前配置的 skills.source_dirs（配置不可用时返回空列表）。"""
    try:
        from sgme.config import load_config

        cfg = load_config() or {}
        return list((cfg.get("skills") or {}).get("source_dirs") or [])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"[warn] 读取配置失败，跳过默认数据源: {e}", file=sys.stderr)
        return []


def _print_report(rows: list[dict], summary: dict) -> None:
    print(
        f"扫描技能 {summary['total']} 个："
        f"有 error {summary['with_errors']} 个，有 warn {summary['with_warnings']} 个，"
        f"error {summary['errors']} 条，warn {summary['warnings']} 条"
    )
    for row in rows:
        if not row["errors"] and not row["warnings"]:
            continue
        print(f"\n- {row['name']}  ({row['origin']})")
        for item in row["errors"]:
            print(f"  [error] {item['rule']}: {item['message']}")
        for item in row["warnings"]:
            print(f"  [warn]  {item['rule']}: {item['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="批量扫描技能格式违规（只读；error 退出码 1）",
    )
    parser.add_argument(
        "--source-dir", action="append", default=[],
        help="技能工作区目录（可重复；递归扫描 <name>/SKILL.md）",
    )
    parser.add_argument(
        "--db", action="append", default=[],
        help="skills.db 路径（可重复；只读模式打开）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    source_dirs = list(args.source_dir)
    if not source_dirs and not args.db:
        source_dirs = _load_config_source_dirs()
        if not source_dirs:
            print("[warn] 未从配置读到 skills.source_dirs，请显式传 --source-dir 或 --db",
                  file=sys.stderr)
            return 2

    rows = scan_source_dirs(source_dirs)
    for db_path in args.db:
        rows.extend(scan_db(db_path))

    summary = summarize(rows)
    if args.json:
        print(json.dumps(
            {"summary": summary, "skills": rows},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        _print_report(rows, summary)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
