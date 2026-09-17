"""tests/test_scene_related_memories_status.py：T-171 场景关联记忆查询未过滤 rejected。

缺陷（审计员定位，2026-09-18）
------------------------------
``scene_dao.list_scenes_page`` 的 ``related_memories`` 子查询
（``scene_memories`` JOIN ``memories``）**没有任何 status 过滤**，
rejected 记忆的正文（截断 120 字）经 ``GET /v1/admin/scenes`` 直接吐给调用端。
生产现网：836 条 rejected + 630 条 expired 关联分布在 133 个 active 场景上。

设计依据：``docs/design/SGME-架构设计-v1.0.md``（第 1434 行）
「rejected·expired 不参与查询/注入/时间线，数据保留可溯源」；
同文档 843 行：「status=active → rejected（用户判错）；数据完整保留，查询/搜索/候选池一律过滤」。

修复口径（最小改动，本文件即其回归护栏）
----------------------------------------
1. 子查询加 ``AND m.status != 'rejected'``；
2. **expired 本次不过滤**——是否过滤属主人待裁决的独立项，
   故用 ``test_expired_link_still_returned`` 把该保守口径钉住，
   防后续改动顺手扩大过滤面（要改必须显式改这条用例）；
3. 防过度过滤：active 关联记忆的正文/维度/排序照常返回。

覆盖矩阵
--------
1. DAO 层：active 场景关联 active+rejected+expired 三态记忆 → related_memories 不含 rejected
2. HTTP 层：GET /v1/admin/scenes 响应体（含 items[].related_memories）无 rejected 正文/ID
3. 防过度过滤：active 关联记忆仍返回（正文前缀 + dimensions 齐备）
4. 钉住保守口径：expired 关联记忆仍返回；memories_count 口径未变（仍是关联条数）
5. 显式 status=rejected 时 rejected **场景**仍可见（场景状态过滤不受本次改动影响）

全部使用 tmp_path 隔离三库，不接触生产库。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, scene_dao
from sgme.raw import store as raw_store
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
ADMIN = {"X-API-Key": ADMIN_KEY}

#: 泄漏判定用的长正文（120 字截断前的完整文本）：含唯一标记串，便于在响应体中检索
REJECTED_CONTENT = (
    "【T171-LEAK-CANARY】主人已明确纠错：父亲透析改为每周二，"
    "此前记录的每周三陪伴安排作废，不得再作为事实引用。"
)
ACTIVE_CONTENT = "【T171-ACTIVE】家庭事实：父亲每周二需透析陪伴（主人 2026-09 纠正后口径）。"
EXPIRED_CONTENT = "【T171-EXPIRED】过时动态事实：主人本季度出差计划（随时间失效）。"


# ---------- fixtures（范式取自 tests/test_routes_admin_browse.py，全隔离） ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    (rd / "sessions").mkdir(parents=True)
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
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
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
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _iso(days_ago: float = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_memory_status(conn: sqlite3.Connection, memory_id: str, status: str) -> None:
    conn.execute("UPDATE memories SET status=? WHERE memory_id=?", (status, memory_id))
    conn.commit()


@pytest.fixture
def seeded(app, conns):
    """场景 a0467c8b 的等价最小复现：1 个 active 场景挂三态关联记忆。

    - active 记忆（updated_at 最新，应排在 related_memories 首位）
    - rejected 记忆（本次修复目标：必须消失）
    - expired 记忆（本次**不过滤**：必须仍在）
    另建 1 个 rejected 场景（验场景自身状态过滤不受影响）。
    """
    mem_conn, _, _ = conns
    dims = [d["id"] for d in memory_dao.list_dimensions(mem_conn, active_only=True)]
    assert len(dims) >= 2, "注册表维度不足，用例前提不成立"
    dim_a = dims[0]

    active_id = memory_dao.insert_memory(
        mem_conn, content=ACTIVE_CONTENT, memory_type="fact", priority=9,
        time_velocity="static", ttl_days=None, dimension_ids=[dim_a],
        created_at=_iso(2), updated_at=_iso(1), occurred_at=_iso(2),
    )
    rejected_id = memory_dao.insert_memory(
        mem_conn, content=REJECTED_CONTENT, memory_type="fact", priority=8,
        time_velocity="static", ttl_days=None, dimension_ids=[dim_a],
        created_at=_iso(2), updated_at=_iso(2), occurred_at=_iso(2),
    )
    memory_dao.reject_memory(mem_conn, rejected_id, "用户纠错：透析日改为周二")

    expired_id = memory_dao.insert_memory(
        mem_conn, content=EXPIRED_CONTENT, memory_type="fact", priority=7,
        time_velocity="dynamic", ttl_days=None, dimension_ids=[dim_a],
        created_at=_iso(3), updated_at=_iso(3), occurred_at=_iso(3),
    )
    _set_memory_status(mem_conn, expired_id, "expired")

    scene_dao.insert_scene(
        mem_conn, "scene_t171", "家庭事实纠错场景", "叙事文档全文（T-171 复现）",
        created_at=_iso(5), updated_at=_iso(5),
    )
    for mid in (active_id, rejected_id, expired_id):
        scene_dao.add_memory_link(mem_conn, "scene_t171", mid)

    scene_dao.insert_scene(mem_conn, "scene_t171_x", "被判错的场景", "场景自身 rejected")
    scene_dao.update_scene_status(mem_conn, "scene_t171_x", "rejected")

    return {
        "active_id": active_id,
        "rejected_id": rejected_id,
        "expired_id": expired_id,
        "scene_id": "scene_t171",
    }


def _related(client) -> dict:
    """GET /v1/admin/scenes 后取 scene_t171 的 related_memories。"""
    body = client.get("/v1/admin/scenes", headers=ADMIN).json()
    by_id = {s["scene_id"]: s for s in body["items"]}
    assert "scene_t171" in by_id, "active 场景未出现在默认列表中，用例前提不成立"
    return by_id["scene_t171"]


# ============================================================
# 1. DAO 层：rejected 关联记忆不得进入 related_memories
# ============================================================

def test_dao_related_memories_excludes_rejected(conns, seeded):
    """list_scenes_page 的 related_memories 不含 rejected 记忆（缺陷根因位于此）。"""
    mem_conn, _, _ = conns
    items, total = scene_dao.list_scenes_page(mem_conn, limit=50)
    assert total == 1
    rel = items[0]["related_memories"]
    ids = [m["memory_id"] for m in rel]

    assert seeded["rejected_id"] not in ids, (
        "rejected 记忆仍出现在 related_memories —— T-171 未修复（正文已随之泄漏）"
    )
    assert not any("T171-LEAK-CANARY" in (m["content"] or "") for m in rel), (
        "rejected 记忆正文出现在 DAO 输出里"
    )
    # 防过度过滤：active / expired 两条照常返回
    assert seeded["active_id"] in ids
    assert seeded["expired_id"] in ids


# ============================================================
# 2. HTTP 层：GET /v1/admin/scenes 响应体无 rejected 正文
# ============================================================

def test_admin_scenes_endpoint_does_not_leak_rejected_content(client, seeded):
    """端到端：响应体（序列化后全文）既无 rejected 记忆 ID，也无其正文。"""
    raw = client.get("/v1/admin/scenes", headers=ADMIN).text
    scene = _related(client)
    rel = scene["related_memories"]

    assert seeded["rejected_id"] not in {m["memory_id"] for m in rel}
    assert "T171-LEAK-CANARY" not in raw, "rejected 记忆正文经 GET /v1/admin/scenes 泄漏"
    payload = json.loads(raw)
    assert all(
        "T171-LEAK-CANARY" not in (m.get("content") or "")
        for s in payload["items"]
        for m in s.get("related_memories") or []
    )


# ============================================================
# 3. 防过度过滤：active 关联记忆照常返回（正文/维度/排序）
# ============================================================

def test_active_related_memory_still_returned_with_dimensions(client, seeded):
    """active 关联记忆不被误伤：正文前缀 + dimensions + updated_at 倒序。"""
    scene = _related(client)
    rel = scene["related_memories"]
    hit = next((m for m in rel if m["memory_id"] == seeded["active_id"]), None)
    assert hit is not None, "active 关联记忆被过度过滤掉了"
    assert hit["content"].startswith("【T171-ACTIVE】")
    assert hit["dimensions"], "active 关联记忆的维度标签丢失"
    stamps = [m["updated_at"] for m in rel]
    assert stamps == sorted(stamps, reverse=True), "related_memories 排序回归"


# ============================================================
# 4. 钉住保守口径：expired 不过滤（本次刻意不动）
# ============================================================

def test_expired_link_still_returned(client, seeded):
    """expired 关联记忆**仍返回**——本次修复只过滤 rejected。

    若未来裁决为「expired 也不参与查询」，必须显式改掉这条用例，
    不允许被顺手扩大过滤面。
    """
    scene = _related(client)
    ids = {m["memory_id"] for m in scene["related_memories"]}
    assert seeded["expired_id"] in ids, (
        "expired 关联记忆被过滤了 —— 超出 T-171 修复口径（本次仅过滤 rejected）"
    )
    # memories_count 口径未变：仍是关联条数（含 rejected 链接），本次只改正文泄漏面
    assert scene["memories_count"] == 3


# ============================================================
# 5. 场景自身状态过滤不受影响
# ============================================================

def test_scene_status_filter_unaffected(client, seeded):
    """rejected **场景**仍只在显式索取时可见（本次改动不碰场景状态过滤）。"""
    default_ids = {s["scene_id"] for s in client.get("/v1/admin/scenes", headers=ADMIN).json()["items"]}
    assert default_ids == {"scene_t171"}

    rejected = client.get("/v1/admin/scenes?status=rejected", headers=ADMIN).json()
    assert {s["scene_id"] for s in rejected["items"]} == {"scene_t171_x"}
