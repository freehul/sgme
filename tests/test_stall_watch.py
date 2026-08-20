"""tests/test_stall_watch.py：T-12 提炼停摆告警闭环测试（0.8 P3）。

覆盖（对应任务验收）：
1. 正常推进不告警：窗口内 refine 且 last_refined_seq 推进 → stalled=False、不发 anomaly_warn
2. 水位停滞告警（时间水位）：refined_at 超阈值 → stalled=True + anomaly_warn 落库
3. 无记录视为停摆：空库 → stalled=True
4. 人为停摆可检测（序号水位空转）：refined_at 新鲜但窗口内全部 last_refined_seq ≤ 0
   → stalled=True + anomaly_warn 落库（这是纯时间水位检测不到的停摆形态）
5. anomaly_warn 载荷携带序号水位细节（seq_stalled / window_refined_count / window_max_seq）
6. 契约回归：check_refinement_stalled 返回字段集合不变
   （stalled / last_refined_at / stalled_hours / threshold_hours）
7. /v1/health 响应契约不变（HTTP 顶层与 refinement 字段逐字段保持）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as health_mod
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# ---------- 契约常量（与 test_operations_health.py 冻结值一致） ----------

HTTP_TOP_KEYS = ["status", "version", "llm", "refinement", "vector"]
HTTP_REFINEMENT_KEYS = [
    "watermark_age_sec",
    "queue_depth",
    "last_refined_at",
    "stalled",
    "stalled_hours",
    "heartbeat_ok",
]
STALLED_CONTRACT_KEYS = ["stalled", "last_refined_at", "stalled_hours", "threshold_hours"]


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


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
def llm_ok_client():
    """mock LLM 探测为可用（避免实际打云端/本地端点）。"""
    def handler(req):
        return httpx.Response(200, json={"data": []})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    yield cli
    cli.close()


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（mock LLM 探测，/v1/health 契约回归用）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "mock",
            "model": "mock-model", "error": None,
        },
    )
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


# ---------- 工具 ----------

def _iso(hours_ago: float) -> str:
    """N 小时前的 UTC ISO 时间戳。"""
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_raw_file(session_conn: sqlite3.Connection, file_id: str,
                     refined_at: str | None, seq: int | None = 1,
                     status: str = "refined") -> None:
    """插入 raw_files 行（直接 SQL，精确控制 refined_at / last_refined_seq）。"""
    session_conn.execute(
        """
        INSERT INTO raw_files
          (file_id, path, session_key, agent_id, started_at, ended_at,
           refined_at, last_refined_seq, status, size)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (file_id, f"raw/sessions/{file_id}.md", f"sess_{file_id}", "test",
         "2026-08-04T10:00:00Z", None, refined_at, seq, status, 100),
    )
    session_conn.commit()


def _count_anomaly_warns(mem_conn: sqlite3.Connection) -> int:
    """统计 signal_events 中 anomaly_warn 事件数。"""
    cur = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM signal_events WHERE type='anomaly_warn'"
    )
    return cur.fetchone()["c"]


def _latest_anomaly_payload(mem_conn: sqlite3.Connection) -> dict:
    """取最近一条 anomaly_warn 事件的载荷 dict。"""
    row = mem_conn.execute(
        "SELECT payload FROM signal_events WHERE type='anomaly_warn' "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["payload"])


# ---------- 1. 正常推进不告警 ----------

def test_healthy_progression_no_warn(conns, cfg, llm_ok_client):
    """窗口内 refine 且 seq 推进（2 / 5）→ stalled=False、heartbeat_ok=True、不发告警。"""
    # Arrange：最近 1 小时两条 refine，seq 持续推进
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-a", refined_at=_iso(1), seq=2)
    _insert_raw_file(session_conn, "f-b", refined_at=_iso(0.5), seq=5)
    before = _count_anomaly_warns(mem_conn)

    # Act
    refine_info = health_mod.check_refinement_stalled(session_conn)
    heartbeat = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=llm_ok_client)

    # Assert
    assert refine_info["stalled"] is False
    assert heartbeat["heartbeat_ok"] is True
    assert heartbeat["stalled"] is False
    assert heartbeat["seq_stalled"] is False
    assert _count_anomaly_warns(mem_conn) == before


# ---------- 2. 水位停滞告警（时间水位） ----------

def test_time_watermark_stall_warns(conns, cfg, llm_ok_client):
    """refined_at 25 小时前（超 24h 阈值）→ stalled=True + anomaly_warn 落库。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-old", refined_at=_iso(25), seq=3)
    before = _count_anomaly_warns(mem_conn)

    # Act
    refine_info = health_mod.check_refinement_stalled(session_conn)
    heartbeat = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=llm_ok_client)

    # Assert
    assert refine_info["stalled"] is True
    assert refine_info["stalled_hours"] > 24
    assert heartbeat["heartbeat_ok"] is False
    assert heartbeat["stalled"] is True
    assert _count_anomaly_warns(mem_conn) > before


# ---------- 3. 无记录视为停摆 ----------

def test_no_records_is_stalled(conns, cfg, llm_ok_client):
    """无任何 refined 记录 → stalled=True（视为停摆）+ anomaly_warn 落库。"""
    # Arrange：空库
    mem_conn, session_conn, _ = conns
    before = _count_anomaly_warns(mem_conn)

    # Act
    refine_info = health_mod.check_refinement_stalled(session_conn)
    heartbeat = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=llm_ok_client)

    # Assert
    assert refine_info["stalled"] is True
    assert refine_info["last_refined_at"] is None
    assert refine_info["stalled_hours"] is None
    assert heartbeat["heartbeat_ok"] is False
    assert _count_anomaly_warns(mem_conn) > before


# ---------- 4. 人为停摆可检测（序号水位空转） ----------

def test_seq_spin_stall_detected(conns, cfg, llm_ok_client):
    """refined_at 新鲜但窗口内全部 last_refined_seq ≤ 0 → 空转停摆 stalled=True。"""
    # Arrange：模拟人为停摆——refine 动作在跑（refined_at 新鲜）但 seq 零推进
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-spin1", refined_at=_iso(1), seq=0)
    _insert_raw_file(session_conn, "f-spin2", refined_at=_iso(0.5), seq=0)

    # Act
    refine_info = health_mod.check_refinement_stalled(session_conn)
    heartbeat = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=llm_ok_client)

    # Assert：时间水位其实新鲜（stalled_hours 很小），停摆来自序号水位空转
    assert refine_info["stalled"] is True
    assert refine_info["stalled_hours"] <= 2
    assert heartbeat["heartbeat_ok"] is False
    assert heartbeat["stalled"] is True
    assert heartbeat["seq_stalled"] is True


def test_seq_advance_but_time_fresh_no_warn(conns, cfg, llm_ok_client):
    """对照：同窗口内只要有一条 seq > 0 → 不判空转（避免误报）。"""
    # Arrange：一条空转（seq=0）+ 一条正常推进（seq=7）
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-spin", refined_at=_iso(1), seq=0)
    _insert_raw_file(session_conn, "f-ok", refined_at=_iso(0.5), seq=7)
    before = _count_anomaly_warns(mem_conn)

    # Act
    refine_info = health_mod.check_refinement_stalled(session_conn)

    # Assert
    assert refine_info["stalled"] is False
    assert _count_anomaly_warns(mem_conn) == before


# ---------- 5. anomaly_warn 载荷携带序号水位细节 ----------

def test_anomaly_payload_carries_seq_detail(conns, cfg, llm_ok_client):
    """seq 空转停摆 → signal_events 落库的载荷含 seq_stalled / window_* 字段。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-payload", refined_at=_iso(1), seq=0)

    # Act
    health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=llm_ok_client)
    payload = _latest_anomaly_payload(mem_conn)

    # Assert
    assert payload["stalled"] is True
    assert payload["seq_stalled"] is True
    assert payload["window_refined_count"] >= 1
    assert payload["window_max_seq"] == 0
    assert payload["window_hours"] == 24
    # 时间水位字段仍在（历史载荷字段不动）
    assert "last_refined_at" in payload
    assert "queue_depth" in payload


# ---------- 6. 契约回归：check_refinement_stalled 字段集合不变 ----------

@pytest.mark.parametrize("scenario", ["healthy", "time_stall", "seq_stall"])
def test_stalled_contract_keys_unchanged(conns, scenario):
    """三种状态下返回字段集合与顺序均保持冻结契约。"""
    # Arrange
    _mem_conn, session_conn, _ = conns
    if scenario == "healthy":
        _insert_raw_file(session_conn, "f-ok", refined_at=_iso(1), seq=1)
    elif scenario == "time_stall":
        _insert_raw_file(session_conn, "f-old", refined_at=_iso(25), seq=1)
    else:  # seq_stall：refined_at 新鲜但 seq 空转
        _insert_raw_file(session_conn, "f-spin", refined_at=_iso(1), seq=0)

    # Act
    result = health_mod.check_refinement_stalled(session_conn)

    # Assert：键序即契约（MCP health 工具原始透传该 dict）
    assert list(result.keys()) == STALLED_CONTRACT_KEYS
    if scenario == "healthy":
        assert result["stalled"] is False
    else:
        assert result["stalled"] is True


def test_heartbeat_return_carries_seq_fields(conns, cfg, llm_ok_client):
    """check_heartbeat 返回含附加 seq 字段（历史字段不变，仅增量）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-ok", refined_at=_iso(1), seq=1)

    # Act
    heartbeat = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=llm_ok_client)

    # Assert：历史字段
    for key in ("llm", "refinement", "queue_depth", "heartbeat_ok", "stalled"):
        assert key in heartbeat
    # 附加字段
    assert heartbeat["seq_stalled"] is False
    assert heartbeat["seq_progression"]["progressed"] is True
    assert heartbeat["seq_progression"]["window_hours"] == 24


# ---------- 7. /v1/health 响应契约不变 ----------

def test_health_endpoint_contract_intact_under_seq_stall(client, conns):
    """seq 空转停摆态下 /v1/health 字段集合与顺序不变，stalled=True 正确透传。"""
    # Arrange：人为停摆（seq 空转）
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-http-spin", refined_at=_iso(1), seq=0)

    # Act
    resp = client.get("/v1/health")

    # Assert：契约逐字段保持
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_TOP_KEYS
    assert list(body["refinement"].keys()) == HTTP_REFINEMENT_KEYS
    assert body["refinement"]["stalled"] is True
    assert body["refinement"]["heartbeat_ok"] is False
    assert body["refinement"]["watermark_age_sec"] is not None  # refined_at 新鲜，仍可算
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0b4"


# ---------- 8. check_seq_progression 单元 ----------

def test_seq_progression_ignores_old_refines(conns):
    """窗口外（30h 前）的 refine 不计入窗口 → progressed=False。"""
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-old", refined_at=_iso(30), seq=9)

    # Act
    info = health_mod.check_seq_progression(session_conn, window_hours=24)

    # Assert
    assert info["progressed"] is False
    assert info["window_refined_count"] == 0
    assert info["window_max_seq"] == 0


def test_seq_progression_true_when_seq_advances(conns):
    """窗口内存在 seq > 0 的 refine → progressed=True。"""
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-a", refined_at=_iso(2), seq=7)

    # Act
    info = health_mod.check_seq_progression(session_conn, window_hours=24)

    # Assert
    assert info["progressed"] is True
    assert info["window_refined_count"] == 1
    assert info["window_max_seq"] == 7


def test_seq_progression_false_when_all_zero(conns):
    """窗口内 refine 但全部 seq ≤ 0 → progressed=False（空转）。"""
    _mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-spin", refined_at=_iso(1), seq=0)

    # Act
    info = health_mod.check_seq_progression(session_conn, window_hours=24)

    # Assert
    assert info["progressed"] is False
    assert info["window_refined_count"] == 1
    assert info["window_max_seq"] == 0


def test_seq_progression_empty_db(conns):
    """空库 → progressed=False，不抛异常。"""
    _mem_conn, session_conn, _ = conns

    # Act
    info = health_mod.check_seq_progression(session_conn, window_hours=24)

    # Assert
    assert info["progressed"] is False
    assert info["window_refined_count"] == 0
    assert info["window_max_seq"] == 0
