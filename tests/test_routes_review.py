"""tests/test_routes_review.py：待审队列 HTTP 路由（含 D2 批量拒绝，2026-11-05）。

覆盖：
1. POST /v1/admin/review/batch-reject 全成功 → 200 + rejected/failed 计数 + 逐条结果；
2. 部分失败（中间夹一条不存在的 id）→ 200，rejected=n-1 / failed=1，其余照常落库；
3. 空列表 / 缺字段 / 超上限 → 400 ERR_INVALID_ARGS（容器级校验，不做部分成功）；
4. 鉴权与单条端点同档：admin key 与 agent key 都放行，无 key → 403；
5. 单条 reject 端点行为不变（批量是新增，不复用路径参数）。

设计背景（收尾交接 D2）：WebUI 批量拒绝原本循环 N 次单条调用，
一页 30 条即 30 次鉴权往返；本端点把同一页收敛成一次请求。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations import review as review_ops
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN = {"X-API-Key": ADMIN_KEY}
AGENT = {"X-API-Key": AGENT_KEY}

BATCH_URL = "/v1/admin/review/batch-reject"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- fixtures ----------

@pytest.fixture()
def conns(tmp_path, monkeypatch):
    """三库连接（隔离 tmp）：memory.db 走真实 schema，含 2.0 迁移列。"""
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    cfg = sgme_config.load_config()
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn, cfg
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture()
def app(tmp_path, monkeypatch, conns):
    mem_conn, session_conn, wiki_conn, cfg = conns
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)
    cfg["skills"] = {"enabled": False, "source_dirs": []}
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )


@pytest.fixture()
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _insert_pending(conn, memory_id: str) -> None:
    """插入一条待审记忆（needs_review=1 + status=active 才在待审队列里）。"""
    conn.execute(
        "INSERT INTO memories(memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, status, needs_review, notes) "
        "VALUES (?, ?, 'persona', 50, 'static', ?, ?, 'active', 1, NULL)",
        (memory_id, f"待审内容-{memory_id}", _now_iso(), _now_iso()),
    )
    conn.commit()


def _status_of(conn, memory_id: str) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT status, reject_reason FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    return (row["status"], row["reject_reason"])


# ---------- 全成功 ----------

def test_batch_reject_all_success(client, conns):
    """三条待审 → 一次请求全部拒绝：200 + rejected=3/failed=0 + 逐条 ok。"""
    mem_conn = conns[0]
    ids = ["m-1", "m-2", "m-3"]
    for mid in ids:
        _insert_pending(mem_conn, mid)

    resp = client.post(BATCH_URL, json={"memory_ids": ids, "reason": "批量清理"}, headers=ADMIN)

    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected"] == 3 and body["failed"] == 0
    assert [r["memory_id"] for r in body["results"]] == ids
    assert all(r["ok"] for r in body["results"])
    for mid in ids:
        assert _status_of(mem_conn, mid) == ("rejected", "批量清理")
    # 拒绝后退出待审队列
    assert client.get("/v1/admin/review/pending", headers=ADMIN).json()["total"] == 0


def test_batch_reject_reason_optional_uses_default(client, conns):
    """理由可空（D-30 同口径）：不传 reason 时落默认串，仍算成功。"""
    mem_conn = conns[0]
    _insert_pending(mem_conn, "m-1")

    body = client.post(BATCH_URL, json={"memory_ids": ["m-1"]}, headers=ADMIN).json()

    assert body["rejected"] == 1
    assert _status_of(mem_conn, "m-1") == ("rejected", review_ops.DEFAULT_REJECT_REASON)


# ---------- 部分失败 ----------

def test_batch_reject_partial_failure(client, conns):
    """夹一条不存在的 id：整批仍 200，失败条目带 error/error_code，其余照常拒绝。"""
    mem_conn = conns[0]
    _insert_pending(mem_conn, "m-1")
    _insert_pending(mem_conn, "m-2")

    body = client.post(
        BATCH_URL,
        json={"memory_ids": ["m-1", "ghost", "m-2"]},
        headers=ADMIN,
    ).json()

    assert body["rejected"] == 2 and body["failed"] == 1
    by_id = {r["memory_id"]: r for r in body["results"]}
    assert by_id["m-1"]["ok"] is True and by_id["m-2"]["ok"] is True
    assert by_id["ghost"]["ok"] is False
    assert by_id["ghost"]["error_code"] == "ERR_NOT_FOUND"
    assert "不存在" in by_id["ghost"]["error"]
    assert _status_of(mem_conn, "m-1")[0] == "rejected"
    assert _status_of(mem_conn, "m-2")[0] == "rejected"


def test_batch_reject_invalid_item_is_per_item_failure(client, conns):
    """数组里夹非法条目（空串/数字）只记该条失败，不炸掉整批。"""
    mem_conn = conns[0]
    _insert_pending(mem_conn, "m-1")

    body = client.post(
        BATCH_URL, json={"memory_ids": ["m-1", "", 123]}, headers=ADMIN
    ).json()

    assert body["rejected"] == 1 and body["failed"] == 2
    bad = [r for r in body["results"] if not r["ok"]]
    assert all(r["error_code"] == "ERR_INVALID_ARGS" for r in bad)


# ---------- 容器级校验 → 400 ----------

def test_batch_reject_empty_list_400(client):
    """空数组 → 400 ERR_INVALID_ARGS（不是 200 + 空结果）。"""
    resp = client.post(BATCH_URL, json={"memory_ids": []}, headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_batch_reject_missing_memory_ids_400(client):
    """缺 memory_ids → 400（不能默认成「全部拒绝」）。"""
    resp = client.post(BATCH_URL, json={"reason": "x"}, headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_batch_reject_string_instead_of_list_400(client):
    """memory_ids 传成字符串 → 400（避免把字符串当可迭代拆成字符）。"""
    resp = client.post(BATCH_URL, json={"memory_ids": "m-1"}, headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_batch_reject_over_limit_400(client, monkeypatch):
    """超过单次上限 → 400（上限读模块常量，防滥用）。"""
    monkeypatch.setattr(review_ops, "MAX_BATCH_REJECT", 2)
    resp = client.post(BATCH_URL, json={"memory_ids": ["a", "b", "c"]}, headers=ADMIN)
    assert resp.status_code == 400
    assert "2" in resp.json()["error"]["message"]


# ---------- 鉴权 ----------

def test_batch_reject_accepts_agent_key(client, conns):
    """鉴权与单条端点同档：require_agent_key → agent key 亦可。"""
    _insert_pending(conns[0], "m-1")
    resp = client.post(BATCH_URL, json={"memory_ids": ["m-1"]}, headers=AGENT)
    assert resp.status_code == 200 and resp.json()["rejected"] == 1


def test_batch_reject_requires_key(client):
    """无 Key → 403（与单条 reject 一致）。"""
    resp = client.post(BATCH_URL, json={"memory_ids": ["m-1"]})
    assert resp.status_code == 403


# ---------- 单条端点行为不变 ----------

def test_single_reject_endpoint_unchanged(client, conns):
    """单条 reject 路径参数形态保持原样（批量是新增端点，不是替换）。"""
    _insert_pending(conns[0], "m-1")
    resp = client.post("/v1/admin/review/m-1/reject", json={"reason": "单条"}, headers=ADMIN)
    assert resp.status_code == 200
    assert _status_of(conns[0], "m-1") == ("rejected", "单条")
