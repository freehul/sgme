# -*- coding: utf-8 -*-
"""tests/test_wiki_2_0.py：知识库 2.0 写侧（D-62 / §5.1）回归。

覆盖：
- insert_page：分类归一（7 类）、page_type/headline/content_hash/updated_by 落库、
  原始路径保留为 origin_category tag、首条修订（create）；
- 同页重写：修订史累加（create → update）；
- update_page_content：追加场景 change_kind=merge、指纹与标题行随内容更新；
- 分类注册表 7 类种子；
- operations 层 create/update 接线；
- 回填脚本：dry-run 零写入 / apply 归一 + 补首条修订 / 未分类清单导出与 >5% 告警。
"""
from __future__ import annotations

import json

from sgme.data import db
from sgme.data import wiki_dao
from sgme.operations import wiki as wiki_ops
from scripts import backfill_wiki_2_0 as bfs


def _conn(tmp_path):
    return db.connect_wiki(tmp_path)


def _row(conn, page_id):
    cur = conn.execute(
        "SELECT category, category_class, page_type, headline, content_hash, updated_by, tags "
        "FROM wiki_pages WHERE page_id=?", (page_id,)
    )
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, cur.fetchone()))


def _revisions(conn, page_id):
    return conn.execute(
        "SELECT revision_no, change_kind, author, summary FROM wiki_revisions "
        "WHERE page_id=? ORDER BY revision_no", (page_id,)
    ).fetchall()


def test_insert_page_writes_2_0_fields_and_first_revision(tmp_path):
    conn = _conn(tmp_path)
    wiki_dao.insert_page(
        conn, "p1", title="研究 DSH 插件",
        content="# 结论\n插件可工作。", category="research/dsh", author="dsh",
    )
    row = _row(conn, "p1")
    # 兼容期（D-58）：category 保留原始路径不改；分类结果落 category_class
    assert row["category"] == "research/dsh"
    assert row["category_class"] == "研究调研"
    assert row["page_type"] == "topic"
    assert row["headline"] == "结论"
    assert len(row["content_hash"]) == 16
    assert row["updated_by"] == "dsh"

    revs = _revisions(conn, "p1")
    assert len(revs) == 1
    assert revs[0]["revision_no"] == 1
    assert revs[0]["change_kind"] == "create"
    assert revs[0]["author"] == "dsh"
    conn.close()


def test_insert_same_page_appends_update_revision(tmp_path):
    conn = _conn(tmp_path)
    for _ in range(2):
        wiki_dao.insert_page(
            conn, "p1", title="手册", content="步骤一", category="skill/sgme", author="agent"
        )
    revs = _revisions(conn, "p1")
    assert [r["change_kind"] for r in revs] == ["create", "update"]
    assert [r["revision_no"] for r in revs] == [1, 2]
    conn.close()


def test_update_page_content_merge_revision_and_hashes(tmp_path):
    conn = _conn(tmp_path)
    wiki_dao.insert_page(
        conn, "p1", title="手册", content="第一段", category="操作手册", author="agent"
    )
    old_hash = _row(conn, "p1")["content_hash"]
    ok = wiki_dao.update_page_content(
        conn, "p1", "第一段\n\n第二段（新增）",
        change_kind="merge", author="agent", summary="追加经验",
    )
    assert ok is True
    row = _row(conn, "p1")
    assert row["content_hash"] != old_hash
    assert row["headline"] == "第一段"
    revs = _revisions(conn, "p1")
    assert revs[-1]["change_kind"] == "merge"
    assert revs[-1]["summary"] == "追加经验"
    conn.close()


def test_categories_seeded_with_seven_defaults(tmp_path):
    conn = _conn(tmp_path)
    wiki_dao.insert_page(conn, "p1", title="t", content="c", category=None)
    cats = {c["name"] for c in wiki_dao.list_categories(conn)}
    assert cats == {
        "研究调研", "操作手册", "设计文档", "运行日志", "实体档案", "索引", "未分类"
    }
    conn.close()


def test_unclassified_stats(tmp_path):
    conn = _conn(tmp_path)
    wiki_dao.insert_page(conn, "p1", title="t1", content="c1", category="研究调研")
    wiki_dao.insert_page(conn, "p2", title="t2", content="c2", category="随便")
    stats = wiki_dao.unclassified_stats(conn)
    assert stats["total"] == 2 and stats["unclassified"] == 1
    assert abs(stats["ratio"] - 0.5) < 1e-9
    conn.close()


def test_operations_create_and_append_write_revisions(tmp_path):
    conn = _conn(tmp_path)
    res = wiki_ops.create_page(
        conn, title="SGME 运维手册", content="# 启动\n命令", category="skill/sgme",
        author="dsh",
    )
    assert res.ok
    page_id = res.data["page_id"]
    assert _row(conn, page_id)["category"] == "skill/sgme"
    assert _row(conn, page_id)["category_class"] == "操作手册"
    res2 = wiki_ops.update_page(conn, page_id, "补充：重启后验证", append=True, author="dsh")
    assert res2.ok
    revs = _revisions(conn, page_id)
    assert revs[-1]["change_kind"] == "merge"
    assert revs[-1]["author"] == "dsh"
    conn.close()


def _make_legacy_page(conn, page_id="legacy1"):
    """模拟迁移前页面：旧 category、无 2.0 结构字段、无修订史。"""
    wiki_dao.insert_page(
        conn, page_id, title="竞品调研", content="# 对比\nA 与 B", category="研究调研",
        author="agent",
    )
    conn.execute(
        "UPDATE wiki_pages SET category='research/competitor', category_class=NULL, "
        "page_type=NULL, headline=NULL, content_hash=NULL WHERE page_id=?", (page_id,)
    )
    conn.execute("DELETE FROM wiki_revisions WHERE page_id=?", (page_id,))
    conn.execute("UPDATE wiki_pages SET tags=NULL WHERE page_id=?", (page_id,))
    conn.commit()


def test_backfill_dry_run_then_apply(tmp_path):
    conn = _conn(tmp_path)
    _make_legacy_page(conn)
    conn.close()

    dry = bfs.run(tmp_path, apply=False)
    assert dry["applied"] is False
    assert dry["would_update"] == 1
    assert dry["revisions_missing"] == 1
    conn = _conn(tmp_path)
    assert _row(conn, "legacy1")["category_class"] is None, "dry-run 不得写库"
    assert _revisions(conn, "legacy1") == []
    conn.close()

    export = tmp_path / "unc.json"
    applied = bfs.run(tmp_path, apply=True, export_unclassified=str(export))
    assert applied["applied"] is True
    conn = _conn(tmp_path)
    row = _row(conn, "legacy1")
    # 默认守 1.x 兼容：只写 category_class，不改 category
    assert row["category"] == "research/competitor"
    assert row["category_class"] == "研究调研"
    assert row["page_type"] == "topic"
    assert row["headline"] == "对比"
    assert len(row["content_hash"]) == 16
    assert row["updated_by"] == "system:backfill"
    revs = _revisions(conn, "legacy1")
    assert len(revs) == 1 and revs[0]["change_kind"] == "create"
    conn.close()
    assert json.loads(export.read_text(encoding="utf-8")) == []

    # --migrate-category 才改写 category，并把原始路径留成 tag
    migrated = bfs.run(tmp_path, apply=True, migrate_category=True)
    assert migrated["migrate_category"] is True
    conn = _conn(tmp_path)
    row = _row(conn, "legacy1")
    assert row["category"] == "研究调研"
    tags = json.loads(row["tags"])
    assert "origin_category:research/competitor" in tags
    conn.close()
