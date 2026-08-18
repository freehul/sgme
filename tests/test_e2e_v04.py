"""T17 端到端验收测试：v0.4 完整链路 mock LLM 全链路（TestClient + mock LLM）。

覆盖：
- test_e2e_full_pipeline：完整链路
    append → refine/trigger（含 L2 CREATE）→ 验证 L2 场景生成
    → tier0/refresh → inject（tier0.present=True）→ search → events/pull（memory_updated）
    → health（可观测性字段）→ backup/create → backup/restore
- test_e2e_search_with_vector_rrf：造记忆 + 向量 → search 返回 RRF 融合结果
- test_e2e_signal_flow：refine → events/pull 收到 memory_updated + anomaly_warn（如触发）

mock LLM 用 httpx.MockTransport 拦截 /chat/completions 与 /embeddings。
所有测试 tmp_path 隔离，不污染项目 data/ 与 raw/。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as health_mod
from sgme.engine import l15 as l15_mod
from sgme.engine import l2 as l2_mod
from sgme.engine import refine as refine_mod
from sgme.profile import tier0 as tier0_mod
from sgme.raw import store as raw_store
from sgme.data.search import vector as vector_mod
from sgme.server.app import create_app
from sgme.signal import engine as signal_engine
from sgme.data import db as db_mod
from sgme.data import memory_dao, scene_dao, session_dao


# ---------- 公共 fixtures ----------

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
def summary_path(tmp_path, monkeypatch):
    """隔离 tier0_summary.json 路径到 tmp_path。"""
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


@pytest.fixture
def app(tmp_path, cfg, raw_dir, summary_path, monkeypatch):
    """创建隔离的 FastAPI 应用。

    - data_dir / raw_dir / tier0_summary 路径全部隔离到 tmp_path
    - mock LLM 探测（避免 /v1/health 实际打 127.0.0.1:1014）
    - 备份目录指向 tmp_path（避免污染项目 data/backups）
    - 清理 SGME_BEARER_TOKEN 环境变量，防止其他测试污染
    """
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    # mock LLM 探测：避免 health 实际打 127.0.0.1:1014
    monkeypatch.setattr(
        health_mod, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )
    # 备份配置覆盖到 tmp_path
    cfg["backup"] = {
        "dir": str(tmp_path / "backups"),
        "schedule": "0 2 * * *",
        "raw_cold_days": 90,
        "remote_dir": None,
    }
    # data_dir 与连接的库目录对齐：backup manager 从 cfg["paths"]["data_dir"]
    # 取库路径（routes_backup），不覆盖会直连全局 data/ 撞 Gateway 的
    # memory.db-wal 锁（WinError 32，GLM 报告的 test_e2e_full_pipeline 根因）
    cfg["paths"]["data_dir"] = str(tmp_path / "data")
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        data_dir=tmp_path / "data",
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    try:
        db_mod.close(mem_conn)
    except Exception:
        pass
    try:
        db_mod.close(wiki_conn)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _stop_backup_scheduler_after():
    """每个测试后停止 backup_scheduler 常驻线程（防跨文件连接泄漏）。

    backup/create 端点会拉起常驻 daemon 线程（自建连接）；若不在测试结束时
    停止，线程会持有 tmp 库文件句柄导致清理失败（PermissionError WinError 32），
    或在其他测试 teardown 后访问已关闭连接 → Windows access violation。
    """
    yield
    from sgme.engine import backup_scheduler
    backup_scheduler.stop_scheduler(timeout=2.0)


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg_text(role: str, content: str, ts: str | None = None) -> str:
    ts = ts or _now_iso()
    return f"# {ts} {role}\n{content}\n"


# ---------- mock LLM 工具 ----------

def _mock_llm_client(response_text: str) -> httpx.Client:
    """构造 mock httpx 客户端，对所有 /chat/completions 请求返回固定文本。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_text}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_client_sequence(bodies: list[str]) -> httpx.Client:
    """按顺序返回多个 chat/completions 响应（L1 + L2 串联测试用）。"""
    state = {"i": 0}

    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_embed_client(embedding: list[float]) -> httpx.Client:
    """构造 mock httpx 客户端，对 /embeddings 请求返回固定向量。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"embedding": list(embedding)}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _patch_refine_with_client(monkeypatch, mock_client: httpx.Client) -> None:
    """patch refine_mod.refine_file 注入 mock LLM client（端点级集成用）。"""
    original_refine_file = refine_mod.refine_file

    def patched_refine_file(file_id, mem, wiki, cfg, source_type="session", client=None):
        return original_refine_file(
            file_id, mem, wiki, cfg, source_type=source_type, client=mock_client,
        )

    monkeypatch.setattr(refine_mod, "refine_file", patched_refine_file)


def _patch_l15_with_client(monkeypatch, mock_client: httpx.Client) -> None:
    """patch l15_mod.resolve_conflicts 注入 mock LLM client。

    routes_admin._persist_memories 调 resolve_conflicts 时不传 client，
    导致 L1.5 走真实 LLM（非确定性）。此 patch 保证 L1.5 也用 mock client。
    """
    original_resolve = l15_mod.resolve_conflicts

    def patched_resolve(new_memories, mem_conn, cfg, client=None, **kwargs):
        return original_resolve(
            new_memories, mem_conn, cfg, client=mock_client, **kwargs,
        )

    monkeypatch.setattr(l15_mod, "resolve_conflicts", patched_resolve)


def _patch_l2_with_client(monkeypatch, mock_client: httpx.Client) -> None:
    """patch l2_mod.aggregate 注入 mock LLM client。

    L2 聚合在 finalize_refinement（L1.5 落库后）调用，client 参数默认 None；
    此 patch 保证 L2 也用 mock client（确定性输出）。
    """
    original_aggregate = l2_mod.aggregate

    def patched_aggregate(memories, mem_conn, cfg, client=None, **kwargs):
        return original_aggregate(
            memories, mem_conn, cfg, client=mock_client, **kwargs,
        )

    monkeypatch.setattr(l2_mod, "aggregate", patched_aggregate)


def _count_active_scenes(mem_conn) -> int:
    """统计 memory.db scenes 表 active 场景数（v0.7 三库拆分后归属）。"""
    cur = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM scenes WHERE status='active'"
    )
    return cur.fetchone()["c"]


def _count_signal_events(mem_conn, event_type: str | None = None) -> int:
    """统计 signal_events 表中指定类型事件数。"""
    if event_type:
        cur = mem_conn.execute(
            "SELECT COUNT(*) AS c FROM signal_events WHERE type=?",
            (event_type,),
        )
    else:
        cur = mem_conn.execute("SELECT COUNT(*) AS c FROM signal_events")
    return cur.fetchone()["c"]


# ---------- 完整链路测试 ----------

def test_e2e_full_pipeline(client, monkeypatch):
    """完整链路：append → refine → L2 场景 → tier0 → inject → search → events → health → backup。"""
    app = client.app
    mem_conn = app.state.mem_conn

    # ---------- Arrange：mock L1 + L2（L2 输出 CREATE 动作） ----------
    new_sid = str(uuid.uuid4())
    l1_body = json.dumps([
        {"content": "用户是独立开发者", "dimensions": ["身份"],
         "memory_type": "persona", "priority": 85, "time_velocity": "static",
         "source_message_ids": [1]},
        {"content": "用户正在开发 SGME 记忆引擎", "dimensions": ["项目"],
         "memory_type": "persona", "priority": 80, "time_velocity": "dynamic",
         "source_message_ids": [2]},
    ])
    l2_body = json.dumps([
        {"action": "create", "target_scene_id": new_sid,
         "merged_content": "# 用户工程画像\n独立开发者，正在做 SGME",
         "reason": "新主题：开发者画像"},
    ])
    cli = _mock_llm_client_sequence([l1_body, l2_body])
    _patch_refine_with_client(monkeypatch, cli)
    _patch_l15_with_client(monkeypatch, cli)
    _patch_l2_with_client(monkeypatch, cli)

    # mock tier0 generate_summary 返回固定摘要
    expected_summary = "独立开发者，注重简洁稳定的工程实践。"
    monkeypatch.setattr(
        tier0_mod, "generate_summary",
        lambda mem_conn_arg, cfg_arg, client=None: expected_summary,
    )

    # ---------- 1. POST /v1/append ----------
    content = (
        _msg_text("user", "我正在做 SGME 项目", "2024-01-01T10:00:00Z")
        + _msg_text("assistant", "你好！", "2024-01-01T10:00:30Z")
        + _msg_text("user", "我正在开发 SGME 记忆引擎", "2024-01-01T10:01:00Z")
    )
    r = client.post("/v1/append", json={
        "session_key": "e2e-v04-full",
        "started_at": "2024-01-01T10:00:00Z",
        "content": content,
    }, headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    file_id = r.json()["file_id"]

    # ---------- 2. POST /v1/admin/refine/trigger ----------
    r = client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    refine_body = r.json()
    assert refine_body["status"] == "refined"
    assert refine_body["memories_count"] >= 1
    assert refine_body["l15"]["stored"] >= 1

    # ---------- 3. 验证 L2 场景生成（memory.db scenes 表出现行，id 系统生成） ----------
    scene_count = _count_active_scenes(mem_conn)
    assert scene_count >= 1, "L2 应生成至少 1 个 active 场景"
    scenes = scene_dao.list_active_scenes(mem_conn, limit=10)
    assert len(scenes) >= 1
    scene = scenes[0]
    assert scene["status"] == "active"
    assert scene["heat"] == 1
    assert "独立开发者" in scene["content"]

    # ---------- 4. POST /v1/admin/tier0/refresh ----------
    r = client.post("/v1/admin/tier0/refresh", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert r.json()["summary_length"] == len(expected_summary)

    # ---------- 5. POST /v1/inject：tier0.present=True ----------
    r = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["blocks"]) >= 1
    assert body["tier0"]["present"] is True
    assert body["tier0"]["content"] == expected_summary

    # ---------- 6. POST /v1/search：返回结果 ----------
    r = client.post("/v1/search", json={
        "query": "记忆引擎",
        "scopes": ["memory"],
    }, headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) >= 1
    first = body["results"][0]
    assert first["source"] == "memory"
    assert len(first["trace"]) >= 1
    assert first["trace"][0]["file_id"] == file_id

    # ---------- 7. GET /v1/events/pull：memory_updated 事件 ----------
    r = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "e2e-v04-full", "limit": 50},
        headers=AGENT_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "events" in body
    assert "next_cursor" in body
    # refine 成功后应发布 source='refine' 的 memory_updated 事件
    memory_updated = [e for e in body["events"] if e.get("type") == "memory_updated"]
    assert len(memory_updated) >= 1
    refine_events = [e for e in memory_updated if e.get("source") == "refine"]
    assert len(refine_events) >= 1, "应至少有一条 source=refine 的 memory_updated 事件"
    assert refine_events[0]["payload"]["file_id"] == file_id

    # ---------- 8. GET /v1/health：可观测性字段 ----------
    r = client.get("/v1/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0b3"
    assert "available" in body["llm"]
    assert body["llm"]["available"] is True
    assert "stalled" in body["refinement"]
    assert "heartbeat_ok" in body["refinement"]
    assert "last_refined_at" in body["refinement"]
    assert "watermark_age_sec" in body["refinement"]
    assert "queue_depth" in body["refinement"]
    # 提炼后水位推进
    assert body["refinement"]["queue_depth"] == 0
    assert body["refinement"]["watermark_age_sec"] is not None
    assert body["refinement"]["watermark_age_sec"] >= 0

    # ---------- 9. POST /v1/admin/backup/create ----------
    r = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "snapshot_id" in body
    assert body["level"] == "full"
    assert body["snapshot_id"].startswith("full_")
    snapshot_id = body["snapshot_id"]

    # ---------- 10. POST /v1/admin/backup/restore ----------
    r = client.post(
        "/v1/admin/backup/restore",
        json={"snapshot_id": snapshot_id},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "restored" in body
    assert body["restored"]["snapshot_id"] == snapshot_id
    assert "pre_restore_snapshot" in body
    assert body["pre_restore_snapshot"].startswith("pre_restore_")


# ---------- 向量 + RRF 测试 ----------

def test_e2e_search_with_vector_rrf(client, monkeypatch):
    """造记忆 + 向量 → /v1/search 返回 RRF 融合结果（routes 含 rrf/vector/bm25）。"""
    app = client.app
    mem_conn = app.state.mem_conn

    # 插入一条记忆
    mid = memory_dao.insert_memory(
        mem_conn, content="Python FastAPI 底座设计", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    # 为已落库记忆补 embedding（用 mock embed client）
    embedding = [1.0, 0.0, 0.0, 0.0]
    cli = _mock_embed_client(embedding)
    assert vector_mod.upsert_memory_vector(
        mem_conn, mid, "Python FastAPI 底座设计", app.state.cfg, cli
    ) is True

    # mock /v1/search 内部 embed 调用（routes_memory 调 do_search → vector_mod.embed）
    monkeypatch.setattr(vector_mod, "embed", lambda query, cfg, client=None: embedding)

    # 触发 /v1/search
    resp = client.post("/v1/search", json={
        "query": "Python",
        "scopes": ["memory"],
        "dimensions": ["tech_stack"],
    }, headers=AGENT_HEADERS)

    # 断言：RRF 融合后该记忆应同时命中 bm25 + vector
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) >= 1
    target = next((r for r in body["results"] if r["memory_id"] == mid), None)
    assert target is not None, "目标记忆未命中"
    assert "rrf" in target["routes"]
    assert "vector" in target["routes"]
    assert "bm25" in target["routes"]
    # meta.routes 也应含 rrf
    assert "rrf" in body["meta"]["routes"]


# ---------- 信号流测试 ----------

def test_e2e_signal_flow_memory_updated(client, monkeypatch):
    """refine 成功后 events/pull 收到 source=refine 的 memory_updated 事件。"""
    app = client.app

    # Arrange：构造 raw 文件 + raw_files 行
    mem_conn = app.state.mem_conn
    session_conn = app.state.session_conn
    fid = "f-e2e-signal-flow"
    msgs = [
        {"timestamp": "2024-01-01T10:00:00Z", "role": "user",
         "content": "我用 Python 写后端"},
        {"timestamp": "2024-01-01T10:01:00Z", "role": "assistant",
         "content": "好的，记下了"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess_signal_flow",
        started_at="2024-01-01T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess_signal_flow", started_at="2024-01-01T10:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(fid),
    )

    # mock L1 + L2（L2 输出空动作列表，避免场景聚合干扰信号断言）
    l1_body = json.dumps([
        {"content": "用户用 Python 写后端", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
    ])
    l2_body = json.dumps([])
    cli = _mock_llm_client_sequence([l1_body, l2_body])
    _patch_refine_with_client(monkeypatch, cli)
    _patch_l15_with_client(monkeypatch, cli)
    _patch_l2_with_client(monkeypatch, cli)

    # Act：触发提炼
    r = client.post("/v1/admin/refine/trigger", json={
        "file_id": fid,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refined"

    # Assert：拉取事件，应含 source=refine 的 memory_updated
    r = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "e2e-signal-flow", "limit": 50},
        headers=AGENT_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    memory_updated = [
        e for e in body["events"]
        if e.get("type") == "memory_updated" and e.get("source") == "refine"
    ]
    assert len(memory_updated) >= 1, "未收到 source=refine 的 memory_updated 事件"
    assert memory_updated[0]["payload"]["file_id"] == fid
    assert memory_updated[0]["payload"]["memories_count"] >= 1


def test_e2e_signal_flow_anomaly_warn(client, monkeypatch):
    """check_heartbeat 检测异常时发布 anomaly_warn 信号（/v1/health 触发）。"""
    app = client.app
    mem_conn = app.state.mem_conn

    # Arrange：制造停滞场景（无任何 refined 记录 → stalled=True）
    # app fixture 已 mock check_llm_available 返回 available=True，
    # 但 check_refinement_stalled 会因无 refined_at 记录返回 stalled=True，
    # 触发 anomaly_warn 发布。
    # 先确认当前无 anomaly_warn
    initial_warns = _count_signal_events(mem_conn, "anomaly_warn")

    # Act：调 /v1/health 触发 check_heartbeat
    r = client.get("/v1/health")
    assert r.status_code == 200, r.text
    body = r.json()
    # 无 refined 记录 → stalled=True
    assert body["refinement"]["stalled"] is True
    assert body["refinement"]["heartbeat_ok"] is False

    # Assert：signal_events 出现 anomaly_warn
    final_warns = _count_signal_events(mem_conn, "anomaly_warn")
    assert final_warns > initial_warns, "未发布 anomaly_warn 信号"

    # 拉取事件验证
    r = client.get(
        "/v1/events/pull",
        params={"subscriber_id": "e2e-anomaly", "limit": 50},
        headers=AGENT_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    anomaly_events = [e for e in body["events"] if e.get("type") == "anomaly_warn"]
    assert len(anomaly_events) >= 1
    assert anomaly_events[0]["source"] == "health"
    assert "stalled" in anomaly_events[0]["payload"]
    assert "llm_available" in anomaly_events[0]["payload"]
