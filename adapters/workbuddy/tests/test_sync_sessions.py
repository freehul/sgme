# -*- coding: utf-8 -*-
"""sync_sessions.py 回归测试（T2，2026-11-13）。

覆盖：首次导入打标（D1）/ 二次幂等 / 增量只发新消息 / 已删与会话缺失 /
jsonl 截断保护 / dry-run 不写状态。全部零网络（FakeClient）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER / "scripts"))

import sync_sessions as ss  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
CWD = "C:/Users/user/WorkBuddy/2026-11-13-10-00-00"  # scan-allow（虚构示例路径，非本机实况）


class FakeClient:
    """只记录调用，返回递增 file_id。"""

    def __init__(self):
        self.calls: list[dict] = []

    def append(self, session_key, text, role="user", agent_id=None,
               started_at=None, metadata=None, raw=False):
        self.calls.append({
            "session_key": session_key, "text": text, "started_at": started_at,
            "metadata": metadata, "raw": raw,
        })
        return {"status": "new", "file_id": f"fid-{len(self.calls)}"}


def _make_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, status TEXT, "
            "created_at TEXT, updated_at TEXT, deleted_at TEXT, is_playground TEXT, "
            "is_background_automation TEXT)"
        )
        conn.execute("DELETE FROM sessions")
        for r in rows:
            conn.execute(
                "INSERT INTO sessions (id, cwd, title, status, created_at, updated_at, "
                "deleted_at, is_playground, is_background_automation) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["id"], r.get("cwd", CWD), r.get("title", "t"), r.get("status", "idle"),
                 r.get("created_at", "1794000000000"), r.get("updated_at", "1794000001000"),
                 r.get("deleted_at"), r.get("is_playground"), r.get("is_background_automation")),
            )
        conn.commit()
    finally:
        conn.close()


def _write_jsonl(projects: Path, messages: list[dict], sid: str = SID, cwd: str = CWD) -> Path:
    d = projects / ss.encode_cwd(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8", newline="\n") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return p


def _msg(role: str, text: str, ts: int = 1794000000000) -> dict:
    return {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            "timestamp": ts}


def _run(tmp_path: Path, client, *, rows=None, messages=None, dry_run=False):
    db = tmp_path / "workbuddy.db"
    projects = tmp_path / "projects"
    state_path = tmp_path / "state.json"
    _make_db(db, rows if rows is not None else [
        {"id": SID, "updated_at": "1794000001000", "is_playground": "1"},
    ])
    if messages is not None:
        _write_jsonl(projects, messages)
    state = ss.load_state(state_path)
    report = ss.sync_all(
        client, ss.discover_sessions(db), state, projects,
        dry_run=dry_run, state_path=state_path,
    )
    return report, state_path


def test_first_sync_imports_with_metadata(tmp_path):
    client = FakeClient()
    report, state_path = _run(tmp_path, client, messages=[
        _msg("user", "帮我看下 SGME 的检索"),
        _msg("assistant", "好的，先查记忆库", ts=1794000005000),
    ])

    assert report["scanned"] == 1
    assert report["ingested"] == 1
    assert report["appended_messages"] == 2
    assert report["failed"] == 0

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["raw"] is True
    assert call["session_key"] == f"workbuddy-{SID[:8]}"
    assert call["metadata"] == {
        "origin": "workbuddy", "playground": True, "background_automation": False,
    }
    assert call["text"].startswith("# ") and "\n" in call["text"]
    assert "## " in call["text"]                       # assistant 块
    assert call["started_at"].endswith("+08:00")       # 本机时区口径

    state = json.loads(state_path.read_text(encoding="utf-8"))
    entry = state["sessions"][SID]
    assert entry["file_id"] == "fid-1"
    assert entry["synced_messages"] == 2


def test_second_run_is_idempotent(tmp_path):
    client = FakeClient()
    db = tmp_path / "workbuddy.db"
    projects = tmp_path / "projects"
    state_path = tmp_path / "state.json"
    _make_db(db, [{"id": SID, "updated_at": "1794000001000"}])
    _write_jsonl(projects, [_msg("user", "hi")])

    state = ss.load_state(state_path)
    r1 = ss.sync_all(client, ss.discover_sessions(db), state, projects, state_path=state_path)
    state = ss.load_state(state_path)          # 模拟下次进程重新读盘
    r2 = ss.sync_all(client, ss.discover_sessions(db), state, projects, state_path=state_path)

    assert r1["ingested"] == 1
    assert r2["ingested"] == 0
    assert r2["skip_reasons"] == {"unchanged": 1}
    assert len(client.calls) == 1              # 没有重复发


def test_incremental_append_only_new_messages(tmp_path):
    client = FakeClient()
    db = tmp_path / "workbuddy.db"
    projects = tmp_path / "projects"
    state_path = tmp_path / "state.json"
    _make_db(db, [{"id": SID, "updated_at": "1794000001000"}])
    _write_jsonl(projects, [_msg("user", "第一条"), _msg("assistant", "回复一", 1794000001000)])

    state = ss.load_state(state_path)
    ss.sync_all(client, ss.discover_sessions(db), state, projects, state_path=state_path)

    # WorkBuddy 追加一条用户消息并更新 updated_at
    _write_jsonl(projects, [
        _msg("user", "第一条"), _msg("assistant", "回复一", 1794000001000),
        _msg("user", "第二条", 1794000009000),
    ])
    _make_db(db, [{"id": SID, "updated_at": "1794000009000"}])
    state = ss.load_state(state_path)
    r2 = ss.sync_all(client, ss.discover_sessions(db), state, projects, state_path=state_path)

    assert r2["ingested"] == 1
    assert r2["appended_messages"] == 1
    assert len(client.calls) == 2
    assert "第一条" not in client.calls[1]["text"]
    assert "第二条" in client.calls[1]["text"]


def test_deleted_missing_and_empty_are_reported(tmp_path):
    client = FakeClient()
    other = "99999999-0000-0000-0000-000000000000"
    report, _ = _run(
        tmp_path, client,
        rows=[
            {"id": SID, "updated_at": "1794000001000", "deleted_at": "1794000002000"},
            {"id": other, "updated_at": "1794000001000"},
        ],
        messages=[],   # 两条都没有可解析消息
    )
    assert report["ingested"] == 0
    assert report["skip_reasons"] == {"deleted": 1, "no_jsonl": 1}
    assert client.calls == []


def test_empty_jsonl_skipped(tmp_path):
    client = FakeClient()
    db = tmp_path / "workbuddy.db"
    projects = tmp_path / "projects"
    _make_db(db, [{"id": SID, "updated_at": "1794000001000"}])
    _write_jsonl(projects, [])
    report = ss.sync_all(client, ss.discover_sessions(db), ss.load_state(tmp_path / "s.json"), projects)
    assert report["skip_reasons"] == {"empty": 1}


def test_jsonl_shrunk_does_not_duplicate(tmp_path):
    client = FakeClient()
    db = tmp_path / "workbuddy.db"
    projects = tmp_path / "projects"
    state_path = tmp_path / "state.json"
    _make_db(db, [{"id": SID, "updated_at": "1794000001000"}])
    _write_jsonl(projects, [_msg("user", "a"), _msg("assistant", "b", 1794000001000)])
    state = ss.load_state(state_path)
    ss.sync_all(client, ss.discover_sessions(db), state, projects, state_path=state_path)

    # jsonl 被截断（只剩 1 条）且 updated_at 变化 → 不猜、不重发
    _write_jsonl(projects, [_msg("user", "a")])
    _make_db(db, [{"id": SID, "updated_at": "1794000009000"}])
    state = ss.load_state(state_path)
    report = ss.sync_all(client, ss.discover_sessions(db), state, projects, state_path=state_path)

    assert report["skip_reasons"] == {"jsonl_shrunk": 1}
    assert len(client.calls) == 1


def test_dry_run_writes_nothing(tmp_path):
    client = FakeClient()
    report, state_path = _run(tmp_path, client, messages=[_msg("user", "hi")], dry_run=True)
    assert report["ingested"] == 1
    assert report["appended_messages"] == 1   # 报告口径：计划发送的消息数
    assert client.calls == []
    assert not state_path.exists()


# ---------------------------------------------------------------- 同步窗口（2026-11-13）

def test_resolve_window_defaults_to_seven_days():
    now = ss.datetime(2026, 11, 13, 12, 0, tzinfo=ss.TZ)
    got = ss.resolve_since_ms(None, None, False, now=now)
    want = int((now - ss.timedelta(days=ss.DEFAULT_WINDOW_DAYS)).timestamp() * 1000)
    assert got == want


def test_resolve_window_all_disables():
    assert ss.resolve_since_ms(None, None, True) is None


def test_resolve_window_zero_days_disables():
    assert ss.resolve_since_ms(None, 0, False) is None


def test_resolve_window_custom_days():
    now = ss.datetime(2026, 11, 13, 12, 0, tzinfo=ss.TZ)
    got = ss.resolve_since_ms(None, 30, False, now=now)
    want = int((now - ss.timedelta(days=30)).timestamp() * 1000)
    assert got == want


def test_resolve_window_since_overrides_all_forms():
    want = ss._parse_since("2026-11-07")
    now = ss.datetime(2026, 11, 13, 12, 0, tzinfo=ss.TZ)
    assert ss.resolve_since_ms("2026-11-07", 30, True, now=now) == want


def test_parser_defaults_are_windowed_not_full():
    args = ss.build_parser().parse_args([])
    assert args.days is None and args.all is False and args.since is None
    args_all = ss.build_parser().parse_args(["--all"])
    assert args_all.all is True
    args_days = ss.build_parser().parse_args(["--days", "3"])
    assert args_days.days == 3


def test_window_filters_discovery(tmp_path):
    """窗口下界真的作用到 discover_sessions（老会话被过滤）。"""
    now = ss.datetime(2026, 11, 13, 12, 0, tzinfo=ss.TZ)
    old_ms = int((now - ss.timedelta(days=40)).timestamp() * 1000)
    new_ms = int((now - ss.timedelta(days=1)).timestamp() * 1000)
    db = tmp_path / "wb.db"
    _make_db(db, [
        {"id": "old-old-old-old-old", "updated_at": str(old_ms)},
        {"id": "new-new-new-new-new", "updated_at": str(new_ms)},
    ])
    since = ss.resolve_since_ms(None, None, False, now=now)
    got = [r["id"][:3] for r in ss.discover_sessions(db, since)]
    assert got == ["new"]
    assert len(ss.discover_sessions(db, None)) == 2
