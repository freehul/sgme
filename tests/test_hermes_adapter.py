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


# ==========================================================================
# T-171：能力面对齐 MCP 41 工具基准（40 工具 + 2 项永久豁免）
# 基准唯一真源 = sgme/mcp_server.py 的工具面；hermes 侧只允许 2 项豁免
# （agent_onboarding：provider 槽位接入无 MCP 握手；append：sync_turn 自动入库）。
# ==========================================================================

import re  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import adapters.hermes as hermes_mod  # noqa: E402
from adapters.hermes import _TOOL_HANDLERS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# 永久豁免（理由见 adapters/hermes/README.md「工具清单」）
EXEMPT_TOOLS = {"agent_onboarding", "append"}
# 适配器自有工具（不在基准内，登记在 scripts/adapter_parity_map.yaml extra）
HERMES_EXTRA_TOOLS = {"sgme_conversation_search"}
# 基准名 → 适配器名的别名（parity map 的 aliases.search.hermes）
ALIASED = {"search": "sgme_memory_search"}

# 工具 → 期望 HTTP 端点（方法, 路径）；覆盖全部 40 个工具
_ENDPOINTS = {
    "sgme_memory_search": ("POST", "/v1/search"),
    "sgme_conversation_search": ("POST", "/v1/search"),
    "sgme_inject": ("POST", "/v1/inject"),
    "sgme_answer": ("POST", "/v1/answer"),
    "sgme_wiki_search": ("GET", "/v1/wiki/search"),
    "sgme_wiki_pages": ("GET", "/v1/wiki/pages"),
    "sgme_wiki_page": ("GET", "/v1/wiki/pages/page-1"),
    "sgme_wiki_page_add": ("POST", "/v1/wiki/pages"),
    "sgme_wiki_page_update": ("PATCH", "/v1/wiki/pages/page-1"),
    "sgme_wiki_evolve_trigger": ("POST", "/v1/wiki/evolve/trigger"),
    "sgme_memory_get": ("GET", "/v1/memory/mem-1"),
    "sgme_memory_reject": ("POST", "/v1/memory/mem-1/reject"),
    "sgme_memory_unreject": ("POST", "/v1/memory/mem-1/unreject"),
    "sgme_refine_trigger": ("POST", "/v1/admin/refine/trigger"),
    "sgme_refine_batch": ("POST", "/v1/admin/refine/trigger_async"),
    "sgme_refine_status": ("GET", "/v1/admin/refine_runs"),
    "sgme_stats": ("GET", "/v1/admin/stats"),
    "sgme_health": ("GET", "/v1/health"),
    "sgme_config_get": ("GET", "/v1/admin/config"),
    "sgme_config_update": ("POST", "/v1/admin/config"),
    "sgme_idea_add": ("POST", "/v1/admin/ideas"),
    "sgme_demand_create": ("POST", "/v1/admin/demands"),
    "sgme_project_register": ("POST", "/v1/admin/projects"),
    "sgme_signal_pull": ("GET", "/v1/events/pull"),
    "sgme_signal_claim": ("POST", "/v1/admin/care/signals/ev-1/consume"),
    "sgme_signal_ack": ("POST", "/v1/admin/care/signals/ev-1/ack"),
    "sgme_signal_clear": ("POST", "/v1/admin/events/consume_all"),
    "sgme_role_list": ("GET", "/v1/admin/roles"),
    "sgme_role_assemble": ("GET", "/v1/admin/roles/steward/assemble"),
    "sgme_role_active_get": ("GET", "/v1/admin/care/active-role"),
    "sgme_role_active_set": ("PUT", "/v1/admin/care/active-role"),
    "sgme_skill_search": ("POST", "/v1/search"),
    "sgme_skill_digest": ("GET", "/v1/skills/demo-skill/digest"),
    "sgme_skill_get": ("GET", "/v1/skills/demo-skill"),
    "sgme_skill_materialize": ("POST", "/v1/skills/demo-skill/materialize"),
    "sgme_skill_list": ("GET", "/v1/skills"),
    "sgme_skill_coldstart": ("GET", "/v1/skills/coldstart"),
    "sgme_skill_put": ("PUT", "/v1/admin/skills/demo-skill"),
    "sgme_skill_delete": ("DELETE", "/v1/admin/skills/demo-skill"),
    "sgme_skill_rename": ("POST", "/v1/admin/skills/demo-skill/rename"),
}

# 工具 → 最小调用参数（保证走到 HTTP 层，不被参数校验短路）
_CALL_ARGS = {
    "sgme_memory_search": {"query": "关键词"},
    "sgme_conversation_search": {"query": "关键词"},
    "sgme_inject": {},
    "sgme_answer": {"query": "我问过几次 X"},
    "sgme_wiki_search": {"query": "手册"},
    "sgme_wiki_pages": {},
    "sgme_wiki_page": {"page_id": "page-1"},
    "sgme_wiki_page_add": {"title": "标题", "content": "正文"},
    "sgme_wiki_page_update": {"page_id": "page-1", "content": "追加正文"},
    "sgme_wiki_evolve_trigger": {},
    "sgme_memory_get": {"memory_id": "mem-1"},
    "sgme_memory_reject": {"memory_id": "mem-1", "reason": "记错了"},
    "sgme_memory_unreject": {"memory_id": "mem-1"},
    "sgme_refine_trigger": {},
    "sgme_refine_batch": {},
    "sgme_refine_status": {},
    "sgme_stats": {},
    "sgme_health": {},
    "sgme_config_get": {},
    "sgme_config_update": {"section": "refine", "values": {"enabled": True}},
    "sgme_idea_add": {"content": "一个创意"},
    "sgme_demand_create": {"title": "一件事"},
    "sgme_project_register": {"project_id": "sgme"},
    "sgme_signal_pull": {},
    "sgme_signal_claim": {"event_id": "ev-1"},
    "sgme_signal_ack": {"event_id": "ev-1"},
    "sgme_signal_clear": {},
    "sgme_role_list": {},
    "sgme_role_assemble": {"role_id": "steward"},
    "sgme_role_active_get": {},
    "sgme_role_active_set": {"role_id": "steward"},
    "sgme_skill_search": {"query": "docker 部署"},
    "sgme_skill_digest": {"name": "demo-skill"},
    "sgme_skill_get": {"name": "demo-skill"},
    "sgme_skill_materialize": {"name": "demo-skill", "dest_dir": "/tmp/skills"},
    "sgme_skill_list": {},
    "sgme_skill_coldstart": {},
    "sgme_skill_put": {"name": "demo-skill", "content": "---\nname: demo\n---\n正文"},
    "sgme_skill_delete": {"name": "demo-skill"},
    "sgme_skill_rename": {"name": "demo-skill", "new_name": "demo-skill-2"},
}

# 写侧/管理类工具 → 必须走管理员 Key（服务端 /v1/admin/* 或写侧治理动作）
_ADMIN_TOOLS = {
    "sgme_wiki_page_add",
    "sgme_wiki_page_update",
    "sgme_memory_reject",
    "sgme_memory_unreject",
    "sgme_refine_trigger",
    "sgme_refine_batch",
    "sgme_refine_status",
    "sgme_stats",
    "sgme_config_get",
    "sgme_config_update",
    "sgme_idea_add",
    "sgme_demand_create",
    "sgme_project_register",
    "sgme_signal_clear",
    "sgme_skill_put",
    "sgme_skill_delete",
    "sgme_skill_rename",
}


class _RecordingResponse:
    """可配置响应 stub。"""

    def __init__(self, status_code: int = 200, json_body: Any = None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._json_body


class _RecordingClient:
    """记录全部 HTTP 动词调用的客户端 stub（对齐 httpx.Client 接口）。"""

    def __init__(self, response: _RecordingResponse | None = None, raise_error: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self._response = response or _RecordingResponse()
        self._raise = raise_error
        self.is_closed = False  # type: ignore[assignment]

    def _record(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._raise:
            raise ConnectionError("Connection refused（Gateway 未启动）")
        return self._response

    def get(self, url: str, **kwargs):
        return self._record("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._record("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self._record("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self._record("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self._record("DELETE", url, **kwargs)

    def close(self) -> None:
        self.is_closed = True


def _tool_provider(response: _RecordingResponse | None = None, raise_error: bool = False):
    """构造注入了记录客户端的 provider（不触网）。"""
    fake = _RecordingClient(response=response, raise_error=raise_error)
    p = SGMEProvider(base_url="http://127.0.0.1:9910", agent_key="agent-key-x", admin_key="admin-key-x")
    p._client = fake  # type: ignore[assignment]
    p._available = True
    p._probe_at = time.monotonic()  # 探活命中缓存（避免测试里多打一次 /v1/health）
    return p, fake


# ---------- 注册面：数量 + 名单 + 基准对齐 ----------


def _baseline_tools() -> set:
    """从 sgme/mcp_server.py 解析 MCP 工具面（唯一基准）。"""
    src = (REPO_ROOT / "sgme" / "mcp_server.py").read_text(encoding="utf-8")
    return set(re.findall(r'\{"name":\s*"([a-z_]+)"', src))


def test_tool_count_and_names():
    """注册 40 个工具：39 个基准覆盖 + conversation_search 自有工具。"""
    p, _ = _tool_provider()
    names = [s["name"] for s in p.get_tool_schemas()]
    assert len(names) == 40, f"工具数应为 40，实际 {len(names)}"
    assert len(set(names)) == 40, "工具名重复"
    assert set(EXEMPT_TOOLS).isdisjoint({n[len("sgme_"):] for n in names}), "豁免工具不应被实现"
    expected = (
        {f"sgme_{t}" for t in _baseline_tools() - EXEMPT_TOOLS - set(ALIASED)}
        | set(ALIASED.values())
        | HERMES_EXTRA_TOOLS
    )
    assert set(names) == expected, f"工具名单与基准不一致：缺 {sorted(expected - set(names))}，多 {sorted(set(names) - expected)}"


def test_tool_schemas_shape():
    """每个 schema 结构可用：description 非空 + parameters 是合法 object + required ⊆ properties。"""
    p, _ = _tool_provider()
    for s in p.get_tool_schemas():
        params = s.get("parameters")
        assert isinstance(s.get("description"), str) and s["description"].strip(), f"{s['name']} 缺 description"
        assert isinstance(params, dict) and params.get("type") == "object", f"{s['name']} parameters 非法"
        props = params.get("properties") or {}
        assert isinstance(props, dict)
        for key in params.get("required") or []:
            assert key in props, f"{s['name']} required 项 {key} 未在 properties 声明"


def test_every_tool_resolves_to_handler():
    """工具名 → 处理器方法必须存在（防 schema 与实现漂移）。"""
    p, _ = _tool_provider()
    for s in p.get_tool_schemas():
        name = s["name"]
        handler = _TOOL_HANDLERS.get(name) or f"_t_{name[len('sgme_'):]}"
        assert callable(getattr(p, handler, None)), f"{name} 无处理器 {handler}"
    # 未知工具名 → 结构化错误，不抛异常
    err = json.loads(p.handle_tool_call("sgme_不存在", {}))
    assert "error" in err


def test_endpoint_matrix():
    """40 个工具各自的 HTTP 方法与端点正确（契约矩阵）。"""
    p, fake = _tool_provider()
    assert set(_ENDPOINTS) == set(_CALL_ARGS), "端点矩阵与参数矩阵工具集不一致"
    for tool, (method, path) in _ENDPOINTS.items():
        fake.calls.clear()
        p.handle_tool_call(tool, dict(_CALL_ARGS[tool]))
        assert len(fake.calls) == 1, f"{tool} 期望 1 次 HTTP 调用，实际 {len(fake.calls)}"
        call = fake.calls[0]
        assert call["method"] == method, f"{tool} 方法应为 {method}，实际 {call['method']}"
        assert call["url"] == f"http://127.0.0.1:9910{path}", f"{tool} 端点应为 {path}，实际 {call['url']}"


# ---------- Key 口径：写侧 admin / 读侧 agent / health 免鉴权 ----------


def test_write_side_tools_use_admin_key():
    """写侧/管理类工具走管理员 Key，且 description 注明「需管理员 Key」。"""
    p, fake = _tool_provider()
    descriptions = {s["name"]: s["description"] for s in p.get_tool_schemas()}
    for tool in sorted(_ADMIN_TOOLS):
        fake.calls.clear()
        p.handle_tool_call(tool, dict(_CALL_ARGS[tool]))
        assert fake.calls, f"{tool} 未发起请求"
        assert fake.calls[0]["headers"]["X-API-Key"] == "admin-key-x", f"{tool} 应走管理员 Key"
        assert "管理员 Key" in descriptions[tool], f"{tool} description 应注明需管理员 Key"


def test_read_side_tools_use_agent_key():
    """读侧工具走 Agent Key。"""
    p, fake = _tool_provider()
    read_tools = sorted(set(_CALL_ARGS) - _ADMIN_TOOLS - {"sgme_health"})
    assert read_tools, "读侧工具集不应为空"
    for tool in read_tools:
        fake.calls.clear()
        p.handle_tool_call(tool, dict(_CALL_ARGS[tool]))
        assert fake.calls, f"{tool} 未发起请求"
        assert fake.calls[0]["headers"]["X-API-Key"] == "agent-key-x", f"{tool} 应走 Agent Key"


def test_health_needs_no_api_key():
    """/v1/health 免鉴权：不带 X-API-Key。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_health", {})
    assert "X-API-Key" not in fake.calls[0]["headers"]


# ---------- 检索层：search 必须带 skills scope（原缺陷修复点） ----------


def test_memory_search_includes_skills_scope():
    """缺陷修复回归：sgme_memory_search scopes 默认 [memory, skills]（旧版写死 ["memory"]）。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_memory_search", {"query": "docker 部署"})
    body = fake.calls[0]["json"]
    assert body["scopes"] == ["memory", "skills"], f"scopes 应含技能层，实际 {body['scopes']}"
    desc = [s for s in p.get_tool_schemas() if s["name"] == "sgme_memory_search"][0]["description"]
    assert "技能层" in desc and "sgme_skill_get" in desc, "description 应说明可检索技能层并配合 skill_get 取全文"


def test_memory_search_scope_override_and_filter():
    """scopes 可按参数覆盖，未知层被白名单挡掉。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_memory_search", {"query": "q", "scopes": ["wiki", "bogus", "memory"]})
    assert fake.calls[0]["json"]["scopes"] == ["wiki", "memory"]
    fake.calls.clear()
    p.handle_tool_call("sgme_memory_search", {"query": "q"})
    assert fake.calls[0]["json"]["scopes"] == ["memory", "skills"]


def test_memory_search_dimensions_and_limit():
    """dimensions/match 透传；limit 夹在 [1,20]。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_memory_search", {"query": "q", "dimensions": ["goals"], "match": "all", "limit": 99})
    body = fake.calls[0]["json"]
    assert body["dimensions"] == ["goals"] and body["match"] == "all"
    assert body["limit"] == 20, "limit 应夹到上限 20"


def test_conversation_search_uses_sessions_scope():
    """sgme_conversation_search 查 L0 会话层（sessions scope）。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_conversation_search", {"query": "原话"})
    assert fake.calls[0]["json"]["scopes"] == ["sessions"]


def test_search_returns_structured_results():
    """检索结果以 JSON 返回（query/scopes/count/results）。"""
    p, _ = _tool_provider(response=_RecordingResponse(200, {"results": [{"source": "memory", "content": "x"}]}))
    out = json.loads(p.handle_tool_call("sgme_memory_search", {"query": "q"}))
    assert out["count"] == 1 and out["results"][0]["content"] == "x"


# ---------- 技能层细节：section 归一化 + materialize ----------


def test_skill_get_normalizes_section():
    """section 传骨架原样行（带 # 前缀）也要命中服务端契约（纯标题）。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_skill_get", {"name": "demo-skill", "section": "## 前置条件"})
    assert fake.calls[0]["params"] == {"section": "前置条件"}
    fake.calls.clear()
    p.handle_tool_call("sgme_skill_get", {"name": "demo-skill", "section": "  前置条件  "})
    assert fake.calls[0]["params"] == {"section": "前置条件"}
    fake.calls.clear()
    p.handle_tool_call("sgme_skill_get", {"name": "demo-skill"})
    assert "params" not in fake.calls[0], "无 section 时不应传 params"


def test_skill_materialize_requires_dest_dir_and_passes_it():
    """skill_materialize 需要 dest_dir，返回服务端 path + sha256。"""
    p, fake = _tool_provider()
    err = json.loads(p.handle_tool_call("sgme_skill_materialize", {"name": "demo-skill"}))
    assert "error" in err and not fake.calls, "缺 dest_dir 应结构化报错且不发请求"

    p2, fake2 = _tool_provider(_RecordingResponse(200, {"path": "/srv/skills/demo-skill/SKILL.md", "sha256": "ab" * 32}))
    out = json.loads(p2.handle_tool_call("sgme_skill_materialize", {"name": "demo-skill", "dest_dir": "/srv/skills"}))
    assert fake2.calls[0]["json"] == {"dest_dir": "/srv/skills"}
    assert out["path"].endswith("SKILL.md") and len(out["sha256"]) == 64


def test_hard_delete_and_flags_pass_as_query():
    """skill_delete 的 hard/force 走 query 参数（服务端读 Query 而非 body）。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_skill_delete", {"name": "demo-skill", "hard": True, "force": "true"})
    assert fake.calls[0]["params"] == {"hard": "true", "force": "true"}
    fake.calls.clear()
    p.handle_tool_call("sgme_skill_delete", {"name": "demo-skill"})
    assert fake.calls[0].get("params") is None


# ---------- 写侧护栏与参数校验 ----------


def test_config_update_requires_section_and_values():
    """config_update 缺 section/values 时结构化报错，不发请求（防整段误覆盖）。"""
    p, fake = _tool_provider()
    assert "error" in json.loads(p.handle_tool_call("sgme_config_update", {"values": {"a": 1}}))
    assert "error" in json.loads(p.handle_tool_call("sgme_config_update", {"section": "refine"}))
    assert not fake.calls
    p.handle_tool_call("sgme_config_update", {"section": "refine", "values": {"enabled": True}})
    assert fake.calls[0]["json"] == {"section": "refine", "values": {"enabled": True}}


def test_wiki_page_update_defaults_append_true():
    """wiki_page_update 默认 append=true；显式 false 透传。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_wiki_page_update", {"page_id": "page-1", "content": "踩坑"})
    assert fake.calls[0]["json"]["append"] is True
    assert fake.calls[0]["json"]["author"] == "hermes", "写侧应自报 agent_id 溯源"
    fake.calls.clear()
    p.handle_tool_call("sgme_wiki_page_update", {"page_id": "page-1", "content": "重写", "append": False})
    assert fake.calls[0]["json"]["append"] is False


def test_wiki_page_add_normalizes_tags():
    """tags 逗号分隔字符串 → 字符串数组。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_wiki_page_add", {"title": "t", "content": "c", "tags": "sgme, 运维 ,踩坑"})
    assert fake.calls[0]["json"]["tags"] == ["sgme", "运维", "踩坑"]


def test_memory_reject_reason_default():
    """memory_reject 无理由时用默认文案（幂等更新理由）。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_memory_reject", {"memory_id": "mem-1"})
    assert fake.calls[0]["json"]["reason"] == "用户纠错"


def test_signal_clear_query_params():
    """signal_clear 的 type/subscriber_id 走 query。"""
    p, fake = _tool_provider()
    p.handle_tool_call("sgme_signal_clear", {"signal_type": "care_daily", "subscriber_id": "hermes"})
    assert fake.calls[0]["params"] == {"type": "care_daily", "subscriber_id": "hermes"}


# ---------- 错误路径：Gateway 不可达 → 结构化错误（绝不抛异常） ----------


def test_gateway_unreachable_returns_structured_error_for_all_tools():
    """全部 40 个工具在 Gateway 不可达时都返回 {"error": ...}，不抛异常。"""
    p, _ = _tool_provider(raise_error=True)
    for tool in sorted(_CALL_ARGS):
        try:
            raw = p.handle_tool_call(tool, dict(_CALL_ARGS[tool]))
        except Exception as e:  # pragma: no cover - 断言失败路径
            pytest.fail(f"{tool} 抛异常而非返回结构化错误: {e}")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict) and "error" in parsed, f"{tool} 未返回结构化错误: {raw!r}"
        assert isinstance(parsed["error"], str) and parsed["error"].strip(), f"{tool} 错误信息为空"


def test_http_error_status_returns_structured_error():
    """非 2xx 响应 → 结构化错误（含状态码），不抛异常。"""
    p, _ = _tool_provider(_RecordingResponse(403, {"error": "forbidden"}, text="ERR_FORBIDDEN"))
    parsed = json.loads(p.handle_tool_call("sgme_stats", {}))
    assert "error" in parsed and "403" in parsed["error"]


def test_tool_layer_exception_does_not_bubble(monkeypatch):
    """处理器内部异常被兜底捕获（工具层异常不得冒泡到 Hermes 主流程）。"""
    p, _ = _tool_provider()

    def _boom(_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(p, "_t_stats", _boom)
    parsed = json.loads(p.handle_tool_call("sgme_stats", {}))
    assert "error" in parsed and "boom" in parsed["error"]


def test_httpx_missing_returns_structured_error(monkeypatch):
    """httpx 缺失时工具返回结构化错误而不是崩溃。"""
    p, _ = _tool_provider()
    monkeypatch.setattr(hermes_mod, "_HAS_HTTPX", False)
    parsed = json.loads(p.handle_tool_call("sgme_memory_search", {"query": "q"}))
    assert "error" in parsed and "httpx" in parsed["error"]


# ---------- install.py：服务发现清单 install.json ----------


def test_plugin_yaml_version_follows_sgme_version():
    """version 口径归一：plugin.yaml 版本 == SGME 版本（动态比对，不锁死具体值）。"""
    import sgme

    text = (REPO_ROOT / "adapters" / "hermes" / "plugin.yaml").read_text(encoding="utf-8")
    matched = re.search(r'^version:\s*"?([^"\s]+)"?', text, re.M)
    assert matched, "plugin.yaml 缺 version 字段"
    version = matched.group(1)
    assert version == sgme.__version__, f"plugin.yaml 版本 {version} 应跟随 SGME {sgme.__version__}"


def test_install_json_written_without_plaintext_key(tmp_path, monkeypatch):
    """install.py 写 ~/.sgme/install.json：地址端口 + Key 环境变量名引用，不含明文密钥。"""
    from adapters.hermes import install as hermes_install

    monkeypatch.setenv("SGME_BASE_URL", "http://10.0.0.9:9910")
    monkeypatch.setenv("SGME_MCP_PORT", "9999")
    monkeypatch.setenv("SGME_AGENT_KEY", "agt_secret_value_should_not_leak")
    monkeypatch.setenv("SGME_ADMIN_KEY", "adm_secret_value_should_not_leak")
    target = tmp_path / "sgme" / "install.json"

    written = hermes_install.write_install_json(target)

    assert written == target and target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["adapter"] == "hermes"
    assert data["base_url"] == "http://10.0.0.9:9910"
    assert data["http"] == {"host": "10.0.0.9", "port": 9910}
    assert data["mcp"]["port"] == 9999
    assert data["keys"] == {
        "admin": "SGME_ADMIN_KEY",
        "agent": "SGME_AGENT_KEY",
        "bearer": "SGME_BEARER_TOKEN",
    }
    assert data["agent_id"] == "hermes"
    # 铁律：清单里不得出现任何明文密钥
    raw = target.read_text(encoding="utf-8")
    assert "secret_value_should_not_leak" not in raw
    assert all(v.startswith("SGME_") for v in data["keys"].values())


def test_install_json_defaults_to_loopback(tmp_path, monkeypatch):
    """无 SGME_BASE_URL 时回环默认，不写内网地址。"""
    from adapters.hermes import install as hermes_install

    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    target = tmp_path / "install.json"
    hermes_install.write_install_json(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["http"] == {"host": "127.0.0.1", "port": 9910}
    assert data["base_url"] == "http://127.0.0.1:9910"


def test_install_copies_plugin_files(tmp_path):
    """install() 幂等部署插件副本到 $HERMES_HOME/plugins/sgme/（不碰真实 Hermes 目录）。"""
    from adapters.hermes import install as hermes_install

    dest = hermes_install.install(tmp_path)
    assert dest == tmp_path / "plugins" / "sgme"
    for name in ("__init__.py", "plugin.yaml"):
        assert (dest / name).exists(), f"缺失 {name}"
    hermes_install.install(tmp_path)  # 二次部署幂等
    assert (dest / "plugin.yaml").exists()


def test_install_json_env_override(tmp_path, monkeypatch):
    """SGME_INSTALL_JSON 可覆盖清单落点（便于测试与非标准环境）。"""
    from adapters.hermes import install as hermes_install

    target = tmp_path / "custom" / "install.json"
    monkeypatch.setenv("SGME_INSTALL_JSON", str(target))
    assert hermes_install.install_json_path() == target
    assert hermes_install.write_install_json() == target
    assert target.exists()
