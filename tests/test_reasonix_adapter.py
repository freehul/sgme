# -*- coding: utf-8 -*-
"""Reasonix 适配器增量导出测试（ST-23③，对称 tests/test_hermes_adapter.py）。

覆盖 2026-08-11 定案：
- SessionEnd 增量导出：游标持久化（.export_cursor.json），重跑只 append 新增段
- tool 消息去重：同一 tool 事件（tool_call_id / name+content 指纹）只导一次
- 每轮 append 用导出时刻作 started_at（毫秒精度 + 单调递增兜底）→ 引擎追加语义
- 游标推进时机：append 成功后才推进（失败重跑重导本段，不丢消息）
- 升级种子：本地游标缺失但 SGME 已有该会话 → 整段视为已导出（防重复追加）

假客户端 stub 模式，不依赖真实 SGME 服务。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "reasonix"))

import bridge  # noqa: E402


# ---------- 假客户端 stub（记录请求，不网络调用） ----------

class _FakeResponse:
    """最小响应 stub。"""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"ok": True}
        self.text = ""

    def json(self):
        return self._json


class _FakeClient:
    """记录 POST/GET 调用的 httpx 客户端 stub。"""

    def __init__(self):
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.append_status = 200
        self.sessions_items: list[dict] = []

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse(self.append_status)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.gets.append({"url": url, **kwargs})
        return _FakeResponse(200, {"items": self.sessions_items, "total": len(self.sessions_items)})

    def close(self) -> None:
        pass


# ---------- 会话样例构造 ----------

def _session_lines(n_msgs: int = 4, tool_dup: int = 4, no_id_dup: int = 0) -> list[dict]:
    """构造 Reasonix 会话 jsonl 行。

    n_msgs=4：user + assistant + tool(call_001) + assistant；
    tool_dup：同一 tool 事件（同 tool_call_id）重复记录次数（实锤场景）；
    no_id_dup：无 tool_call_id 的同 name+content 重复条数（内容指纹兜底场景）。
    """
    lines = [
        {"role": "user", "content": "<reasoning-language>\n中文\n</reasoning-language>\n\n你好",
         "raw_content": "你好", "createdAt": "2026-08-07T02:10:51.9678252Z"},
        {"role": "assistant", "content": "回答一", "workDurationMs": 100},
        {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_001", "name": "read"},
        {"role": "assistant", "content": "回答二"},
    ]
    dup = {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_001", "name": "read"}
    for _ in range(tool_dup - 1):
        lines.insert(3, dict(dup))
    for _ in range(no_id_dup):
        lines.insert(3, {"role": "tool", "content": '{"no_id": 1}', "name": "grep"})
        lines.insert(3, {"role": "tool", "content": '{"no_id": 1}', "name": "grep"})
    return lines


def _session_key(sid: str) -> str:
    return f"reasonix-{sid}"


# ---------- fixture ----------

@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离环境：APPDATA → tmp_path；假客户端；游标文件 → tmp_path。"""
    projects = tmp_path / "reasonix" / "projects" / "d--projects-rx-hook-test" / "sessions"
    projects.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    fake = _FakeClient()
    monkeypatch.setattr(bridge, "_http", lambda: fake)
    monkeypatch.setattr(bridge, "_CURSOR_FILE", tmp_path / "cursor.json")
    ctx = SimpleNamespace(projects=projects, fake=fake, tmp=tmp_path)

    def write_session(sid: str, lines: list[dict] | None = None, **kw) -> Path:
        p = projects / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(l, ensure_ascii=False)
                               for l in (lines or _session_lines(**kw))) + "\n",
                     encoding="utf-8")
        return p

    ctx.write_session = write_session
    return ctx


def _payload(sid: str) -> dict:
    return {"event": "SessionEnd", "sessionId": sid, "cwd": r"D:\Projects\rx-hook-test"}


def _appends(env) -> list[dict]:
    """只取 /v1/append 的 POST 记录。"""
    return [p for p in env.fake.posts if p["url"].endswith("/v1/append")]


def _cursor(env) -> dict:
    return json.loads(bridge._CURSOR_FILE.read_text(encoding="utf-8"))


# ---------- 测试 ----------

def test_rerun_idempotent_no_duplicate_export(env):
    """首次 SessionEnd 全量导出；重跑（同文件）不重复导出；游标持久化。"""
    sid = "20260807-021137.257664600-test"
    env.write_session(sid, tool_dup=4)  # 4 条有效消息（tool 去重后）
    assert bridge.cmd_end(_payload(sid)) == 0
    appends = _appends(env)
    assert len(appends) == 1, "首次应导出一次"
    body = appends[0]["json"]
    assert body["session_key"] == _session_key(sid)
    assert body["content"].count("# ") == 4, "tool 4 次重复只导 1 条"
    # 游标已推进并落盘
    cur = _cursor(env)
    assert cur[_session_key(sid)]["exported"] == 4

    # 重跑（同一 SessionEnd 再次触发）：无新增 → 零 append
    env.fake.posts.clear()
    assert bridge.cmd_end(_payload(sid)) == 0
    assert env.fake.posts == [], "重跑不得重复导出"


def test_delta_export_only_new_messages(env):
    """会话文件增长后重跑：只 append 新增段；started_at 每轮不同。"""
    sid = "20260807-022241.123456789-delta"
    env.write_session(sid, n_msgs=4)
    assert bridge.cmd_end(_payload(sid)) == 0
    # 会话继续：文件追加 2 条消息
    p = env.projects / f"{sid}.jsonl"
    extra = [
        {"role": "user", "content": "继续", "raw_content": "继续",
         "createdAt": "2026-08-07T02:20:00.0000000Z"},
        {"role": "assistant", "content": "补充回答", "workDurationMs": 50},
    ]
    with p.open("a", encoding="utf-8") as f:
        for l in extra:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    assert bridge.cmd_end(_payload(sid)) == 0

    appends = _appends(env)
    assert len(appends) == 2
    body2 = appends[1]["json"]
    assert body2["content"].count("# ") == 2, "第二轮只导新增 2 条"
    assert "你好" not in body2["content"], "已导出消息不得重复"
    assert "继续" in body2["content"]
    # 每轮 started_at = 导出时刻，互不相同（否则引擎幂等丢弃后续轮）
    assert appends[0]["json"]["started_at"] != appends[1]["json"]["started_at"]
    assert _cursor(env)[_session_key(sid)]["exported"] == 6


def test_tool_dedup_by_call_id_and_content_fingerprint(env):
    """同一 tool 事件只导一次：tool_call_id 判重 + 无 ID 时 name+content 指纹兜底。"""
    sid = "20260807-021051.000000000-dedup"
    env.write_session(sid, tool_dup=4, no_id_dup=1)
    assert bridge.cmd_end(_payload(sid)) == 0
    body = _appends(env)[0]["json"]
    # user + assistant + tool(call_001) + tool(no_id 同内容去重后 1 条) + assistant
    assert body["content"].count("# ") == 5
    assert body["content"].count("**tool**") == 2, "两类 tool 各只 1 条"
    assert body["content"].count('{"ok": true}') == 1
    assert body["content"].count('{"no_id": 1}') == 1


def test_cursor_seed_from_sgme_skips_old_sessions(env):
    """本地游标缺失但 SGME 已有该会话（旧版全量导出）：整段视为已导出，不重复。"""
    sid = "20260807-021122.000000000-seed"
    env.write_session(sid, n_msgs=4)
    env.fake.sessions_items = [{"session_key": _session_key(sid), "file_id": "f1"}]
    assert bridge.cmd_end(_payload(sid)) == 0
    assert env.fake.posts == [], "SGME 已有会话 → 跳过导出（防升级后整段重复追加）"
    # 确认确实查了 SGME（种子来源）
    assert any("admin/sessions" in g["url"] for g in env.fake.gets)


def test_no_seed_when_sgme_absent(env):
    """SGME 无该会话且无游标：正常全量导出（种子不生效）。"""
    sid = "20260807-021137.111111111-fresh"
    env.write_session(sid, n_msgs=4)
    assert bridge.cmd_end(_payload(sid)) == 0
    assert len(_appends(env)) == 1
    assert _cursor(env)[_session_key(sid)]["exported"] == 4


def test_append_failure_does_not_advance_cursor(env):
    """append 失败：游标不推进（重跑重导本段，不丢消息）；恢复后成功推进。"""
    sid = "20260807-021137.222222222-fail"
    env.write_session(sid, n_msgs=4)
    env.fake.append_status = 500
    assert bridge.cmd_end(_payload(sid)) == 0
    assert not bridge._CURSOR_FILE.exists(), "失败不得推进游标"

    env.fake.append_status = 200
    env.fake.posts.clear()
    assert bridge.cmd_end(_payload(sid)) == 0
    assert len(_appends(env)) == 1, "重跑补导本段"
    assert _appends(env)[0]["json"]["content"].count("# ") == 4
    assert _cursor(env)[_session_key(sid)]["exported"] == 4


def test_next_started_at_ms_precision_and_monotonic_bump():
    """started_at：毫秒精度；last 在未来/同刻时单调 +1ms 兜底。"""
    ts = bridge._next_started_at(None)
    assert ts.endswith("Z") and "." in ts, "毫秒精度（防同秒碰撞幂等丢弃）"
    # 单调兜底：时钟回拨/同毫秒连续导出 → 强制 +1ms
    future = "2099-01-01T00:00:00.000Z"
    assert bridge._next_started_at(future) == "2099-01-01T00:00:00.001Z"
    assert bridge._next_started_at("2099-01-01T00:00:00.999Z") == "2099-01-01T00:00:01.000Z"


def test_cmd_end_missing_session_id_safe():
    """缺 sessionId 的 payload：静默返回 0，不抛异常、无网络调用。"""
    assert bridge.cmd_end({}) == 0
    assert bridge.cmd_end({"event": "SessionEnd", "cwd": r"D:\x"}) == 0
