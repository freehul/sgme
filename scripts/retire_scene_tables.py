# -*- coding: utf-8 -*-
"""scripts/retire_scene_tables.py：场景四表退役 rename（T-427，只 rename 不 drop）。

执行顺序（顺序错误会当场坏 FTS，务必保持）：
1. DROP TRIGGER scenes_ai / scenes_ad / scenes_au（external-content FTS 触发器）；
2. DROP scenes_fts（虚拟表；SQLite 会连带删除 5 张影子表）——必须先于 scenes rename；
3. rename 子表：scene_vectors → scene_vectors_retired、scene_memories →
   scene_memories_retired、scene_versions → scene_versions_retired；
4. rename 主表：scenes → scenes_retired；
5. memory_edges 中场景共现边（relation='belongs_to' AND source='system'）
   **保留并打标**为 source='scene'（不清边、不删行）。

默认 dry-run；真实执行必须显式 `--apply`。执行前请确认：
- S3/S4 代码已部署，且无活路径写 scenes（GET /v1/admin/scenes 只读 DAO 会自动回落 _retired）；
- 已按 S1 要求备份 memory.db。

用法：
  python scripts/retire_scene_tables.py --data-dir <data>            # 预演
  python scripts/retire_scene_tables.py --data-dir <data> --apply    # 执行
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: rename 顺序：子表先、主表最后（父表 rename 会同步更新子表 FK 引用）
RENAME_ORDER = ("scene_vectors", "scene_memories", "scene_versions", "scenes")
TRIGGERS = ("scenes_ai", "scenes_ad", "scenes_au")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _exists(conn: sqlite3.Connection, name: str, kind: str = "table") -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])


def _check_scene_tables(conn: sqlite3.Connection) -> dict:
    """预演：表/触发器/FTS/边计数。"""
    tables = {t: _exists(conn, t) for t in RENAME_ORDER}
    retired = {f"{t}_retired": _exists(conn, f"{t}_retired") for t in RENAME_ORDER}
    triggers = {t: _exists(conn, t, "trigger") for t in TRIGGERS}
    edge_rows = 0
    if _exists(conn, "memory_edges"):
        edge_rows = int(conn.execute(
            "SELECT COUNT(*) c FROM memory_edges "
            "WHERE relation='belongs_to' AND source='system'"
        ).fetchone()["c"])
    return {
        "tables": tables,
        "retired_tables": retired,
        "triggers": triggers,
        "scenes_fts_exists": _exists(conn, "scenes_fts"),
        "scene_edges_to_mark": edge_rows,
        "rows": {t: _count(conn, t) for t in RENAME_ORDER},
    }


def retire(db_path: Path, apply: bool) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db 不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        before = _check_scene_tables(conn)
        report: dict = {
            "generated_at": _now(),
            "mode": "apply" if apply else "dry_run",
            "db": str(db_path),
            "before": before,
            "actions": [],
        }
        if before["retired_tables"].get("scenes_retired") and not before["tables"].get("scenes"):
            report["actions"].append("already_retired：scenes 不存在而 scenes_retired 存在，跳过")
            return report

        plan = []
        for trig in TRIGGERS:
            if before["triggers"].get(trig):
                plan.append(f"DROP TRIGGER {trig}")
        if before["scenes_fts_exists"]:
            plan.append("DROP TABLE scenes_fts（含 5 张影子表）")
        for table in RENAME_ORDER:
            if before["tables"].get(table):
                plan.append(f"ALTER TABLE {table} RENAME TO {table}_retired")
        if before["scene_edges_to_mark"]:
            plan.append(
                f"UPDATE memory_edges SET source='scene' "
                f"WHERE relation='belongs_to' AND source='system' "
                f"（{before['scene_edges_to_mark']} 行）"
            )
        report["plan"] = plan
        if not apply:
            report["actions"].append("dry_run：未执行任何写操作")
            return report

        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for trig in TRIGGERS:
                if before["triggers"].get(trig):
                    conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
                    report["actions"].append(f"已 DROP TRIGGER {trig}")
            if before["scenes_fts_exists"]:
                conn.execute("DROP TABLE IF EXISTS scenes_fts")
                report["actions"].append("已 DROP TABLE scenes_fts（影子表随之删除）")
            for table in RENAME_ORDER:
                if before["tables"].get(table) and not _exists(conn, f"{table}_retired"):
                    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_retired")
                    report["actions"].append(f"已 rename {table} → {table}_retired")
            if before["scene_edges_to_mark"]:
                cur = conn.execute(
                    "UPDATE memory_edges SET source='scene' "
                    "WHERE relation='belongs_to' AND source='system'"
                )
                report["actions"].append(
                    f"已打标场景边 source=scene：{cur.rowcount} 行（保留不删）"
                )
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            report["foreign_key_violations"] = len(fk_violations)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        report["after"] = _check_scene_tables(conn)
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    from sgme import config as config_mod

    ap = argparse.ArgumentParser(description="场景四表退役 rename（只 rename 不 drop）")
    ap.add_argument("--data-dir", default=None, help="memory.db 所在目录（默认 config.DATA_DIR）")
    ap.add_argument("--apply", action="store_true", help="真实执行（缺省 dry-run）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else Path(config_mod.DATA_DIR)
    report = retire(data_dir / "memory.db", apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"场景四表退役（{report['mode']}）：{report['db']}")
        for item in report.get("plan") or []:
            print(f"- 计划：{item}")
        for item in report.get("actions") or []:
            print(f"- 执行：{item}")
        if report.get("foreign_key_violations") is not None:
            print(f"- 外键校验：{report['foreign_key_violations']} 处违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
