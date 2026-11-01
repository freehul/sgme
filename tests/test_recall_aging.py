"""D-73 测试：召回加固的幂律有效期放大 + TTL 处置审计（recall_aging）。

- compute_ttl_multiplier：幂律放大、单调递增、上限 cap（默认 3×）、recall_count<=0 归一
- apply_recall_aging：读 memory_stats.recall_count + memories.memory_type；
  forget_request 豁免（永不放大）；只计算返回、不写 memories 表
- ensure_ttl_disposition_table：审计表建表幂等（重复建无害）
- record_disposition：五种合法处置写入；非法值抛 ValueError
"""
from __future__ import annotations

import math

import pytest

from sgme.engine.recall_aging import (
    compute_ttl_multiplier,
)

# 放大系数基准值（1 + log2(1+n) * 0.5 的关键锚点，用于 Green 后防公式漂移）
_CASES = {0: 1.0, 1: 1.5, 3: 2.0, 7: 2.5, 15: 3.0}


@pytest.mark.parametrize(("recall_count", "expected"), sorted(_CASES.items()))
def test_multiplier_formula_anchors(recall_count: int, expected: float):
    """锚点值：1 + log2(1+n)*0.5——n=1→1.5、n=7→2.5、n=15→3.0（恰好顶到上限）。"""
    assert compute_ttl_multiplier(recall_count) == pytest.approx(expected)


def test_multiplier_nonpositive_recall_is_one():
    """recall_count<=0（未命中/非法输入）→ 1.0（不放大也不缩小）。"""
    assert compute_ttl_multiplier(0) == 1.0
    assert compute_ttl_multiplier(-5) == 1.0


def test_multiplier_monotonically_increasing():
    """单调递增：命中越多放大越大，绝不回落。"""
    counts = [0, 1, 2, 3, 4, 5, 8, 13, 21, 50, 100, 1000]
    values = [compute_ttl_multiplier(n) for n in counts]
    assert all(b >= a for a, b in zip(values, values[1:]))
    # cap 之前严格递增；到达 cap 后恒等于 cap（平台期不再增）
    capped = [v for v in values if v >= 3.0]
    below = [v for v in values if v < 3.0]
    assert all(b > a for a, b in zip(below, below[1:]))
    assert capped and all(v == 3.0 for v in capped)
    assert below == values[: len(below)]  # cap 值只出现在序列尾部


def test_multiplier_capped_at_three():
    """上限 3×：无限命中也不会超过默认 cap=3.0。"""
    for n in (16, 63, 100, 10_000, 10**9):
        assert compute_ttl_multiplier(n) == pytest.approx(3.0)
        assert compute_ttl_multiplier(n) <= 3.0


def test_multiplier_custom_cap():
    """自定义 cap 同样封顶（cap=2.0 → 最大 2.0；cap=1.0 → 恒 1.0）。"""
    assert compute_ttl_multiplier(100, cap=2.0) == pytest.approx(2.0)
    assert compute_ttl_multiplier(0, cap=2.0) == pytest.approx(1.0)
    assert compute_ttl_multiplier(1, cap=1.0) == pytest.approx(1.0)
    assert compute_ttl_multiplier(1000, cap=1.0) == pytest.approx(1.0)


def test_multiplier_returns_float():
    """契约：返回 float。"""
    assert isinstance(compute_ttl_multiplier(7), float)


# ============ apply_recall_aging（真实 memory.db 连接） ============


@pytest.fixture
def mem_conn(tmp_path):
    from sgme.data import db as db_mod

    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


def _insert_memory(conn, memory_id: str, memory_type: str = "fact") -> None:
    """插入一条最小合法记忆（memories 必填列齐全）。"""
    conn.execute(
        """
        INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,
                              ttl_days, created_at, updated_at, status)
        VALUES (?, '测试记忆内容', ?, 50, 'dynamic', 30, '2026-11-01T00:00:00Z',
                '2026-11-01T00:00:00Z', 'active')
        """,
        (memory_id, memory_type),
    )
    conn.commit()


def _set_recall_count(conn, memory_id: str, recall_count: int) -> None:
    """写入 memory_stats（模拟 inject 链路累计的命中数）。"""
    conn.execute(
        """
        INSERT INTO memory_stats (memory_id, last_recalled_at, recall_count, last_injected_at)
        VALUES (?, NULL, ?, '2026-11-01T00:00:00Z')
        ON CONFLICT(memory_id) DO UPDATE SET recall_count = excluded.recall_count
        """,
        (memory_id, recall_count),
    )
    conn.commit()


def test_apply_aging_amplifies_by_recall_count(mem_conn):
    """普通记忆：recall_count=7 → 倍率 2.5，ttl_days 30 → 75（int 截断）。"""
    _insert_memory(mem_conn, "mem-hot", memory_type="fact")
    _set_recall_count(mem_conn, "mem-hot", 7)
    from sgme.engine.recall_aging import apply_recall_aging

    out = apply_recall_aging(mem_conn, "mem-hot", 30)
    assert out == {"ttl_days": 75, "multiplier": pytest.approx(2.5), "exempt": False}
    assert isinstance(out["ttl_days"], int)
    assert isinstance(out["multiplier"], float)


def test_apply_aging_no_stats_row_defaults_to_base(mem_conn):
    """无 memory_stats 行（从未被命中）→ 倍率 1.0，ttl 不变，非豁免。"""
    _insert_memory(mem_conn, "mem-cold", memory_type="fact")
    from sgme.engine.recall_aging import apply_recall_aging

    out = apply_recall_aging(mem_conn, "mem-cold", 30)
    assert out["ttl_days"] == 30
    assert out["multiplier"] == pytest.approx(1.0)
    assert out["exempt"] is False


def test_apply_aging_missing_memory_row_defaults_to_base(mem_conn):
    """memories 无此行（异常输入防御）→ 按 base 返回、不放大、不抛错。"""
    from sgme.engine.recall_aging import apply_recall_aging

    out = apply_recall_aging(mem_conn, "mem-ghost", 30)
    assert out["ttl_days"] == 30
    assert out["multiplier"] == pytest.approx(1.0)
    assert out["exempt"] is False


def test_apply_aging_forget_request_exempt(mem_conn):
    """forget_request 豁免：命中再多也永不放大（清理指令不能被自己续命）。"""
    _insert_memory(mem_conn, "mem-forget", memory_type="forget_request")
    _set_recall_count(mem_conn, "mem-forget", 100)
    from sgme.engine.recall_aging import apply_recall_aging

    out = apply_recall_aging(mem_conn, "mem-forget", 30)
    assert out == {"ttl_days": 30, "multiplier": 1.0, "exempt": True,
                   "reason": "forget_request"}


def test_apply_aging_does_not_write_memories(mem_conn):
    """只计算返回、不落库：memories.ttl_days 调用前后不变。"""
    _insert_memory(mem_conn, "mem-readonly", memory_type="fact")
    _set_recall_count(mem_conn, "mem-readonly", 15)
    from sgme.engine.recall_aging import apply_recall_aging

    before = mem_conn.execute(
        "SELECT ttl_days FROM memories WHERE memory_id='mem-readonly'"
    ).fetchone()[0]
    apply_recall_aging(mem_conn, "mem-readonly", 30)
    after = mem_conn.execute(
        "SELECT ttl_days FROM memories WHERE memory_id='mem-readonly'"
    ).fetchone()[0]
    assert before == after == 30


# ============ ensure_ttl_disposition_table / record_disposition ============


def test_ensure_ttl_disposition_table_creates_schema(mem_conn):
    """建表：ttl_disposition 存在且含契约约定的全部列。"""
    from sgme.engine.recall_aging import ensure_ttl_disposition_table

    ensure_ttl_disposition_table(mem_conn)
    row = mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ttl_disposition'"
    ).fetchone()
    assert row is not None
    cols = {r[1] for r in mem_conn.execute("PRAGMA table_info(ttl_disposition)")}
    assert cols == {"id", "memory_id", "disposition", "m_score", "renew_count",
                    "old_expire_at", "new_expire_at", "created_at"}


def test_ensure_ttl_disposition_table_idempotent(mem_conn):
    """重复建表无害：连调两次不抛错、结构不变。"""
    from sgme.engine.recall_aging import ensure_ttl_disposition_table

    ensure_ttl_disposition_table(mem_conn)
    ensure_ttl_disposition_table(mem_conn)  # 第二次必须安静通过
    assert mem_conn.execute(
        "SELECT COUNT(*) FROM ttl_disposition"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("disposition", ["valid_expired", "renewed", "anchor", "sleeping", "archived"])
def test_record_disposition_accepts_valid(mem_conn, disposition):
    """五种合法处置各写一行，字段完整落库。"""
    from sgme.engine.recall_aging import ensure_ttl_disposition_table, record_disposition

    ensure_ttl_disposition_table(mem_conn)
    record_disposition(
        mem_conn, "mem-x", disposition,
        old_expire_at="2026-10-25T00:00:00Z", new_expire_at="2026-12-25T00:00:00Z",
        m_score=0.75, renew_count=2,
    )
    row = mem_conn.execute(
        "SELECT memory_id, disposition, m_score, renew_count, old_expire_at, new_expire_at "
        "FROM ttl_disposition WHERE disposition=?",
        (disposition,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("mem-x", disposition, 0.75, 2,
                          "2026-10-25T00:00:00Z", "2026-12-25T00:00:00Z")
    created = mem_conn.execute(
        "SELECT created_at FROM ttl_disposition WHERE disposition=?", (disposition,)
    ).fetchone()[0]
    assert created  # 落库时自动补 created_at


def test_record_disposition_defaults(mem_conn):
    """m_score/renew_count 缺省 0.0/0；old/new_expire_at 允许 None。"""
    from sgme.engine.recall_aging import ensure_ttl_disposition_table, record_disposition

    ensure_ttl_disposition_table(mem_conn)
    record_disposition(mem_conn, "mem-y", "sleeping", None, None)
    row = mem_conn.execute(
        "SELECT m_score, renew_count, old_expire_at, new_expire_at "
        "FROM ttl_disposition WHERE memory_id='mem-y'"
    ).fetchone()
    assert tuple(row) == (0.0, 0, None, None)


@pytest.mark.parametrize("bad", ["deleted", "purge", "", "RENEWED", "expired"])
def test_record_disposition_rejects_invalid(mem_conn, bad):
    """非法 disposition 一律 ValueError（大小写敏感、不静默纠错）。"""
    from sgme.engine.recall_aging import ensure_ttl_disposition_table, record_disposition

    ensure_ttl_disposition_table(mem_conn)
    with pytest.raises(ValueError):
        record_disposition(mem_conn, "mem-z", bad, None, None)
    # 拒绝后不落任何行
    assert mem_conn.execute("SELECT COUNT(*) FROM ttl_disposition").fetchone()[0] == 0


def test_multiplier_log_math_sanity():
    """公式数学自检：log2(1+1)=1 → 1.5（防实现偏离幂律定义）。"""
    assert compute_ttl_multiplier(1) == pytest.approx(1 + math.log2(2) * 0.5)
