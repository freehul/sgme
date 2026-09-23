# -*- coding: utf-8 -*-
"""SGME × Hermes 适配插件自测（mock 网络层，零网络、可离线跑）。

G-2（2026-09-24 深度审查观察项）补齐：hermes 适配器此前无自带测试目录
（doubao/mimo/workbuddy 有 client 测试、dsh 有 install/import 测试），
本文件提供**适配器目录级**自测，与仓库根 tests/test_hermes_adapter.py 互补：
根测试面向「与 SGME 服务端/引擎契约对齐」，本文件面向「插件自身关键路径」。

覆盖：
- append 增量导出：首行 `# {ts} {role}` 格式、session_key/agent_id/started_at 契约
- tool 消息去重（tool_call_id 命中 / 内容指纹兜底）与「每轮 started_at 不同」追加语义
- on_session_end：补最后增量 + 触发异步提炼
- prefetch（inject 面）：scopes=[memory, wiki]、记忆/场景分块渲染
- 工具面：数量与 schema 形状、未知工具结构化错误、网关不可达不抛异常
- install.py：插件幂等部署、install.json 三键只写环境变量名、回环默认
- B152：client 关闭后自动重建（防「Cannot send a request, as the client has been closed」）

运行：
  python -m pytest adapters/hermes/tests -q
或（SGME 项目 venv）：
  .venv\\Scripts\\python.exe -m pytest adapters/hermes/tests -q

测试样例全部使用假值：私网语义用 10.0.0.x，密钥用低熵占位/拼接写法
（形如 `agt_<16+位>` 的字面量会被 .githooks 密钥扫描误拦）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.hermes import SGMEProvider  # noqa: E402
from adapters.hermes import install as hermes_install  # noqa: E402

# 低熵占位密钥（非真实）；拼接写法避免「agt_ + 长字母数字串」触发密钥门禁
AGENT_KEY = "agt_" + "sample_key_for_tests_only"
ADMIN_KEY = "adm_" + "sample_key_for_tests_only"
BASE_URL = "http://127.0.0.1:9910"


# ---------- 最小网络层 stub ----------


class _Response:
    """最小响应 stub。"""

    def __init__(self, status_code: int = 200, json_body: Any = None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._json_body


class _Client:
    """记录全部 HTTP 调用；closed 后按 httpx 语义抛 RuntimeError。"""

    def __init__(self, response: _Response | None = None, raise_error: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self.closed = False
        self.close_calls = 0
        self._response = response or _Response()
        self._raise = raise_error

    @property
    def is_closed(self) -> bool:
        return self.closed

    def _record(self, method: str, url: str, **kwargs):
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
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
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1


def _provider(client: _Client | None = None, monkeypatch=None) -> tuple[SGMEProvider, _Client]:
    """构造注入了 stub 的 provider（探活命中缓存，不触网）。"""
    fake = client or _Client()
    p = SGMEProvider(base_url=BASE_URL, agent_key=AGENT_KEY, admin_key=ADMIN_KEY)
    p._client = fake  # type: ignore[assignment]
    p._available = True
    p._probe_at = time.monotonic()
    p._session_key = "hermes-test-session"
    p._session_id = "hermes-test-session"
    if monkeypatch is not None:
        monkeypatch.setattr(p, "_probe", lambda: True)
    return p, fake


def _wait_posts(fake: _Client, n: int, timeout: float = 5.0) -> None:
    """等待后台线程完成 n 次 HTTP 调用。"""
    deadline = timeout
    while deadline > 0:
        if len(fake.calls) >= n:
            return
        threading.Event().wait(0.05)
        deadline -= 0.05
    pytest.fail(f"超时：期望 {n} 次调用，实际 {len(fake.calls)}")


def _append_bodies(fake: _Client) -> List[Dict[str, Any]]:
    return [c["json"] for c in fake.calls if c["url"].endswith("/v1/append")]


# ---------- append 增量导出 ----------


def test_append_delta_contract(monkeypatch):
    """append body 契约：session_key / agent_id / started_at / content 首行 `# {ts} {role}`。"""
    p, fake = _provider(monkeypatch=monkeypatch)
    p.sync_turn("问题", "回答", messages=[
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ])
    _wait_posts(fake, 1)

    body = _append_bodies(fake)[0]
    assert body["session_key"] == "hermes-test-session"
    assert body["agent_id"] == "hermes"
    assert body["started_at"], "append 必须带 started_at（引擎幂等键之一）"
    first = body["content"].splitlines()[0]
    assert first.startswith("# ") and first.endswith(" user"), f"首行格式不符：{first!r}"


def test_tool_dedup_by_call_id(monkeypatch):
    """同 tool_call_id 的重复 tool 消息只导一次。"""
    p, fake = _provider(monkeypatch=monkeypatch)
    p.sync_turn("q", "a", messages=[
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": '{"r": 1}', "tool_call_id": "call-1"},
        {"role": "tool", "content": '{"r": 1}', "tool_call_id": "call-1"},
    ])
    _wait_posts(fake, 1)
    assert _append_bodies(fake)[0]["content"].count("# ") == 3


def test_tool_dedup_by_content_fingerprint(monkeypatch):
    """无 tool_call_id 时按内容指纹去重。"""
    p, fake = _provider(monkeypatch=monkeypatch)
    p.sync_turn("q", "a", messages=[
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": '{"same": true}'},
        {"role": "tool", "content": '{"same": true}'},
    ])
    _wait_posts(fake, 1)
    assert _append_bodies(fake)[0]["content"].count("# ") == 3


def test_started_at_differs_per_turn(monkeypatch):
    """每轮 started_at 取导出时刻（相同则引擎按幂等丢弃后续轮次）。"""
    p, fake = _provider(monkeypatch=monkeypatch)
    turn1 = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    p.sync_turn("q1", "a1", messages=turn1)
    _wait_posts(fake, 1)
    turn2 = turn1 + [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]
    p.sync_turn("q2", "a2", messages=turn2)
    _wait_posts(fake, 2)

    bodies = _append_bodies(fake)
    assert len(bodies) == 2
    assert bodies[0]["started_at"] != bodies[1]["started_at"]
    assert bodies[1]["content"].count("# ") == 2, "第二轮只导新增块"
    assert "q1" not in bodies[1]["content"]


def test_on_session_end_flush_and_refine(monkeypatch):
    """会话结束：补最后未导出增量 + 触发异步提炼。"""
    p, fake = _provider(monkeypatch=monkeypatch)
    turn = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    p.sync_turn("q", "a", messages=turn)
    _wait_posts(fake, 1)

    p.on_session_end(turn + [
        {"role": "user", "content": "收尾补充"},
        {"role": "assistant", "content": "收到"},
    ])
    _wait_posts(fake, 2)

    assert "收尾补充" in _append_bodies(fake)[-1]["content"]
    assert any("refine" in c["url"] for c in fake.calls), "会话结束应触发提炼"


# ---------- prefetch（inject 面）与工具面 ----------


def _prefetch_provider(monkeypatch, results: list[dict]):
    fake = _Client(response=_Response(200, {"results": results}))
    p, _ = _provider(fake, monkeypatch=monkeypatch)
    return p, fake


def test_prefetch_scopes_include_wiki_and_split_blocks(monkeypatch):
    """prefetch 走 scopes=[memory, wiki]，记忆与 L2 场景分块渲染。"""
    p, fake = _prefetch_provider(monkeypatch, [
        {"source": "memory", "content": "用户是独立开发者"},
        {"source": "wiki_scene", "title": "SGME 开发", "content": "记忆引擎架构"},
    ])

    out = p.prefetch("SGME 架构", session_id="s1")

    search = [c for c in fake.calls if c["url"].endswith("/v1/search")]
    assert len(search) == 1 and search[0]["json"]["scopes"] == ["memory", "wiki"]
    assert "# 相关记忆（SGME）" in out and "独立开发者" in out
    assert "# 相关场景（L2 匹配）" in out and "[SGME 开发]" in out


def test_prefetch_empty_results_returns_empty(monkeypatch):
    """检索无结果 → 返回空串（不产生空块噪音）。"""
    p, _ = _prefetch_provider(monkeypatch, [])
    assert p.prefetch("随便问问", session_id="s1") == ""


def test_inject_tool_hits_inject_endpoint():
    """sgme_inject 工具 → POST /v1/inject（读侧用 agent key）。"""
    p, fake = _provider()
    p.handle_tool_call("sgme_inject", {"mode": "coding"})
    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1/inject")
    assert call["headers"]["X-API-Key"] == AGENT_KEY


def test_tool_schemas_shape_and_handler_resolution(monkeypatch):
    """工具面自洽：schema 合法 + 每个工具都有处理器。"""
    p, _ = _provider()
    schemas = p.get_tool_schemas()
    assert len(schemas) >= 40, f"工具面不应少于 40（实际 {len(schemas)}）"
    assert len({s["name"] for s in schemas}) == len(schemas), "工具名重复"
    for s in schemas:
        params = s.get("parameters") or {}
        assert isinstance(s.get("description"), str) and s["description"].strip()
        assert params.get("type") == "object"
        for key in params.get("required") or []:
            assert key in (params.get("properties") or {}), f"{s['name']} required 项缺声明"


def test_unknown_tool_returns_structured_error():
    """未知工具名 → 结构化错误，不抛异常。"""
    p, fake = _provider()
    out = json.loads(p.handle_tool_call("sgme_not_exist", {}))
    assert "error" in out and not fake.calls


def test_gateway_unreachable_returns_structured_error():
    """Gateway 不可达 → 结构化错误（不冒泡到宿主）。"""
    p, _ = _provider(_Client(raise_error=True))
    out = json.loads(p.handle_tool_call("sgme_stats", {}))
    assert "error" in out and out["error"].strip()


# ---------- B152：client 生命周期 ----------


def test_client_rebuilt_after_close(monkeypatch):
    """client 被 shutdown 关闭后，后续请求自动重建 client（不复用已关闭实例）。"""
    import adapters.hermes as hermes_mod

    closed = _Client()
    closed.closed = True
    p, _ = _provider(closed, monkeypatch=monkeypatch)

    rebuilt = _Client()
    monkeypatch.setattr(hermes_mod.httpx, "Client", lambda **kwargs: rebuilt)

    p._trigger_refine()

    assert p._client is rebuilt
    assert len(rebuilt.calls) == 1 and "refine" in rebuilt.calls[0]["url"]
    assert closed.closed is True, "旧 client 应保持关闭"


# ---------- install.py ----------


def test_install_deploys_plugin_files_idempotently(tmp_path):
    """插件幂等部署到 $HERMES_HOME/plugins/sgme/（不触碰真实 HERMES_HOME）。"""
    dest = hermes_install.install(tmp_path)
    assert dest == tmp_path / "plugins" / "sgme"
    for name in ("__init__.py", "plugin.yaml"):
        assert (dest / name).exists(), f"缺失 {name}"
    hermes_install.install(tmp_path)  # 二次部署幂等
    assert (dest / "plugin.yaml").exists()


def test_install_json_has_no_plaintext_key(tmp_path, monkeypatch):
    """install.json：地址端口 + 三键**只写环境变量名**，绝不落明文密钥。"""
    monkeypatch.setenv("SGME_BASE_URL", "http://10.0.0.9:9910")
    monkeypatch.setenv("SGME_MCP_PORT", "9999")
    monkeypatch.setenv("SGME_AGENT_KEY", AGENT_KEY)
    monkeypatch.setenv("SGME_ADMIN_KEY", ADMIN_KEY)
    target = tmp_path / "install.json"

    written = hermes_install.write_install_json(target)

    assert written == target and target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["adapter"] == "hermes" and data["schema_version"] == 1
    assert data["http"] == {"host": "10.0.0.9", "port": 9910}
    assert data["mcp"]["port"] == 9999
    assert data["keys"] == {
        "admin": "SGME_ADMIN_KEY",
        "agent": "SGME_AGENT_KEY",
        "bearer": "SGME_BEARER_TOKEN",
    }
    raw = target.read_text(encoding="utf-8")
    assert "sample_key_for_tests_only" not in raw, "清单不得出现任何密钥明文"


def test_install_json_defaults_to_loopback(tmp_path, monkeypatch):
    """无 SGME_BASE_URL → 回环默认（不写内网地址）。"""
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    target = tmp_path / "install.json"
    hermes_install.write_install_json(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["base_url"] == "http://127.0.0.1:9910"
    assert data["http"] == {"host": "127.0.0.1", "port": 9910}


def test_resolve_base_url_precedence(monkeypatch):
    """地址解析：显式参数 > SGME_BASE_URL > 回环默认；尾部斜杠归一。"""
    monkeypatch.setenv("SGME_BASE_URL", "http://10.0.0.9:9910/")
    assert hermes_install.resolve_base_url() == "http://10.0.0.9:9910"
    assert hermes_install.resolve_base_url("http://10.0.0.7:9910/") == "http://10.0.0.7:9910"
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    assert hermes_install.resolve_base_url() == hermes_install.DEFAULT_BASE_URL


def test_resolve_agent_id_env_override(monkeypatch):
    """agent_id 溯源标识：SGME_HERMES_AGENT_ID 覆盖，缺省 hermes。"""
    monkeypatch.delenv("SGME_HERMES_AGENT_ID", raising=False)
    assert hermes_install.resolve_agent_id() == "hermes"
    monkeypatch.setenv("SGME_HERMES_AGENT_ID", "hermes-nas")
    assert hermes_install.resolve_agent_id() == "hermes-nas"


def test_adapter_version_follows_plugin_yaml_and_sgme():
    """版本口径：install.py 读到的 plugin.yaml 版本 == SGME 版本（动态比对，不锁值）。"""
    import sgme

    assert hermes_install.adapter_version() == sgme.__version__


def test_install_json_path_env_override(tmp_path, monkeypatch):
    """SGME_INSTALL_JSON 覆盖清单落点（测试/非标准环境用）。"""
    target = tmp_path / "custom" / "install.json"
    monkeypatch.setenv("SGME_INSTALL_JSON", str(target))
    assert hermes_install.install_json_path() == target
    assert hermes_install.write_install_json() == target
    assert target.exists()
