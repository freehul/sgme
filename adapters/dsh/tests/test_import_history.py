"""import_history.py 测试（dsh 历史会话补导入，T-90 对齐 rc8 zstd 格式）。

测试覆盖：
- L0 格式转换（to_l0）
- zstd 事件流解析（parse_session_file：session 头/消息提取/噪音过滤）
- 会话发现（discover_sessions：新老目录命名/Temp 排除/无目录）
- 幂等去重键（main 的 session_key 生成逻辑）
- append 调用（mock httpx）

不依赖真实 SGME 服务。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_history  # noqa: E402

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

pytestmark = pytest.mark.skipif(zstandard is None, reason="需要 zstandard 依赖")


def make_zstd_file(path: Path, events: list[dict]) -> Path:
    """把事件列表压缩为 zstd 会话文件（与 dsh rc8 同构：多行 JSON）。"""
    payload = "\n".join(json.dumps(e, ensure_ascii=False) for e in events).encode("utf-8")
    cctx = zstandard.ZstdCompressor()
    path.write_bytes(cctx.compress(payload))
    return path


def sample_events() -> list[dict]:
    """构造与真实 dsh rc8 会话同构的事件流（含噪音事件）。"""
    return [
        {"type": "session", "version": 0, "id": "sess-abc", "createdAt": 1787208816366,
         "cwd": "D:\\Projects\\SGME", "agentPreset": "code"},
        {"type": "turn/start", "seq": 0, "time": 1787208816366, "data": {"turn": 1}},
        {"type": "user/message", "seq": 1, "time": 1787208816366,
         "data": {"role": "user", "content": [{"type": "text", "text": "你好"}]}},
        {"type": "assistant/message", "seq": 2, "time": 1787208816367,
         "data": {"turn": 1, "step": 0, "message": {"role": "assistant", "content": [
             {"type": "reasoning", "text": "思考过程（应被忽略）"},
             {"type": "text", "text": "回答第一段"},
             {"type": "text", "text": "回答第二段"},
         ]}}},
        {"type": "assistant/chunk", "seq": 3, "time": 1787208816368, "data": {}},
        {"type": "tool/result", "seq": 4, "time": 1787208816369,
         "data": {"turn": 1, "step": 1, "message": {"source": {"callId": "c1"},
                    "content": [{"type": "tool-result", "toolCallId": "c1",
                                 "content": [{"type": "text", "text": "工具输出"}]}]}}},
        {"type": "step/end", "seq": 5, "time": 1787208816370, "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "seq": 6, "time": 1787208816371, "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]


# ---------- L0 格式转换 ----------

def test_to_l0_user_message():
    """user 消息转 L0 格式（# {ts} user）。"""
    messages = [{"role": "user", "content": "你好", "ts": "2026-08-14T10:00:00Z"}]
    l0 = import_history.to_l0(messages)
    assert "# 2026-08-14T10:00:00Z user\n你好" in l0


def test_to_l0_assistant_message():
    """assistant 消息转 L0 格式（## {ts} assistant）。"""
    messages = [{"role": "assistant", "content": "回答", "ts": "2026-08-14T10:00:01Z"}]
    l0 = import_history.to_l0(messages)
    assert "## 2026-08-14T10:00:01Z assistant\n回答" in l0


def test_to_l0_tool_message():
    """tool 消息转 L0 格式（## {ts} tool + **tool**: name 前缀）。"""
    messages = [{"role": "tool", "content": '{"ok": true}', "name": "memory_search",
                 "ts": "2026-08-14T10:00:02Z"}]
    l0 = import_history.to_l0(messages)
    assert "## 2026-08-14T10:00:02Z tool\n**tool**: memory_search\n" in l0


def test_to_l0_multiple_messages():
    """多消息拼接正确。"""
    messages = [
        {"role": "user", "content": "问", "ts": "2026-08-14T10:00:00Z"},
        {"role": "assistant", "content": "答", "ts": "2026-08-14T10:00:01Z"},
    ]
    l0 = import_history.to_l0(messages)
    assert l0.count("\n\n") == 1  # 两个块之间一个空行分隔
    assert l0.endswith("\n")  # 末尾换行


# ---------- zstd 事件流解析 ----------

def test_parse_session_file_zstd_full(tmp_path):
    """完整事件流解析：session 头 + 噪音过滤 + 三类消息提取。"""
    f = make_zstd_file(tmp_path / "session.jsonl.zstd", sample_events())
    msgs = import_history.parse_session_file(f)
    assert len(msgs) == 3
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "你好"
    assert msgs[1]["role"] == "assistant"
    # reasoning 块被忽略，text 块按序拼接
    assert msgs[1]["content"] == "回答第一段\n回答第二段"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["content"] == "工具输出"
    assert msgs[2]["name"] == "tool"
    # 时间戳毫秒 → ISO
    assert msgs[0]["ts"] == "2026-08-20T06:53:36Z"  # 1787208816366 ms


def test_parse_session_file_skips_noise_and_invalid(tmp_path):
    """噪音事件（chunk/step/session 头）与非法行全部跳过。"""
    f = make_zstd_file(tmp_path / "session.jsonl.zstd", [
        {"type": "session", "version": 0, "id": "s1"},
        {"type": "assistant/chunk", "seq": 1, "time": 1, "data": {}},
        {"type": "step/start", "seq": 2, "time": 2, "data": {}},
        "this is not json",
        {"type": "user/message", "seq": 3, "time": 1787208816366,
         "data": {"role": "user", "content": [{"type": "text", "text": "有效"}]}},
    ])
    msgs = import_history.parse_session_file(f)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "有效"


def test_parse_session_file_filters_empty_content(tmp_path):
    """过滤空内容消息。"""
    f = make_zstd_file(tmp_path / "session.jsonl.zstd", [
        {"type": "user/message", "seq": 0, "time": 1787208816366,
         "data": {"role": "user", "content": [{"type": "text", "text": "   "}]}},
        {"type": "user/message", "seq": 1, "time": 1787208816367,
         "data": {"role": "user", "content": [{"type": "text", "text": "有内容"}]}},
    ])
    msgs = import_history.parse_session_file(f)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "有内容"


def test_parse_session_file_corrupt_zstd(tmp_path):
    """损坏的 zstd 文件返回空列表（不抛异常）。"""
    f = tmp_path / "session.jsonl.zstd"
    f.write_bytes(b"not zstd data at all")
    assert import_history.parse_session_file(f) == []


# ---------- 会话发现 ----------

def _make_session_tree(root: Path, workspace: str, session_id: str) -> Path:
    """构造 sessions/<workspace>/<id>/session.jsonl.zstd 树。"""
    sdir = root / workspace / session_id
    sdir.mkdir(parents=True)
    f = sdir / "session.jsonl.zstd"
    return make_zstd_file(f, sample_events())


def test_discover_sessions_finds_old_and_new_naming(tmp_path, monkeypatch):
    """老格式 session-<uuid>/ 与新格式 <uuid>/ 都被发现。"""
    root = tmp_path / "sessions"
    monkeypatch.setattr(import_history, "_DSH_HOME", tmp_path)
    _make_session_tree(root, "--D-Projects-SGME--", "session-abc123")
    _make_session_tree(root, "--D-Projects-SGME--", "def456")
    files = import_history.discover_sessions()
    assert len(files) == 2
    names = {f.parent.name for f in files}
    assert names == {"session-abc123", "def456"}


def test_discover_sessions_excludes_test_workspaces(tmp_path, monkeypatch):
    """Temp / cli-test 工作区排除（e2e 不污染记忆池）。"""
    root = tmp_path / "sessions"
    monkeypatch.setattr(import_history, "_DSH_HOME", tmp_path)
    _make_session_tree(root, "--D-Projects-SGME--", "good1")
    _make_session_tree(root, "--C-Users-LEO-AppData-Local-Temp-dsh-cli-test--", "bad1")
    _make_session_tree(root, "--Tmp-cli-test--", "bad2")
    files = import_history.discover_sessions()
    assert len(files) == 1
    assert files[0].parent.name == "good1"


def test_discover_sessions_returns_empty_when_no_dir(tmp_path, monkeypatch):
    """会话目录不存在时返回空列表（不抛异常）。"""
    monkeypatch.setattr(import_history, "_DSH_HOME", tmp_path / "nonexistent")
    assert import_history.discover_sessions() == []


# ---------- 幂等键（T-90 统一 key：毫秒对齐实时链路 + 双形态查重） ----------

def test_session_key_for_uses_first_user_ms():
    """session_key 优先 dsh-{首条 user 消息毫秒}（对齐实时链路 session-sync）。"""
    messages = [
        {"role": "user", "content": "你好", "ts": "2026-08-20T02:53:36Z", "ms": 1787208816366},
        {"role": "assistant", "content": "回答", "ts": "2026-08-20T02:53:37Z"},
    ]
    assert import_history.session_key_for(messages, "session-abc") == "dsh-1787208816366"


def test_session_key_for_fallback_dir_name_without_ms():
    """无有效 user 毫秒时兜底目录名（防御，正常空会话不会导入）。"""
    messages = [{"role": "assistant", "content": "回答", "ts": "2026-08-20T02:53:37Z"}]
    assert import_history.session_key_for(messages, "session-abc") == "dsh-session-abc"


def test_session_key_for_skips_empty_ms():
    """user 消息 ms 为空时继续找下一条有效 user 毫秒。"""
    messages = [
        {"role": "user", "content": "无毫秒", "ts": "2026-08-20T02:53:36Z", "ms": None},
        {"role": "user", "content": "有毫秒", "ts": "2026-08-20T02:53:37Z", "ms": 1787208816370},
    ]
    assert import_history.session_key_for(messages, "dir") == "dsh-1787208816370"


def test_is_already_imported_dir_form():
    """目录名形态命中（历史导入的 130 条）→ 已导入。"""
    msgs = [{"role": "user", "content": "x", "ts": "t", "ms": 1787208816366}]
    existing = {"dsh-session-abc"}
    assert import_history.is_already_imported("session-abc", msgs, existing) is True


def test_is_already_imported_ms_form():
    """首条毫秒形态命中（实时链路已覆盖）→ 已导入。"""
    msgs = [{"role": "user", "content": "x", "ts": "t", "ms": 1787208816366}]
    existing = {"dsh-1787208816366"}
    assert import_history.is_already_imported("session-abc", msgs, existing) is True


def test_is_already_imported_not_hit():
    """两种形态都未命中 → 待导入。"""
    msgs = [{"role": "user", "content": "x", "ts": "t", "ms": 1787208816366}]
    existing = {"dsh-other"}
    assert import_history.is_already_imported("session-abc", msgs, existing) is False


# ---------- NAS 查重（T-90 修复：查生产真相源） ----------

def test_get_existing_session_keys_queries_nas(monkeypatch):
    """查重走 NAS /v1/admin/sessions 分页拉取（不再查本地库）。"""
    class FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    pages = iter([
        FakeResp(200, {"items": [{"session_key": "dsh-session-abc"}, {"session_key": "dsh-def456"}],
                       "total": 150, "page": 1, "limit": 100}),
        FakeResp(200, {"items": [{"session_key": "dsh-other"}, {"session_key": "not-dsh-key"}],
                       "total": 150, "page": 2, "limit": 100}),
    ])

    class FakeClient:
        def __init__(self, *a, **k):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            return next(pages)

        def close(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(import_history, "_http", lambda: fake)
    keys = import_history.get_existing_session_keys()
    assert keys == {"dsh-session-abc", "dsh-def456", "dsh-other"}  # 非 dsh- 前缀排除
    assert len(fake.calls) == 2
    # 参数校验：agent_id + Admin Key + 分页
    assert fake.calls[0]["params"]["agent_id"] == "dsh"
    assert fake.calls[0]["headers"]["X-API-Key"] == import_history._ADMIN_KEY
    assert fake.calls[0]["params"]["page"] == 1
    assert fake.calls[1]["params"]["page"] == 2


def test_get_existing_session_keys_fallback_empty_on_error(monkeypatch):
    """查询失败回退空集合（服务端幂等兜底，不抛异常）。"""
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, url, **kwargs):
            raise ConnectionError("NAS 不可达")

        def close(self):
            pass

    monkeypatch.setattr(import_history, "_http", lambda: FakeClient())
    assert import_history.get_existing_session_keys() == set()


def test_get_existing_session_keys_stops_on_non_200(monkeypatch):
    """非 200 响应停止分页（不抛异常）。"""
    class FakeResp:
        status_code = 500

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, url, **kwargs):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(import_history, "_http", lambda: FakeClient())
    assert import_history.get_existing_session_keys() == set()


# ---------- 429 退避重试 ----------

def test_append_to_sgme_retries_on_429(monkeypatch):
    """429 限流按 retry_after 退避重试，成功后返回 True。"""
    import time

    class FakeResp:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    resp_seq = iter([
        FakeResp(429, {"error": {"details": {"retry_after_sec": 0}}}),  # 0s 加速测试
        FakeResp(200),
    ])

    class FakeClient:
        def __init__(self, *a, **k):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append(kwargs.get("json", {}).get("session_key"))
            return next(resp_seq)

        def close(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(import_history, "_http", lambda: fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # 不实际等待
    result = import_history.append_to_sgme("l0", "dsh-test", "ts")
    assert result is True
    assert len(fake.posts) == 2  # 429 后重试一次成功


def test_append_to_sgme_gives_up_after_max_retries(monkeypatch):
    """连续 429 超过 max_retries 返回 False。"""
    import time

    class FakeResp:
        status_code = 429

        def json(self):
            return {"error": {"details": {"retry_after_sec": 0}}}

    class FakeClient:
        def __init__(self, *a, **k):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append(1)
            return FakeResp()

        def close(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(import_history, "_http", lambda: fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    result = import_history.append_to_sgme("l0", "dsh-test", "ts", max_retries=2)
    assert result is False
    assert len(fake.posts) == 2  # 初始 + 1 次重试后放弃


# ---------- append 调用 ----------

def test_append_to_sgme_success(monkeypatch):
    """append 成功返回 True。"""
    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append({"url": url, **kwargs})
            return FakeResp()

        def close(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(import_history, "_http", lambda: fake)
    result = import_history.append_to_sgme("l0 text", "dsh-test", "2026-08-14T10:00:00Z")
    assert result is True
    assert len(fake.posts) == 1
    payload = fake.posts[0]["json"]
    assert payload["session_key"] == "dsh-test"
    assert payload["agent_id"] == "dsh"
    assert payload["content"] == "l0 text"


def test_append_to_sgme_failure_returns_false(monkeypatch):
    """append 失败返回 False（不抛异常）。"""
    class FakeResp:
        status_code = 500

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(import_history, "_http", lambda: FakeClient())
    assert import_history.append_to_sgme("l0", "dsh-test", "ts") is False
