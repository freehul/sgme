"""T15 测试：提炼可观测性增强（health 模块 + /v1/health 扩展字段）。

覆盖：
- check_refinement_stalled：1h 前 → False；25h 前 → True；无记录 → True（停摆）
- check_llm_available：mock httpx 200 → True；mock ConnectError → False
- check_heartbeat：全部正常 → ok=True 不发 anomaly_warn；
  停滞 → ok=False 发 anomaly_warn；
  LLM 不可达 → ok=False 发 anomaly_warn
- /v1/health：返回含 refinement.stalled / refinement.heartbeat_ok / llm.available
- /v1/health 开启 Bearer 后无 Bearer → 401
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme import __version__  # health version 契约动态断言（B123 单源化）
from sgme.engine import health as health_mod
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao, signal_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（避免 /v1/health 测试触发未隔离 raw_store）。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def app(tmp_path, cfg, raw_dir, monkeypatch):
    """创建隔离的 FastAPI 应用 + mock LLM 探测（避免实际打 127.0.0.1:1014）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # mock LLM 探测：check_heartbeat 内部 check_llm_available → 返回 available=True
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 时间工具 ----------

def _iso(hours_ago: float) -> str:
    """N 小时前的 UTC ISO 时间戳。"""
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_raw_file(session_conn, file_id: str, refined_at: str | None, status: str = "refined"):
    """插入 raw_files 行（直接 SQL，便于精确控制 refined_at）。"""
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


# ---------- 1. check_refinement_stalled ----------

def test_check_refinement_stalled_ok(session_conn):
    """refined_at 在 1 小时前 → stalled=False。"""
    # Arrange
    _insert_raw_file(session_conn, "f-ok", refined_at=_iso(1))

    # Act
    result = health_mod.check_refinement_stalled(session_conn, threshold_hours=24)

    # Assert
    assert result["stalled"] is False
    assert result["last_refined_at"] == _iso(1) or result["last_refined_at"] is not None
    assert result["stalled_hours"] is not None
    assert 0 <= result["stalled_hours"] <= 2
    assert result["threshold_hours"] == 24


def test_check_refinement_stalled_true(session_conn):
    """refined_at 在 25 小时前 → stalled=True。"""
    # Arrange
    _insert_raw_file(session_conn, "f-stalled", refined_at=_iso(25))

    # Act
    result = health_mod.check_refinement_stalled(session_conn, threshold_hours=24)

    # Assert
    assert result["stalled"] is True
    assert result["last_refined_at"] is not None
    assert result["stalled_hours"] > 24
    assert result["threshold_hours"] == 24


def test_check_refinement_stalled_no_records(session_conn):
    """无任何 refined 记录 → stalled=True（视为停摆）。"""
    # Act（session_conn 为空库）
    result = health_mod.check_refinement_stalled(session_conn, threshold_hours=24)

    # Assert
    assert result["stalled"] is True
    assert result["last_refined_at"] is None
    assert result["stalled_hours"] is None
    assert result["threshold_hours"] == 24


# ---------- 2. check_llm_available ----------

def test_check_llm_available_ok(cfg):
    """mock httpx 200 → available=True。"""
    # Arrange：mock httpx.Client.get 返回 200
    def handler(req):
        return httpx.Response(200, json={"data": []})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    # Act
    result = health_mod.check_llm_available(cfg, client=cli)

    # Assert
    assert result["available"] is True
    # v0.5 主模型已切云端：provider 名随配置首链（当前为 deepseek），不写死 lm-studio
    assert result["provider"] == cfg["llm"]["chains"]["refinement"][0]["provider"]
    assert result["model"] is not None
    assert result["error"] is None
    cli.close()


def test_check_llm_available_unreachable(cfg):
    """mock httpx ConnectError → available=False。"""
    # Arrange：mock httpx.Client.get 抛 ConnectError
    def handler(req):
        raise httpx.ConnectError("connection refused")

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    # Act
    result = health_mod.check_llm_available(cfg, client=cli)

    # Assert
    assert result["available"] is False
    assert result["provider"] == cfg["llm"]["chains"]["refinement"][0]["provider"]
    assert result["error"] is not None
    assert "连接错误" in result["error"]
    cli.close()


# ---------- 3. check_heartbeat ----------

def _count_anomaly_warns(mem_conn) -> int:
    """统计 signal_events 中 anomaly_warn 事件数。"""
    cur = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM signal_events WHERE type='anomaly_warn'"
    )
    return cur.fetchone()["c"]


def test_check_heartbeat_ok(mem_conn, session_conn, cfg):
    """全部正常 → heartbeat_ok=True，不发 anomaly_warn。"""
    # Arrange：refined_at 1 小时前 + LLM 可用
    _insert_raw_file(session_conn, "f-ok", refined_at=_iso(1))

    def handler(req):
        return httpx.Response(200, json={"data": []})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    # Act
    result = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli)

    # Assert
    assert result["heartbeat_ok"] is True
    assert result["stalled"] is False
    assert result["llm"]["available"] is True
    assert result["queue_depth"] == 0  # status='refined'，无 new
    assert _count_anomaly_warns(mem_conn) == 0
    cli.close()


def test_check_heartbeat_stalled_warns(mem_conn, session_conn, cfg):
    """停滞 → heartbeat_ok=False，signal_events 出现 anomaly_warn。"""
    # Arrange：refined_at 25 小时前 + LLM 可用
    _insert_raw_file(session_conn, "f-stalled", refined_at=_iso(25))

    def handler(req):
        return httpx.Response(200, json={"data": []})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    # Act
    result = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli)

    # Assert
    assert result["heartbeat_ok"] is False
    assert result["stalled"] is True
    assert result["llm"]["available"] is True
    assert _count_anomaly_warns(mem_conn) >= 1
    cli.close()


def test_check_heartbeat_llm_down_warns(mem_conn, session_conn, cfg):
    """LLM 不可达 → heartbeat_ok=False，发 anomaly_warn。"""
    # Arrange：refined_at 1 小时前 + LLM 不可达
    _insert_raw_file(session_conn, "f-llm-down", refined_at=_iso(1))

    def handler(req):
        raise httpx.ConnectError("connection refused")

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    # Act
    result = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli)

    # Assert
    assert result["heartbeat_ok"] is False
    assert result["stalled"] is False  # refined 时间正常
    assert result["llm"]["available"] is False
    assert _count_anomaly_warns(mem_conn) >= 1
    cli.close()


# ---------- 3.5 T-125：anomaly_warn 同状态去重 ----------

def test_check_heartbeat_duplicate_warn_suppressed(mem_conn, session_conn, cfg):
    """同状态重复告警 → 窗口内抑制：连续两次异常心跳只落 1 条 anomaly_warn。"""
    _insert_raw_file(session_conn, "f-dup", refined_at=_iso(25))  # stalled=True

    def handler(req):
        return httpx.Response(200, json={"data": []})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    r1 = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli)
    assert r1["heartbeat_ok"] is False
    assert _count_anomaly_warns(mem_conn) == 1  # 首次照常发布
    r2 = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli)
    assert r2["heartbeat_ok"] is False
    assert _count_anomaly_warns(mem_conn) == 1  # 同状态窗口内 → 抑制，未新增
    cli.close()


def test_check_heartbeat_state_change_publishes(mem_conn, session_conn, cfg):
    """状态变化（stalled/llm_available 任一变化）→ 窗口内也照常发布。"""
    _insert_raw_file(session_conn, "f-chg1", refined_at=_iso(1))  # 不 stalled

    def down_handler(req):
        raise httpx.ConnectError("connection refused")

    cli_down = httpx.Client(transport=httpx.MockTransport(down_handler), trust_env=False)
    r1 = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli_down)
    assert r1["stalled"] is False and r1["llm"]["available"] is False
    assert _count_anomaly_warns(mem_conn) == 1
    cli_down.close()

    # 状态变化：LLM 恢复但提炼停摆（stalled False→True）——改写既有行 refined_at
    # 至 25h 前（MAX(refined_at) 才会越过 24h 阈值判停摆）
    session_conn.execute(
        "UPDATE raw_files SET refined_at=? WHERE file_id='f-chg1'",
        (_iso(25),),
    )
    session_conn.commit()

    def ok_handler(req):
        return httpx.Response(200, json={"data": []})

    cli_ok = httpx.Client(transport=httpx.MockTransport(ok_handler), trust_env=False)
    r2 = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli_ok)
    assert r2["stalled"] is True and r2["llm"]["available"] is True
    assert _count_anomaly_warns(mem_conn) == 2  # 状态变化 → 照常发布
    cli_ok.close()


def test_check_heartbeat_duplicate_after_window_publishes(mem_conn, session_conn, cfg):
    """同状态但距上次已超抑制窗口（1800s）→ 照常发布。"""
    _insert_raw_file(session_conn, "f-win", refined_at=_iso(25))  # stalled=True
    # 预置 1 小时前（> 1800s 窗口）同状态的 anomaly_warn
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    signal_dao.insert_event(
        conn=mem_conn,
        event_id="old-warn",
        event_type="anomaly_warn",
        source="health",
        payload_json=json.dumps({"stalled": True, "llm_available": True}),
        ts=old_ts,
    )

    def handler(req):
        return httpx.Response(200, json={"data": []})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = health_mod.check_heartbeat(mem_conn, session_conn, cfg, client=cli)
    assert result["heartbeat_ok"] is False
    assert _count_anomaly_warns(mem_conn) == 2  # 旧事件 + 新发布
    cli.close()


def test_signal_dao_get_recent_event(mem_conn):
    """get_recent_event 返回同源同类型最近一条完整行（含 ts/payload）。"""
    signal_dao.insert_event(
        mem_conn, "e1", "anomaly_warn", "health",
        json.dumps({"stalled": True}), ts="2026-08-30T01:00:00Z",
    )
    signal_dao.insert_event(
        mem_conn, "e2", "anomaly_warn", "health",
        json.dumps({"stalled": True, "llm_available": False}),
        ts="2026-08-30T02:00:00Z",
    )
    signal_dao.insert_event(
        mem_conn, "e3", "anomaly_warn", "other",
        json.dumps({"stalled": True}), ts="2026-08-30T03:00:00Z",
    )
    row = signal_dao.get_recent_event(mem_conn, "anomaly_warn", "health")
    assert row is not None
    assert row["event_id"] == "e2"
    assert row["ts"] == "2026-08-30T02:00:00Z"
    assert json.loads(row["payload"])["llm_available"] is False
    # 无匹配 → None
    assert signal_dao.get_recent_event(mem_conn, "anomaly_warn", "nope") is None


# ---------- 4. /v1/health 端点 ----------

def test_health_endpoint_returns_full_fields(client):
    """/v1/health 返回含 refinement.stalled / refinement.heartbeat_ok / llm.available。"""
    # Act（app fixture 已 mock LLM 探测为 available=True）
    resp = client.get("/v1/health")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    # llm 字段
    assert "available" in body["llm"]
    assert "provider" in body["llm"]
    assert "model" in body["llm"]
    # refinement 字段
    assert "watermark_age_sec" in body["refinement"]
    assert "queue_depth" in body["refinement"]
    assert "last_refined_at" in body["refinement"]
    assert "stalled" in body["refinement"]
    assert "heartbeat_ok" in body["refinement"]
    assert "stalled_hours" in body["refinement"]


def test_health_endpoint_no_key_401(tmp_path, cfg, raw_dir, monkeypatch):
    """开启 Bearer 后无 Bearer → 401。"""
    # Arrange：通过 monkeypatch.setenv 设置 Bearer（避免 app.py 的 os.environ.setdefault 泄漏）
    monkeypatch.setenv("SGME_BEARER_TOKEN", "secret-bearer")
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # mock LLM 探测，避免实际打 127.0.0.1:1014
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock", "error": None,
        },
    )
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="a", agent_key="b",
    )
    try:
        c = TestClient(app)
        # 无 Bearer → 401
        r1 = c.get("/v1/health")
        assert r1.status_code == 401
        # 错误 Bearer → 401
        r2 = c.get("/v1/health", headers={"Authorization": "Bearer wrong"})
        assert r2.status_code == 401
        # 正确 Bearer → 200
        r3 = c.get("/v1/health", headers={"Authorization": "Bearer secret-bearer"})
        assert r3.status_code == 200
    finally:
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)
