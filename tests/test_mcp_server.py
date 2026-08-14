"""MCP Server 测试：工具集完整性 + 核心工具调用（server 侧直调，不起 HTTP）。

注意：config_update 会落盘 sgme.yaml——测试用 SGME_CONFIG_PATH 环境变量隔离。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from sgme import config as sgme_config
from sgme.mcp_server import bind_app_state, build_mcp_server, mount_mcp
from sgme.raw import store as raw_store
from sgme.data import db as db_mod
from sgme.data import memory_dao


def _call(mcp, name: str, args: dict):
    """同步包装 async call_tool → 返回 (results: list[TextContent], meta)。

    统一解包为 (text, meta)。
    """
    raw = asyncio.run(mcp.call_tool(name, args))
    results, meta = raw if isinstance(raw, tuple) else (raw, None)
    text = "\n".join(c.text for c in results if getattr(c, "text", None))
    return text, meta


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def mcp(tmp_path, monkeypatch, raw_dir):
    """构建绑定隔离 app_state 的 MCP server。"""
    # 配置落盘隔离
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    from sgme.server.app import AgentKeyStore

    _key_store = AgentKeyStore(admin_key="test-admin-key", agent_key="test-agent-key",
                               store_path=tmp_path / "agent_keys.json")
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
        "key_store": _key_store,
    })
    server = build_mcp_server()
    yield server
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def test_mcp_tools_available(mcp):
    """工具集完整性（2026-08-14：+signal 三工具 + role 四工具）。"""
    tools = asyncio.run(mcp.list_tools())
    names = [t.name for t in tools]
    expected = {"append", "inject", "search", "memory_get", "memory_reject",
                "refine_trigger", "refine_batch", "refine_status",
                "stats", "health", "config_get", "config_update", "agent_onboarding",
                "idea_add", "demand_create", "project_register",
                "signal_pull", "signal_claim", "signal_ack",
                "role_list", "role_assemble", "role_active_get", "role_active_set"}
    assert expected <= set(names), f"缺工具: {expected - set(names)}"


def test_mcp_stats(mcp):
    """stats 返回记忆统计结构。"""
    result = _call(mcp, "stats", {})
    data = json.loads(result[0])
    assert "memories" in data
    assert "raw_files" in data
    assert data["memories"]["total"] >= 0


def test_mcp_idea_add(mcp):
    """idea_add：人工添加创意 → ideas 独立表（T-56）+ 列表可见（用户主动记录）。"""
    text, _ = _call(mcp, "idea_add", {"content": "MCP 测试创意：做待办看板", "priority": 85})
    data = json.loads(text)
    assert "error" not in data, data
    idea = data["idea"]
    assert idea["content"] == "MCP 测试创意：做待办看板"
    assert idea["priority"] == 85
    assert idea["idea_id"]
    assert idea["status"] == "active"

    # 缺失 content → FastMCP 参数校验层拒绝（必填参数由框架把关）
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _call(mcp, "idea_add", {})


def test_mcp_demand_create(mcp):
    """demand_create：新建待办（可带 project_id 标记）→ 状态 pending + 时间戳。"""
    text, _ = _call(mcp, "demand_create", {"title": "MCP 待办：接通项目过滤", "project_id": "sgme"})
    data = json.loads(text)
    assert "error" not in data, data
    assert data["title"] == "MCP 待办：接通项目过滤"
    assert data["project_id"] == "sgme"
    assert data["status"] == "pending"
    assert data["created_at"] and data["resolved_at"] is None


def test_mcp_project_register(mcp):
    """project_register：登记项目 → project_meta 落库；二次登记=更新（created=False）。"""
    text, _ = _call(mcp, "project_register", {
        "project_id": "testproj", "path": "D:/Projects/testproj", "name": "测试项目",
    })
    data = json.loads(text)
    assert "error" not in data, data
    assert data["created"] is True
    assert data["project"]["name"] == "测试项目"

    text2, _ = _call(mcp, "project_register", {
        "project_id": "testproj", "path": "D:/Projects/testproj", "milestone": "v1.0",
    })
    data2 = json.loads(text2)
    assert "error" not in data2, data2
    assert data2["created"] is False
    assert data2["project"]["milestone"] == "v1.0"


def test_mcp_append_and_inject(mcp):
    """append 写入 → inject 能查到（Tier0 摘要降级路径不崩）。"""
    r = _call(mcp, "append", {
        "session_key": "mcp-test", "started_at": "2026-08-04T10:00:00Z",
        "content": "# 2026-08-04T10:00:00Z user\nMCP 测试写入\n",
    })
    data = json.loads(r[0])
    assert "file_id" in data
    assert data["status"] == "new"

    r2 = _call(mcp, "inject", {"mode": "daily"})
    data2 = json.loads(r2[0])
    assert "blocks" in data2
    assert "stats" in data2


def test_mcp_append_agent_id_passthrough(mcp):
    """B35：MCP append 支持可选 agent_id 参数（溯源自报），落库可查。"""
    import sqlite3

    from sgme.mcp_server import _app_state

    r = _call(mcp, "append", {
        "session_key": "mcp-test-agent", "started_at": "2026-08-04T11:00:00Z",
        "content": "# 2026-08-04T11:00:00Z user\nMCP 带 agent_id 写入\n",
        "agent_id": "mcp-agent-x",
    })
    data = json.loads(r[0])
    assert "file_id" in data, data

    conn: sqlite3.Connection = _app_state["session_conn"]
    row = conn.execute("SELECT agent_id FROM raw_files WHERE file_id=?", (data["file_id"],)).fetchone()
    assert row is not None and row[0] == "mcp-agent-x"


def test_mcp_append_without_agent_id_stays_null(mcp):
    """B35：MCP append 不传 agent_id → 落 NULL（与历史行为一致，客户端可自报）。"""
    import sqlite3

    from sgme.mcp_server import _app_state

    r = _call(mcp, "append", {
        "session_key": "mcp-test-null", "started_at": "2026-08-04T12:00:00Z",
        "content": "# 2026-08-04T12:00:00Z user\nMCP 不带 agent_id 写入\n",
    })
    data = json.loads(r[0])
    conn: sqlite3.Connection = _app_state["session_conn"]
    row = conn.execute("SELECT agent_id FROM raw_files WHERE file_id=?", (data["file_id"],)).fetchone()
    assert row is not None and row[0] is None


def test_mcp_search(mcp):
    """search 返回结果结构。"""
    r = _call(mcp, "search", {"query": "测试", "limit": 5})
    data = json.loads(r[0])
    assert "results" in data


def test_mcp_config_get_update(mcp, tmp_path):
    """config_get / config_update（隔离落盘）。"""
    r = _call(mcp, "config_get", {"section": "refine"})
    data = json.loads(r[0])
    assert data["section"] == "refine"
    assert "refine_on_append" in data["config"]

    r2 = _call(mcp, "config_update", {
        "section": "refine", "values": {"refine_on_append": True},
    })
    data2 = json.loads(r2[0])
    assert data2["status"] == "ok"
    assert data2["config"]["refine_on_append"] is True
    # 落盘隔离文件
    import yaml
    persisted = yaml.safe_load((tmp_path / "sgme_test.yaml").read_text(encoding="utf-8"))
    assert persisted["refine"]["refine_on_append"] is True
    # 还原
    _call(mcp, "config_update", {
        "section": "refine", "values": {"refine_on_append": False},
    })


def test_mcp_health(mcp):
    """health 返回状态。"""
    r = _call(mcp, "health", {})
    data = json.loads(r[0])
    assert data["status"] == "ok"
    assert "version" in data


def test_mcp_memory_reject(mcp):
    """memory_reject：接线 operations.reject_memory——成功标记 + 记忆不存在报错。"""
    from sgme.data import memory_dao

    mid = memory_dao.insert_memory(
        mcp_test_conn(), "测试记忆内容", "fact", 50, "static", None, ["projects"],
        agent_tag="mcp-test",
    )
    r = _call(mcp, "memory_reject", {"memory_id": mid, "reason": "测试纠错"})
    data = json.loads(r[0])
    assert data["memory_id"] == mid
    assert data["status"] == "rejected"
    assert data["reject_reason"] == "测试纠错"

    # 幂等：重复 reject 更新 reason
    r2 = _call(mcp, "memory_reject", {"memory_id": mid, "reason": "再次纠错"})
    data2 = json.loads(r2[0])
    assert data2["reject_reason"] == "再次纠错"

    # 不存在
    r3 = _call(mcp, "memory_reject", {"memory_id": "no-such-id"})
    data3 = json.loads(r3[0])
    assert "error" in data3


def test_mcp_refine_batch_and_status(mcp):
    """refine_batch 校验分支 + async 排队返回；refine_status 进度结构。"""
    # 空列表 → InvalidArgs
    r = _call(mcp, "refine_batch", {"file_ids": [], "async_mode": True})
    data = json.loads(r[0])
    assert "error" in data

    # 不存在的文件 → 存在性预检报错（异步也先预检）
    r1 = _call(mcp, "refine_batch", {"file_ids": ["no-such-file"], "async_mode": True})
    data1 = json.loads(r1[0])
    assert "raw_files 表无记录" in data1.get("error", "")

    # async 模式：真实文件 → 排队返回（不实际触发提炼，避免真实 LLM 调用）
    r2 = _call(mcp, "append", {
        "session_key": "mcp-batch-test", "started_at": "2026-08-04T10:00:00Z",
        "content": "# 2026-08-04T10:00:00Z user\n批量提炼测试\n",
    })
    file_id = json.loads(r2[0])["file_id"]
    r3 = _call(mcp, "refine_batch", {"file_ids": [file_id], "async_mode": True})
    data3 = json.loads(r3[0])
    assert data3.get("status") == "queued"
    assert data3.get("triggered") == "async"
    assert file_id in data3.get("file_ids", [])

    # refine_status：进度结构
    r4 = _call(mcp, "refine_status", {})
    data4 = json.loads(r4[0])
    assert "pending" in data4
    assert "completed" in data4
    assert "total" in data4


def test_mcp_agent_onboarding(mcp):
    """agent_onboarding：版本 + 能力清单与 @mcp.tool 工具列表一一对应（防漂移）。"""
    r = _call(mcp, "agent_onboarding", {})
    data = json.loads(r[0])
    assert data["server"] == "SGME 拾光记忆引擎"
    assert "version" in data
    tools = asyncio.run(mcp.list_tools())
    declared = {t["name"] for t in data["tools"]}
    actual = {t.name for t in tools}
    assert declared == actual, f"能力清单与工具列表漂移: {declared ^ actual}"
    assert "quickstart" in data
    assert "register" in data["quickstart"]


def test_mcp_agent_onboarding_self_config(mcp):
    """agent_onboarding 自助配置段（2026-08-13）：版本标记 + 自查步骤 + 模板 + 失败路径。

    「只要求结果、不限制过程」：agent 按自己工具机制把模板写入自己的身份文件；
    幂等（版本 >= v1 跳过）；写入失败必须如实报告（禁止谎称已完成）。
    """
    r = _call(mcp, "agent_onboarding", {})
    data = json.loads(r[0])
    sc = data.get("self_config")
    assert sc, "agent_onboarding 必须包含 self_config 段"
    assert sc["version"] == "SGME-ONBOARDING-v1"
    # 步骤含幂等自查 / 读回验证 / 失败路径
    steps = "\n".join(sc["steps"])
    for keyword in ("自查", "跳过", "读回验证", "报告主人"):
        assert keyword in steps, f"steps 缺关键动作: {keyword}"
    # 模板含版本标记与核心纪律（含事件对接双模式，ST-30）
    tmpl = sc["template"]
    for keyword in (
        "SGME-ONBOARDING-v1",
        "服务发现",
        "每轮对话结束 append",
        "refine_trigger",
        "inject 按场景取画像",
        "强制查询",
        "422",
        "X-API-Key",
        # ST-30 事件对接：SSE 长连 + 游标拉取 + MCP pull 三接法 + 事件三类
        "/v1/events/stream",
        "/v1/events/pull",
        "subscriber_id",
        "Last-Event-ID",
        "care_*",
        "memory_updated",
        "anomaly_warn",
        "谁消费谁标记",
        "role_list",
        # ST-31 通信渠道兜底铁律
        "兜底通信渠道",
        "微信",
        "飞书",
        "Telegram",
    ):
        assert keyword in tmpl, f"template 缺关键内容: {keyword}"


def test_mcp_role_tools(mcp, tmp_path, monkeypatch):
    """ST-29：role_list/role_assemble/role_active_get/role_active_set 四工具闭环。

    换皮不换芯：角色只是沟通外皮，记忆池不动。隔离 roles/ 目录避免碰真实角色卡。
    """
    from sgme.care import roles as roles_mod

    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", tmp_path / "personas")
    monkeypatch.setattr(sgme_config, "DATA_DIR", tmp_path / "data")

    # 造一张角色卡（butler）
    roles_mod.save_role(rd, "butler", {
        "data": {
            "name": "管家",
            "description": "专业可靠的个人管家",
            "system_prompt": "你是{{char}}，{{user}}的个人管家。{{original}}",
            "extensions": {"sgme_care": {"greeting_templates": ["早上好"]}},
        },
    })

    # role_list 列出角色 + 当前角色（未设置 → None）
    text, _ = _call(mcp, "role_list", {})
    data = json.loads(text)
    assert "error" not in data, data
    assert data["active_role"] is None
    assert any(r["role_id"] == "butler" for r in data["roles"])

    # role_assemble 装配：system_prompt + care_policy
    text, _ = _call(mcp, "role_assemble", {"role_id": "butler"})
    data = json.loads(text)
    assert "error" not in data, data
    assert "管家" in data["system_prompt"]
    assert data["care_policy"]["greeting_templates"] == ["早上好"]

    # role_assemble 角色不存在 → error
    text, _ = _call(mcp, "role_assemble", {"role_id": "nobody"})
    data = json.loads(text)
    assert "error" in data

    # role_active_set → role_active_get 闭环
    text, _ = _call(mcp, "role_active_set", {"role_id": "butler"})
    data = json.loads(text)
    assert "error" not in data, data
    assert data["role_id"] == "butler"

    text, _ = _call(mcp, "role_active_get", {})
    data = json.loads(text)
    assert data["role_id"] == "butler"

    # role_list 现在反映当前角色
    text, _ = _call(mcp, "role_list", {})
    data = json.loads(text)
    assert data["active_role"] == "butler"


def test_trae_notification_patch():
    """Trae 通知宽容补丁：幂等 + 未知通知类型可校验通过（ST-23⑤）。

    补丁把 mcp.types.ClientNotification 替换为宽松 RootModel（root= 构造），
    未知 method 字符串落入兜底成员，不再被严格枚举拒绝。
    """
    from sgme.mcp_server import _NOTIFICATION_PATCHED, _patch_lenient_notifications

    # 幂等：重复调用只打一次
    _patch_lenient_notifications()
    flag_after_first = _NOTIFICATION_PATCHED
    _patch_lenient_notifications()
    assert _NOTIFICATION_PATCHED is flag_after_first

    # 未知通知（Trae session_stop 风格）可构造且校验通过（运行时补丁，model_validate 走 pydantic 解析）
    from mcp import types as mcp_types

    try:
        n = mcp_types.ClientNotification.model_validate(
            {"method": "notifications/trae/session_stop", "params": None}
        )
        assert n is not None
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"未知通知类型被拒绝: {exc}")


def mcp_test_conn():
    """取当前 fixture 绑定的 mem_conn（供插入测试记忆用）。"""
    from sgme.mcp_server import _app_state
    return _app_state["mem_conn"]


# ---------- PR#1：HTTP 层鉴权（ApiKeyMiddleware） ----------

def _mcp_http_app(tmp_path, monkeypatch, raw_dir, *, with_middleware=True):
    """构建带鉴权中间件的 MCP streamable-http app（Starlette，TestClient 直打）。"""
    from sgme.server.app import AgentKeyStore, create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    # 复用 create_app 的 key_store（与 HTTP 通道同一鉴权设施）
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    mcp = mount_mcp(app, start_server=False)  # 只构建不启动线程
    starlette_app = mcp.streamable_http_app()
    if with_middleware:
        from sgme.mcp_server import ApiKeyMiddleware
        starlette_app.add_middleware(ApiKeyMiddleware, key_store=app.state.key_store)
    # 必须用 with 进入 lifespan（FastMCP 的 task group 在 lifespan 初始化）
    with TestClient(starlette_app) as client:
        yield client, app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _mcp_initialize(client, api_key=None):
    """发 JSON-RPC initialize（MCP 协议握手），返回 (status, body)。

    - Host 头必须给 127.0.0.1（FastMCP transport_security 校验 Host，
      TestClient 默认 testserver 会触发 421 Misdirected Request）
    - Accept 必须含 text/event-stream（MCP 协议要求，否则 406）
    - 响应可能是 SSE 流（text/event-stream）——body 提取 JSON-RPC 结果，
      失败则返回原文（供断言诊断）
    """
    headers = {"X-API-Key": api_key} if api_key else {}
    headers["Host"] = "127.0.0.1:9913"
    headers["Accept"] = "application/json, text/event-stream"
    headers["Content-Type"] = "application/json"
    r = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }, headers=headers)
    # SSE 响应：data: {...} 行；纯 JSON 响应：直接对象
    text = r.text
    body = None
    if "data:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    body = json.loads(line[5:].strip())
                    break
                except Exception:
                    continue
    else:
        try:
            body = json.loads(text)
        except Exception:
            body = None
    return r.status_code, body or {"raw": text[:300]}


def test_mcp_http_no_key_rejected(tmp_path, monkeypatch, raw_dir):
    """PR#1：MCP HTTP 无 X-API-Key → 403（此前无鉴权直通）。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        status, body = _mcp_initialize(client)
        assert status == 403, f"预期 403，实际 {status}: {body}"
    finally:
        gen.close()


def test_mcp_http_wrong_key_rejected(tmp_path, monkeypatch, raw_dir):
    """PR#1：错误 X-API-Key → 403。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        status, _ = _mcp_initialize(client, api_key="wrong-key-123")
        assert status == 403
    finally:
        gen.close()


def test_mcp_http_agent_key_allowed(tmp_path, monkeypatch, raw_dir):
    """PR#1：env agent key → 放行（initialize 握手 200）。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        status, body = _mcp_initialize(client, api_key="test-agent-key")
        assert status == 200, f"预期 200，实际 {status}: {body}"
        assert body["result"]["serverInfo"]["name"] == "SGME"
    finally:
        gen.close()


def test_mcp_http_registered_key_allowed(tmp_path, monkeypatch, raw_dir):
    """PR#1：注册 agt_* key → 放行（与 HTTP 通道同规则）。"""
    from sgme.server.app import AgentKeyStore

    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        # 直接经 key_store 注册（避开 admin HTTP 端点，聚焦 MCP 中间件）
        key = app.state.key_store.register_agent("planner", ["projects"])
        status, body = _mcp_initialize(client, api_key=key)
        assert status == 200, f"预期 200，实际 {status}: {body}"
    finally:
        gen.close()


def test_mcp_http_admin_key_allowed(tmp_path, monkeypatch, raw_dir):
    """PR#1：admin key → 放行（is_agent 语义：admin 可调全部）。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        status, body = _mcp_initialize(client, api_key="test-admin-key")
        assert status == 200, f"预期 200，实际 {status}: {body}"
    finally:
        gen.close()


# ---------- PR#2：MCP append 鉴权 key → agent_id 反查兜底 ----------

def _mcp_call_tool(client, session_id, name, arguments, api_key):
    """完整 MCP 会话流：initialize → initialized → tools/call。

    返回 tools/call 的 JSON-RPC 响应体（SSE data 行解析）。
    """
    # initialize（建会话）
    headers = {"X-API-Key": api_key, "Host": "127.0.0.1:9913",
               "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "pytest", "version": "1.0"}},
    }, headers=headers)
    sid = r.headers.get("mcp-session-id")
    assert sid, f"无 session id: {r.status_code} {r.text[:200]}"

    # initialized 通知（会话就绪）
    h2 = {**headers, "Mcp-Session-Id": sid}
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, headers=h2)

    # tools/call
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers=h2)
    assert r.status_code == 200, f"tools/call 失败: {r.status_code} {r.text[:300]}"
    for line in r.text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                continue
    return None


def test_mcp_append_agent_id_resolves_from_registered_key(tmp_path, monkeypatch, raw_dir):
    """PR#2：注册 agt_* key 调 append（不带 agent_id）→ 落绑定 agent_id。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        key = app.state.key_store.register_agent("planner", ["projects"])
        body = _mcp_call_tool(client, None, "append", {
            "session_key": "pr2-reg", "started_at": "2026-08-11T10:00:00Z",
            "content": "# 2026-08-11T10:00:00Z user\nPR2 注册 key 反查\n",
        }, api_key=key)
        assert body is not None and "result" in body, body

        conn: sqlite3.Connection = app.state.session_conn
        row = conn.execute("SELECT agent_id FROM raw_files WHERE session_key='pr2-reg'").fetchone()
        assert row is not None and row[0] == "planner", f"预期 planner，实际 {row}"
    finally:
        gen.close()


def test_mcp_append_agent_id_default_from_env_key(tmp_path, monkeypatch, raw_dir):
    """PR#2：env agent key 调 append（不带 agent_id）→ 落 default。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        body = _mcp_call_tool(client, None, "append", {
            "session_key": "pr2-env", "started_at": "2026-08-11T10:00:00Z",
            "content": "# 2026-08-11T10:00:00Z user\nPR2 env key 反查\n",
        }, api_key="test-agent-key")
        assert body is not None and "result" in body, body

        conn: sqlite3.Connection = app.state.session_conn
        row = conn.execute("SELECT agent_id FROM raw_files WHERE session_key='pr2-env'").fetchone()
        assert row is not None and row[0] == "default", f"预期 default，实际 {row}"
    finally:
        gen.close()


def test_mcp_append_explicit_agent_id_wins(tmp_path, monkeypatch, raw_dir):
    """PR#2：显式 agent_id 参数优先于 key 反查。"""
    gen = _mcp_http_app(tmp_path, monkeypatch, raw_dir)
    client, app = next(gen)
    try:
        key = app.state.key_store.register_agent("planner", [])
        body = _mcp_call_tool(client, None, "append", {
            "session_key": "pr2-explicit", "started_at": "2026-08-11T10:00:00Z",
            "agent_id": "explicit-x",
            "content": "# 2026-08-11T10:00:00Z user\nPR2 显式优先\n",
        }, api_key=key)
        assert body is not None and "result" in body, body

        conn: sqlite3.Connection = app.state.session_conn
        row = conn.execute("SELECT agent_id FROM raw_files WHERE session_key='pr2-explicit'").fetchone()
        assert row is not None and row[0] == "explicit-x", f"预期 explicit-x，实际 {row}"
    finally:
        gen.close()
