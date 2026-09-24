"""T-205 C2 测试：TTL 回填策略（ttl_policy 单一实现 + episodic/fact 规则）。

- 记忆级显式 ttl_days 永远优先；ideas 维度强制 None（T-26 铁律）
- episodic = min(维度默认, 30)：status 7d 维持、纯静态维 30d（不可一律 30）
- fact 案 A 变体：动态维随维度 TTL、静态维 90d
- persona/instruction 维持旧行为（动态随维度、静态 None）
- l15._store_memory / l2 接线：委托后语义一致
"""
from __future__ import annotations

import pytest

from sgme.engine.ttl_policy import (
    EPISODIC_MAX_TTL_DAYS,
    FACT_STATIC_TTL_DAYS,
    backfill_ttl,
)


@pytest.fixture
def mem_conn_and_cfg(tmp_path):
    from sgme import config as sgme_config
    from sgme.data import db as db_mod, memory_dao

    cfg = sgme_config.load_config()
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn, cfg
    conn.close()


DIMS = [
    {"id": "status", "ttl_days": 7},
    {"id": "focus", "ttl_days": 30},
    {"id": "goals", "ttl_days": 90},
    {"id": "identity", "ttl_days": None},
    {"id": "tech_stack", "ttl_days": None},
    {"id": "preferences", "ttl_days": None},
    {"id": "ideas", "ttl_days": None},
]


def test_explicit_ttl_wins():
    assert backfill_ttl(14, ["status"], DIMS, memory_type="episodic") == 14


def test_ideas_forces_none():
    assert backfill_ttl(None, ["ideas"], DIMS, memory_type="episodic") is None
    assert backfill_ttl(None, ["ideas", "goals"], DIMS, memory_type="fact") is None
    assert backfill_ttl(14, ["ideas"], DIMS) is None  # 铁律优先级高于显式值


def test_episodic_takes_min_of_dim_and_30():
    # status 7d → 维持语义（不可一律 30 而延长污染）
    assert backfill_ttl(None, ["status"], DIMS, memory_type="episodic") == 7
    # status+goals → min(7, 90) = 7
    assert backfill_ttl(None, ["status", "goals"], DIMS, memory_type="episodic") == 7
    # 纯静态维度（无默认）→ 30 上限
    assert backfill_ttl(None, ["tech_stack"], DIMS, memory_type="episodic") == EPISODIC_MAX_TTL_DAYS
    # focus 30 → min(30, 30) = 30
    assert backfill_ttl(None, ["focus"], DIMS, memory_type="episodic") == 30


def test_fact_case_a_variant():
    # 动态维随维度 TTL（fact 不享受特权）
    assert backfill_ttl(None, ["status"], DIMS, memory_type="fact") == 7
    assert backfill_ttl(None, ["goals"], DIMS, memory_type="fact") == 90
    # 静态维 → 90d 保底
    assert backfill_ttl(None, ["identity"], DIMS, memory_type="fact") == FACT_STATIC_TTL_DAYS
    assert backfill_ttl(None, ["tech_stack"], DIMS, memory_type="fact") == FACT_STATIC_TTL_DAYS
    # 混合（静态+动态）→ 取最小（最严约束）
    assert backfill_ttl(None, ["identity", "status"], DIMS, memory_type="fact") == 7


def test_persona_instruction_legacy_behavior():
    # 动态维度默认照旧
    assert backfill_ttl(None, ["status"], DIMS, memory_type="persona") == 7
    assert backfill_ttl(None, ["status"], DIMS, memory_type="instruction") == 7
    # 静态维度 NULL（永不过期，长期资产）
    assert backfill_ttl(None, ["identity"], DIMS, memory_type="persona") is None
    assert backfill_ttl(None, ["tech_stack"], DIMS, memory_type="instruction") is None


def test_unknown_type_legacy_behavior():
    assert backfill_ttl(None, ["status"], DIMS) == 7
    assert backfill_ttl(None, ["identity"], DIMS) is None


def test_l15_store_memory_wires_memory_type(mem_conn_and_cfg):
    """l15 落库链：episodic 打静态维 → 落库 ttl=30（C2 生效穿过 _store_memory）。"""
    conn, cfg = mem_conn_and_cfg
    from sgme.engine.l15 import _store_memory

    mid = _store_memory(
        conn,
        {"content": "2026-09-20 部署了新版本网关", "memory_type": "episodic",
         "priority": 60, "time_velocity": "static",
         "dimensions": ["tech_stack"]},
        cfg["dimensions"],
    )
    row = conn.execute(
        "SELECT ttl_days FROM memories WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row[0] == EPISODIC_MAX_TTL_DAYS
