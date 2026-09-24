"""T-207 ① 测试：raw 正文 FTS5（L0 会话原文全文检索 + 命中片段）。

- init_raw_fts 幂等；rebuild 全量/增量（body_hash 判重）
- 中文正文检索命中 → search_raw_files 合并（matched_in=body）
- 追加增量段后 hash 变化 → 重索引
- extract_hit_snippet 命中窗口；未命中回退 None
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sgme.data import db as db_mod, session_dao
from sgme.data.search import raw_fts


MD_TMPL = """---
format_version: 1
file_id: {file_id}
session_key: {session_key}
agent_id: hermes
source_type: session
started_at: 2026-09-25T10:00:00Z
---

# 2026-09-25T10:00:01Z user
{body}
"""


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def raw_dir(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    return d


def _write_raw(raw_dir: Path, file_id: str, body: str, session_key: str = "s1") -> Path:
    d = raw_dir / "2026" / "09"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{file_id}.md"
    p.write_text(MD_TMPL.format(file_id=file_id, session_key=session_key, body=body),
                 encoding="utf-8")
    return p


def test_init_raw_fts_idempotent(session_conn):
    raw_fts.init_raw_fts(session_conn)
    raw_fts.init_raw_fts(session_conn)  # 重复建表不抛
    tables = {
        r[0] for r in session_conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('raw_fts','raw_fts_state')"
        )
    }
    assert tables == {"raw_fts", "raw_fts_state"}


def test_rebuild_indexes_and_search_chinese(session_conn, raw_dir):
    _write_raw(raw_dir, "aaa", "把 InfiniteTalk 的模型下载到 D 盘统一仓库")
    _write_raw(raw_dir, "bbb", "今天讨论了晚饭吃什么，火锅还是烧烤")
    stats = raw_fts_mod_rebuild(session_conn, raw_dir)
    assert stats["indexed"] == 2

    hits = raw_fts.search_raw_body(session_conn, "模型下载", limit=10)
    assert [h["file_id"] for h in hits] == ["aaa"]

    # 重跑幂等：全部 skipped
    stats2 = raw_fts_mod_rebuild(session_conn, raw_dir)
    assert stats2["indexed"] == 0 and stats2["skipped"] == 2


def raw_fts_mod_rebuild(conn: sqlite3.Connection, raw_dir: Path) -> dict:
    return raw_fts.rebuild_raw_fts(conn, raw_dir, incremental=True)


def test_search_raw_files_merges_body_hits(session_conn, raw_dir):
    """正文命中（元数据不含检索词）也要召回——T-207 核心语义。"""
    p = _write_raw(raw_dir, "ccc", "贫血复诊要带甲功报告，下次周一去")
    stats = raw_fts_mod_rebuild(session_conn, raw_dir)
    assert stats["indexed"] == 1
    # 元数据注册（body 命中文件必须在 raw_files 表内才返回）
    session_dao.insert_raw_file(
        session_conn, file_id="ccc", path="2026/09/ccc.md",
        session_key="s1", started_at="2026-09-25T10:00:00Z",
        agent_id="hermes", agent_model=None, ended_at=None,
        status="active", size=p.stat().st_size, content_hash="x",
    )
    rows = session_dao.search_raw_files(session_conn, "甲功报告", limit=10)
    assert any(r["file_id"] == "ccc" and r.get("matched_in") == "body" for r in rows)
    # 未索引文件退回元数据 LIKE 仍可用
    rows2 = session_dao.search_raw_files(session_conn, "ccc", limit=10)
    assert any(r["file_id"] == "ccc" for r in rows2)


def test_append_changes_hash_reindexes(session_conn, raw_dir):
    p = _write_raw(raw_dir, "ddd", "第一段内容：会议纪要")
    raw_fts_mod_rebuild(session_conn, raw_dir)
    # 追加增量段
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n## 2026-09-25T11:00:00Z user\n新增了 ssh 端口修改的踩坑记录\n")
    assert raw_fts.index_raw_file(session_conn, p) is True  # hash 变 → 重索引
    hits = raw_fts.search_raw_body(session_conn, "ssh 端口", limit=5)
    assert [h["file_id"] for h in hits] == ["ddd"]


def test_extract_hit_snippet_window():
    body = "前情提要省略。\n" + "复" * 40 + "这里提到甲功报告需要携带，后续还有内容。" + "尾" * 40
    snippet = raw_fts.extract_hit_snippet(body, "甲功报告", window=20)
    assert snippet is not None
    assert "甲功报告" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert raw_fts.extract_hit_snippet(body, "完全不存在词", window=20) is None


def test_search_raw_body_empty_and_unavailable(session_conn):
    assert raw_fts.search_raw_body(session_conn, "", limit=5) == []
    # 表存在但为空 → 空结果（不抛）
    raw_fts.init_raw_fts(session_conn)
    assert raw_fts.search_raw_body(session_conn, "任何词", limit=5) == []
