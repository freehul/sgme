"""tests/test_coldstart_wiring.py：D-72 对话冷场修复接线测试。

覆盖（接线目标，不重复测 conversation_state 内部判据）：
1. 不传 history → 注入响应与既有契约完全一致（无修复块、无新字段）
2. 传冷场 history → blocks 末尾追加一条 kind="coldstart_repair" 的修复块，
   内容等于 conversation_state.repair_hint(assess(history))
3. 传正常 history → 不追加修复块（is_cold=False）
4. already_repaired=True → 修补闸拦住，不重复追加
5. custom_filter 分支同样接线
6. history 含非 dict 脏数据不炸（容错归一，不改变注入主链路）
7. HTTP POST /v1/inject 透传请求体 history
8. MCP inject 工具接受可选 history（MCP 无对话上下文，须显式传入）
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine.conversation_state import assess as cs_assess
from sgme.engine.conversation_state import repair_hint as cs_repair_hint
from sgme.operations.inject import inject as inject_operation
from sgme.profile import tier0 as tier0_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app

# ---------- 会话样例 ----------


def _u(text: str) -> dict:
    return {"role": "user", "content": text}


def _a(text: str) -> dict:
    return {"role": "assistant", "content": text}


# 冷场：最近两条用户消息「嗯」「哦」→ reason=short_replies
COLD_HISTORY = [
    _u("今天聊聊你最近在忙什么"),
    _a("我最近在整理旧照片，翻到好多高中时候的合影，挺感慨的。"),
    _u("嗯"),
    _a("是吗，那几张照片里你还留着当年的校服呢。"),
    _u("哦"),
]

# 正常对话：长度适中、有问有答
NORMAL_HISTORY = [
    _u("今天我们聊聊你上次说的那个旅行计划吧"),
    _a("好呀！我最近确实在纠结要不要去云南，你看这个季节合适吗？"),
    _u("十一月去大理挺舒服的，人少天气也好，就是早晚温差大"),
    _a("那带件外套就行。对了你觉得住古城里还是洱海边比较好？"),
    _u("住洱海边吧，早上拉开窗帘就能看到日出，想想就很值"),
]


# ---------- fixtures（照 test_operations_inject.py，inject 不调 LLM） ----------


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def summary_path(tmp_path, monkeypatch):
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


@pytest.fixture
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mcp(tmp_path, monkeypatch, raw_dir):
    """构建绑定隔离 app_state 的 MCP server（照 test_mcp_server.py）。"""
    from sgme.mcp_server import bind_app_state, build_mcp_server
    from sgme.server.app import AgentKeyStore

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    bind_app_state({
        "cfg": cfg,
        "mem_conn": mem_conn,
        "session_conn": session_conn,
        "wiki_conn": wiki_conn,
        "key_store": AgentKeyStore(
            admin_key="test-admin-key", agent_key="test-agent-key",
            store_path=tmp_path / "agent_keys.json",
        ),
    })
    server = build_mcp_server()
    yield server
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _call_mcp(mcp, name: str, args: dict) -> dict:
    raw = asyncio.run(mcp.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return json.loads("\n".join(c.text for c in results if getattr(c, "text", None)))


def _data(res) -> dict:
    """取 OperationResult.data 并断言成功/非 None（便于类型检查与失败定位）。"""
    assert res.ok is True, res.message
    assert res.data is not None
    return res.data


def _repair_blocks(data: dict) -> list[dict]:
    return [b for b in data.get("blocks", []) if b.get("kind") == "coldstart_repair"]


# ---------- 1. 向后兼容：不传 history ----------


def test_mode_inject_without_history_has_no_repair(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    data = _data(inject_operation(mem_conn, cfg, mode="daily"))
    assert _repair_blocks(data) == []
    # 不新增顶层字段，既有契约不变
    assert set(data) == {"blocks", "stats", "tier0"}


# ---------- 2. 冷场 history → 追加修复块 ----------


def test_cold_history_appends_repair_block(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    data = _data(inject_operation(mem_conn, cfg, mode="daily", history=COLD_HISTORY))
    blocks = _repair_blocks(data)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["present"] is True
    assert block["reason"] == "short_replies"
    assert block["items"] == [{"content": cs_repair_hint(cs_assess(COLD_HISTORY))}]
    assert block["signals"] == cs_assess(COLD_HISTORY)["signals"]
    # 修复块在末尾，不挤占既有块顺序
    assert data["blocks"][-1] is block


def test_cold_history_does_not_alter_existing_blocks(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    base = _data(inject_operation(mem_conn, cfg, mode="daily"))
    wired = _data(inject_operation(mem_conn, cfg, mode="daily", history=COLD_HISTORY))
    assert wired["blocks"][: len(base["blocks"])] == base["blocks"]
    assert wired["stats"] == base["stats"]
    assert wired["tier0"] == base["tier0"]


# ---------- 3. 正常 history → 不追加 ----------


def test_normal_history_no_repair(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    data = _data(inject_operation(mem_conn, cfg, mode="daily", history=NORMAL_HISTORY))
    assert _repair_blocks(data) == []


# ---------- 4. 修补闸：已修补过不重复 ----------


def test_already_repaired_suppresses_repair(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    data = _data(inject_operation(
        mem_conn, cfg, mode="daily", history=COLD_HISTORY, already_repaired=True,
    ))
    assert _repair_blocks(data) == []


# ---------- 5. custom_filter 分支同样接线 ----------


def test_custom_filter_cold_history_appends_repair(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    data = _data(inject_operation(
        mem_conn, cfg,
        custom_filter={"dimensions": ["identity"], "limit": 3},
        history=COLD_HISTORY,
    ))
    assert data["stats"]["mode"] == "custom"
    assert len(_repair_blocks(data)) == 1


# ---------- 6. 脏 history 容错 ----------


def test_malformed_history_entries_are_tolerated(conns, cfg, summary_path):
    mem_conn, _s, _w = conns
    dirty = ["not-a-dict", None, {"role": "user"}, _u("嗯"), _u("哦")]
    data = _data(inject_operation(mem_conn, cfg, mode="daily", history=dirty))  # type: ignore[arg-type]
    assert len(_repair_blocks(data)) == 1


# ---------- 7. HTTP 路由透传 ----------


def test_http_inject_passes_history(client):
    resp = client.post(
        "/v1/inject",
        json={"mode": "daily", "history": COLD_HISTORY},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200, resp.text
    assert len(_repair_blocks(resp.json())) == 1


def test_http_inject_without_history_unchanged(client):
    resp = client.post(
        "/v1/inject",
        json={"mode": "daily"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200, resp.text
    assert _repair_blocks(resp.json()) == []


# ---------- 8. MCP 工具可选 history ----------


def test_mcp_inject_accepts_optional_history(mcp):
    data = _call_mcp(mcp, "inject", {"mode": "daily", "history": COLD_HISTORY})
    assert "blocks" in data
    assert len(_repair_blocks(data)) == 1


def test_mcp_inject_without_history_unchanged(mcp):
    data = _call_mcp(mcp, "inject", {"mode": "daily"})
    assert _repair_blocks(data) == []
