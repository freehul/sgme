"""T3 测试：L0 捕获（frontmatter + 消息块 + 追加 + 增量段 + 坏文件）。

所有测试用 tmp_path 隔离 raw/ 目录（monkeypatch config.RAW_DIR）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sgme import config
from sgme.raw import store
from sgme.data import db as db_mod
from sgme.data import session_dao


# ---------- fixtures ----------

@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录到 tmp_path。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


# ---------- 消息构造辅助 ----------

def _msg(ts: str, role: str, content: str, tool_name: str | None = None) -> dict:
    m = {"timestamp": ts, "role": role, "content": content}
    if tool_name:
        m["tool_name"] = tool_name
    return m


# ---------- T3.1 写新文件 ----------

def test_write_new_file_creates_with_frontmatter(raw_dir):
    """写新文件：raw/sessions/{file_id}.md 存在，frontmatter 完整。"""
    path = store.write_new_file(
        file_id="f-001", session_key="sess_a",
        started_at="2026-08-04T10:00:00Z", agent_id="hermes",
        source_type="session",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "你好")],
        metadata={"app": "test"},
    )
    assert path.exists()
    assert path.parent.name == "sessions"
    assert path.name == "f-001.md"

    parsed = store.parse_file("f-001")
    fm = parsed.frontmatter
    assert fm["format_version"] == 1
    assert fm["file_id"] == "f-001"
    assert fm["session_key"] == "sess_a"
    assert fm["agent_id"] == "hermes"
    assert fm["source_type"] == "session"
    assert fm["started_at"] == "2026-08-04T10:00:00Z"
    assert fm["metadata"] == {"app": "test"}


def test_write_new_file_uses_utc_now_if_started_at_missing(raw_dir):
    """started_at 缺省 → 自动填 UTC now。"""
    store.write_new_file(
        file_id="f-002", session_key="s2",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "hi")],
    )
    parsed = store.parse_file("f-002")
    assert parsed.frontmatter["started_at"] is not None
    assert parsed.frontmatter["started_at"].endswith("Z")


def test_write_new_file_first_message_uses_h1(raw_dir):
    """首消息块用 # 标题。"""
    store.write_new_file(
        file_id="f-003", session_key="s3",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "首条")],
    )
    text = store.file_path("f-003").read_text(encoding="utf-8")
    assert "\n# 2026-08-04T10:00:00Z user\n" in text


# ---------- T3.2 解析 ----------

def test_parse_file_splits_messages_with_seq(raw_dir):
    """解析：消息块按序号 1..n 编号，role/content 正确。"""
    store.write_new_file(
        file_id="f-004", session_key="s4",
        started_at="2026-08-04T10:00:00Z",
        first_messages=[
            _msg("2026-08-04T10:00:00Z", "user", "用户问题"),
            _msg("2026-08-04T10:01:00Z", "assistant", "助手回答"),
        ],
    )
    parsed = store.parse_file("f-004")
    assert len(parsed.messages) == 2
    assert parsed.messages[0].seq == 1
    assert parsed.messages[0].role == "user"
    assert parsed.messages[0].content == "用户问题"
    assert parsed.messages[1].seq == 2
    assert parsed.messages[1].role == "assistant"
    assert parsed.messages[1].content == "助手回答"


def test_parse_file_tool_block_extracts_tool_name(raw_dir):
    """tool 块：解析 tool_name，正文为工具输出。"""
    store.write_new_file(
        file_id="f-005", session_key="s5",
        first_messages=[
            _msg("2026-08-04T10:00:00Z", "user", "读文件"),
            _msg("2026-08-04T10:00:05Z", "tool", "文件内容 ABC", tool_name="read_file"),
            _msg("2026-08-04T10:00:10Z", "assistant", "总结"),
        ],
    )
    parsed = store.parse_file("f-005")
    assert len(parsed.messages) == 3
    tool_msg = parsed.messages[1]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_name == "read_file"
    assert tool_msg.content == "文件内容 ABC"


def test_parse_file_supports_system_role(raw_dir):
    """system role 也可解析。"""
    store.write_new_file(
        file_id="f-006", session_key="s6",
        first_messages=[_msg("2026-08-04T10:00:00Z", "system", "系统提示")],
    )
    parsed = store.parse_file("f-006")
    assert parsed.messages[0].role == "system"


def test_parse_file_not_found_raises(raw_dir):
    with pytest.raises(FileNotFoundError):
        store.parse_file("nonexistent")


# ---------- T3.3 追加 ----------

def test_append_messages_uses_h2_and_preserves_seq(raw_dir):
    """追加：用 ## 标题，已有消息序号不变，新消息续编。"""
    store.write_new_file(
        file_id="f-007", session_key="s7",
        started_at="2026-08-04T10:00:00Z",
        first_messages=[
            _msg("2026-08-04T10:00:00Z", "user", "Q1"),
            _msg("2026-08-04T10:01:00Z", "assistant", "A1"),
        ],
    )
    store.append_messages(
        "f-007",
        [_msg("2026-08-04T11:00:00Z", "user", "Q2"),
         _msg("2026-08-04T11:01:00Z", "assistant", "A2")],
    )
    text = store.file_path("f-007").read_text(encoding="utf-8")
    # 追加块用 ##
    assert "\n## 2026-08-04T11:00:00Z user\n" in text
    assert "\n## 2026-08-04T11:01:00Z assistant\n" in text
    # 首块仍是 #
    assert "\n# 2026-08-04T10:00:00Z user\n" in text

    parsed = store.parse_file("f-007")
    assert len(parsed.messages) == 4
    # 旧序号不变
    assert parsed.messages[0].seq == 1
    assert parsed.messages[0].content == "Q1"
    # 新消息续编
    assert parsed.messages[2].seq == 3
    assert parsed.messages[2].content == "Q2"
    assert parsed.messages[3].seq == 4
    assert parsed.messages[3].content == "A2"


def test_append_messages_file_not_found_raises(raw_dir):
    with pytest.raises(FileNotFoundError):
        store.append_messages("nope", [_msg("2026-08-04T10:00:00Z", "user", "x")])


# ---------- T3.4 增量段 ----------

def test_extract_incremental_returns_only_new(raw_dir):
    """增量段：seq > last_refined_seq 的消息。"""
    store.write_new_file(
        file_id="f-008", session_key="s8",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "Q1")],
    )
    store.append_messages(
        "f-008",
        [_msg("2026-08-04T11:00:00Z", "user", "Q2"),
         _msg("2026-08-04T11:01:00Z", "assistant", "A2")],
    )
    parsed = store.parse_file("f-008")
    # 已提炼到 seq=1
    inc = store.extract_incremental(parsed, last_refined_seq=1)
    assert len(inc) == 2
    assert inc[0].seq == 2
    assert inc[1].seq == 3
    # 全量（last_refined_seq=None）
    full = store.extract_incremental(parsed, last_refined_seq=None)
    assert len(full) == 3
    # last_refined_seq=0 → 全量
    full0 = store.extract_incremental(parsed, last_refined_seq=0)
    assert len(full0) == 3


def test_extract_incremental_no_new_returns_empty(raw_dir):
    """无增量（last_refined_seq >= 最大 seq）→ 空列表。"""
    store.write_new_file(
        file_id="f-009", session_key="s9",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "Q1")],
    )
    parsed = store.parse_file("f-009")
    inc = store.extract_incremental(parsed, last_refined_seq=1)
    assert inc == []


# ---------- T3.5 坏文件 ----------

def test_missing_frontmatter_raises_format_error(raw_dir):
    """缺 frontmatter 的坏文件 → L0FormatError。"""
    path = store.file_path("f-bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("这是正文，没有 frontmatter", encoding="utf-8")
    with pytest.raises(store.L0FormatError, match="frontmatter"):
        store.parse_file("f-bad")


def test_unclosed_frontmatter_raises(raw_dir):
    """frontmatter 未闭合 → L0FormatError。"""
    path = store.file_path("f-bad2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nfile_id: x\nsession_key: y\n（缺闭合）\n正文", encoding="utf-8")
    with pytest.raises(store.L0FormatError, match="未闭合"):
        store.parse_file("f-bad2")


def test_wrong_format_version_raises(raw_dir):
    """format_version 不识别 → L0FormatError。"""
    path = store.file_path("f-bad3")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nformat_version: 99\nfile_id: x\nsession_key: y\n---\n正文",
        encoding="utf-8",
    )
    with pytest.raises(store.L0FormatError, match="format_version"):
        store.parse_file("f-bad3")


def test_validate_or_mark_error_returns_false_for_bad_file(raw_dir):
    """坏文件 → validate_or_mark_error 返回 (False, reason)。"""
    path = store.file_path("f-bad4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("无 frontmatter", encoding="utf-8")
    ok, reason = store.validate_or_mark_error("f-bad4")
    assert ok is False
    assert "frontmatter" in reason


def test_validate_or_mark_error_returns_true_for_good_file(raw_dir):
    store.write_new_file(
        file_id="f-good", session_key="s",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "ok")],
    )
    ok, reason = store.validate_or_mark_error("f-good")
    assert ok is True
    assert reason is None


# ---------- T3.6 完整 append 流程（文件 + raw_files 表） ----------

def test_append_session_flow_updates_raw_files(raw_dir, session_conn):
    """完整 append 流程：写文件 + raw_files 表更新（status=new）。

    模拟 server 层 /v1/append 的协调逻辑：
    1. 文件不存在 → write_new_file + insert_raw_file(status=new)
    2. 重复 append（同 session_key）→ append_messages + mark_status(new)
    """
    # 首次 append
    path = store.write_new_file(
        file_id="f-flow", session_key="sess_flow",
        started_at="2026-08-04T10:00:00Z", agent_id="hermes",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "Q1")],
    )
    session_dao.insert_raw_file(
        session_conn, file_id="f-flow",
        path=store.relative_path("f-flow"),
        session_key="sess_flow", started_at="2026-08-04T10:00:00Z",
        agent_id="hermes", status="new", size=store.file_size("f-flow"),
    )

    rf = session_dao.get_raw_file(session_conn, "f-flow")
    assert rf["status"] == "new"
    assert rf["session_key"] == "sess_flow"
    assert rf["size"] > 0

    # 第二次 append（追加新消息）
    store.append_messages(
        "f-flow",
        [_msg("2026-08-04T11:00:00Z", "assistant", "A1")],
    )
    # 更新 raw_files：status 重置 new（增量提炼触发）+ ended_at + size
    session_dao.mark_status(
        session_conn, "f-flow", status="new",
        ended_at="2026-08-04T11:00:00Z", size=store.file_size("f-flow"),
    )
    rf2 = session_dao.get_raw_file(session_conn, "f-flow")
    assert rf2["status"] == "new"
    assert rf2["ended_at"] == "2026-08-04T11:00:00Z"
    assert rf2["size"] > rf["size"]

    # 增量段 = 新消息（last_refined_seq=1）
    parsed = store.parse_file("f-flow")
    inc = store.extract_incremental(parsed, last_refined_seq=1)
    assert len(inc) == 1
    assert inc[0].content == "A1"


def test_append_session_idempotent_same_session_key(raw_dir, session_conn):
    """同 session_key 重复 append：文件追加，raw_files 不重复插入。"""
    store.write_new_file(
        file_id="f-idem", session_key="sess_idem",
        started_at="2026-08-04T10:00:00Z",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "Q1")],
    )
    session_dao.insert_raw_file(
        session_conn, file_id="f-idem", path=store.relative_path("f-idem"),
        session_key="sess_idem", started_at="2026-08-04T10:00:00Z",
        status="new", size=store.file_size("f-idem"),
    )
    # 幂等：再 insert_raw_file 同 file_id（upsert）
    session_dao.insert_raw_file(
        session_conn, file_id="f-idem", path=store.relative_path("f-idem"),
        session_key="sess_idem", started_at="2026-08-04T10:00:00Z",
        status="new", size=store.file_size("f-idem"),
    )
    # raw_files 仍只有一行
    assert session_dao.count_by_status(session_conn, "new") == 1


def test_bad_file_marked_error_in_raw_files(raw_dir, session_conn):
    """坏文件 → raw_files.status=error。"""
    path = store.file_path("f-err")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("无 frontmatter", encoding="utf-8")
    session_dao.insert_raw_file(
        session_conn, file_id="f-err", path=store.relative_path("f-err"),
        session_key="s", started_at="2026-08-04T10:00:00Z", status="new",
    )
    ok, reason = store.validate_or_mark_error("f-err")
    assert not ok
    session_dao.mark_status(session_conn, "f-err", status="error")
    rf = session_dao.get_raw_file(session_conn, "f-err")
    assert rf["status"] == "error"


# ---------- T3.7 路径与大小 ----------

def test_relative_path_uses_sessions_subdir(raw_dir):
    store.write_new_file(
        file_id="f-rel", session_key="s",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "x")],
    )
    rel = store.relative_path("f-rel")
    assert "sessions" in rel.replace("\\", "/")
    assert rel.replace("\\", "/").endswith("f-rel.md")


def test_file_size_returns_bytes(raw_dir):
    store.write_new_file(
        file_id="f-size", session_key="s",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "12345")],
    )
    assert store.file_size("f-size") > 0


def test_upload_source_type_uses_uploads_subdir(raw_dir):
    """source_type=upload → uploads/ 子目录。"""
    store.write_new_file(
        file_id="f-up", session_key="upload-1",
        source_type="upload",
        first_messages=[_msg("2026-08-04T10:00:00Z", "user", "资料内容")],
    )
    rel = store.relative_path("f-up", source_type="upload")
    assert "uploads" in rel.replace("\\", "/")
