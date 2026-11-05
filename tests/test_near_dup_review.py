"""tests/test_near_dup_review.py：灰区人工批复通道（2026-11-05，B313）。

覆盖：隔离区列表 / 丢弃 / 放行 + 理由留痕 + 状态机 + HTTP 端点。

背景：2026-11-05 核查发现灰区复核**只有 DAO，没有端点/工具/界面**——
生产上 458 条 pending 从未被处理过，不是没人清，是没有清的手段。用户同日
拍板补通道，并要求「人工批复要能加备注」：放行侧已有 restore_reason，
丢弃侧本批补 discard_reason；理由**不设必填**。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, near_dup_dao
from sgme.operations import review as review_ops
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_NOT_FOUND
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

MATCHED_CONTENT = "用户要求统一 SGME 三模块（skills、memory、wiki）平级的理念。"
NEW_CONTENT = "用户查证并确认 SGME 的 skills 模块与 memory、wiki 平级，并要求统一三模块平级理念。"


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def seeded(conns, monkeypatch):
    """一条既有记忆 + 一条待审隔离行；向量回填打桩（测试不调真模型）。"""
    from sgme.data.search import vector as vector_mod

    monkeypatch.setattr(vector_mod, "upsert_memory_vector", lambda *a, **k: True)

    mem_conn, _, _ = conns
    mid = memory_dao.insert_memory(
        mem_conn,
        content=MATCHED_CONTENT,
        memory_type="persona",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=[],  # 临时库未灌维度注册表；FK 指向 dimensions，故留空
    )
    near_dup_dao.upsert_hit(
        mem_conn,
        content=NEW_CONTENT,
        matched_memory_id=mid,
        score=0.933,
        source_ref="hermes-20261105_test",
        dimension_ids=[],
    )
    item = near_dup_dao.list_items(mem_conn, status="pending")[0]
    return {"mem_conn": mem_conn, "mid": mid, "qid": item["id"]}


# ---------- 列表 ----------

def test_list_returns_matched_content_for_side_by_side(seeded):
    """列表必须带匹配记忆原文——灰区复核靠左右对照判断，缺右侧就没有依据。"""
    res = review_ops.list_quarantine(seeded["mem_conn"], status="pending")
    assert res.ok is True
    data = res.data
    assert data["total"] == 1
    item = data["items"][0]
    assert item["content"] == NEW_CONTENT
    assert item["matched_content"] == MATCHED_CONTENT
    assert item["score"] == pytest.approx(0.933, abs=1e-6)


def test_list_limit_out_of_range(seeded):
    with pytest.raises(Exception):
        review_ops.list_quarantine(seeded["mem_conn"], limit=0)


# ---------- 丢弃 ----------

def test_discard_writes_reason(seeded):
    res = review_ops.discard_quarantine(
        seeded["mem_conn"], seeded["qid"], reason="与既有记忆同一件事（三模块平级）"
    )
    assert res.ok is True and res.data["ok"] is True
    assert res.data["status"] == "discarded"
    row = near_dup_dao.get_item(seeded["mem_conn"], seeded["qid"])
    assert row["status"] == "discarded"
    assert row["discard_reason"] == "与既有记忆同一件事（三模块平级）"


def test_discard_reason_optional(seeded):
    """用户拍板：理由不设必填。空理由也要能丢，字段留空即可。"""
    res = review_ops.discard_quarantine(seeded["mem_conn"], seeded["qid"])
    assert res.data["ok"] is True
    row = near_dup_dao.get_item(seeded["mem_conn"], seeded["qid"])
    assert row["status"] == "discarded"
    assert row["discard_reason"] is None


def test_discard_twice_is_noop(seeded):
    review_ops.discard_quarantine(seeded["mem_conn"], seeded["qid"], reason="第一次")
    res = review_ops.discard_quarantine(seeded["mem_conn"], seeded["qid"], reason="第二次")
    assert res.data["ok"] is False
    assert res.data["status"] == "discarded"
    row = near_dup_dao.get_item(seeded["mem_conn"], seeded["qid"])
    assert row["discard_reason"] == "第一次"  # 首次理由不被覆盖


def test_discard_missing_id(seeded):
    res = review_ops.discard_quarantine(seeded["mem_conn"], 99999, reason="x")
    assert res.ok is False and res.error_code == ERR_NOT_FOUND


def test_discard_bad_id(seeded):
    with pytest.raises(Exception):
        review_ops.discard_quarantine(seeded["mem_conn"], "abc")


# ---------- 放行 ----------

def test_restore_creates_memory_and_marks(seeded):
    mem_conn = seeded["mem_conn"]
    res = review_ops.restore_quarantine(mem_conn, seeded["qid"], reason="其实是独立事实")
    assert res.ok is True
    new_mid = res.data["memory_id"]
    assert new_mid
    row = near_dup_dao.get_item(mem_conn, seeded["qid"])
    assert row["status"] == "restored"
    assert row["restored_memory_id"] == new_mid
    assert row["restore_reason"] == "其实是独立事实"
    got = memory_dao.get_memory(mem_conn, new_mid) if hasattr(memory_dao, "get_memory") else None
    if got is None:
        got = mem_conn.execute(
            "SELECT content FROM memories WHERE memory_id=?", (new_mid,)
        ).fetchone()
        assert got[0] == NEW_CONTENT


def test_restore_supersedes_old_by_default(seeded):
    """人工放行默认替代：新记忆 priority=80，旧记忆 lifecycle=archived + 指针。

    用户 2026-11-05 定：人工看过左右对照后放行的判断质量高于 L3 自动裁决，
    池子里该听它的——旧的退出注入/召回但不删。
    """
    mem_conn = seeded["mem_conn"]
    res = review_ops.restore_quarantine(mem_conn, seeded["qid"], reason="新的更准")
    assert res.ok is True and res.data["supersede"] is True
    new_mid = res.data["memory_id"]

    new_row = mem_conn.execute(
        "SELECT priority FROM memories WHERE memory_id=?", (new_mid,)
    ).fetchone()
    assert new_row["priority"] == 80, "人工确认档应为 80（普通提炼是 50）"

    old_row = mem_conn.execute(
        "SELECT status, lifecycle, superseded_by FROM memories WHERE memory_id=?",
        (seeded["mid"],),
    ).fetchone()
    assert old_row["lifecycle"] == "archived", "旧记忆应退出注入/召回"
    assert old_row["superseded_by"] == new_mid
    assert old_row["status"] == "active", "D-56：替代走生命周期轴，不写 status=rejected"


def test_restore_can_coexist(seeded):
    """supersede=False：两条并存（适用于都是独立事实、只是措辞相似）。"""
    mem_conn = seeded["mem_conn"]
    res = review_ops.restore_quarantine(
        mem_conn, seeded["qid"], reason="是两件事", supersede=False
    )
    assert res.ok is True and res.data["supersede"] is False
    new_mid = res.data["memory_id"]

    new_row = mem_conn.execute(
        "SELECT priority FROM memories WHERE memory_id=?", (new_mid,)
    ).fetchone()
    assert new_row["priority"] == 50, "并存时保持普通档"

    old_row = mem_conn.execute(
        "SELECT lifecycle, superseded_by FROM memories WHERE memory_id=?",
        (seeded["mid"],),
    ).fetchone()
    assert old_row["lifecycle"] is None and old_row["superseded_by"] is None


def test_restore_twice_rejected(seeded):
    mem_conn = seeded["mem_conn"]
    review_ops.restore_quarantine(mem_conn, seeded["qid"], reason="first")
    res = review_ops.restore_quarantine(mem_conn, seeded["qid"], reason="second")
    assert res.ok is False and res.error_code == ERR_INVALID_ARGS


# ---------- HTTP 接线 ----------

@pytest.fixture
def app(conns, cfg, tmp_path):
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


def test_http_list_and_discard(app, seeded):
    client = TestClient(app)
    r = client.get("/v1/admin/near-dup/quarantine?status=pending", headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["matched_content"] == MATCHED_CONTENT

    r2 = client.post(
        f"/v1/admin/near-dup/{seeded['qid']}/discard",
        json={"reason": "重复"},
        headers=AGENT_HEADERS,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "discarded"
