"""tests/test_operations_health.py：operations 层 health 样板测试（v0.7 P2-T3）。

覆盖：
1. operations.health() 返回 OperationResult(ok=True)
2. data 的 HTTP 形态字段完整 + watermark_age_sec 由 last_refined_at 正确计算
3. last_refined_at 为 None → watermark_age_sec 为 None（不抛异常）
4. last_refined_at 格式非法 → watermark_age_sec 为 None（ValueError/TypeError 容错保留）
5. MCP 形态与 HTTP 形态的历史差异符合预期（原始 refinement 无 watermark_age_sec）
6. **契约等价性**（最关键）：/v1/health 与 MCP health 工具经 operations 包装后，
   字段集合与顺序仍与 v0.6 逐字段一致
7. 副作用保留：心跳异常仍发布 anomaly_warn 信号
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as engine_health
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.errors import OperationResult
from sgme.operations.health import SGME_VERSION, _watermark_age_sec, health, http_payload, mcp_payload
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao, stats_dao

# ---------- v0.6 冻结契约（改造前逐字段抄录，任何变动即破坏性变更） ----------
# ST-22② 有意新增顶层 vector 字段（只增不改既有字段）——列表随之更新。

HTTP_TOP_KEYS = ["status", "version", "llm", "refinement", "vector", "model_config", "update_available", "latest_version", "update_checked_at", "update_error"]  # T-53：Key 缺失引导（只增不改既有字段）；ST-34：自动更新检测（只增不改既有字段）
HTTP_REFINEMENT_KEYS = [
    "watermark_age_sec",
    "queue_depth",
    "last_refined_at",
    "stalled",
    "stalled_hours",
    "heartbeat_ok",
]
MCP_TOP_KEYS = ["status", "version", "llm", "refinement"]
# MCP 的 refinement 是 engine check_refinement_stalled 的原始返回
MCP_REFINEMENT_KEYS = ["stalled", "last_refined_at", "stalled_hours", "threshold_hours"]


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def mock_llm(monkeypatch):
    """mock LLM 探测为可用（避免实际打 127.0.0.1:1014）。

    注意：monkeypatch 的是 engine.health 模块全局 check_llm_available，
    check_heartbeat 内部按模块全局解析，因此 operations 层调用同样生效。
    """
    monkeypatch.setattr(
        engine_health, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, raw_dir, mock_llm, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mcp(conns, cfg, raw_dir, mock_llm):
    """绑定同一批连接的 MCP server。"""
    mem_conn, session_conn, wiki_conn = conns
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
    })
    return build_mcp_server()


# ---------- 工具 ----------

def _iso(hours_ago: float) -> str:
    """N 小时前的 UTC ISO 时间戳。"""
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_raw_file(session_conn: sqlite3.Connection, file_id: str,
                     refined_at: str | None, status: str = "refined") -> None:
    """插入 raw_files 行（直接 SQL，精确控制 refined_at）。"""
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


def _count_anomaly_warns(mem_conn: sqlite3.Connection) -> int:
    """统计 signal_events 中 anomaly_warn 事件数。"""
    cur = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM signal_events WHERE type='anomaly_warn'"
    )
    return cur.fetchone()["c"]


def _call_mcp(mcp_server, name: str, args: dict) -> str:
    """同步包装 async call_tool → 返回文本。"""
    raw = asyncio.run(mcp_server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return "\n".join(c.text for c in results if getattr(c, "text", None))


# ---------- 1. 返回类型 ----------

def test_health_returns_operation_result_ok(conns, cfg, mock_llm):
    """operations.health() 返回 OperationResult 且 ok=True。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-ok", refined_at=_iso(1))

    # Act
    res = health(mem_conn, session_conn, cfg)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert isinstance(res.data, dict)


# ---------- 2. HTTP 形态字段完整性 + watermark 计算 ----------

def test_data_http_shape_complete(conns, cfg, mock_llm):
    """data["refinement"]（HTTP 形态）字段齐全，watermark_age_sec 由 last_refined_at 算出。"""
    # Arrange：2 小时前提炼 → watermark ≈ 7200s
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-2h", refined_at=_iso(2))

    # Act
    data = health(mem_conn, session_conn, cfg).data

    # Assert：字段集合与顺序
    assert list(data["refinement"].keys()) == HTTP_REFINEMENT_KEYS
    # watermark 允许 ±120s 抖动（时间戳精确到秒 + 测试执行耗时）
    assert 7080 <= data["refinement"]["watermark_age_sec"] <= 7320
    assert data["refinement"]["queue_depth"] == 0
    assert data["refinement"]["stalled"] is False
    assert data["refinement"]["heartbeat_ok"] is True
    assert data["status"] == "ok"
    assert data["version"] == SGME_VERSION == "1.1.0"
    assert data["llm"]["available"] is True


def test_watermark_helper_computes_seconds():
    """_watermark_age_sec：正常 ISO 时间戳 → 秒数。"""
    # Arrange / Act
    age = _watermark_age_sec(_iso(1))

    # Assert
    assert age is not None
    assert 3540 <= age <= 3660


# ---------- 3. last_refined_at 为 None ----------

def test_watermark_none_when_no_refined_record(conns, cfg, mock_llm):
    """无任何 refined 记录 → last_refined_at=None，watermark_age_sec=None（不抛异常）。"""
    # Arrange：库中无 raw_files 行
    mem_conn, session_conn, _ = conns

    # Act
    data = health(mem_conn, session_conn, cfg).data

    # Assert
    assert data["refinement"]["last_refined_at"] is None
    assert data["refinement"]["watermark_age_sec"] is None
    # 无提炼记录按 engine 口径视为停摆
    assert data["refinement"]["stalled"] is True
    assert data["refinement"]["heartbeat_ok"] is False


@pytest.mark.parametrize("bad", [None, ""])
def test_watermark_helper_none_on_empty(bad):
    """_watermark_age_sec：空值 → None。"""
    assert _watermark_age_sec(bad) is None


# ---------- 4. last_refined_at 格式非法 ----------

def test_watermark_none_when_malformed_timestamp(conns, cfg, mock_llm):
    """refined_at 格式非法 → watermark_age_sec=None，整体不抛异常（容错保留）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-bad", refined_at="not-a-timestamp")

    # Act
    res = health(mem_conn, session_conn, cfg)

    # Assert
    assert res.ok is True
    assert res.data["refinement"]["last_refined_at"] == "not-a-timestamp"
    assert res.data["refinement"]["watermark_age_sec"] is None


@pytest.mark.parametrize("bad", ["not-a-timestamp", "2026-13-45T99:99:99Z", "20260804"])
def test_watermark_helper_none_on_malformed(bad):
    """_watermark_age_sec：非法格式 → None（catch ValueError/TypeError）。"""
    assert _watermark_age_sec(bad) is None


# ---------- 5. MCP 形态与 HTTP 形态的历史差异 ----------

def test_mcp_shape_differs_from_http_as_expected(conns, cfg, mock_llm):
    """MCP 形态是 engine 原始 refinement：无 watermark_age_sec/queue_depth/heartbeat_ok，有 threshold_hours。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-diff", refined_at=_iso(1))

    # Act
    data = health(mem_conn, session_conn, cfg).data
    http_body = http_payload(data)
    mcp_body = mcp_payload(data)

    # Assert：HTTP 形态含 ST-22② 新增的 vector 顶层键；MCP 形态保持 v0.6 原样
    # （MCP health 契约冻结，向量字段属另一任务的议题——两端顶层键由此有意分叉）
    assert list(http_body.keys()) == HTTP_TOP_KEYS
    assert list(mcp_body.keys()) == MCP_TOP_KEYS
    assert list(mcp_body["refinement"].keys()) == MCP_REFINEMENT_KEYS
    assert "watermark_age_sec" not in mcp_body["refinement"]
    assert "queue_depth" not in mcp_body["refinement"]
    assert "heartbeat_ok" not in mcp_body["refinement"]
    assert "threshold_hours" in mcp_body["refinement"]
    assert "threshold_hours" not in http_body["refinement"]
    # 两端共有字段取值必须相同（同一次心跳，不允许出现两次探测）
    assert http_body["refinement"]["stalled"] == mcp_body["refinement"]["stalled"]
    assert http_body["refinement"]["last_refined_at"] == mcp_body["refinement"]["last_refined_at"]
    assert http_body["llm"] is mcp_body["llm"]


# ---------- 6. 契约等价性（最关键） ----------

def test_http_endpoint_contract_unchanged(client, conns):
    """GET /v1/health 经 operations 包装后，字段集合与顺序仍与 v0.6 一致。"""
    # Arrange
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-http", refined_at=_iso(1))

    # Act
    resp = client.get("/v1/health")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_TOP_KEYS
    assert list(body["refinement"].keys()) == HTTP_REFINEMENT_KEYS
    assert body["status"] == "ok"
    assert body["version"] == "1.1.0"
    assert set(body["llm"].keys()) == {"available", "provider", "model", "error"}
    assert body["refinement"]["watermark_age_sec"] is not None
    assert body["refinement"]["heartbeat_ok"] is True


def test_mcp_tool_contract_unchanged(mcp, conns):
    """MCP health 工具经 operations 包装后，字段集合与顺序仍与 v0.6 一致。"""
    # Arrange
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-mcp", refined_at=_iso(1))

    # Act
    text = _call_mcp(mcp, "health", {})
    body = json.loads(text)

    # Assert
    assert list(body.keys()) == MCP_TOP_KEYS
    assert list(body["refinement"].keys()) == MCP_REFINEMENT_KEYS
    assert body["status"] == "ok"
    assert body["version"] == "1.1.0"
    assert body["llm"]["available"] is True


def test_http_and_mcp_agree_on_shared_fields(client, mcp, conns):
    """同一状态下两端输出的共有字段取值一致（差异只在 refinement 形态）。"""
    # Arrange
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-both", refined_at=_iso(3))

    # Act
    http_body = client.get("/v1/health").json()
    mcp_body = json.loads(_call_mcp(mcp, "health", {}))

    # Assert
    assert http_body["status"] == mcp_body["status"]
    assert http_body["version"] == mcp_body["version"]
    assert http_body["llm"] == mcp_body["llm"]
    assert http_body["refinement"]["stalled"] == mcp_body["refinement"]["stalled"]
    assert (http_body["refinement"]["last_refined_at"]
            == mcp_body["refinement"]["last_refined_at"])


# ---------- 7. 副作用保留：anomaly_warn ----------

def test_health_still_publishes_anomaly_warn(conns, cfg, mock_llm):
    """心跳异常（无 refined 记录 → stalled）时仍发布 anomaly_warn 信号。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    before = _count_anomaly_warns(mem_conn)

    # Act
    res = health(mem_conn, session_conn, cfg)

    # Assert
    assert res.data["refinement"]["heartbeat_ok"] is False
    assert _count_anomaly_warns(mem_conn) > before


def test_health_no_warn_when_healthy(conns, cfg, mock_llm):
    """一切正常时不发 anomaly_warn。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-healthy", refined_at=_iso(1))
    before = _count_anomaly_warns(mem_conn)

    # Act
    res = health(mem_conn, session_conn, cfg)

    # Assert
    assert res.data["refinement"]["heartbeat_ok"] is True
    assert _count_anomaly_warns(mem_conn) == before


# ---------- 8. 向量可用性（ST-22②） ----------

def _seed_vector(mem_conn: sqlite3.Connection, memory_id: str = "m-vec") -> None:
    """插入一条记忆 + 对应向量行（FK 满足 + memory_vectors 计数可控）。"""
    memory_dao.insert_memory(
        mem_conn, content="向量测试记忆", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"], memory_id=memory_id,
    )
    memory_dao.upsert_vector(mem_conn, memory_id, b"\x00" * 16, "mock-model", 4)


def test_vector_available_sqlite_vec(conns, cfg, mock_llm, monkeypatch):
    """sqlite-vec 可用 + 有向量数据 → available=True / engine=sqlite-vec / reason=None。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _seed_vector(mem_conn)
    monkeypatch.setattr("sgme.data.search.vector.try_load_vec_extension", lambda conn: True)

    # Act
    res = health(mem_conn, session_conn, cfg)
    data = res.data or {}

    # Assert
    v = data["vector"]
    assert v["available"] is True
    assert v["engine"] == "sqlite-vec"
    assert v["memory_vectors"] == 1
    assert v["scene_vectors"] == 0
    assert v["reason"] is None


def test_stats_dao_count_vector_rows(conns, cfg, mock_llm):
    """data/stats_dao.count_vector_rows 直测（T-9 收口）：有向量行 + 缺表按 0。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _seed_vector(mem_conn)

    # Act
    counts = stats_dao.count_vector_rows(mem_conn)

    # Assert
    assert counts == {"memory_vectors": 1, "scene_vectors": 0}
    # 永不抛异常：不存在的表名也按 0 计
    assert "memory_vectors" in stats_dao.count_vector_rows(mem_conn)


def test_vector_numpy_fallback(conns, cfg, mock_llm, monkeypatch):
    """sqlite-vec 不可用 → 降级 numpy：available=True + 中文降级原因 + 空库引导。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    monkeypatch.setattr("sgme.data.search.vector.try_load_vec_extension", lambda conn: False)

    # Act
    res = health(mem_conn, session_conn, cfg)
    v = (res.data or {})["vector"]

    # Assert
    assert v["available"] is True
    assert v["engine"] == "numpy-fallback"
    assert "numpy" in v["reason"]
    assert "向量数据" in v["reason"]  # 空库 → 附加「无向量数据回退 BM25」引导


def test_vector_probe_error_never_raises(conns, cfg, mock_llm, monkeypatch):
    """探测抛异常 → available=False + 原因，health 仍 ok（永不抛异常）。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    def _boom(conn):
        raise RuntimeError("模拟向量探测崩溃")

    monkeypatch.setattr("sgme.data.search.vector.try_load_vec_extension", _boom)

    # Act
    res = health(mem_conn, session_conn, cfg)

    # Assert
    assert res.ok is True
    v = (res.data or {})["vector"]
    assert v["available"] is False
    assert v["engine"] == "unavailable"
    assert "向量可用性探测失败" in v["reason"]


def test_vector_field_in_http_response(client, conns):
    """HTTP /v1/health 顶层含 vector 字段（ST-22② 验收：可用/不可用 + 原因）。"""
    # Arrange
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-vec-http", refined_at=_iso(1))

    # Act
    body = client.get("/v1/health").json()

    # Assert：字段齐全；本机测试环境已安装 sqlite_vec → 引擎可用
    assert "vector" in body
    assert set(body["vector"].keys()) == {
        "available", "engine", "memory_vectors", "scene_vectors", "reason",
        "connectivity",  # T-53 2026-08-18：向量模型连通性（只增不改既有字段）
    }
    assert "available" in body["vector"]["connectivity"]
    assert body["vector"]["available"] is True
    assert body["vector"]["engine"] == "sqlite-vec"
    assert body["vector"]["memory_vectors"] == 0
    assert "向量数据" in body["vector"]["reason"]
