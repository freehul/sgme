# -*- coding: utf-8 -*-
"""Hermes 适配器测试：每轮增量导出 + tool 消息去重 + started_at 追加语义（ST-23③）。

覆盖 2026-08-11 修复：
- v0.5 固定 started_at 曾导致 08-07 起每轮捕获失效（引擎幂等：同 session_key +
  同 started_at 丢弃）→ 现在每轮用导出时刻作 started_at，恢复追加语义
- tool 消息重复治理（同 tool_call_id 只导一次）
- on_session_end 补最后一轮增量 + 触发提炼
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import pytest

from adapters.hermes import SGMEProvider


class _FakeResponse:
    """最小响应 stub（append 返回 200）。"""

    def __init__(self, status_code: int = 200, text: str = "", json_body: Any = None):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def json(self):
        return self._json_body if self._json_body is not None else {"ok": True}


class _FakeClient:
    """记录 POST 调用的 httpx 客户端 stub。"""

    def __init__(self):
        self.posts: List[Dict[str, Any]] = []
        self.closed = False  # B152：对齐 httpx.Client.is_closed 接口

    @property
    def is_closed(self) -> bool:
        return self.closed

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse(200)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        return _FakeResponse(200)

    def close(self) -> None:
        pass


@pytest.fixture
def provider(monkeypatch) -> SGMEProvider:
    """构造 provider + 注入假客户端（探活恒真，记录请求）。"""
    fake = _FakeClient()
    p = SGMEProvider()
    p._session_key = "hermes-test-session"
    p._client = fake
    p._available = True
    p._probe_at = 0.0
    monkeypatch.setattr(p, "_probe", lambda: True)
    p._fake = fake  # type: ignore[attr-defined]
    yield p


def _wait_posts(provider, n: int, timeout: float = 5.0) -> None:
    """等待 provider 后台线程完成 n 次 append（轮询假客户端记录数）。"""
    deadline = timeout
    while deadline > 0:
        if len(provider._fake.posts) >= n:  # type: ignore[attr-defined]
            return
        threading.Event().wait(0.05)
        deadline -= 0.05
    pytest.fail(f"超时：期望 {n} 次 append，实际 {len(provider._fake.posts)}")


def test_incremental_export_dedup(provider):
    """两轮 sync_turn：只导出新增消息；重复 tool 消息只导一份。"""
    turn1 = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "tool", "content": '{"result": 1}', "tool_call_id": "call-1"},
        # 同 tool_call_id 重复块（实锤的导出重复场景）
        {"role": "tool", "content": '{"result": 1}', "tool_call_id": "call-1"},
    ]
    provider.sync_turn("第一轮问题", "第一轮回答", messages=turn1)
    _wait_posts(provider, 1)

    turn2 = turn1 + [
        {"role": "user", "content": "第二轮问题"},
        {"role": "assistant", "content": "第二轮回答"},
    ]
    provider.sync_turn("第二轮问题", "第二轮回答", messages=turn2)
    _wait_posts(provider, 2)

    posts = provider._fake.posts  # type: ignore[attr-defined]
    assert len(posts) == 2, f"期望 2 次 append，实际 {len(posts)}"
    # B35：append body 必须自报 agent_id（溯源）
    assert posts[0]["json"].get("agent_id") == "hermes", "append 应带 agent_id=hermes"
    # 第一轮：user + assistant + tool（去重后 3 块）
    body1 = posts[0]["json"]
    assert body1["session_key"] == "hermes-test-session"
    assert body1["content"].count("# ") == 3, "第一轮应含 3 块（tool 重复已去重）"
    # 第二轮：只导出新增 2 块（第一轮消息不重复导出）
    body2 = posts[1]["json"]
    assert body2["content"].count("# ") == 2, "第二轮应只含新增 2 块"
    assert "第一轮问题" not in body2["content"], "已导出消息不得重复"
    assert "第二轮问题" in body2["content"]


def test_started_at_per_turn(provider):
    """每轮 started_at 不同（导出时刻）→ 引擎追加语义恢复。"""
    provider.sync_turn("问题1", "回答1", messages=[
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
    ])
    _wait_posts(provider, 1)
    provider.sync_turn("问题2", "回答2", messages=[
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "问题2"},
        {"role": "assistant", "content": "回答2"},
    ])
    _wait_posts(provider, 2)
    posts = provider._fake.posts  # type: ignore[attr-defined]
    assert posts[0]["json"]["started_at"] != posts[1]["json"]["started_at"], \
        "每轮 started_at 必须不同（否则引擎幂等丢弃后续轮次）"


def test_on_session_end_flush_and_refine(provider):
    """会话结束：补最后增量（未导出消息）+ 触发提炼。"""
    turn = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
    # 第一轮已导出
    provider.sync_turn("问题", "回答", messages=turn)
    _wait_posts(provider, 1)
    # 会话结束前新增一轮（未同步）
    turn_final = turn + [
        {"role": "user", "content": "最后的补充"},
        {"role": "assistant", "content": "补充回答"},
    ]
    provider.on_session_end(turn_final)
    _wait_posts(provider, 2)

    posts = provider._fake.posts  # type: ignore[attr-defined]
    # 补的最后一轮只含新增 2 块
    body2 = posts[1]["json"]
    assert body2["content"].count("# ") == 2
    assert "最后的补充" in body2["content"]
    # 提炼触发（第三、四次 POST 分别是 append 补导 + refine trigger）
    urls = [p["url"] for p in posts]
    assert any("refine" in u for u in urls), "会话结束应触发提炼"


def test_no_messages_fallback(provider):
    """无 messages 参数 → 退化路径（旧行为：写 user/assistant 文本）。"""
    provider.sync_turn("旧调用方", "文本回答")
    _wait_posts(provider, 1)
    body = provider._fake.posts[0]["json"]  # type: ignore[attr-defined]
    assert "旧调用方" in body["content"]


def test_tool_dedup_without_call_id(provider):
    """无 tool_call_id 的 tool 消息：同内容指纹去重。"""
    provider.sync_turn("q", "a", messages=[
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": '{"same": true}'},
        {"role": "tool", "content": '{"same": true}'},
    ])
    _wait_posts(provider, 1)
    body = provider._fake.posts[0]["json"]  # type: ignore[attr-defined]
    assert body["content"].count("# ") == 3, "同内容 tool 消息应去重"


# ---------- prefetch 双 scope（T-42 修正：场景 = 对话内容驱动） ----------

def _make_search_fake(search_results: list[dict]):
    """构造 search 返回指定 results 的假客户端。"""

    class _FakeClientWithSearch(_FakeClient):
        def post(self, url: str, **kwargs) -> _FakeResponse:
            self.posts.append({"url": url, **kwargs})
            if url.endswith("/v1/search"):
                return _FakeResponse(200, json_body={"results": search_results})
            return _FakeResponse(200)

    return _FakeClientWithSearch()


def _prefetch_provider(monkeypatch, search_results: list[dict]):
    """prefetch 专用 provider（假客户端返回指定 search 结果）。"""
    fake = _make_search_fake(search_results)
    p = SGMEProvider()
    p._client = fake
    p._available = True
    p._probe_at = 0.0
    monkeypatch.setattr(p, "_probe", lambda: True)
    p._fake = fake  # type: ignore[attr-defined]
    return p


def test_prefetch_scopes_include_wiki(monkeypatch):
    """prefetch 请求 scopes = [memory, wiki]（场景语义匹配注入）。"""
    p = _prefetch_provider(monkeypatch, [])

    out = p.prefetch("今天聊什么", session_id="s1")

    assert out == ""  # 空结果 → 空串
    search_req = [x for x in p._fake.posts if x["url"].endswith("/v1/search")]  # type: ignore[attr-defined]
    assert len(search_req) == 1
    assert search_req[0]["json"]["scopes"] == ["memory", "wiki"]


def test_prefetch_splits_memory_and_scene_blocks(monkeypatch):
    """memory 结果进「相关记忆」块，wiki_scene 结果进「相关场景（L2 匹配）」块。"""
    p = _prefetch_provider(monkeypatch, [
        {"source": "memory", "content": "用户是独立开发者"},
        {"source": "wiki_scene", "title": "SGME 开发", "content": "SGME 记忆引擎架构设计，Python 自研，标签化记忆池"},
        {"source": "wiki_scene", "title": "Trae 配置", "content": "Trae 规则体系"},
    ])

    out = p.prefetch("SGME 架构", session_id="s1")

    assert "# 相关记忆（SGME）" in out
    assert "独立开发者" in out
    assert "# 相关场景（L2 匹配）" in out
    assert "[SGME 开发]" in out
    assert "[Trae 配置]" in out


def test_prefetch_scene_only_no_memory(monkeypatch):
    """只有场景命中 → 只出场景块（记忆块省略）。"""
    p = _prefetch_provider(monkeypatch, [
        {"source": "wiki_scene", "title": "SGME 开发", "content": "场景内容"},
    ])

    out = p.prefetch("SGME", session_id="s1")

    assert "相关记忆" not in out
    assert "# 相关场景（L2 匹配）" in out


# ---------- B152: client 生命周期（closed 后自动重建 + 关闭线程安全） ----------


class _ClosedAwareClient:
    """模拟 httpx.Client 关闭行为：closed=True 后所有请求抛 RuntimeError。

    同时记录 close() 调用次数与并发标记，用于验证关闭的线程安全性。
    """

    def __init__(self):
        self.posts: List[Dict[str, Any]] = []
        self.closed = False
        self.close_calls = 0
        self.close_concurrent = False  # close() 执行期间再进 close() 即置位

    @property
    def is_closed(self) -> bool:
        return self.closed

    def post(self, url: str, **kwargs) -> _FakeResponse:
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse(200)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return _FakeResponse(200)

    def close(self) -> None:
        if self.closed:
            self.close_concurrent = True
            return
        self.closed = True
        self.close_calls += 1


def _provider_with_closed_client(monkeypatch) -> SGMEProvider:
    """provider + 已关闭的假客户端 + 探活恒真。"""
    fake = _ClosedAwareClient()
    fake.closed = True  # 模拟 shutdown() 关闭后的状态
    p = SGMEProvider()
    p._session_key = "hermes-test-session"
    p._client = fake
    p._available = True
    p._probe_at = 0.0
    monkeypatch.setattr(p, "_probe", lambda: True)
    p._fake = fake  # type: ignore[attr-defined]
    return p


def test_trigger_refine_recovers_after_client_closed(monkeypatch):
    """B152：client 被 shutdown 关闭后，_trigger_refine 应自动重建 client 并成功发送。

    复现生产症状（2026-09-05，341 次/天）：on_session_end 的后台线程拿 client 引用
    → agent 拆卸 shutdown() 关闭 client → 线程用已关闭 client 发请求报
    "Cannot send a request, as the client has been closed."。
    拦截 httpx.Client 构造器让重建返回可控桩（测试零网络）。
    """
    import adapters.hermes as hermes_mod

    p = _provider_with_closed_client(monkeypatch)
    old = p._fake
    p._session_id = "s-recover"
    assert old.closed is True

    rebuilt = _ClosedAwareClient()
    monkeypatch.setattr(hermes_mod.httpx, "Client", lambda **kwargs: rebuilt)

    p._trigger_refine()

    # client 已重建（不复用已关闭的旧 client），请求从新 client 发出
    assert p._client is rebuilt
    assert old.closed is True  # 旧 client 保持关闭，未被复用
    assert len(rebuilt.posts) == 1
    assert "/v1/admin/refine/trigger_async" in rebuilt.posts[0]["url"]


def test_append_delta_recovers_after_client_closed(monkeypatch):
    """B152：client 已关闭时 _append_delta 同样自动重建（L0 捕获不因 shutdown 丢消息）。"""
    import adapters.hermes as hermes_mod

    p = _provider_with_closed_client(monkeypatch)
    old = p._fake
    p._session_id = "s-append"
    rebuilt = _ClosedAwareClient()
    monkeypatch.setattr(hermes_mod.httpx, "Client", lambda **kwargs: rebuilt)
    msgs = [
        {"role": "user", "content": "你好", "ts": "2026-09-05T10:00:00Z"},
        {"role": "assistant", "content": "你好呀", "ts": "2026-09-05T10:00:01Z"},
    ]

    p._append_delta(msgs)

    assert p._client is rebuilt
    assert len(rebuilt.posts) == 1


def test_shutdown_is_idempotent_and_concurrent_safe(monkeypatch):
    """B152：shutdown() 幂等 + 并发安全（消灭 WinError 10038 套接字竞争）。"""
    p = SGMEProvider()
    fake = _ClosedAwareClient()
    p._client = fake

    # 并发 shutdown ×3
    threads = [threading.Thread(target=p.shutdown) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fake.close_calls == 1  # 只关一次
    assert fake.close_concurrent is False  # 无并发重入
    assert p._client is None

    # 再次 shutdown 幂等
    p.shutdown()
    assert fake.close_calls == 1
