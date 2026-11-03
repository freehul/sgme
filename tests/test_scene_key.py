# -*- coding: utf-8 -*-
"""tests/test_scene_key.py：D-53 场景键规则与回填回归。

覆盖：
- 主维度 R1（priority 最小）/ R2（并列按 id 字典序，确定性）/ R3（无标签 → None）；
- 来源类映射（精确、前缀、未登记 → other；空 agent_tag → unknown）；
- insert_memory 写入 scene_key（含显式 extra 优先）；
- 回填脚本：dry-run 零写入 / --apply 落库 / unknown 清单可导出。
"""
from __future__ import annotations

import json

from sgme.data import db
from sgme.data import memory_dao
from sgme.data import scene_key as sk
from scripts import backfill_scene_key as bf


def _seed_registry(conn) -> None:
    rows = [
        ("identity", "身份", 10),
        ("family", "家庭", 20),
        ("tech_stack", "技术栈", 80),
        ("status", "状态", 120),
    ]
    for dim_id, name, prio in rows:
        conn.execute(
            "INSERT INTO dimension_registry (id, display_name, category, time_velocity, "
            "created_at, priority) VALUES (?,?,?,?,?,?)",
            (dim_id, name, "静态", "static", "2026-10-01T00:00:00Z", prio),
        )


def _insert_memory(conn, mid, dims, agent_tag):
    return memory_dao.insert_memory(
        conn,
        content="内容-" + mid,
        memory_type="fact",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=dims,
        agent_tag=agent_tag,
        memory_id=mid,
    )


# ---------- 纯规则 ----------


def test_main_dimension_r1_r2_r3():
    prios = {"identity": 10, "family": 20, "tech_stack": 80, "unknown_dim": None}
    assert sk.main_dimension(["tech_stack", "identity", "family"], prios) == "identity"
    # R2：并列取字典序；未知维度（priority None）排最后
    assert sk.main_dimension(["family", "identity"], {"family": 10, "identity": 10}) == "family"
    assert sk.main_dimension(["unknown_dim", "family"], prios) == "family"
    # R3
    assert sk.main_dimension([], prios) is None


def test_origin_class_mapping_and_prefix():
    assert sk.origin_class("dsh") == "coding"
    assert sk.origin_class("claude-code") == "coding"
    assert sk.origin_class("hermes-nas") == "desktop"
    assert sk.origin_class("workbuddy_laptop") == "desktop"
    assert sk.origin_class("doubao") == "mobile"
    assert sk.origin_class("some-new-agent") == "other"
    assert sk.origin_class(None) == "other"


def test_build_scene_key_unknown_paths():
    prios = {"identity": 10}
    assert sk.build_scene_key(["identity"], "dsh", prios) == "identity:coding"
    assert sk.build_scene_key([], "dsh", prios) == sk.UNKNOWN, "无主维度 → unknown（R3）"
    assert sk.build_scene_key(["identity"], None, prios) == sk.UNKNOWN, "无 agent_tag → unknown"
    assert sk.build_scene_key(["identity"], "  ", prios) == sk.UNKNOWN


# ---------- 落库钩子 ----------


def test_insert_memory_writes_scene_key(tmp_path):
    conn = db.connect_memory(tmp_path)
    _seed_registry(conn)
    conn.commit()
    _insert_memory(conn, "m1", ["tech_stack", "identity"], "dsh")
    _insert_memory(conn, "m2", ["identity"], None)
    conn.commit()
    keys = dict(
        conn.execute("SELECT memory_id, scene_key FROM memories").fetchall()
    )
    assert keys["m1"] == "identity:coding"
    assert keys["m2"] == sk.UNKNOWN
    conn.close()


def test_insert_memory_respects_explicit_scene_key(tmp_path):
    conn = db.connect_memory(tmp_path)
    _seed_registry(conn)
    conn.commit()
    memory_dao.insert_memory(
        conn,
        content="显式场景键",
        memory_type="fact",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["identity"],
        agent_tag="dsh",
        memory_id="m-explicit",
        extra={"scene_key": "manual:override"},
    )
    assert conn.execute(
        "SELECT scene_key FROM memories WHERE memory_id='m-explicit'"
    ).fetchone()[0] == "manual:override"
    conn.close()


# ---------- 回填脚本 ----------


def test_backfill_dry_run_then_apply(tmp_path):
    conn = db.connect_memory(tmp_path)
    _seed_registry(conn)
    conn.commit()
    # 绕过落库钩子，制造"未回填"的老数据
    for mid, dims, tag in (("a", "identity", "dsh"), ("b", "", "dsh"), ("c", "identity", None)):
        conn.execute(
            "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
            "created_at, updated_at, status, agent_tag) VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, "c-" + mid, "fact", 50, "static",
             "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "active", tag),
        )
        if dims:
            conn.execute(
                "INSERT INTO memory_tags (memory_id, dimension_id) VALUES (?,?)", (mid, dims)
            )
    conn.commit()
    conn.close()

    dry = bf.run(tmp_path, apply=False)
    assert dry["candidates"] == 3 and dry["applied"] is False
    conn = db.connect_memory(tmp_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE scene_key IS NOT NULL"
    ).fetchone()[0] == 0, "dry-run 不得写库"
    conn.close()

    # 注意：connect_memory 会走迁移，但不会自动回填历史 scene_key
    export = tmp_path / "unknown.json"
    applied = bf.run(tmp_path, apply=True, export_unknown=str(export))
    assert applied["applied"] is True
    assert applied["unknown"] == 2
    assert applied["unknown_no_tag"] == 1
    assert applied["unknown_no_agent"] == 1
    assert applied["distribution"]["identity:coding"] == 1
    unknown_ids = json.loads(export.read_text(encoding="utf-8"))
    assert set(unknown_ids) == {"b", "c"}
    conn = db.connect_memory(tmp_path)
    assert conn.execute(
        "SELECT scene_key FROM memories WHERE memory_id='a'"
    ).fetchone()[0] == "identity:coding"
    conn.close()
