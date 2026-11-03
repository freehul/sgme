# -*- coding: utf-8 -*-
"""tests/test_inject_lifecycle.py：D-52/3.6 注入可见性谓词与锚点块回归。

覆盖：
- lifecycle 单向谓词：sleeping/archived 不进注入（检索与回溯不受影响，另测）；
- TTL 起算改 COALESCE(ttl_renewed_at, updated_at)：续期后的记忆不再被判过期；
- inject_tier='anchor' 从主块剥离（进锚点块）；
- 闸门 A：planned + valid_to 已过 → 不注入；
- anchor 块：M 降序、超预算截断并显式标注未展开条数（D-47 不许静默省略）；
- operations 层接线：_attach_anchor_block 追加块与 stats。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sgme.data import db
from sgme.data import memory_dao
from sgme.operations.inject import _attach_anchor_block
from sgme.profile.inject import build_anchor_block, query_section


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(days_ago: float) -> str:
    return (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_dim(conn, dim_id="identity") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry "
        "(id, display_name, category, time_velocity, created_at) VALUES (?,?,?,?,?)",
        (dim_id, "身份", "静态", "static", _iso(0)),
    )


def _insert(
    conn,
    memory_id: str,
    *,
    dim: str = "identity",
    ttl_days: float | None = None,
    updated_days_ago: float = 0,
    ttl_renewed_at: str | None = None,
    lifecycle: str | None = None,
    inject_tier: str | None = None,
    meaning_anchor: float | None = None,
    modality: str = "done",
    valid_to: str | None = None,
) -> None:
    _seed_dim(conn, dim)
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, occurred_at, status, ttl_days, ttl_renewed_at, "
        "lifecycle, inject_tier, meaning_anchor, modality, valid_to, route, subject, needs_review) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            memory_id, "内容-" + memory_id, "fact", 50, "static",
            _iso(updated_days_ago + 1), _iso(updated_days_ago), _iso(updated_days_ago),
            "active", ttl_days, ttl_renewed_at, lifecycle, inject_tier,
            meaning_anchor, modality, valid_to, "memory", "self", 0,
        ),
    )
    conn.execute("INSERT INTO memory_tags (memory_id, dimension_id) VALUES (?,?)",
                 (memory_id, dim))


def _ids(rows: list[dict]) -> set[str]:
    return {r["memory_id"] for r in rows}


# ---------- lifecycle 谓词 ----------


def test_lifecycle_filter_excludes_sleeping_and_archived(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "active1")
    _insert(conn, "sleep1", lifecycle="sleeping")
    _insert(conn, "arch1", lifecycle="archived")
    conn.commit()

    default = memory_dao.list_memories_by_dimension(conn, ["identity"], limit=10)
    assert _ids(default) == {"active1", "sleep1", "arch1"}, "默认不过滤（检索/回溯保留）"

    visible = memory_dao.list_memories_by_dimension(
        conn, ["identity"], limit=10, lifecycle_active=True
    )
    assert _ids(visible) == {"active1"}
    conn.close()


# ---------- TTL 起算 ----------


def test_ttl_basis_honors_renewed_at(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "expired_by_updated", ttl_days=30, updated_days_ago=100)
    _insert(conn, "renewed", ttl_days=30, updated_days_ago=100, ttl_renewed_at=_iso(5))
    conn.commit()
    rows = memory_dao.list_memories_by_dimension(conn, ["identity"], limit=10)
    assert _ids(rows) == {"renewed"}, "续期后按 ttl_renewed_at 起算，不再判过期"
    conn.close()


# ---------- 主块剥离 ----------


def test_exclude_anchor_and_valid_expired(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "main1")
    _insert(conn, "anchor1", inject_tier="anchor")
    _insert(conn, "plan1", modality="planned", valid_to=_iso(2))
    conn.commit()
    rows = memory_dao.list_memories_by_dimension(
        conn, ["identity"], limit=10, exclude_anchor=True, exclude_valid_expired=True
    )
    assert _ids(rows) == {"main1"}
    conn.close()


def test_query_section_applies_all_filters(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "ok")
    _insert(conn, "sleep", lifecycle="sleeping")
    _insert(conn, "anchor", inject_tier="anchor")
    _insert(conn, "stale_plan", modality="planned", valid_to=_iso(3))
    conn.commit()
    section = {"title": "身份", "query": {"dimensions": ["identity"], "limit": 10}}
    rows = query_section(conn, section, [{"id": "identity", "time_velocity": "static"}])
    assert _ids(rows) == {"ok"}, "注入主块只保留可见且非锚点的记忆"
    conn.close()


# ---------- 锚点块 ----------


def test_build_anchor_block_orders_and_annotates_truncation(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "a_low", inject_tier="anchor", meaning_anchor=0.40)
    _insert(conn, "a_high", inject_tier="anchor", meaning_anchor=0.90)
    _insert(conn, "a_mid", inject_tier="anchor", meaning_anchor=0.60)
    _insert(conn, "sleep_anchor", inject_tier="anchor", lifecycle="sleeping")
    conn.commit()

    block = build_anchor_block(conn, {"enabled": True, "max_items": 2})
    assert block is not None
    assert block["kind"] == "anchor"
    assert [i["memory_id"] for i in block["items"]] == ["a_high", "a_mid"]
    assert block["note"] == "另有 1 条长期记忆未展开，可按需检索"

    disabled = build_anchor_block(conn, {"enabled": False})
    assert disabled is None
    conn.close()


def test_attach_anchor_block_wires_stats(tmp_path):
    conn = db.connect_memory(tmp_path)
    _insert(conn, "a1", inject_tier="anchor", meaning_anchor=0.9)
    conn.commit()
    response: dict = {"blocks": [], "stats": {}}
    out = _attach_anchor_block(response, conn, {"inject": {"anchor": {"enabled": True}}})
    assert out["stats"]["anchor_present"] is True
    assert out["stats"]["anchor_items"] == 1
    assert out["blocks"][0]["kind"] == "anchor"
    conn.close()
