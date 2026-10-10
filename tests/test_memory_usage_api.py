"""tests/test_memory_usage_api.py：记忆调用统计端点（T-253，2026-10-10）。

覆盖：
1. GET /v1/admin/memories/usage 默认聚合模式（mode/items/days/order 契约）
2. 过滤参数（source / caller / memory_id）+ order=last_ts + limit
3. unused=true 零调用模式（窗口语义 + 只列 active）
4. 参数边界（days/limit 越界钳制；source / order 非法 → 400 ERR_INVALID_ARGS）
5. 空数据（无行 → items=[] / total_rows=0）
6. 鉴权（缺失 key / agent key → 403）
7. 端到端：POST /v1/search 命中 → 端点查询可见（写行→查）

零真实网络、零 LLM：向量默认关闭（出厂基线），检索走 BM25/LIKE。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, memory_usage_dao
from sgme.server.app import create_app

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(conns, cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    mem_conn, session_conn, wiki_conn = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    return TestClient(app)


def _insert_memory(mem_conn, content, dims=None, created_at=None):
    return memory_dao.insert_memory(
        mem_conn,
        content=content,
        memory_type="persona",
        priority=60,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=dims or ["goals"],
        created_at=created_at,
    )


# ---------- 1/2. 聚合模式 + 过滤 ----------


def test_endpoint_usage_default_and_aggregation(conns, client):
    mem_conn = conns[0]
    mid1 = _insert_memory(mem_conn, "端点多用的记忆")
    mid2 = _insert_memory(mem_conn, "端点偶用的记忆")
    for _ in range(2):
        memory_usage_dao.record_usage(mem_conn, [mid1], "search", "dsh")
    memory_usage_dao.record_usage(mem_conn, [mid1], "inject", "dsh")
    memory_usage_dao.record_usage(mem_conn, [mid2], "search", "workbuddy")

    resp = client.get("/v1/admin/memories/usage?days=7", headers=ADMIN_HEADERS)
    assert resp.status_code == 200  # 静态路径可达（不被动态路由吞）
    body = resp.json()
    assert body["mode"] == "usage"
    assert body["days"] == 7
    assert body["order"] == "hits"
    assert body["sources"] == ["search", "inject"]
    assert body["total_rows"] == 3
    # 聚合：mid1/search 两条日行合并为 hits=2；次数降序居首
    first = body["items"][0]
    assert (first["memory_id"], first["source"], first["hits"]) == (mid1, "search", 2)
    assert first["content_head"] == "端点多用的记忆"
    assert first["memory_type"] == "persona"
    assert first["status"] == "active"


def test_endpoint_filters_and_last_ts_order(conns, client):
    mem_conn = conns[0]
    mid = _insert_memory(mem_conn, "过滤目标记忆")
    memory_usage_dao.record_usage(mem_conn, [mid], "search", "dsh")
    memory_usage_dao.record_usage(mem_conn, [mid], "inject", "other")

    # source 过滤（聚合行不含 caller 列——caller 是过滤维度，见 DAO GROUP BY 口径）
    items = client.get(
        "/v1/admin/memories/usage?source=inject", headers=ADMIN_HEADERS
    ).json()["items"]
    assert [(i["memory_id"], i["source"]) for i in items] == [(mid, "inject")]

    # caller 过滤：dsh 只命中 search 行
    items = client.get(
        "/v1/admin/memories/usage?caller=dsh", headers=ADMIN_HEADERS
    ).json()["items"]
    assert [(i["memory_id"], i["source"]) for i in items] == [(mid, "search")]
    items = client.get(
        "/v1/admin/memories/usage?caller=other", headers=ADMIN_HEADERS
    ).json()["items"]
    assert [(i["memory_id"], i["source"]) for i in items] == [(mid, "inject")]

    resp = client.get(
        f"/v1/admin/memories/usage?memory_id={mid}&order=last_ts",
        headers=ADMIN_HEADERS,
    )
    body = resp.json()
    assert body["order"] == "last_ts"
    assert body["total_rows"] == 2
    assert {i["memory_id"] for i in body["items"]} == {mid}

    # limit 生效（total_rows = 截断后的行数，照 skills/usage 口径）
    limited = client.get(
        "/v1/admin/memories/usage?limit=1", headers=ADMIN_HEADERS
    ).json()
    assert limited["limit"] == 1
    assert len(limited["items"]) == 1


# ---------- 3. unused 零调用模式 ----------


def test_endpoint_unused_mode(conns, client):
    mem_conn = conns[0]
    used = _insert_memory(mem_conn, "窗口内被用过", created_at="2026-10-03T00:00:00Z")
    cold = _insert_memory(mem_conn, "从未被用过", created_at="2026-10-02T00:00:00Z")
    old = _insert_memory(mem_conn, "窗口外被用过", created_at="2026-10-01T00:00:00Z")
    rejected = _insert_memory(
        mem_conn, "被标记不采用", created_at="2026-10-04T00:00:00Z"
    )
    mem_conn.execute(
        "UPDATE memories SET status='rejected' WHERE memory_id=?", (rejected,)
    )
    mem_conn.commit()

    memory_usage_dao.record_usage(mem_conn, [used], "search", "dsh")
    memory_usage_dao.record_usage(
        mem_conn, [old], "inject", "dsh", ts="2020-01-01T00:00:00Z"
    )

    resp = client.get(
        "/v1/admin/memories/usage?unused=true&days=30", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "unused"
    assert body["days"] == 30
    ids = [i["memory_id"] for i in body["items"]]
    assert used not in ids  # 窗口内有调用
    assert old in ids  # 窗口外旧调用 → 仍算未被使用
    assert cold in ids  # 从未被用过
    assert rejected not in ids  # 只列 active
    assert body["total_rows"] == 2
    assert ids == [cold, old]  # created_at DESC

    # unused 模式 limit 生效（默认 500）
    limited = client.get(
        "/v1/admin/memories/usage?unused=true&limit=1", headers=ADMIN_HEADERS
    ).json()
    assert limited["limit"] == 1 and len(limited["items"]) == 1


# ---------- 4. 参数边界 ----------


def test_endpoint_param_validation(client):
    # days / limit 越界 → 钳制（照 skills/usage 先例）
    body = client.get(
        "/v1/admin/memories/usage?days=99999&limit=99999", headers=ADMIN_HEADERS
    ).json()
    assert body["days"] == 400
    assert body["limit"] == 1000

    # source / order 非法 → 400 ERR_INVALID_ARGS（统一错误结构 {"error": {...}}）
    bad_source = client.get(
        "/v1/admin/memories/usage?source=recall", headers=ADMIN_HEADERS
    )
    assert bad_source.status_code == 400
    assert bad_source.json()["error"]["code"] == "ERR_INVALID_ARGS"

    bad_order = client.get(
        "/v1/admin/memories/usage?order=random", headers=ADMIN_HEADERS
    )
    assert bad_order.status_code == 400
    assert bad_order.json()["error"]["code"] == "ERR_INVALID_ARGS"


# ---------- 5. 空数据 ----------


def test_endpoint_empty_data(client):
    resp = client.get("/v1/admin/memories/usage", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "usage"
    assert body["total_rows"] == 0 and body["items"] == []

    resp2 = client.get("/v1/admin/memories/usage?unused=true", headers=ADMIN_HEADERS)
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["mode"] == "unused"
    assert body2["total_rows"] == 0 and body2["items"] == []


# ---------- 6. 鉴权 ----------


def test_endpoint_requires_admin_key(client):
    assert client.get("/v1/admin/memories/usage").status_code == 403
    assert (
        client.get("/v1/admin/memories/usage", headers=AGENT_HEADERS).status_code == 403
    )
    assert (
        client.get("/v1/admin/memories/usage", headers=ADMIN_HEADERS).status_code == 200
    )


# ---------- 7. 端到端：写行 → 查 ----------


def test_endpoint_e2e_after_search(conns, client):
    """POST /v1/search 命中 → GET /v1/admin/memories/usage 可见 search 行。"""
    mem_conn = conns[0]
    mid = _insert_memory(mem_conn, "ApiE2EToken 端点端到端用例")
    resp = client.post(
        "/v1/search",
        json={"query": "ApiE2EToken", "scopes": ["memory"]},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 200

    body = client.get("/v1/admin/memories/usage", headers=ADMIN_HEADERS).json()
    hit = next(i for i in body["items"] if i["memory_id"] == mid)
    assert (hit["source"], hit["hits"]) == ("search", 1)
    # caller 落在账本行上（聚合响应不含该列，直查账本核验反查链路）
    row = mem_conn.execute(
        "SELECT caller FROM memory_usage_daily WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row["caller"] == "default"
