"""tests/test_near_dup_bulk.py：批量丢弃（2026-11-08 用户拍板 ≥0.92 直接丢）。

覆盖：DAO count/bulk 的 pending+score 过滤；ops dry-run/apply 语义；行保留可审计。
"""
from __future__ import annotations

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_dao, near_dup_dao
from sgme.operations import review as review_ops


@pytest.fixture
def cfg():
    from sgme import config
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _seed(mem_conn):
    for i, score in enumerate((0.91, 0.93, 0.95)):
        near_dup_dao.upsert_hit(mem_conn, f"内容-{i}", f"mem-{i}", score)


def test_dao_bulk_discard_filters_pending_and_score(mem_conn):
    _seed(mem_conn)
    assert near_dup_dao.count_pending_ge(mem_conn, 0.92) == 2
    assert near_dup_dao.bulk_discard(mem_conn, min_score=0.92, reason="auto_backlog") == 2
    assert near_dup_dao.count_pending_ge(mem_conn, 0.92) == 0
    rows = mem_conn.execute(
        "SELECT content, status, discard_reason FROM near_dup_quarantine ORDER BY content"
    ).fetchall()
    by_content = {r[0]: (r[1], r[2]) for r in rows}
    assert by_content["内容-0"] == ("pending", None)
    assert by_content["内容-1"] == ("discarded", "auto_backlog")
    assert by_content["内容-2"] == ("discarded", "auto_backlog")


def test_ops_bulk_dry_run_then_apply(mem_conn):
    _seed(mem_conn)
    dry = review_ops.bulk_discard_quarantine(mem_conn, 0.92, "auto_backlog", apply=False)
    assert dry.ok and dry.data["matched"] == 2 and dry.data["discarded"] == 0
    assert near_dup_dao.count_pending_ge(mem_conn, 0.92) == 2  # dry-run 不写
    res = review_ops.bulk_discard_quarantine(mem_conn, 0.92, "auto_backlog", apply=True)
    assert res.ok and res.data["matched"] == 2 and res.data["discarded"] == 2
    assert near_dup_dao.count_pending_ge(mem_conn, 0.92) == 0


def test_ops_bulk_rejects_invalid_score(mem_conn):
    from sgme.operations.errors import InvalidArgs

    with pytest.raises(InvalidArgs):
        review_ops.bulk_discard_quarantine(mem_conn, 1.2, "x", apply=False)
