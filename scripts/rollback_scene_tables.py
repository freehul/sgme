# -*- coding: utf-8 -*-
"""scripts/rollback_scene_tables.py：场景四表退役回滚（T-427，30 天观察期内可用）。

回滚不是「只改回表名」——必须同时重建 FTS 与三个触发器，否则 external-content
FTS 的 `content='scenes'` 会指向不存在的表。顺序：
1. rename 主表 scenes_retired → scenes；
2. rename 子表 scene_vectors_retired / scene_memories_retired /
   scene_versions_retired → 原名；
3. 回填 content_seg（NULL/空串按 title+content 重新分词）；
4. 重建 scenes_fts（含 5 张影子表）与 scenes_ai/ad/au 触发器；
5. `INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild')`；
6. memory_edges 打标还原：source='scene' AND relation='belongs_to' → 'system'。

默认 dry-run；真实执行必须显式 `--apply`。

用法：
  python scripts/rollback_scene_tables.py --data-dir <data>            # 预演
  python scripts/rollback_scene_tables.py --data-dir <data> --apply    # 执行
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

#: 回滚 rename 顺序：主表先、子表后（与退役相反）
RENAME_ORDER = ("scenes", "scene_versions", "scene_memories", "scene_vectors")
TRIGGERS = ("scenes_ai", "scenes_ad", "scenes_au")

SCENES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS scenes_fts USING fts5(
    content_seg,
    scene_id UNINDEXED,
    content='scenes',
    content_rowid='rowid'
);
"""
SCENES_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS scenes_ai AFTER INSERT ON scenes BEGIN
    INSERT INTO scenes_fts(rowid, content_seg, scene_id)
    VALUES (new.rowid, new.content_seg, new.scene_id);
END;
CREATE TRIGGER IF NOT EXISTS scenes_ad AFTER DELETE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, content_seg, scene_id)
    VALUES ('delete', old.rowid, old.content_seg, old.scene_id);
END;
CREATE TRIGGER IF NOT EXISTS scenes_au AFTER UPDATE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, content_seg, scene_id)
    VALUES ('delete', old.rowid, old.content_seg, old.scene_id);
    INSERT INTO scenes_fts(rowid, content_seg, scene_id)
    VALUES (new.rowid, new.content_seg, new.scene_id);
END;
"""


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


def _plan(conn: sqlite3.Connection) -> dict:
    return {
        "retired_tables": {f"{t}_retired": _exists(conn, f"{t}_retired") for t in RENAME_ORDER},
        "tables": {t: _exists(conn, t) for t in RENAME_ORDER},
        "scenes_fts_exists": _exists(conn, "scenes_fts"),
        "triggers": {t: _exists(conn, t, "trigger") for t in TRIGGERS},
        "scene_edges_marked": (
            int(conn.execute(
                "SELECT COUNT(*) c FROM memory_edges "
                "WHERE relation='belongs_to' AND source='scene'"
            ).fetchone()["c"])
            if _exists(conn, "memory_edges") else 0
        ),
        "rows": {t: _count(conn, t) for t in RENAME_ORDER},
    }


def rollback(db_path: Path, apply: bool) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db 不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        before = _plan(conn)
        report: dict = {
            "generated_at": _now(),
            "mode": "apply" if apply else "dry_run",
            "db": str(db_path),
            "before": before,
            "actions": [],
        }
        if not before["retired_tables"].get("scenes_retired"):
            report["actions"].append("nothing_to_rollback：scenes_retired 不存在")
            return report

        plan = [f"ALTER TABLE {t}_retired RENAME TO {t}" for t in RENAME_ORDER
                if before["retired_tables"].get(f"{t}_retired")]
        plan.append("回填 content_seg（NULL/空串）")
        plan.append("重建 scenes_fts + scenes_ai/ad/au 触发器 + rebuild")
        if before["scene_edges_marked"]:
            plan.append(
                f"UPDATE memory_edges SET source='system' "
                f"WHERE relation='belongs_to' AND source='scene' "
                f"（{before['scene_edges_marked']} 行）"
            )
        report["plan"] = plan
        if not apply:
            report["actions"].append("dry_run：未执行任何写操作")
            return report

        from sgme.segment import segment

        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in RENAME_ORDER:
                if before["retired_tables"].get(f"{table}_retired"):
                    conn.execute(f"ALTER TABLE {table}_retired RENAME TO {table}")
                    report["actions"].append(f"已 rename {table}_retired → {table}")

            rows = conn.execute(
                "SELECT rowid, title, content FROM scenes "
                "WHERE content_seg IS NULL OR content_seg = ''"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE scenes SET content_seg=? WHERE rowid=?",
                    (segment(f"{row['title']} {row['content']}"), row["rowid"]),
                )
            report["actions"].append(f"已回填 content_seg：{len(rows)} 行")

            for trig in TRIGGERS:
                conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
            conn.execute("DROP TABLE IF EXISTS scenes_fts")
            conn.executescript(SCENES_FTS_DDL + SCENES_FTS_TRIGGERS)
            conn.execute("INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild')")
            report["actions"].append("已重建 scenes_fts + 3 触发器并 rebuild")

            if before["scene_edges_marked"]:
                cur = conn.execute(
                    "UPDATE memory_edges SET source='system' "
                    "WHERE relation='belongs_to' AND source='scene'"
                )
                report["actions"].append(
                    f"已还原场景边 source=system：{cur.rowcount} 行"
                )
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            report["foreign_key_violations"] = len(fk_violations)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        report["after"] = _plan(conn)
        report["fts_rows"] = _count(conn, "scenes_fts")
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    from sgme import config as config_mod

    ap = argparse.ArgumentParser(description="场景四表退役回滚（含 FTS/触发器重建）")
    ap.add_argument("--data-dir", default=None, help="memory.db 所在目录（默认 config.DATA_DIR）")
    ap.add_argument("--apply", action="store_true", help="真实执行（缺省 dry-run）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else Path(config_mod.DATA_DIR)
    report = rollback(data_dir / "memory.db", apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"场景四表回滚（{report['mode']}）：{report['db']}")
        for item in report.get("plan") or []:
            print(f"- 计划：{item}")
        for item in report.get("actions") or []:
            print(f"- 执行：{item}")
        if report.get("fts_rows") is not None:
            print(f"- scenes_fts 行数：{report['fts_rows']}")
        if report.get("foreign_key_violations") is not None:
            print(f"- 外键校验：{report['foreign_key_violations']} 处违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
