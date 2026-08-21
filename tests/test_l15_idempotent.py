"""2026-08-22 幂等修复测试：同一 source_ref 重试抽出的「同源 + 同内容」记忆被跳过落库。

防止重试提炼在 L1.5 处造出重复记忆（根因：候选池 prescreen 截断 + LLM 漏判 skip）。
"""

from __future__ import annotations

import httpx
import pytest

from sgme import config
from sgme.engine import l15
from sgme.data import db as db_mod, memory_dao


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _mock_llm_empty() -> httpx.Client:
    """mock LLM：返回空裁决（[]），即不产出任何动作。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "[]"}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- DAO 层：find_active_by_source_ref_content ----------

def test_dao_find_active_by_source_ref_content(mem_conn):
    mid = memory_dao.insert_memory(
        mem_conn, content="用户偏好红色",
        memory_type="persona", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        sources=[("F:1", "session")],
    )
    # 同源同内容（精确）→ 命中
    assert memory_dao.find_active_by_source_ref_content(mem_conn, "F:1", "用户偏好红色") == mid
    # 首尾空白不影响（TRIM 比对）
    assert memory_dao.find_active_by_source_ref_content(mem_conn, "F:1", "  用户偏好红色\n") == mid
    # 不同内容 → None
    assert memory_dao.find_active_by_source_ref_content(mem_conn, "F:1", "用户偏好蓝色") is None
    # 不同 source_ref → None
    assert memory_dao.find_active_by_source_ref_content(mem_conn, "F:2", "用户偏好红色") is None
    # 拒绝态（status != active）→ None
    memory_dao.reject_memory(mem_conn, mid, "测试拒绝")
    assert memory_dao.find_active_by_source_ref_content(mem_conn, "F:1", "用户偏好红色") is None


# ---------- L1.5 集成：重试同源同内容记忆被跳过 ----------

def test_resolve_conflicts_idempotent_skip(mem_conn, cfg):
    # 既有 active 记忆：同源 F:1 + 同内容
    memory_dao.insert_memory(
        mem_conn, content="X 已被 Y 替代",
        memory_type="persona", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=["goals"],
        sources=[("F:1", "session")],
    )

    # 关闭 prescreen（避免测试环境依赖 embedding 端点），用维度召回路径
    cfg["l15"] = {"prescreen": None}

    new_memories = [{
        "content": "X 已被 Y 替代",
        "dimension_ids": ["goals"],
        "memory_type": "persona",
        "priority": 50,
        "time_velocity": "static",
        "source_message_ids": [],
        "supersedes": [],
    }]
    res = l15.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_empty(),
        source_ref="F:1", prompt_version="l1_extraction:v1",
    )

    # 幂等跳过：不新增、计入 skipped
    assert res.stored == [], f"不应新增记忆，实际 stored={res.stored}"
    assert 0 in res.skipped, f"重复记忆应被 skip，skipped={res.skipped}"
    # 库中该内容仍只有 1 条（未造重复）
    rows = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE TRIM(content)=?", ("X 已被 Y 替代",)
    ).fetchone()
    assert rows["c"] == 1


def test_resolve_conflicts_no_source_ref_no_skip(mem_conn, cfg):
    """无 source_ref 时退化为原行为：正常 store（幂等守卫不触发）。"""
    cfg["l15"] = {"prescreen": None}
    new_memories = [{
        "content": "全新事实 Z",
        "dimension_ids": ["goals"],
        "memory_type": "persona",
        "priority": 50,
        "time_velocity": "static",
        "source_message_ids": [],
        "supersedes": [],
    }]
    res = l15.resolve_conflicts(
        new_memories, mem_conn, cfg, client=_mock_llm_empty(),
        source_ref=None, prompt_version="l1_extraction:v1",
    )
    # 无 source_ref → 幂等守卫不触发 → 正常落库（空候选短路 store）
    assert len(res.stored) == 1, f"无 source_ref 应正常 store，实际 stored={res.stored}"
    rows = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE TRIM(content)=?", ("全新事实 Z",)
    ).fetchone()
    assert rows["c"] == 1
