"""tests/test_operations_stats.py：operations 层 stats 模块测试（v0.7 Batch-1）。

覆盖：
1. stats() 返回 OperationResult(ok=True)，data 是协议无关信息超集
2. 计数/维度分布/水位取值正确（含 watermark_age_sec 复用 health 口径）
3. **明文 Key 收口**：_public_agents 只保留 agent_id/role，api_key 绝不外泄
4. 两端投影的历史契约差异：顶层第 2/3 键互换、refinement 形态不同、MCP 无 agents
5. **契约等价性**（最关键）：GET /v1/admin/stats 与 MCP stats 工具经 operations
   包装后，字段集合**与顺序**仍与 v0.6 逐字段一致
6. **失败路径**：DAO 抛异常时不被 catch-all 吞掉，原样上抛（HTTP 侧 → 500）

零真实 DB：全部走 tmp_path 建库，绝不触碰 data/*.db。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.errors import OperationResult
from sgme.operations.stats import _public_agents, http_payload, mcp_payload, stats
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# ---------- v0.6 冻结契约（改造前逐字段抄录，任何变动即破坏性变更） ----------

HTTP_TOP_KEYS = ["memories", "raw_files", "dimension_distribution", "refinement", "agents"]
HTTP_REFINEMENT_KEYS = ["watermark_age_sec", "last_refined_at", "queue_depth"]

# ⚠️ MCP 的第 2/3 个顶层键与 HTTP **互换**（v0.6 两处独立实现的真实差异）
MCP_TOP_KEYS = ["memories", "dimension_distribution", "raw_files", "refinement"]
MCP_REFINEMENT_KEYS = ["last_refined_at"]

MEMORIES_KEYS = ["total", "archived"]
RAW_FILES_KEYS = ["total", "new", "refined", "error", "archived"]
AGENT_KEYS = ["agent_id", "role"]

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path，零真实 DB）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    m, s, w = conns
    return create_app(
        cfg=cfg, mem_conn=m, session_conn=s, wiki_conn=w,
        admin_key="test-admin-key", agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def lenient_client(app):
    """不把服务端异常再抛给测试的 client（验证 500 兜底形态专用）。

    Starlette 的 ServerErrorMiddleware 在调用全局异常处理器后**仍会重抛**，
    默认 TestClient(raise_server_exceptions=True) 会把它抛进测试；
    要断言真实响应体必须关掉这个开关。
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mcp(conns, cfg):
    m, s, w = conns
    bind_app_state({"cfg": cfg, "mem_conn": m, "session_conn": s, "wiki_conn": w})
    return build_mcp_server()


# ---------- 工具 ----------

def _iso(hours_ago: float) -> str:
    """N 小时前的 UTC ISO 时间戳。"""
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_raw_file(session_conn: sqlite3.Connection, file_id: str,
                     refined_at: str | None, status: str = "refined") -> None:
    """插入 raw_files 行（直接 SQL，精确控制 refined_at / status）。"""
    session_conn.execute(
        """
        INSERT INTO raw_files
          (file_id, path, session_key, agent_id, started_at, ended_at,
           refined_at, last_refined_seq, status, size)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (file_id, f"raw/sessions/{file_id}.md", f"sess_{file_id}", "test",
         "2026-08-04T10:00:00Z", None, refined_at, 1, status, 100),
    )
    session_conn.commit()


def _insert_memory(conn: sqlite3.Connection, memory_id: str) -> None:
    memory_dao.insert_memory(
        conn, content=f"内容-{memory_id}", memory_type="fact", priority=3,
        time_velocity="static", ttl_days=None, dimension_ids=[],
        sources=None, memory_id=memory_id,
    )


def _call_mcp(mcp_server, name: str, args: dict) -> str:
    raw = asyncio.run(mcp_server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return "\n".join(c.text for c in results if getattr(c, "text", None))


# ---------- 1. 返回类型与超集结构 ----------

def test_stats_returns_operation_result_ok(conns):
    """stats() 返回 OperationResult 且 ok=True。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    # Act
    res = stats(mem_conn, session_conn)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None


def test_data_is_protocol_agnostic_superset(conns):
    """data 同时携带 HTTP 版 refinement 与 MCP 版 refinement_raw。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-1", refined_at=_iso(2))

    # Act
    data = stats(mem_conn, session_conn).data

    # Assert
    assert set(data.keys()) == {
        "memories", "raw_files", "dimension_distribution",
        "refinement", "refinement_raw", "agents",
    }
    assert list(data["refinement"].keys()) == HTTP_REFINEMENT_KEYS
    assert list(data["refinement_raw"].keys()) == MCP_REFINEMENT_KEYS
    # 两版水位取自同一次查询，last_refined_at 必须一致
    assert data["refinement"]["last_refined_at"] == data["refinement_raw"]["last_refined_at"]


# ---------- 2. 取值正确性 ----------

def test_counts_and_watermark(conns):
    """计数与水位取值正确；watermark_age_sec 复用 health 口径由 last_refined_at 算出。"""
    # Arrange：2 条记忆；3 个 raw 文件（1 refined@2h / 1 new / 1 error）
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "m-1")
    _insert_memory(mem_conn, "m-2")
    _insert_raw_file(session_conn, "f-refined", refined_at=_iso(2), status="refined")
    _insert_raw_file(session_conn, "f-new", refined_at=None, status="new")
    _insert_raw_file(session_conn, "f-err", refined_at=None, status="error")

    # Act
    data = stats(mem_conn, session_conn).data

    # Assert
    assert data["memories"] == {"total": 2, "archived": 0}
    assert data["raw_files"] == {"total": 3, "new": 1, "refined": 1, "error": 1, "archived": 0}
    # queue_depth 即 new 计数（v0.6 口径）
    assert data["refinement"]["queue_depth"] == 1
    # watermark 允许 ±120s 抖动（时间戳精确到秒 + 测试执行耗时）
    assert 7080 <= data["refinement"]["watermark_age_sec"] <= 7320


def test_watermark_none_when_never_refined(conns):
    """从未提炼 → last_refined_at 与 watermark_age_sec 均为 None（不抛异常）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-new", refined_at=None, status="new")

    # Act
    data = stats(mem_conn, session_conn).data

    # Assert
    assert data["refinement"]["last_refined_at"] is None
    assert data["refinement"]["watermark_age_sec"] is None
    assert data["refinement_raw"]["last_refined_at"] is None


def test_dimension_distribution_shape(conns, cfg):
    """维度分布来自注册表 LEFT JOIN，无记忆时计数为 0（保零，非空列表）。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    # Act
    dist = stats(mem_conn, session_conn).data["dimension_distribution"]

    # Assert
    assert len(dist) == len(cfg["dimensions"])
    assert set(dist[0].keys()) == {"id", "display_name", "count"}
    assert all(d["count"] == 0 for d in dist)


# ---------- 3. 明文 Key 收口 ----------

def test_public_agents_strips_plaintext_key():
    """🔴 _public_agents 是明文 Key 进入响应体前的唯一收口，只保留 agent_id/role。"""
    # Arrange：照抄 AgentKeyStore.list_agents() 的真实记录形态（"key" 是明文 Key）
    raw_agents = [
        {"key": "super-secret", "agent_id": "scsm", "role": "admin", "scope": ["*"]},
        {"key": "another-secret", "agent_id": "cli", "role": "agent", "scope": []},
    ]

    # Act
    out = _public_agents(raw_agents)

    # Assert
    assert out == [{"agent_id": "scsm", "role": "admin"},
                   {"agent_id": "cli", "role": "agent"}]
    assert all(list(a.keys()) == AGENT_KEYS for a in out)
    assert "super-secret" not in json.dumps(out)
    assert "another-secret" not in json.dumps(out)


@pytest.mark.parametrize("empty", [None, []])
def test_public_agents_empty(empty):
    """入参为 None / 空列表 → 返回空列表（MCP 不传 agents 的场景）。"""
    assert _public_agents(empty) == []


def test_stats_agents_default_empty(conns):
    """不传 agents（MCP 场景）→ data["agents"] 为空列表。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    # Act / Assert
    assert stats(mem_conn, session_conn).data["agents"] == []


# ---------- 4. 两端投影的历史契约差异 ----------

def test_http_and_mcp_projections_differ_as_expected(conns):
    """顶层第 2/3 键互换、refinement 形态不同、MCP 无 agents——历史差异，v0.8 待统一。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-1", refined_at=_iso(1))
    data = stats(mem_conn, session_conn,
                 agents=[{"key": "k", "agent_id": "a", "role": "agent", "scope": []}]).data

    # Act
    http_body = http_payload(data)
    mcp_body = mcp_payload(data)

    # Assert：键序差异（这是本模块最容易在"顺手对齐"时被破坏的点）
    assert list(http_body.keys()) == HTTP_TOP_KEYS
    assert list(mcp_body.keys()) == MCP_TOP_KEYS
    assert list(http_body.keys())[1] == "raw_files"
    assert list(mcp_body.keys())[1] == "dimension_distribution"

    # Assert：refinement 形态差异
    assert list(http_body["refinement"].keys()) == HTTP_REFINEMENT_KEYS
    assert list(mcp_body["refinement"].keys()) == MCP_REFINEMENT_KEYS
    assert "watermark_age_sec" not in mcp_body["refinement"]
    assert "queue_depth" not in mcp_body["refinement"]

    # Assert：agents 只有 HTTP 有
    assert "agents" not in mcp_body
    assert http_body["agents"] == [{"agent_id": "a", "role": "agent"}]

    # Assert：共有字段同源（同一次查询，不允许出现两次统计）
    assert http_body["memories"] is mcp_body["memories"]
    assert http_body["raw_files"] is mcp_body["raw_files"]
    assert (http_body["refinement"]["last_refined_at"]
            == mcp_body["refinement"]["last_refined_at"])


# ---------- 5. 契约等价性（最关键） ----------

def test_http_endpoint_contract_unchanged(client, conns):
    """GET /v1/admin/stats 经 operations 包装后，字段集合与顺序仍与 v0.6 一致。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "m-1")
    _insert_raw_file(session_conn, "f-1", refined_at=_iso(1))

    # Act
    resp = client.get("/v1/admin/stats", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_TOP_KEYS
    assert list(body["memories"].keys()) == MEMORIES_KEYS
    assert list(body["raw_files"].keys()) == RAW_FILES_KEYS
    assert list(body["refinement"].keys()) == HTTP_REFINEMENT_KEYS
    assert body["memories"]["total"] == 1
    assert body["raw_files"]["total"] == 1
    assert body["refinement"]["watermark_age_sec"] is not None
    assert isinstance(body["agents"], list)


def test_http_agents_never_leak_api_key(client, conns):
    """HTTP 统计响应里的 agents 绝不含明文 Key（端到端验证收口有效）。"""
    # Arrange：默认 key_store 至少含内置 admin/agent 两条记录
    # Act
    body = client.get("/v1/admin/stats", headers=ADMIN_HEADERS).json()

    # Assert
    for agent in body["agents"]:
        assert list(agent.keys()) == AGENT_KEYS
    assert "test-admin-key" not in json.dumps(body, ensure_ascii=False)
    assert "test-agent-key" not in json.dumps(body, ensure_ascii=False)


def test_mcp_tool_contract_unchanged(mcp, conns):
    """MCP stats 工具经 operations 包装后，字段集合与顺序仍与 v0.6 一致。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "m-1")
    _insert_raw_file(session_conn, "f-1", refined_at=_iso(1))

    # Act
    body = json.loads(_call_mcp(mcp, "stats", {}))

    # Assert
    assert list(body.keys()) == MCP_TOP_KEYS
    assert list(body["memories"].keys()) == MEMORIES_KEYS
    assert list(body["raw_files"].keys()) == RAW_FILES_KEYS
    assert list(body["refinement"].keys()) == MCP_REFINEMENT_KEYS
    assert body["memories"]["total"] == 1
    assert "agents" not in body


def test_http_and_mcp_agree_on_shared_fields(client, mcp, conns):
    """同一状态下两端共有字段取值一致（差异只在键序 / refinement / agents）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_memory(mem_conn, "m-1")
    _insert_raw_file(session_conn, "f-1", refined_at=_iso(3))

    # Act
    http_body = client.get("/v1/admin/stats", headers=ADMIN_HEADERS).json()
    mcp_body = json.loads(_call_mcp(mcp, "stats", {}))

    # Assert
    assert http_body["memories"] == mcp_body["memories"]
    assert http_body["raw_files"] == mcp_body["raw_files"]
    assert http_body["dimension_distribution"] == mcp_body["dimension_distribution"]
    assert (http_body["refinement"]["last_refined_at"]
            == mcp_body["refinement"]["last_refined_at"])


# ---------- 6. 失败路径：非预期异常不被吞 ----------

def test_dao_failure_bubbles_up(conns, monkeypatch):
    """stats_dao 抛异常时原样上抛（无 catch-all），交由入口层全局处理器兜底。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    def _boom(conn):
        raise sqlite3.OperationalError("no such table: memories")

    monkeypatch.setattr("sgme.operations.stats.stats_dao.memory_summary", _boom)

    # Act / Assert
    with pytest.raises(sqlite3.OperationalError):
        stats(mem_conn, session_conn)


def test_http_dao_failure_returns_500(lenient_client, monkeypatch):
    """DAO 故障经 HTTP 入口 → 500（沿用 v0.6 的全局异常处理器形态，非 operations 失败态）。

    关键点：run_operation **不**拦截非 InvalidArgs/OperationError 异常，
    因此响应体是全局处理器的 ``内部错误: {e}``，而不是 operations 的失败态文案。
    """
    # Arrange
    def _boom(conn):
        raise sqlite3.OperationalError("no such table: memories")

    monkeypatch.setattr("sgme.operations.stats.stats_dao.memory_summary", _boom)

    # Act
    resp = lenient_client.get("/v1/admin/stats", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 500, resp.text
    err = resp.json()["error"]
    assert err["code"] == "ERR_INTERNAL"
    assert err["message"].startswith("内部错误: ")
