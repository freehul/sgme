# -*- coding: utf-8 -*-
"""SGME × WorkBuddy 适配器客户端单元测试（mock 网络层，可离线跑）。

覆盖：
- 地址解析（环境变量 → 部署配置 client.env → 身份文件 → WorkBuddy mcp.json → 回环默认）
  与 MCP 端点正/反向推导
- 密钥解析优先级（SGME_WORKBUDDY_KEY → 身份文件 → mcp.json → SGME_AGENT_KEY 兜底），
  含「通用兜底必须让位」的回归防线
- WorkBuddy mcp.json 自动发现（地址 + 密钥零配置继承）的解析与容错
- HTTP 层请求构造（append 首行格式 / 查询参数 / 头部 / 错误包装）
- 41 个 MCP 基准能力的方法层 + CLI 命令层全覆盖（缺一即失败）
- 新增/修正方法与 CLI 命令的参数转译（含管理员 Key 范围）
- 测试样例全部使用假值：私网语义用 10.0.0.x，密钥用低熵占位

运行：
  python -m unittest discover -s adapters/workbuddy/tests -v
或（SGME 项目 venv）：
  .venv\\Scripts\\python.exe -m unittest discover -s adapters/workbuddy/tests -v
或：python -m pytest adapters/workbuddy/tests -q
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sgme_client as sc  # noqa: E402
from sgme_client import (  # noqa: E402
    BASELINE_SPEC,
    BASELINE_TOOLS,
    CMD_BY_NAME,
    CLI_NAMES,
    SGME,
    SGMEError,
    derive_mcp_url,
    resolve_addresses,
)

# 测试用占位密钥（非真实）。⚠️ 用字符串拼接而不是长字面量：形如 `agt_<16+位>` 的
# 字面量会被 .githooks/lib_scan.sh 的密钥扫描判为疑似真实 key（低熵豁免对无 `-` 分隔的
# `agt_` 前缀不生效），拼接写法既保持运行时形态又不会误触发门禁。
KEY = "agt_" + "placeholder"
ADMIN_KEY = "admin_" + "placeholder"
# 私网语义测试地址（虚构，非本环境实况）
FAKE_HOST_IP = "10.0.0.9"
FAKE_HTTP = f"http://{FAKE_HOST_IP}:9910"
FAKE_MCP = f"http://{FAKE_HOST_IP}:9913/mcp"


class IdentityIsolated(unittest.TestCase):
    """默认屏蔽本机 ~/.sgme/workbuddy-agent.json 与 ~/.workbuddy/mcp.json，
    避免真实地址/密钥污染断言。"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(sc, "load_workbuddy_identity", return_value={})
        p.start()
        self.addCleanup(p.stop)
        m = mock.patch.object(sc, "load_workbuddy_mcp_json", return_value={})
        m.start()
        self.addCleanup(m.stop)


class FakeResp:
    def __init__(self, payload, status=200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        self._raw = payload.encode("utf-8")
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_client(**ctor):
    """构造客户端：屏蔽真实 SGME 环境变量、本机身份文件与 WorkBuddy MCP 配置，
    只注入占位密钥。"""
    with env_ctx(SGME_AGENT_KEY=KEY), \
            mock.patch.object(sc, "load_workbuddy_identity", return_value={}), \
            mock.patch.object(sc, "load_workbuddy_mcp_json", return_value={}):
        return SGME(**ctor)


# SGME 相关环境变量（测试需隔离的对象）
SGME_ENV_VARS = ("SGME_HTTP_URL", "SGME_BASE_URL", "SGME_MCP_URL",
                 "SGME_WORKBUDDY_KEY", "SGME_AGENT_KEY", "SGME_ADMIN_KEY")


@contextlib.contextmanager
def env_ctx(**kw):
    """临时清空 SGME 环境变量并注入给定值（**保留系统变量**——清空整个 os.environ 会
    让 Windows 上 ssl 初始化失败，故不用 mock.patch.dict(clear=True)）。"""
    saved = {k: os.environ.get(k) for k in SGME_ENV_VARS}
    for k in SGME_ENV_VARS:
        os.environ.pop(k, None)
    for k, v in kw.items():
        if v is not None:
            os.environ[k] = v
    try:
        yield
    finally:
        for k in SGME_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def req_headers(req) -> dict:
    """urllib.Request 头部转小写字典（get_header 在本环境不做大小写规范化）。"""
    return {k.lower(): v for k, v in req.header_items()}


class NoDeployConfig(unittest.TestCase):
    """基类：默认屏蔽部署配置、本机身份文件与 WorkBuddy MCP 配置，保证测试与外机环境无关。"""

    def setUp(self):
        super().setUp()
        for name, ret in (("load_deploy_config", {}),
                          ("load_workbuddy_identity", {}),
                          ("load_workbuddy_mcp_json", {})):
            patcher = mock.patch.object(sc, name, return_value=ret)
            self.addCleanup(patcher.stop)
            patcher.start()


class TestAddressResolution(NoDeployConfig):
    def test_default_loopback(self):
        with env_ctx():
            http, mcp, src = resolve_addresses()
        self.assertEqual(http, "http://127.0.0.1:9910")
        self.assertEqual(mcp, "http://127.0.0.1:9913/mcp")
        self.assertEqual(src, "回环默认")

    def test_env_http_url_wins_over_deploy_config(self):
        deploy = {"SGME_HTTP_URL": "http://10.0.0.1:9910"}
        with env_ctx(SGME_HTTP_URL=FAKE_HTTP):
            http, mcp, src = resolve_addresses(deploy_cfg=deploy)
        self.assertEqual(http, FAKE_HTTP)
        self.assertEqual(mcp, f"http://{FAKE_HOST_IP}:9913/mcp")
        self.assertIn("环境变量", src)

    def test_env_base_url_alias(self):
        with env_ctx(SGME_BASE_URL=FAKE_HTTP):
            http, _mcp, _src = resolve_addresses(deploy_cfg={})
        self.assertEqual(http, FAKE_HTTP)

    def test_deploy_config_used_when_env_absent(self):
        deploy = {"SGME_HTTP_URL": FAKE_HTTP}
        with env_ctx():
            http, mcp, src = resolve_addresses(deploy_cfg=deploy)
        self.assertEqual(http, FAKE_HTTP)
        self.assertIn("部署配置", src)
        self.assertIn("9913/mcp", mcp)

    def test_explicit_args_win(self):
        with env_ctx(SGME_HTTP_URL="http://10.0.0.2:9910"):
            http, mcp, src = resolve_addresses("http://10.0.0.3:9910", deploy_cfg={})
        self.assertEqual(http, "http://10.0.0.3:9910")
        self.assertEqual(mcp, "http://10.0.0.3:9913/mcp")
        self.assertEqual(src, "调用参数")

    def test_mcp_url_env_override(self):
        with env_ctx(SGME_HTTP_URL=FAKE_HTTP, SGME_MCP_URL="http://10.0.0.9:19999/mcp"):
            _http, mcp, _src = resolve_addresses(deploy_cfg={})
        self.assertEqual(mcp, "http://10.0.0.9:19999/mcp")

    def test_derive_mcp_url_port_delta(self):
        self.assertEqual(derive_mcp_url("http://10.0.0.9:9930"), "http://10.0.0.9:9933/mcp")

    def test_client_uses_resolved_addresses(self):
        with env_ctx(SGME_AGENT_KEY=KEY, SGME_BASE_URL=FAKE_HTTP):
            c = SGME()
        self.assertEqual(c.base_url, FAKE_HTTP)
        self.assertEqual(c.mcp_url, f"http://{FAKE_HOST_IP}:9913/mcp")

    def test_deploy_config_reaches_client(self):
        with mock.patch.object(sc, "load_deploy_config", return_value={"SGME_HTTP_URL": FAKE_HTTP}):
            with env_ctx(SGME_AGENT_KEY=KEY):
                c = SGME()
        self.assertEqual(c.base_url, FAKE_HTTP)

    def test_describe_never_reveals_key(self):
        c = make_client()
        info = c.describe()
        self.assertTrue(info["agent_key_set"])
        self.assertNotIn(KEY, json.dumps(info, ensure_ascii=False))


class TestWorkBuddyIdentity(unittest.TestCase):
    def test_load_identity_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "workbuddy-agent.json"
            f.write_text(json.dumps({
                "agent_id": "workbuddy",
                "api_key": "agt_" + "placeholder",
                "http": "http://10.0.0.9:9910",
                "mcp": "http://10.0.0.9:9913/mcp",
            }), encoding="utf-8")
            data = sc.load_workbuddy_identity(f)
            self.assertEqual(data["agent_id"], "workbuddy")
            self.assertEqual(data["http"], "http://10.0.0.9:9910")

    def test_load_identity_missing_and_invalid(self):
        self.assertEqual(sc.load_workbuddy_identity("/nonexistent/workbuddy-agent.json"), {})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "workbuddy-agent.json"
            f.write_text("not-json", encoding="utf-8")
            self.assertEqual(sc.load_workbuddy_identity(f), {})

    def test_identity_used_when_env_absent(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "workbuddy-agent.json"
            f.write_text(json.dumps({
                "api_key": "agt_" + "placeholder",
                "http": "http://10.0.0.9:9910",
                "mcp": "http://10.0.0.9:9913/mcp",
            }), encoding="utf-8")
            with mock.patch.object(sc, "load_workbuddy_identity", return_value=json.loads(f.read_text(encoding="utf-8"))), \
                    env_ctx():
                http, mcp, src = resolve_addresses()
                self.assertEqual(http, "http://10.0.0.9:9910")
                self.assertIn("workbuddy-agent.json", src)
                c = SGME()
                self.assertEqual(c.base_url, "http://10.0.0.9:9910")


class TestDeployConfigParsing(unittest.TestCase):
    """client.env 解析（不继承 NoDeployConfig——本类要测真实解析函数）。"""

    def test_load_deploy_config_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "client.env"
            f.write_text("# 注释\nSGME_HTTP_URL=" + FAKE_HTTP
                         + "\n\nSGME_MCP_URL=http://10.0.0.9:9913/mcp\n", encoding="utf-8")
            cfg = sc.load_deploy_config(f)
        self.assertEqual(cfg["SGME_HTTP_URL"], FAKE_HTTP)
        self.assertEqual(cfg["SGME_MCP_URL"], "http://10.0.0.9:9913/mcp")

    def test_load_deploy_config_missing_file(self):
        self.assertEqual(sc.load_deploy_config("/nonexistent/client.env"), {})

    def test_load_deploy_config_ignores_key_values(self):
        """部署配置只该有地址与「密钥环境变量名」——解析结果里不应出现任何密钥值形态。"""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "client.env"
            f.write_text("SGME_HTTP_URL=" + FAKE_HTTP + "\nSGME_AGENT_KEY_ENV=SGME_AGENT_KEY\n",
                         encoding="utf-8")
            cfg = sc.load_deploy_config(f)
        self.assertEqual(cfg["SGME_AGENT_KEY_ENV"], "SGME_AGENT_KEY")
        self.assertNotIn("SGME_AGENT_KEY", cfg)


class TestInit(NoDeployConfig):
    def test_key_from_env(self):
        c = make_client()
        self.assertEqual(c.key, KEY)

    def test_missing_key_raises(self):
        with env_ctx():
            with self.assertRaises(SGMEError) as ctx:
                SGME()
            self.assertIn("SGME_AGENT_KEY", str(ctx.exception))

    def test_no_proxy_opener(self):
        c = make_client()
        # 无代理：即使存在 ProxyHandler，其代理表也必须是空的（防 Clash 劫持内网）
        ph = [h for h in c._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        for h in ph:
            self.assertEqual(h.proxies, {})


class TestAppend(NoDeployConfig):
    def test_content_first_line_format(self):
        c = make_client()
        with mock.patch.object(c._opener, "open", return_value=FakeResp({"file_id": "f1", "status": "new"})) as mo:
            c.append("s1", "今天完成了接入", role="user")
            req = mo.call_args.args[0]
            self.assertEqual(req_headers(req)["x-api-key"], KEY)
            self.assertTrue(req.full_url.endswith("/v1/append"))
            body = json.loads(req.data.decode("utf-8"))
            first, _, rest = body["content"].partition("\n")
            self.assertTrue(first.startswith("# "))
            self.assertTrue(first.endswith(" user"))
            self.assertEqual(rest, "今天完成了接入")
            self.assertEqual(body["agent_id"], "workbuddy")

    def test_assistant_role(self):
        c = make_client()
        with mock.patch.object(c._opener, "open", return_value=FakeResp({"file_id": "f2"})) as mo:
            c.append("s2", "我说的话", role="assistant")
            req = mo.call_args.args[0]
            body = json.loads(req.data.decode("utf-8"))
            self.assertTrue(body["content"].split("\n")[0].endswith(" assistant"))


class TestHttpCalls(NoDeployConfig):
    def _client(self, payload):
        c = make_client()
        patcher = mock.patch.object(c._opener, "open", return_value=FakeResp(payload))
        mo = patcher.start()
        self.addCleanup(patcher.stop)
        return c, mo

    def test_health_default_loopback(self):
        c, mo = self._client({"status": "ok"})
        self.assertEqual(c.health()["status"], "ok")
        self.assertEqual(mo.call_args.args[0].full_url, "http://127.0.0.1:9910/v1/health")

    def test_health_env_endpoint(self):
        with env_ctx(SGME_AGENT_KEY=KEY, SGME_HTTP_URL=FAKE_HTTP):
            c = SGME()
            with mock.patch.object(c._opener, "open", return_value=FakeResp({"status": "ok"})) as mo:
                c.health()
        self.assertEqual(mo.call_args.args[0].full_url, FAKE_HTTP + "/v1/health")

    def test_search_body(self):
        c, mo = self._client({"results": []})
        c.search("示例检索词", limit=3, scopes=["memory", "skills"])
        req = mo.call_args.args[0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"query": "示例检索词", "limit": 3, "scopes": ["memory", "skills"]})

    def test_memory_reject_path_and_reason(self):
        c, mo = self._client({"ok": True})
        c.memory_reject("mem-1", "与事实不符")
        req = mo.call_args.args[0]
        self.assertIn("/v1/memory/mem-1/reject", req.full_url)
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"reason": "与事实不符"})

    def test_skill_get_quoted_and_section(self):
        c, mo = self._client({"name": "sgme"})
        c.skill_get("sgme")
        self.assertIn("/v1/skills/sgme", mo.call_args.args[0].full_url)
        c.skill_get("sgme", section="用法")
        self.assertIn("section=", mo.call_args.args[0].full_url)

    def test_skill_list_limit_optional(self):
        c, mo = self._client({"skills": []})
        c.skill_list()
        self.assertNotIn("limit=", mo.call_args.args[0].full_url)
        c.skill_list(offset=40, limit=20)
        url = mo.call_args.args[0].full_url
        self.assertIn("offset=40", url)
        self.assertIn("limit=20", url)

    def test_skill_materialize_returns_path_and_sha256(self):
        c, mo = self._client({"path": "/tmp/x/SKILL.md", "sha256": "0" * 64})
        r = c.skill_materialize("sgme", "/tmp/x")
        self.assertIn("sha256", r)
        self.assertIn("/materialize", mo.call_args.args[0].full_url)

    def test_events_pull_query(self):
        c, mo = self._client({"events": []})
        c.events_pull("workbuddy", limit=10)
        req = mo.call_args.args[0]
        self.assertIn("subscriber_id=workbuddy", req.full_url)
        self.assertIn("limit=10", req.full_url)
        self.assertNotIn("types=", req.full_url)

    def test_http_error_raises_sgme_error(self):
        c = make_client()
        with mock.patch.object(c._opener, "open") as mo:
            err = urllib.error.HTTPError("http://x", 403, "Forbidden", {},
                                         io.BytesIO(b'{"error":{"code":"ERR_FORBIDDEN"}}'))
            mo.side_effect = err
            with self.assertRaises(SGMEError) as ctx:
                c.health()
            self.assertIn("403", str(ctx.exception))


class TestMcpMethodSignatures(NoDeployConfig):
    """MCP 层方法 → 工具名/参数/密钥范围（对齐 sgme/mcp_server.py 基准签名）。"""

    def _client(self):
        c = make_client()
        patcher = mock.patch.object(c, "_mcp", return_value={"ok": True})
        mo = patcher.start()
        self.addCleanup(patcher.stop)
        return c, mo

    def _call(self, mo):
        return mo.call_args.args, mo.call_args.kwargs

    def test_agent_onboarding(self):
        c, mo = self._client()
        c.agent_onboarding()
        self.assertEqual(mo.call_args.args[0], "agent_onboarding")

    def test_refine_trigger_params(self):
        c, mo = self._client()
        c.refine_trigger(async_mode=True, file_id="f-1", limit=10)
        args, _kw = self._call(mo)
        self.assertEqual(args[0], "refine_trigger")
        self.assertEqual(args[1], {"async_mode": True, "limit": 10, "file_id": "f-1"})

    def test_refine_batch_params(self):
        c, mo = self._client()
        c.refine_batch(async_mode=True, file_ids=["a", "b"])
        args, _kw = self._call(mo)
        self.assertEqual(args[1], {"async_mode": True, "limit": 50, "file_ids": ["a", "b"]})

    def test_stats(self):
        c, mo = self._client()
        c.stats()
        self.assertEqual(mo.call_args.args, ("stats", {}))

    def test_config_get_section(self):
        c, mo = self._client()
        c.config_get("search")
        self.assertEqual(mo.call_args.args[1], {"section": "search"})
        c.config_get()
        self.assertEqual(mo.call_args.args[1], {})

    def test_config_update_values_scope_auto(self):
        c, mo = self._client()
        c.config_update("search", {"limit": 5})
        args, kw = self._call(mo)
        self.assertEqual(args[0], "config_update")
        self.assertEqual(args[1], {"section": "search", "values": {"limit": 5}})
        self.assertEqual(kw["scope"], "auto")

    def test_wiki_evolve_trigger(self):
        c, mo = self._client()
        c.wiki_evolve_trigger(session_key="s-1", min_rounds=3, limit=2)
        args, _kw = self._call(mo)
        self.assertEqual(args[0], "wiki_evolve_trigger")
        self.assertEqual(args[1], {"min_rounds": 3, "limit": 2, "session_key": "s-1"})

    def test_signal_pull_uses_signal_type_not_subscriber(self):
        c, mo = self._client()
        c.signal_pull(signal_type="care_daily", limit=5)
        args, _kw = self._call(mo)
        self.assertEqual(args[0], "signal_pull")
        self.assertEqual(args[1], {"limit": 5, "signal_type": "care_daily"})
        self.assertNotIn("subscriber_id", args[1])

    def test_signal_ack_params(self):
        c, mo = self._client()
        c.signal_ack("evt-1", "acked", result="已关怀")
        args, _kw = self._call(mo)
        self.assertEqual(args[1], {"event_id": "evt-1", "status": "acked", "result": "已关怀"})

    def test_signal_clear(self):
        c, mo = self._client()
        c.signal_clear(signal_type="care_daily", subscriber_id="workbuddy")
        args, kw = self._call(mo)
        self.assertEqual(args[0], "signal_clear")
        self.assertEqual(args[1], {"signal_type": "care_daily", "subscriber_id": "workbuddy"})
        self.assertEqual(kw["scope"], "auto")

    def test_role_active_get_set(self):
        c, mo = self._client()
        c.role_active_get()
        self.assertEqual(mo.call_args.args, ("role_active_get", {}))
        c.role_active_set("role-1")
        self.assertEqual(mo.call_args.args, ("role_active_set", {"role_id": "role-1"}))

    def test_role_assemble_inject_mode(self):
        c, mo = self._client()
        c.role_assemble("role-1", inject_mode="work")
        self.assertEqual(mo.call_args.args[1], {"role_id": "role-1", "inject_mode": "work"})

    def test_idea_add_matches_baseline_signature(self):
        c, mo = self._client()
        c.idea_add("一个创意", priority=2, source_ref="ref-1")
        args, _kw = self._call(mo)
        self.assertEqual(args[0], "idea_add")
        self.assertEqual(args[1], {"content": "一个创意", "priority": 2, "source_ref": "ref-1"})
        self.assertNotIn("title", args[1])

    def test_demand_create_full_params(self):
        c, mo = self._client()
        c.demand_create("待办标题", content="详情", priority=1, project_id="proj-x")
        args, _kw = self._call(mo)
        self.assertEqual(args[1], {"title": "待办标题", "content": "详情",
                                   "priority": 1, "project_id": "proj-x"})

    def test_project_register_requires_project_id(self):
        c, mo = self._client()
        c.project_register("proj-x", path="/srv/proj-x", name="示例项目")
        args, _kw = self._call(mo)
        self.assertEqual(args[1]["project_id"], "proj-x")
        self.assertEqual(args[1]["path"], "/srv/proj-x")
        self.assertEqual(args[1]["name"], "示例项目")

    def test_admin_scope_requires_admin_key(self):
        c = make_client()
        with env_ctx():
            with self.assertRaises(SGMEError) as ctx:
                c._key_for("admin")
            self.assertIn("SGME_ADMIN_KEY", str(ctx.exception))
        with env_ctx(SGME_ADMIN_KEY=ADMIN_KEY):
            self.assertEqual(c._key_for("admin"), ADMIN_KEY)
            self.assertEqual(c._key_for("auto"), ADMIN_KEY)   # auto 优先管理员 Key
            self.assertEqual(c._key_for("agent"), c.key)      # agent 范围恒用 agent Key

    def test_skill_put_without_admin_key_raises(self):
        """写侧方法：缺管理员 Key 时报密钥错（且不落到网络调用）。"""
        c = make_client()
        with env_ctx():
            with self.assertRaises(SGMEError) as ctx:
                c.skill_put("demo", "---\nname: demo\n---\n正文")
            self.assertIn("SGME_ADMIN_KEY", str(ctx.exception))

    def test_skill_write_side_uses_admin_scope(self):
        c, mo = self._client()
        c.skill_put("demo", "SKILL 全文")
        c.skill_delete("demo", hard=True, force=True)
        c.skill_rename("old-name", "new-name")
        scopes = [call.kwargs.get("scope") for call in mo.call_args_list]
        self.assertEqual(scopes, ["admin", "admin", "admin"])
        self.assertEqual(mo.call_args.args[0], "skill_rename")
        self.assertEqual(mo.call_args.args[1], {"name": "old-name", "new_name": "new-name"})

    def test_mcp_raw_escape_hatch(self):
        c, mo = self._client()
        c.mcp_raw("some_tool", {"a": 1}, scope="admin")
        args, kw = self._call(mo)
        self.assertEqual(args, ("some_tool", {"a": 1}))
        self.assertEqual(kw["scope"], "admin")

    def test_mcp_requires_library(self):
        c = make_client()
        with mock.patch.dict(sys.modules, {"mcp": None}):
            with mock.patch("builtins.__import__", side_effect=ImportError("no mcp")):
                with self.assertRaises(SGMEError) as ctx:
                    c.refine_status()
                self.assertIn("MCP", str(ctx.exception))


class TestCapabilityMatrix(unittest.TestCase):
    """41 个基准能力：方法层 + CLI 层缺一不可。"""

    def test_baseline_count(self):
        self.assertEqual(len(BASELINE_TOOLS), 41)
        self.assertEqual(len(set(BASELINE_TOOLS)), 41)
        self.assertEqual(set(BASELINE_SPEC), set(BASELINE_TOOLS))

    def test_every_baseline_has_method(self):
        missing = [t for t in BASELINE_TOOLS if not hasattr(SGME, BASELINE_SPEC[t][0])]
        self.assertEqual(missing, [])

    def test_every_baseline_has_cli_command(self):
        missing = [t for t in BASELINE_TOOLS if BASELINE_SPEC[t][1] not in CLI_NAMES]
        self.assertEqual(missing, [])

    def test_every_cli_command_is_dispatchable(self):
        parser = sc._build_parser()
        for name in sorted(CLI_NAMES):
            self.assertIn(name, CMD_BY_NAME)
        # 每个命令都能被 argparse 解析（位置参数缺失除外——只验 -h 不炸）
        for name in sorted(CLI_NAMES):
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stdout(io.StringIO()):
                    parser.parse_args([name, "-h"])
            self.assertEqual(ctx.exception.code, 0)

    def test_capability_report_all_covered(self):
        rows = sc.capability_report()
        self.assertEqual(len(rows), 41)
        bad = [r["tool"] for r in rows if not (r["method_ok"] and r["cli_ok"])]
        self.assertEqual(bad, [])


class TestCLI(NoDeployConfig):
    def _run(self, argv, mock_client=None):
        """跑 CLI，返回 (退出码, 输出)。传 mock_client 时同时屏蔽 _print（避免 MagicMock
        被 json 序列化）——那类测试断言的是「CLI → 方法调用」转译。"""
        buf = io.StringIO()
        patchers = []
        if mock_client is not None:
            patchers.append(mock.patch.object(sc, "SGME", mock_client))
            patchers.append(mock.patch.object(sc, "_print", lambda obj: None))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = sc.main(argv)
        return code, buf.getvalue()

    def _run_real_print(self, argv, mock_client):
        """保留 _print 的 CLI 测试（验证真实序列化路径）。"""
        buf = io.StringIO()
        with mock.patch.object(sc, "SGME", mock_client):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = sc.main(argv)
        return code, buf.getvalue()

    def test_offline_capabilities_lists_41(self):
        code, out = self._run(["capabilities"])
        self.assertEqual(code, 0)
        self.assertIn("41/41", out)
        for tool in BASELINE_TOOLS:
            self.assertIn(tool, out)

    def test_offline_env_info_hides_key(self):
        with env_ctx(SGME_AGENT_KEY=KEY):
            code, out = self._run(["env-info"])
        self.assertEqual(code, 0)
        self.assertIn("HTTP 端点", out)
        self.assertNotIn(KEY, out)

    def test_missing_key_returns_2(self):
        with env_ctx():
            code, out = self._run(["health"])
        self.assertEqual(code, 2)
        self.assertIn("SGME_AGENT_KEY", out)

    def test_dict_result_is_json_printed(self):
        """真实 _print 路径：dict 结果序列化为 JSON（含中文不转义）。"""
        m = mock.MagicMock()
        m.return_value.health.return_value = {"status": "ok", "note": "示例"}
        code, out = self._run_real_print(["health"], m)
        self.assertEqual(code, 0)
        self.assertIn('"status": "ok"', out)
        self.assertIn("示例", out)

    def test_search_passes_scopes(self):
        m = mock.MagicMock()
        code, _out = self._run(["search", "示例词", "--limit", "3", "--scopes", "memory,wiki"], m)
        self.assertEqual(code, 0)
        m.return_value.search.assert_called_once_with("示例词", 3, ["memory", "wiki"])

    def test_append_from_file(self):
        m = mock.MagicMock()
        m.return_value.append.return_value = {"file_id": "f1"}
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "round.txt"
            f.write_text("本轮内容", encoding="utf-8")
            code, _out = self._run(["append", "--session", "s-1", "--file", str(f)], m)
        self.assertEqual(code, 0)
        m.return_value.append.assert_called_once_with("s-1", "本轮内容", "user", "workbuddy")

    def test_append_requires_text_or_file(self):
        m = mock.MagicMock()
        code, out = self._run(["append", "--session", "s-1"], m)
        self.assertEqual(code, 2)
        self.assertIn("必须提供内容", out)

    def test_new_mcp_commands_dispatch(self):
        """新增/修正的 MCP 命令：CLI → 方法调用参数逐一核对。

        （密钥范围 scope 是方法内部的默认参数，此处不传；scope 行为在
        TestMcpMethodSignatures 里对「方法 → _mcp」一层断言。）
        """
        cases = [
            (["stats"], "stats", ()),
            (["refine-status"], "refine_status", ()),
            (["role-active-get"], "role_active_get", ()),
            (["role-active-set", "role-1"], "role_active_set", ("role-1",)),
            (["signal-pull", "--signal-type", "care_daily", "--limit", "5"],
             "signal_pull", ("care_daily", 5)),
            (["signal-clear", "--subscriber", "workbuddy"],
             "signal_clear", (None, "workbuddy")),
            (["wiki-evolve-trigger", "--min-rounds", "3"],
             "wiki_evolve_trigger", (None, 3, 5)),
            (["config-update", "search", "--values", '{"limit": 5}'],
             "config_update", ("search", {"limit": 5})),
            (["config-update", "search", "--set", "limit=5", "--set", "enabled=true"],
             "config_update", ("search", {"limit": 5, "enabled": True})),
            (["idea-add", "一个创意"], "idea_add", ("一个创意", None, None)),
            (["project-register", "proj-x", "--path", "/srv/proj-x"],
             "project_register", ("proj-x", "/srv/proj-x", None, None, None)),
            (["skill-delete", "demo", "--hard"], "skill_delete", ("demo", True, False)),
            (["skill-rename", "old-name", "new-name"],
             "skill_rename", ("old-name", "new-name")),
        ]
        for argv, method, expected_args in cases:
            m = mock.MagicMock()
            code, out = self._run(argv, m)
            self.assertEqual(code, 0, msg=f"{argv} 退出码 {code}：{out}")
            fn = getattr(m.return_value, method)
            fn.assert_called_once()
            self.assertEqual(fn.call_args.args, expected_args, msg=f"{argv} 参数不符")

    def test_skill_put_from_file(self):
        m = mock.MagicMock()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "SKILL.md"
            f.write_text("---\nname: demo\n---\n正文", encoding="utf-8")
            code, _out = self._run(["skill-put", "demo", "--file", str(f)], m)
        self.assertEqual(code, 0)
        m.return_value.skill_put.assert_called_once_with("demo", "---\nname: demo\n---\n正文")

    def test_mcp_escape_hatch_kv_parsing(self):
        m = mock.MagicMock()
        code, _out = self._run(["mcp", "some_tool", "a=1", "b=true", "c=x", "--admin"], m)
        self.assertEqual(code, 0)
        m.return_value.mcp_raw.assert_called_once_with(
            "some_tool", {"a": 1, "b": True, "c": "x"}, scope="admin")

    def test_mcp_escape_hatch_json_args(self):
        m = mock.MagicMock()
        code, _out = self._run(["mcp", "some_tool", "--json-args", '{"values": {"limit": 5}}'], m)
        self.assertEqual(code, 0)
        m.return_value.mcp_raw.assert_called_once_with(
            "some_tool", {"values": {"limit": 5}}, scope="agent")

    def test_events_pull_formats_signals(self):
        m = mock.MagicMock()
        m.return_value.events_pull.return_value = {
            "events": [{"event_id": "e1", "type": "care_daily", "title": "提醒休息"}]}
        code, out = self._run(["events-pull", "--types", "care_"], m)
        self.assertEqual(code, 0)
        self.assertIn("care_daily", out)
        self.assertIn("提醒休息", out)

    def test_api_error_returns_2(self):
        m = mock.MagicMock()
        m.return_value.health.side_effect = SGMEError("HTTP 403: 拒绝")
        code, out = self._run(["health"], m)
        self.assertEqual(code, 2)
        self.assertIn("403", out)


class TestWorkBuddyMcpJsonDiscovery(unittest.TestCase):
    """WorkBuddy 特有：从 ~/.workbuddy/mcp.json 零配置继承地址与密钥。"""

    @staticmethod
    def _write(payload, tmpdir):
        f = Path(tmpdir) / "mcp.json"
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return f

    def test_discovers_key_and_url(self):
        with tempfile.TemporaryDirectory() as d:
            f = self._write({"mcpServers": {"sgme": {
                "type": "streamableHttp",
                "url": FAKE_MCP,
                "headers": {"X-API-Key": KEY},
            }}}, d)
            cfg = sc.load_workbuddy_mcp_json(f)
            self.assertEqual(cfg["api_key"], KEY)
            self.assertEqual(cfg["url"], FAKE_MCP)

    def test_lowercase_header_alias(self):
        with tempfile.TemporaryDirectory() as d:
            f = self._write({"mcpServers": {"sgme": {
                "url": FAKE_MCP, "headers": {"x-api-key": KEY}}}}, d)
            self.assertEqual(sc.load_workbuddy_mcp_json(f)["api_key"], KEY)

    def test_other_server_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            f = self._write({"mcpServers": {"other": {"url": FAKE_MCP}}}, d)
            self.assertEqual(sc.load_workbuddy_mcp_json(f), {})

    def test_missing_file_and_invalid_json(self):
        self.assertEqual(sc.load_workbuddy_mcp_json(Path("no") / "such" / "file.json"), {})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "mcp.json"
            f.write_text("{not json", encoding="utf-8")
            self.assertEqual(sc.load_workbuddy_mcp_json(f), {})

    def test_empty_sgme_entry_yields_empty(self):
        with tempfile.TemporaryDirectory() as d:
            f = self._write({"mcpServers": {"sgme": {}}}, d)
            self.assertEqual(sc.load_workbuddy_mcp_json(f), {})

    def test_address_and_key_inherited_end_to_end(self):
        """端到端：无任何环境变量/部署配置，地址与密钥均继承自 mcp.json。"""
        cfg = {"url": FAKE_MCP, "api_key": KEY}
        with env_ctx(), \
                mock.patch.object(sc, "load_workbuddy_mcp_json", return_value=cfg), \
                mock.patch.object(sc, "load_workbuddy_identity", return_value={}):
            c = SGME()
        self.assertEqual(c.base_url, FAKE_HTTP)
        self.assertEqual(c.key, KEY)
        self.assertIn("mcp.json", c.key_source)
        self.assertIn("mcp.json", c.source)


class TestKeyPrecedence(unittest.TestCase):
    """密钥解析优先级（WorkBuddy 口径）：专用变量 > 身份文件 > mcp.json > 通用兜底。"""

    @staticmethod
    def _resolve(env_kv, ident=None, mcp_cfg=None):
        with env_ctx(**env_kv), \
                mock.patch.object(sc, "load_workbuddy_identity", return_value=ident or {}), \
                mock.patch.object(sc, "load_workbuddy_mcp_json", return_value=mcp_cfg or {}):
            return sc.resolve_agent_key()

    def test_dedicated_env_wins_over_all(self):
        k, src = self._resolve(
            {"SGME_WORKBUDDY_KEY": "k-dedicated", "SGME_AGENT_KEY": "k-generic"},
            ident={"api_key": "k-ident"}, mcp_cfg={"api_key": "k-mcp"})
        self.assertEqual(k, "k-dedicated")
        self.assertIn(sc.WORKBUDDY_KEY_ENV, src)

    def test_identity_beats_mcp_json(self):
        k, _ = self._resolve({}, ident={"api_key": "k-ident"}, mcp_cfg={"api_key": "k-mcp"})
        self.assertEqual(k, "k-ident")

    def test_mcp_json_beats_generic_env(self):
        """核心回归防线：SGME_AGENT_KEY 必须让位于 mcp.json。

        本机 .env 的 SGME_AGENT_KEY 绑定 agent_id=dsh；一旦它被优先使用，
        WorkBuddy 写入的 L0 会被打上 dsh 的 agent_tag，污染多 Agent 溯源（T-140）。
        这条断言就是防止有人把顺序改回「环境变量优先」。
        """
        k, _ = self._resolve({"SGME_AGENT_KEY": "k-shared"}, mcp_cfg={"api_key": "k-mcp"})
        self.assertEqual(k, "k-mcp")
        self.assertNotEqual(k, "k-shared")

    def test_generic_env_is_last_resort_with_warning(self):
        k, src = self._resolve({"SGME_AGENT_KEY": "k-shared"})
        self.assertEqual(k, "k-shared")
        self.assertIn("兜底", src)

    def test_nothing_configured_returns_empty(self):
        self.assertEqual(self._resolve({}), ("", ""))

    def test_client_records_key_source(self):
        with env_ctx(SGME_WORKBUDDY_KEY="k-dedicated"), \
                mock.patch.object(sc, "load_workbuddy_identity", return_value={}), \
                mock.patch.object(sc, "load_workbuddy_mcp_json", return_value={}):
            c = SGME()
        self.assertEqual(c.key_source, f"环境变量 {sc.WORKBUDDY_KEY_ENV}")
        self.assertEqual(c.describe()["agent_id"], "workbuddy")
        self.assertTrue(c.describe()["agent_key_set"])


class TestDeriveHttpFromMcp(unittest.TestCase):
    """由 MCP 端点反推 HTTP 端点（WorkBuddy mcp.json 里只有 MCP URL）。"""

    def test_port_minus_three(self):
        self.assertEqual(sc.derive_http_from_mcp(FAKE_MCP), FAKE_HTTP)

    def test_roundtrip_with_derive_mcp_url(self):
        self.assertEqual(sc.derive_http_from_mcp(derive_mcp_url(FAKE_HTTP)), FAKE_HTTP)


if __name__ == "__main__":
    unittest.main()
