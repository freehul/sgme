# -*- coding: utf-8 -*-
"""T-388 R1：wiki_supersede_audit 审计器测试（全部只读，临时库自建最小 schema）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.wiki_supersede_audit import audit_group, run

SCHEMA = """
CREATE TABLE wiki_pages (
  page_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  status TEXT DEFAULT 'active',
  supersedes TEXT,
  ingested_at TEXT
);
"""


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "wiki.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    yield path
    conn.close()


def _ins(conn: sqlite3.Connection, pid: str, title: str, status: str,
         sup: str | None, ts: str) -> None:
    conn.execute(
        "INSERT INTO wiki_pages (page_id, title, status, supersedes, ingested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, title, status, sup, ts),
    )


def _ctx(db: Path):
    """pytest 临时库 + 独立连接（fixture 的 conn 已 yield 完关闭前不可复用，另开只读）。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    return conn


def test_healthy_chain_no_candidates(db):
    """健康链：旧页指向后继、链尾 active → 零候选，exit 语义 0。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", "B", "2026-01-01")
    _ins(conn, "B", "同题", "active", None, "2026-01-02")
    conn.commit()
    conn.close()
    rep = run(db)
    assert rep["candidates"] == 0
    assert rep["same_title_groups"] == 1


def test_missing_pointer_is_candidate(db):
    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", None, "2026-01-01")
    _ins(conn, "B", "同题", "active", None, "2026-01-02")
    conn.commit()
    conn.close()
    rep = run(db)
    kinds = [c["kind"] for c in rep["items"]]
    assert kinds.count("missing_pointer") == 1


def test_self_pointer_with_active_successor(db):
    """C1 自指但同题存在 active → 链断裂嫌疑候选。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", "A", "2026-01-01")
    _ins(conn, "B", "同题", "active", None, "2026-01-02")
    conn.commit()
    conn.close()
    rep = run(db)
    kinds = [c["kind"] for c in rep["items"]]
    assert kinds.count("self_pointer_with_active_successor") == 1


def test_retired_no_successor_is_normal(db):
    """C1 整组自指下线（无 active）→ 正常，不入候选。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "skill:x", "superseded", "A", "2026-08-16T14:41")
    _ins(conn, "B", "skill:y", "superseded", "B", "2026-08-16T14:42")
    conn.commit()
    conn.close()
    rep = run(db)
    assert rep["candidates"] == 0
    assert rep["retired_no_successor"] == 2


def test_terminus_not_active(db):
    """组内最新页非 active 且非自指 → 链尾异常候选。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", "B", "2026-01-01")
    _ins(conn, "B", "同题", "superseded", None, "2026-01-02")  # 链尾缺失指针+非 active
    conn.commit()
    conn.close()
    rep = run(db)
    kinds = [c["kind"] for c in rep["items"]]
    assert "terminus_not_active" in kinds


def test_cross_title_pointer(db):
    """指针指向不同标题页 → 疑似错链候选。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "题一", "superseded", "X", "2026-01-01")
    _ins(conn, "B", "题一", "active", None, "2026-01-02")
    _ins(conn, "X", "题二", "active", None, "2026-01-03")
    conn.commit()
    conn.close()
    rep = run(db)
    kinds = [c["kind"] for c in rep["items"]]
    assert kinds.count("cross_title_pointer") == 1


def test_pointer_cycle_detected(db):
    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", "B", "2026-01-01")
    _ins(conn, "B", "同题", "superseded", "A", "2026-01-02")
    conn.commit()
    conn.close()
    rep = run(db)
    kinds = [c["kind"] for c in rep["items"]]
    assert "pointer_cycle" in kinds


def test_dangling_pointer_counted(db):
    """指针指向库内不存在的 id → 计入悬空指针统计。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", "GHOST", "2026-01-01")
    _ins(conn, "B", "同题", "active", None, "2026-01-02")
    conn.commit()
    conn.close()
    rep = run(db)
    assert rep["dangling_pointers"] == 1


def test_date_titles_form_separate_groups(db):
    """跨日期台账（标题含日期）天然分属不同组——不互配（防错清单②）。"""
    conn = sqlite3.connect(db)
    _ins(conn, "A", "ledger-2026-09-15", "active", None, "2026-09-15")
    _ins(conn, "B", "ledger-2026-09-16", "active", None, "2026-09-16")
    conn.commit()
    conn.close()
    rep = run(db)
    assert rep["same_title_groups"] == 0
    assert rep["candidates"] == 0


def test_readonly_no_writes(db):
    """mode=ro：审计后库文件字节不变（零写入实证）。"""
    import hashlib

    conn = sqlite3.connect(db)
    _ins(conn, "A", "同题", "superseded", None, "2026-01-01")
    _ins(conn, "B", "同题", "active", None, "2026-01-02")
    conn.commit()
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    run(db)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after

def test_draft_only_group_is_not_flagged_as_retired():
    """T-392E（2026-11-16）：全 draft 组不算「退休态」——草稿是未发布，不是已下线。

    没有这条，A-5 分流进来的每条 wiki 草稿都会被误报成 terminus_not_active。
    """
    pages = [{"page_id": "d1", "title": "T", "status": "draft", "supersedes": "",
              "ingested_at": "2026-11-16T00:00:00Z"}]
    cands, rows = audit_group(pages, {p["page_id"]: p for p in pages})
    assert cands == []
    assert rows == []


def test_draft_newest_with_active_below_is_not_terminus():
    """草稿比 active 新时，链尾仍应取 active 页，不得因草稿报异常。"""
    pages = [
        {"page_id": "old", "title": "T", "status": "active", "supersedes": "",
         "ingested_at": "2026-11-15T00:00:00Z"},
        {"page_id": "new", "title": "T", "status": "draft", "supersedes": "",
         "ingested_at": "2026-11-16T00:00:00Z"},
    ]
    cands, _rows = audit_group(pages, {p["page_id"]: p for p in pages})
    assert [c["kind"] for c in cands] == []
