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
- T-211 配置面板：11 字段 schema / save_config（sgme/config.json + 角色同步·取消·自定义角色卡）/ initialize 应用本地配置 / is_available 判定
- T-211 角色系统：角色提示词注入（会话缓存）+ sgme_role_save / sgme_role_delete 工具
- T-214 声明式面板：config_schema.py（桌面渲染数据源）/ 双路径本地配置（sgme/config.json 优先）/ 角色本机优先解析（惰性自定义角色）

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

    def __init__(self, response: _Response | None = None, raise_error: bool = False,
                 responses: List[_Response] | None = None):
        self.calls: List[Dict[str, Any]] = []
        self.closed = False
        self.close_calls = 0
        self._response = response or _Response()
        self._raise = raise_error
        self._responses: List[_Response] = list(responses or [])

    @property
    def is_closed(self) -> bool:
        return self.closed

    def _record(self, method: str, url: str, **kwargs):
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._raise:
            raise ConnectionError("Connection refused（Gateway 未启动）")
        if self._responses:
            return self._responses.pop(0)
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


# ---------- T-211：配置面板（schema / save_config / initialize / is_available） ----------


def _resp(body: Any, status: int = 200) -> _Response:
    return _Response(status_code=status, json_body=body)


def test_config_schema_shape_and_role_choices():
    """配置面板 schema：11 字段 + 类型/密钥标记；角色下拉实时拉取服务端角色。"""
    import adapters.hermes as hermes_mod

    hermes_mod._role_board_cache_reset()
    fake = _Client(responses=[
        _resp({"roles": [{"role_id": "butler", "name": "管家"},
                         {"role_id": "companion", "name": "伴侣"}], "total": 2}),
        _resp({"role_id": "butler"}),
    ])
    p, _ = _provider(fake)

    schema = p.get_config_schema()
    assert [f["key"] for f in schema] == [
        "base_url", "agent_key", "admin_key", "inject_mode", "inject_max_tokens",
        "capture_enabled", "refine_on_end", "agent_id", "role_id",
        "custom_role_name", "custom_role_prompt",
    ]
    by = {f["key"]: f for f in schema}
    assert by["agent_key"]["secret"] is True and by["agent_key"]["env_var"] == "SGME_AGENT_KEY"
    assert by["admin_key"]["secret"] is True
    assert by["inject_mode"]["choices"] == ["daily", "coding", "work", "full"]
    assert by["inject_max_tokens"]["type"] == "integer"
    assert by["capture_enabled"]["type"] == "boolean"
    role = by["role_id"]
    assert role["choices"][0] == "（不使用角色）"
    assert "butler" in role["choices"] and "companion" in role["choices"]
    assert role["default"] == "butler"

    calls = len(fake.calls)  # 缓存：二次调用不再触网
    p.get_config_schema()
    assert len(fake.calls) == calls


def test_config_schema_role_fallback_on_gateway_error():
    """网关不可达 → 角色下拉退化为「不使用角色」单项且不抛异常。"""
    import adapters.hermes as hermes_mod

    hermes_mod._role_board_cache_reset()
    p, _ = _provider(_Client(raise_error=True))
    schema = p.get_config_schema()
    role = [f for f in schema if f["key"] == "role_id"][0]
    assert role["choices"] == ["（不使用角色）"]
    assert role["default"] == "（不使用角色）"


def test_save_config_writes_json_and_skips_secrets(tmp_path):
    """保存：非密钥值 → sgme/config.json（密钥与 role_id 不落本地）。"""
    p, fake = _provider(_Client(_resp({"role_id": None})))
    p.save_config({
        "base_url": "http://10.0.0.9:9910",
        "inject_mode": "coding",
        "inject_max_tokens": 1200,
        "capture_enabled": False,
        "refine_on_end": True,
        "agent_id": "hermes-x",
        "role_id": "（不使用角色）",
        "custom_role_name": "",
        "custom_role_prompt": "",
        "agent_key": "should-not-be-persisted",
        "admin_key": "should-not-be-persisted",
    }, str(tmp_path))

    data = json.loads((tmp_path / "sgme" / "config.json").read_text(encoding="utf-8"))
    assert data["base_url"] == "http://10.0.0.9:9910"
    assert data["inject_mode"] == "coding"
    assert data["inject_max_tokens"] == 1200
    assert data["capture_enabled"] is False
    assert data["refine_on_end"] is True
    assert data["agent_id"] == "hermes-x"
    assert "agent_key" not in data and "admin_key" not in data
    assert "role_id" not in data
    assert not [c for c in fake.calls if c["method"] == "PUT"], "空态+服务端无角色 → 不发动作"


def test_save_config_syncs_selected_role(tmp_path):
    """选择具体角色并保存 → PUT 服务端「当前角色」。"""
    p, fake = _provider(_Client(responses=[
        _resp({"role_id": None}),
        _resp({"role_id": "companion", "status": "active"}),
    ]))
    p.save_config({"role_id": "companion"}, str(tmp_path))
    puts = [c for c in fake.calls if c["method"] == "PUT"]
    assert len(puts) == 1
    assert puts[0]["url"].endswith("/v1/admin/care/active-role")
    assert puts[0]["json"] == {"role_id": "companion"}


def test_save_config_clears_role_with_sentinel(tmp_path):
    """选「不使用角色」且服务端有角色 → PUT 空串（取消当前角色）。"""
    p, fake = _provider(_Client(responses=[
        _resp({"role_id": "butler"}),
        _resp({"role_id": None, "status": "cleared"}),
    ]))
    p.save_config({"role_id": "（不使用角色）"}, str(tmp_path))
    puts = [c for c in fake.calls if c["method"] == "PUT"]
    assert len(puts) == 1 and puts[0]["json"] == {"role_id": ""}


def test_save_config_role_noop_when_same(tmp_path):
    """提交值与服务端当前一致 → 不发 PUT（避免每次保存空转）。"""
    p, fake = _provider(_Client(responses=[_resp({"role_id": "butler"})]))
    p.save_config({"role_id": "butler"}, str(tmp_path))
    assert not [c for c in fake.calls if c["method"] == "PUT"]


def test_save_config_custom_role_upsert_and_activate(tmp_path):
    """自定义角色提示词：upsert 角色卡 → 自动启用为当前角色。"""
    p, fake = _provider(_Client(responses=[
        _Response(status_code=404, json_body={}, text="角色不存在"),
        _resp({"role_id": "my-secretary", "status": "saved"}),
        _resp({"role_id": None}),
        _resp({"role_id": "my-secretary", "status": "active"}),
    ]))
    p.save_config({
        "custom_role_name": "My Secretary",
        "custom_role_prompt": "你是{{user}}的秘书，先结论后细节。",
        "role_id": "（不使用角色）",
    }, str(tmp_path))

    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"].endswith("/v1/admin/roles/my-secretary")
    assert posts[0]["json"]["data"]["system_prompt"].startswith("你是{{user}}的秘书")
    puts = [c for c in fake.calls if c["method"] == "PUT"]
    assert puts and puts[0]["json"] == {"role_id": "my-secretary"}


def test_save_config_role_failure_raises_and_json_untouched(tmp_path):
    """角色同步失败 → 抛错且不写本地 json（失败即报，不脏数据）。"""
    p, _ = _provider(_Client(responses=[
        _resp({"role_id": None}),
        _Response(status_code=500, json_body={}, text="boom"),
    ]))
    with pytest.raises(ValueError):
        p.save_config({"role_id": "butler", "inject_mode": "coding"}, str(tmp_path))
    assert not (tmp_path / "sgme" / "config.json").exists()


def test_initialize_applies_local_config(monkeypatch, tmp_path):
    """会话初始化应用旧路径 sgme.json（兼容读取；开新会话生效）。"""
    (tmp_path / "sgme.json").write_text(json.dumps({
        "base_url": "http://10.0.0.9:9910",
        "inject_mode": "coding",
        "inject_max_tokens": 1500,
        "capture_enabled": False,
        "refine_on_end": False,
        "agent_id": "hermes-x",
    }), encoding="utf-8")
    p, _ = _provider(monkeypatch=monkeypatch)
    p.initialize("sess-t211", hermes_home=str(tmp_path))
    assert p.base_url == "http://10.0.0.9:9910"
    assert p.inject_mode == "coding"
    assert p.inject_max_tokens == 1500
    assert p.capture_enabled is False
    assert p.refine_on_end is False
    assert p.agent_id == "hermes-x"


def test_initialize_non_primary_disables_capture(monkeypatch, tmp_path):
    """cron/subagent 上下文禁用写入（即使配置开启）。"""
    (tmp_path / "sgme.json").write_text(json.dumps({"capture_enabled": True}), encoding="utf-8")
    p, _ = _provider(monkeypatch=monkeypatch)
    p.initialize("sess-x", hermes_home=str(tmp_path), agent_context="cron")
    assert p.capture_enabled is False


def test_is_available_gates_fallback_key_on_remote():
    """兜底开发 key + 非回环 → 不可用；回环或真 key → 可用。"""
    from adapters.hermes import _DEV_AGENT_KEY

    assert SGMEProvider(base_url="http://10.0.0.5:9910", agent_key=_DEV_AGENT_KEY).is_available() is False
    assert SGMEProvider(base_url="http://127.0.0.1:9910", agent_key=_DEV_AGENT_KEY).is_available() is True
    assert SGMEProvider(base_url="http://localhost:9910", agent_key=_DEV_AGENT_KEY).is_available() is True
    assert SGMEProvider(base_url="http://10.0.0.5:9910", agent_key=AGENT_KEY).is_available() is True


# ---------- T-211：角色注入与新工具 ----------


def test_system_prompt_block_injects_role_and_profile():
    """角色提示词 + 画像同块注入；角色块按会话缓存（不重复拉取）。"""
    p, fake = _provider(_Client(responses=[
        _resp({"role_id": "butler"}),
        _resp({"role_id": "butler", "role_name": "管家",
               "system_prompt": "你是{{user}}的管家，说话简洁。",
               "persona": None, "profile_blocks": [], "care_policy": None}),
        _resp({"blocks": [{"title": "近期", "items": [{"content": "用户在搞 SGME"}]}]}),
        _resp({"blocks": [{"title": "近期", "items": [{"content": "用户在搞 SGME"}]}]}),
    ]))
    block = p.system_prompt_block()
    assert "沟通角色：管家" in block
    assert "说话简洁" in block
    assert "用户画像" in block and "SGME" in block
    assert len(fake.calls) == 3

    again = p.system_prompt_block()
    assert "沟通角色：管家" in again
    assert len(fake.calls) == 4  # 角色块命中缓存，仅重拉画像


def test_system_prompt_block_no_role_only_profile():
    """未设置角色 → 仅画像（与旧版行为一致）。"""
    p, _ = _provider(_Client(responses=[
        _resp({"role_id": None}),
        _resp({"blocks": [{"title": "近期", "items": [{"content": "x"}]}]}),
    ]))
    block = p.system_prompt_block()
    assert "沟通角色" not in block
    assert "用户画像" in block


def test_role_save_tool_posts_card():
    """sgme_role_save：既有卡不存在 → POST 新卡（提示词透传）。"""
    p, fake = _provider(_Client(responses=[
        _Response(status_code=404, json_body={}, text="角色不存在"),
        _resp({"role_id": "coach", "status": "saved"}),
    ]))
    out = json.loads(p.handle_tool_call("sgme_role_save", {
        "role_id": "coach", "name": "教练", "system_prompt": "你是教练，先问目标再给建议。",
    }))
    assert "error" not in out and out.get("role_id") == "coach"
    post = [c for c in fake.calls if c["method"] == "POST"][0]
    assert post["url"].endswith("/v1/admin/roles/coach")
    assert post["json"]["data"]["name"] == "教练"


def test_role_delete_tool_archives():
    """sgme_role_delete：DELETE 归档（原件永不删由服务端保证）。"""
    p, fake = _provider(_Client(_resp({"role_id": "coach", "status": "archived"})))
    out = json.loads(p.handle_tool_call("sgme_role_delete", {"role_id": "coach"}))
    assert out.get("status") == "archived"
    assert fake.calls[0]["method"] == "DELETE"
    assert fake.calls[0]["url"].endswith("/v1/admin/roles/coach")


def test_new_role_tools_in_schema_and_dispatch():
    """新工具入列 schema 且可经统一派发触达。"""
    p, _ = _provider(_Client(_resp({"role_id": "custom", "status": "saved"})))
    names = {s["name"] for s in p.get_tool_schemas()}
    assert {"sgme_role_save", "sgme_role_delete"} <= names
    out = json.loads(p.handle_tool_call("sgme_role_save", {"system_prompt": "x"}))
    assert "error" not in out  # 派发到 _t_role_save（role_id 回退 custom）

# ---------- T-214：声明式面板（桌面「设置 → 记忆与上下文 → 持久记忆」数据源） ----------


def _load_declared_schema():
    """加载 adapters/hermes/config_schema.py（Hermes 框架按路径加载的同款方式）。

    优先用开发者机器上的真实框架模块；无框架环境（本仓 venv）退回最小 shim——
    shim 与 plugins/memory/config_schema.py 的数据类逐字段对齐，仅供自测。
    """
    import importlib.util
    import types

    try:
        import plugins.memory.config_schema  # noqa: F401  （真实框架在场时直接可用）
    except Exception:
        pkg = sys.modules.setdefault("plugins", types.ModuleType("plugins"))
        pkg.__path__ = []
        mem = sys.modules.setdefault("plugins.memory", types.ModuleType("plugins.memory"))
        mem.__path__ = []
        shim = types.ModuleType("plugins.memory.config_schema")
        from dataclasses import dataclass, field as dc_field

        shim.KIND_TEXT = "text"
        shim.KIND_SELECT = "select"
        shim.KIND_SECRET = "secret"
        shim.KIND_BOOL = "bool"
        shim.KIND_NUMBER = "number"
        shim.KIND_JSON = "json"

        @dataclass(frozen=True)
        class ProviderFieldOption:
            value: str
            label: str
            description: str = ""

        @dataclass(frozen=True)
        class ProviderField:
            key: str
            label: str
            kind: str = "text"
            default: str = ""
            description: str = ""
            placeholder: str = ""
            options: tuple = ()
            env_key: Any = None
            aliases: tuple = ()
            env_fallbacks: tuple = ()
            inline: bool = False
            group: str = ""
            info: str = ""
            scope: str = "host"

            @property
            def is_secret(self) -> bool:
                return self.kind == "secret"

            def allowed_values(self):
                return {o.value for o in self.options}

        @dataclass(frozen=True)
        class ProviderConfigSchema:
            name: str
            label: str
            storage: str = "flat_json"
            docs_url: str = ""
            fields: tuple = dc_field(default_factory=tuple)

        shim.ProviderFieldOption = ProviderFieldOption
        shim.ProviderField = ProviderField
        shim.ProviderConfigSchema = ProviderConfigSchema
        sys.modules["plugins.memory.config_schema"] = shim

    path = REPO_ROOT / "adapters" / "hermes" / "config_schema.py"
    spec = importlib.util.spec_from_file_location("_sgme_test_declared_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CONFIG_SCHEMA


def test_declared_schema_shape():
    """T-214：config_schema.py 的 CONFIG_SCHEMA——11 字段 / 密钥声明 / 角色选项 / inline 子集。"""
    schema = _load_declared_schema()
    assert schema.name == "sgme"
    keys = [f.key for f in schema.fields]
    assert keys == [
        "base_url", "agent_key", "admin_key", "inject_mode", "inject_max_tokens",
        "capture_enabled", "refine_on_end", "agent_id", "role_id",
        "custom_role_name", "custom_role_prompt",
    ]
    by = {f.key: f for f in schema.fields}
    assert by["agent_key"].is_secret and by["agent_key"].env_key == "SGME_AGENT_KEY"
    assert by["admin_key"].is_secret and by["admin_key"].env_key == "SGME_ADMIN_KEY"
    assert by["inject_mode"].kind == "select"
    assert by["inject_mode"].default in by["inject_mode"].allowed_values()
    opts = [o.value for o in by["role_id"].options]
    assert opts[0] == "（不使用角色）"
    assert {"butler", "companion", "friend", "mentor"} <= set(opts)
    assert by["role_id"].default in by["role_id"].allowed_values()
    assert by["role_id"].inline and not by["custom_role_prompt"].inline


def test_declared_schema_keys_match_method():
    """T-214：声明式 schema（桌面路径）与 get_config_schema()（向导路径）字段键一一对应。"""
    import adapters.hermes as hermes_mod

    hermes_mod._role_board_cache_reset()
    p, _ = _provider(_Client(responses=[
        _resp({"roles": [{"role_id": "butler", "name": "管家"}], "total": 1}),
        _resp({"role_id": "butler"}),
    ]))
    declared = [f.key for f in _load_declared_schema().fields]
    assert declared == [f["key"] for f in p.get_config_schema()]


def test_local_config_declared_path_overrides_legacy(tmp_path):
    """T-214：双路径合并——sgme/config.json（桌面面板）优先于旧 sgme.json。"""
    import adapters.hermes as hermes_mod

    (tmp_path / "sgme.json").write_text(json.dumps({
        "inject_mode": "work", "agent_id": "legacy-id",
    }), encoding="utf-8")
    (tmp_path / "sgme").mkdir()
    (tmp_path / "sgme" / "config.json").write_text(json.dumps({
        "inject_mode": "coding", "base_url": "http://10.0.0.8:9910",
    }), encoding="utf-8")
    merged = hermes_mod._read_local_config(str(tmp_path))
    assert merged["inject_mode"] == "coding"      # 新路径优先
    assert merged["agent_id"] == "legacy-id"      # 旧路径兜底
    assert merged["base_url"] == "http://10.0.0.8:9910"


def test_initialize_applies_declared_config(tmp_path, monkeypatch):
    """T-214：会话初始化应用 sgme/config.json（桌面面板写入点）。"""
    (tmp_path / "sgme").mkdir()
    (tmp_path / "sgme" / "config.json").write_text(json.dumps({
        "base_url": "http://10.0.0.9:9910",
        "inject_mode": "coding",
        "inject_max_tokens": 1500,
        "capture_enabled": False,
        "refine_on_end": False,
        "agent_id": "hermes-x",
    }), encoding="utf-8")
    p, _ = _provider(monkeypatch=monkeypatch)
    p.initialize("sess-t214", hermes_home=str(tmp_path))
    assert p.base_url == "http://10.0.0.9:9910"
    assert p.inject_mode == "coding"
    assert p.inject_max_tokens == 1500
    assert p.capture_enabled is False
    assert p.refine_on_end is False
    assert p.agent_id == "hermes-x"


def test_role_block_local_sentinel_skips_server(tmp_path):
    """T-214：本机显式「不使用角色」→ 不回退服务端当前角色（也不触网）。"""
    p, fake = _provider(_Client(responses=[_resp({"role_id": "butler"})]))
    (tmp_path / "sgme").mkdir()
    (tmp_path / "sgme" / "config.json").write_text(
        json.dumps({"role_id": "（不使用角色）"}), encoding="utf-8")
    p._hermes_home = str(tmp_path)
    assert p._role_prompt_block() == ""
    assert not fake.calls


def test_role_block_local_role_wins_over_server(tmp_path):
    """T-214：本机 role_id 已设置 → 直接采用（不查服务端当前角色）。"""
    p, fake = _provider(_Client(responses=[
        _resp({"role_id": "companion", "role_name": "伴侣", "system_prompt": "你是伴侣。"}),
    ]))
    (tmp_path / "sgme").mkdir()
    (tmp_path / "sgme" / "config.json").write_text(
        json.dumps({"role_id": "companion"}), encoding="utf-8")
    p._hermes_home = str(tmp_path)
    block = p._role_prompt_block()
    assert "沟通角色：伴侣" in block and "你是伴侣。" in block
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/v1/admin/roles/companion/assemble")


def test_role_block_custom_prompt_lazy_upsert(tmp_path):
    """T-214：自定义提示词 → 惰性 upsert 角色卡（内容变化才写）+ 采用；二次调用走缓存。"""
    p, fake = _provider(_Client(responses=[
        _Response(status_code=404, json_body={}, text="角色不存在"),
        _resp({"role_id": "my-secretary", "status": "saved"}),
        _resp({"role_id": "my-secretary", "role_name": "My Secretary", "system_prompt": "你是秘书。"}),
    ]))
    (tmp_path / "sgme").mkdir()
    (tmp_path / "sgme" / "config.json").write_text(json.dumps({
        "custom_role_name": "My Secretary",
        "custom_role_prompt": "你是秘书。",
    }), encoding="utf-8")
    p._hermes_home = str(tmp_path)
    block = p._role_prompt_block()
    assert "你是秘书。" in block
    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["url"].endswith("/v1/admin/roles/my-secretary")
    assert posts[0]["json"]["data"]["system_prompt"] == "你是秘书。"

    before = len(fake.calls)
    assert p._role_prompt_block() == block  # 会话缓存：不再触网
    assert len(fake.calls) == before

