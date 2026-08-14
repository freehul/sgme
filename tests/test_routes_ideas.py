"""tests/test_routes_ideas.py：创意池管理端点（T-56 独立表版）。

覆盖矩阵
--------
1. 列表：默认仅 active；分页信封 items/count/total/page/limit 自洽
2. 隔离：memories 里旧 ideas 标签记忆**不**出现在创意列表（表化后 API 只读 ideas 表）
3. 过滤：status=all、q 子串、custom_flag、has_flag
4. 分页硬上限：limit=200 放行，>200 → 400；page<1 / 非法 sort → 400
5. 单条：GET 200 / 不存在 404
6. 编辑：PATCH content/priority；两字段都没给 → 400；不存在 → 404
7. 备注：POST notes 追加式（不覆盖历史）；空 text → 400
8. 标记：PUT flag 设置 / 空串清除；不存在 → 404
9. 软删除：DELETE → status=rejected、deleted=False（可恢复）；不存在 → 404
10. 恢复：POST restore → status=active
11. 升格：POST promote → 置 promoted 标记 + 创建 demand（回填 origin_idea_id=idea_id）；title 缺失 → 400；不存在 → 404
12. 新建：POST 创建 → ideas 表（status=active，默认 priority=50，source_ref 可选）
13. 鉴权：全部端点 require_admin_key（缺 Key → 403/4xx）

数据源：ideas 独立表（idea_dao.add_idea 写入），不再依赖 memories/memory_tags。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import idea_dao
from sgme.data import memory_dao
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
ADMIN = {"X-API-Key": ADMIN_KEY}

# 端点（鉴权用例遍历）
IDEA_ENDPOINTS = [
    "/v1/admin/ideas",
    "/v1/admin/ideas/any-idea-id",
    "/v1/admin/ideas/any-idea-id/notes",
    "/v1/admin/ideas/any-idea-id/flag",
    "/v1/admin/ideas/any-idea-id/restore",
    "/v1/admin/ideas/any-idea-id/promote",
]


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
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（注入同一批连接）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("SGME_MCP_DISABLED", "1")
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key="test-agent-key",
        bearer_token="",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _iso(days_ago: float = 0) -> str:
    """N 天前的 UTC ISO 时间戳（全库统一格式）。"""
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_idea(
    conn: sqlite3.Connection,
    content: str,
    *,
    priority: int = 50,
    created_days_ago: float = 1,
    source_ref: str | None = None,
) -> str:
    """插入一条创意（ideas 独立表）。"""
    return idea_dao.add_idea(
        conn, content, priority=priority,
        source_ref=source_ref, created_at=_iso(created_days_ago),
    )


# ---------- 列表 ----------

def test_list_default_active_only(client, conns):
    """列表默认仅 active（软删除不可见）。"""
    mem_conn, _, _ = conns
    id1 = _insert_idea(mem_conn, "创意甲：做 WebUI 设计", priority=80)
    id2 = _insert_idea(mem_conn, "创意乙：NAS 部署", priority=60)

    r = client.get("/v1/admin/ideas", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    ids = [i["idea_id"] for i in body["items"]]
    assert {id1, id2} == set(ids)
    assert body["page"] == 1 and body["limit"] == 50
    assert "generated_at" in body


def test_list_ignores_legacy_memories_ideas(client, conns):
    """表化隔离：memories 里旧的 ideas 标签记忆不出现在创意列表（只读 ideas 表）。"""
    mem_conn, _, _ = conns
    _insert_idea(mem_conn, "新表创意", priority=80)
    # 旧形态数据：memories + ideas 标签（迁移后原件保留的形态）
    memory_dao.insert_memory(
        mem_conn, content="旧记忆里的创意（memories 标签）", memory_type="episodic",
        priority=90, time_velocity="dynamic", ttl_days=None,
        dimension_ids=["ideas"], created_at=_iso(0), updated_at=_iso(0),
        occurred_at=_iso(0),
    )
    r = client.get("/v1/admin/ideas", headers=ADMIN)
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["content"] == "新表创意"


def test_list_status_all_shows_rejected(client, conns):
    """status=all 时软删除条目可见。"""
    mem_conn, _, _ = conns
    _insert_idea(mem_conn, "创意将被删除", priority=70)
    r = client.get("/v1/admin/ideas", headers=ADMIN)
    assert r.json()["total"] == 1
    # 软删除后默认不可见
    del_id = r.json()["items"][0]["idea_id"]
    client.delete(f"/v1/admin/ideas/{del_id}", headers=ADMIN)
    assert client.get("/v1/admin/ideas", headers=ADMIN).json()["total"] == 0
    # status=all 可见
    r2 = client.get("/v1/admin/ideas?status=all", headers=ADMIN)
    assert r2.json()["total"] == 1
    assert r2.json()["items"][0]["status"] == "rejected"


def test_list_filters(client, conns):
    """q / custom_flag / has_flag 过滤。"""
    mem_conn, _, _ = conns
    a = _insert_idea(mem_conn, "创意：检索设计", priority=80)
    b = _insert_idea(mem_conn, "创意：注入优化", priority=60)
    # custom_flag 过滤
    client.put(f"/v1/admin/ideas/{a}/flag", json={"custom_flag": "promoted"}, headers=ADMIN)
    r = client.get("/v1/admin/ideas?custom_flag=promoted", headers=ADMIN)
    assert r.json()["total"] == 1 and r.json()["items"][0]["idea_id"] == a
    # has_flag=true
    r = client.get("/v1/admin/ideas?has_flag=true", headers=ADMIN)
    assert r.json()["total"] == 1
    r = client.get("/v1/admin/ideas?has_flag=false", headers=ADMIN)
    assert r.json()["total"] == 1 and r.json()["items"][0]["idea_id"] == b
    # q 子串
    r = client.get("/v1/admin/ideas?q=检索", headers=ADMIN)
    assert r.json()["total"] == 1 and r.json()["items"][0]["idea_id"] == a


def test_list_limit_ceiling_and_param_validation(client, conns):
    """limit 上限 200；page<1 / 非法 sort → 400。"""
    mem_conn, _, _ = conns
    for i in range(3):
        _insert_idea(mem_conn, f"创意 {i}", priority=50 + i)

    assert client.get("/v1/admin/ideas?limit=200", headers=ADMIN).status_code == 200
    assert client.get("/v1/admin/ideas?limit=201", headers=ADMIN).status_code == 400
    assert client.get("/v1/admin/ideas?page=0", headers=ADMIN).status_code == 400
    assert client.get("/v1/admin/ideas?sort=bogus", headers=ADMIN).status_code == 400


# ---------- 单条 ----------

def test_get_idea(client, conns):
    """GET 单条 200；不存在 → 404。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "创意详情", priority=88)
    r = client.get(f"/v1/admin/ideas/{idea_id}", headers=ADMIN)
    assert r.status_code == 200
    idea = r.json()["idea"]
    assert idea["idea_id"] == idea_id
    assert idea["content"] == "创意详情"
    assert idea["priority"] == 88
    assert idea["status"] == "active"
    assert idea["notes"] == []
    assert idea["custom_flag"] is None

    assert client.get("/v1/admin/ideas/nope", headers=ADMIN).status_code == 404


# ---------- 编辑 ----------

def test_update_idea(client, conns):
    """PATCH 编辑 content / priority；无字段 → 400。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "旧内容", priority=50)
    r = client.patch(f"/v1/admin/ideas/{idea_id}", json={"content": "新内容", "priority": 95}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["updated_fields"] == ["content", "priority"]
    assert r.json()["idea"]["content"] == "新内容"
    assert r.json()["idea"]["priority"] == 95

    assert client.patch(f"/v1/admin/ideas/{idea_id}", json={}, headers=ADMIN).status_code == 400
    assert client.patch("/v1/admin/ideas/nope", json={"content": "x"}, headers=ADMIN).status_code == 404


# ---------- 备注 ----------

def test_append_note_is_append_only(client, conns):
    """备注追加式——不覆盖历史。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "有备注的创意")
    r1 = client.post(f"/v1/admin/ideas/{idea_id}/notes", json={"text": "第一条备注"}, headers=ADMIN)
    assert r1.status_code == 200
    assert r1.json()["count"] == 1
    r2 = client.post(f"/v1/admin/ideas/{idea_id}/notes", json={"text": "第二条备注"}, headers=ADMIN)
    assert r2.json()["count"] == 2
    texts = [n["text"] for n in r2.json()["notes"]]
    assert texts == ["第一条备注", "第二条备注"]

    assert client.post(f"/v1/admin/ideas/{idea_id}/notes", json={"text": "  "}, headers=ADMIN).status_code == 400
    assert client.post("/v1/admin/ideas/nope/notes", json={"text": "x"}, headers=ADMIN).status_code == 404


# ---------- 标记 ----------

def test_flag_set_and_clear(client, conns):
    """PUT flag 设置；空串清除。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "待标记创意")
    r = client.put(f"/v1/admin/ideas/{idea_id}/flag", json={"custom_flag": "promoted"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["custom_flag"] == "promoted"
    # 清除
    r = client.put(f"/v1/admin/ideas/{idea_id}/flag", json={"custom_flag": ""}, headers=ADMIN)
    assert r.json()["custom_flag"] is None

    assert client.put("/v1/admin/ideas/nope/flag", json={"custom_flag": "x"}, headers=ADMIN).status_code == 404


# ---------- 软删除 / 恢复 ----------

def test_soft_delete_and_restore(client, conns):
    """软删除 status=rejected / deleted=False；恢复 active。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "要删除的创意")
    r = client.request("DELETE", f"/v1/admin/ideas/{idea_id}", json={"reason": "不采用"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["deleted"] is False
    assert r.json()["reject_reason"] == "不采用"

    r = client.post(f"/v1/admin/ideas/{idea_id}/restore", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "active"

    assert client.delete("/v1/admin/ideas/nope", headers=ADMIN).status_code == 404
    assert client.post("/v1/admin/ideas/nope/restore", headers=ADMIN).status_code == 404


# ---------- 新建（人工添加，2026-08-13 用户定：创意由用户主动提出） ----------

def test_create_idea_success(client, conns):
    """POST 新建创意：ideas 表 status=active + 默认 priority=50。"""
    r = client.post(
        "/v1/admin/ideas",
        json={"content": "新创意：做一个跨项目待办面板"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    body = r.json()
    idea = body["idea"]
    assert idea["content"] == "新创意：做一个跨项目待办面板"
    assert idea["priority"] == 50
    assert idea["status"] == "active"
    assert body["created"] is True

    # 创建后列表可见（默认 active 过滤）
    lst = client.get("/v1/admin/ideas", headers=ADMIN).json()
    assert any(i["idea_id"] == idea["idea_id"] for i in lst["items"])


def test_create_idea_with_priority_and_source(client, conns):
    """指定 priority / source_ref 生效。"""
    r = client.post(
        "/v1/admin/ideas",
        json={"content": "带优先级创意", "priority": 88, "source_ref": "用户对话 2026-08-13"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    idea = r.json()["idea"]
    assert idea["priority"] == 88
    assert idea["source_ref"] == "用户对话 2026-08-13"


def test_create_idea_validation(client, conns):
    """content 缺失/空白 → 400；priority 越界 → 400。"""
    assert client.post("/v1/admin/ideas", json={}, headers=ADMIN).status_code == 400
    assert client.post("/v1/admin/ideas", json={"content": "   "}, headers=ADMIN).status_code == 400
    r = client.post(
        "/v1/admin/ideas", json={"content": "越界", "priority": 101}, headers=ADMIN
    )
    assert r.status_code == 400


# ---------- 升格 ----------

def test_promote_idea_creates_demand_and_marks_flag(client, conns):
    """升格：置 promoted 标记 + 创建 demand（回填 origin_idea_id=idea_id）。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "创意：做 WebUI 设计", priority=80)

    r = client.post(
        f"/v1/admin/ideas/{idea_id}/promote",
        json={"title": "WebUI 设计", "content": "基于升格", "priority": 70},
        headers=ADMIN,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["idea_id"] == idea_id
    assert body["promoted"] is True
    assert body["demand"]["title"] == "WebUI 设计"
    assert body["demand"]["origin_idea_id"] == idea_id
    assert body["demand"]["priority"] == 70

    # 创意侧标记已置 promoted
    idea = client.get(f"/v1/admin/ideas/{idea_id}", headers=ADMIN).json()["idea"]
    assert idea["custom_flag"] == "promoted"

    # 需求池可见该需求（关联来源创意）
    list_r = client.get("/v1/admin/demands", headers=ADMIN)
    assert list_r.status_code == 200
    demands = list_r.json()["items"]
    assert any(d["demand_id"] == body["demand"]["demand_id"] for d in demands)


def test_promote_requires_title_and_existing_idea(client, conns):
    """升格 title 缺失 → 400；创意不存在 → 404。"""
    mem_conn, _, _ = conns
    idea_id = _insert_idea(mem_conn, "无标题升格测试")
    r = client.post(f"/v1/admin/ideas/{idea_id}/promote", json={}, headers=ADMIN)
    assert r.status_code == 400
    assert client.post("/v1/admin/ideas/nope/promote", json={"title": "x"}, headers=ADMIN).status_code == 404


# ---------- 鉴权 ----------

def test_all_endpoints_require_admin_key(client):
    """全部端点缺 Key → 被拒（4xx）。

    无 Key 请求必须返回 4xx 拒绝（403/405/404 均可）。
    SPA catch-all（app.py 静态托管）使无 GET 路由的端点（如
    ``/ideas/{id}/notes`` 仅 POST）在 GET 时回 404，而非 FastAPI 默认 405。
    """
    for ep in IDEA_ENDPOINTS:
        for method in ("get", "post", "put", "patch", "delete"):
            if not hasattr(client, method):
                continue
            resp = getattr(client, method)(ep, headers={})
            assert 400 <= resp.status_code < 500, f"{method.upper()} {ep} => {resp.status_code}"
