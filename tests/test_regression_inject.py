# -*- coding: utf-8 -*-
"""tests/test_regression_inject.py：9.4 #9 注入回归对照器（只读）回归。"""
from __future__ import annotations

from pathlib import Path

import pytest

from sgme import config as sgme_config
from sgme import regression_inject as reg
from sgme.data import db
from sgme.data import memory_dao


@pytest.fixture()
def conn(tmp_path):
    c = db.connect_memory(tmp_path)
    cfg = sgme_config.load_config()
    memory_dao.import_registry(c, cfg["dimensions"], cfg["aliases"])
    memory_dao.insert_memory(
        c, "主人偏好短句、先给结论。", memory_type="preference", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-id-1",
    )
    memory_dao.insert_memory(
        c, "技术栈用 Python + SQLite。", memory_type="fact", priority=80,
        time_velocity="static", ttl_days=None, dimension_ids=["tech_stack"],
        agent_tag="dsh", memory_id="m-tech-1",
    )
    c.commit()
    yield c
    c.close()


def test_run_ten_queries_deterministic(conn):
    a = reg.run_queries(conn)
    b = reg.run_queries(conn)
    assert len(a) == 10 and len(b) == 10
    assert [q["hash"] for q in a] == [q["hash"] for q in b]
    names = {q["name"] for q in a}
    assert {"身份", "技术栈", "空分类"} <= names
    idq = [q for q in a if q["name"] == "身份"][0]
    assert idq["count"] >= 1


def test_compare_pass_and_fail(conn, tmp_path):
    base = reg.run_queries(conn)
    same = reg.compare_results(reg.run_queries(conn), base)
    assert same["passed"] is True and same["changed"] == 0
    # 改一条内容 → 对应查询哈希变化，差异率 10% > 1%
    conn.execute("UPDATE memories SET content='改过了' WHERE memory_id='m-id-1'")
    conn.commit()
    diff = reg.compare_results(reg.run_queries(conn), base)
    assert diff["passed"] is False
    assert any(d["name"] == "身份" for d in diff["diffs"])


def test_save_and_load_baseline(conn, tmp_path):
    path = tmp_path / "baseline.json"
    reg.save_baseline(reg.run_queries(conn), path)
    loaded = reg.load_baseline(path)
    assert len(loaded) == 10


def test_scenes_facts_inject_has_no_scene_path(conn):
    facts = reg.scenes_facts(conn)
    assert facts["inject_has_scene_path"] is False
    assert facts["inject_scene_refs"] == 0
    assert "scenes" in facts["scenes_rows"]
