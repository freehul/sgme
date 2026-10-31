"""注入侧 route 过滤（2.0 D-67）：技能/知识候选不得进画像。

背景：v015 实测 12 个真实会话，33% 的记忆 route=skill_candidate（可复用经验），
13% 是 memory。这些技能类记忆如果照样进画像注入，就等于把"怎么做事"当成
"用户是谁"注入——画像会被技术细节淹没。

兼容：1.x 存量记忆 route 为 NULL，必须**照常注入**（COALESCE(route,'memory')）。
"""

from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao


@pytest.fixture
def conn(tmp_path):
    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield mem
    for c in (mem, sess, wiki):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _add(conn, content: str, route: str | None) -> str:
    extra = {"type": "事实", "subject": "self"}
    if route is not None:
        extra["route"] = route
    return memory_dao.insert_memory(
        conn,
        content=content,
        memory_type="fact",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["preferences"],
        occurred_at=None,
        prompt_version="test",
        extra=extra,
    )


def test_skill_and_wiki_candidates_excluded(conn):
    keep = _add(conn, "用户偏好全程中文沟通", "memory")
    _add(conn, "给 agent 的指令要附合法跳过路径", "skill_candidate")
    _add(conn, "某页知识：fnOS 网关配置步骤", "wiki_candidate")

    rows = memory_dao.list_memories_by_dimension(conn, ["preferences"], routes=("memory",))
    ids = {r["memory_id"] for r in rows}
    assert ids == {keep}, "只有 route=memory 的记忆能进画像"


def test_null_route_is_treated_as_memory(conn):
    """1.x 存量记忆 route 为 NULL，不能被过滤掉。"""
    plain = _add(conn, "用户偏好全程中文沟通", None)
    rows = memory_dao.list_memories_by_dimension(conn, ["preferences"], routes=("memory",))
    assert {r["memory_id"] for r in rows} == {plain}


def test_no_routes_param_keeps_old_behavior(conn):
    """不传 routes 时行为不变（检索/统计等调用方不受影响）。"""
    _add(conn, "用户偏好全程中文沟通", "memory")
    _add(conn, "给 agent 的指令要附合法跳过路径", "skill_candidate")
    rows = memory_dao.list_memories_by_dimension(conn, ["preferences"])
    assert len(rows) == 2
