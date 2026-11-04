"""待审记忆队列操作层测试（设计决策 D-30，SGME 2.0）。

L2 提炼会产出 needs_review=1 的记忆（D-23 待审闸门）：不进主注入、不进人格
证据，只在本队列可见，等待人工放行（approve）或拒绝（reject）；超期（默认
7 天）未处理的由 auto_resolve_expired 按保守直存消化（放行并追加
「未经人工确认」标记，去重）。

测试策略：tmp 目录建真实 memory.db schema（db.connect_memory，含 2.0 迁移
补列 needs_review/notes 等），插入可控 created_at/needs_review 的记忆行，
对 operations.review 的四个函数做行为断言。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sgme.data import db
from sgme.operations import review


# ---------- 夹具辅助 ----------

def _iso(days_ago: float = 0.0) -> str:
    """相对当前的 UTC ISO 时间戳（库内定宽格式，字典序 == 时间序）。"""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_conn(tmp_path):
    """建真实 memory.db schema（connect_memory 负责建表 + 2.0 列迁移）。"""
    return db.connect_memory(tmp_path / "data")


def _insert_dim(conn, dim_id: str) -> None:
    """注册维度（memory_tags 外键要求 dimension_registry 先有行）。"""
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry"
        "(id, display_name, category, time_velocity, active, created_at) "
        "VALUES (?, ?, 'persona', 'static', 1, ?)",
        (dim_id, dim_id, _iso()),
    )


def _insert_memory(
    conn,
    memory_id: str,
    *,
    days_ago: float = 0.0,
    needs_review: int = 1,
    status: str = "active",
    notes: str | None = None,
    dimensions: tuple[str, ...] = (),
) -> None:
    """按测试需要插入一条记忆（可选打维度标签，tag 前先注册维度）。"""
    conn.execute(
        "INSERT INTO memories(memory_id, content, memory_type, priority, time_velocity, "
        "created_at, updated_at, status, needs_review, notes) "
        "VALUES (?, ?, 'persona', 50, 'static', ?, ?, ?, ?, ?)",
        (memory_id, f"内容-{memory_id}", _iso(days_ago), _iso(), status, needs_review, notes),
    )
    for dim in dimensions:
        _insert_dim(conn, dim)
        conn.execute(
            "INSERT INTO memory_tags(memory_id, dimension_id) VALUES (?, ?)",
            (memory_id, dim),
        )
    conn.commit()


# ---------- list_pending：待审列表 ----------

class TestListPending:
    def test_returns_only_pending_active_sorted_desc(self, tmp_path):
        """只返回 needs_review=1 且 status='active' 的记忆，按 created_at 倒序。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "old", days_ago=2.0)
        _insert_memory(conn, "new", days_ago=1.0)
        _insert_memory(conn, "approved", needs_review=0)    # 已放行 → 不在队列
        _insert_memory(conn, "rejected", status="rejected")  # 已拒绝 → 不在队列

        out = review.list_pending(conn)

        assert out.data["total"] == 2
        assert [it["memory_id"] for it in out.data["items"]] == ["new", "old"]

    def test_pagination_limit_offset(self, tmp_path):
        """limit/offset 分页：total 始终是过滤后总数，不受分页影响。"""
        conn = _make_conn(tmp_path)
        for i, age in enumerate((3.0, 2.0, 1.0)):
            _insert_memory(conn, f"m{i}", days_ago=age)

        page1 = review.list_pending(conn, limit=2, offset=0)
        page2 = review.list_pending(conn, limit=2, offset=2)

        assert page1.data["total"] == 3 and len(page1.data["items"]) == 2
        assert [it["memory_id"] for it in page1.data["items"]] == ["m2", "m1"]
        assert page2.data["total"] == 3 and len(page2.data["items"]) == 1
        assert page2.data["items"][0]["memory_id"] == "m0"

    def test_dimension_filter_via_memory_tags(self, tmp_path):
        """dimension 过滤走 memory_tags 关联；不过滤时全量返回。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "with_dim", dimensions=("identity",))
        _insert_memory(conn, "other", dimensions=("preferences",))

        out = review.list_pending(conn, dimension="identity")

        assert out.data["total"] == 1
        assert out.data["items"][0]["memory_id"] == "with_dim"

    def test_pending_items_include_dimensions(self, tmp_path):
        """B292：列表一次批量带出 dimensions（WebUI 不再逐条补全）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "with_dim", dimensions=("identity",))
        _insert_memory(conn, "no_dim")
        out = review.list_pending(conn)
        by = {it["memory_id"]: it for it in out.data["items"]}
        assert by["with_dim"]["dimensions"] == ["identity"]
        assert by["no_dim"]["dimensions"] == []
        assert review.list_pending(conn).data["total"] == 2


# ---------- approve：人工放行 ----------

class TestApprove:
    def test_approve_clears_needs_review(self, tmp_path):
        """放行 = 置 needs_review=0，记忆回到正常可见状态。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1")

        out = review.approve(conn, "m1")

        assert out.ok is True and out.data["memory_id"] == "m1"
        row = conn.execute(
            "SELECT needs_review, status FROM memories WHERE memory_id='m1'"
        ).fetchone()
        assert row["needs_review"] == 0 and row["status"] == "active"
        # 放行后退出待审队列
        assert review.list_pending(conn).data["total"] == 0

    def test_approve_with_note_appends_to_notes(self, tmp_path):
        """note 非空时写入 notes 列（含放行时间戳，可读回原文）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1")

        out = review.approve(conn, "m1", note="用户确认过，放心")

        assert out.ok is True
        notes = conn.execute(
            "SELECT notes FROM memories WHERE memory_id='m1'"
        ).fetchone()[0]
        assert "用户确认过，放心" in notes

    def test_approve_without_note_leaves_notes_untouched(self, tmp_path):
        """note 为 None 时不写 notes（原值保持）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1", notes="既有备注")

        review.approve(conn, "m1")

        notes = conn.execute(
            "SELECT notes FROM memories WHERE memory_id='m1'"
        ).fetchone()[0]
        assert notes == "既有备注"

    def test_approve_not_found(self, tmp_path):
        """不存在的记忆 → ok=False + error=not_found。"""
        conn = _make_conn(tmp_path)
        out = review.approve(conn, "ghost")

        assert out.ok is False and out.error_code == "ERR_NOT_FOUND"

    def test_approve_idempotent(self, tmp_path):
        """幂等：已放行的记忆再放行不报错、notes 不重复追加。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1")
        review.approve(conn, "m1", note="第一次放行")

        out = review.approve(conn, "m1", note="第二次放行")

        assert out.ok is True and out.data["memory_id"] == "m1"
        notes = conn.execute(
            "SELECT notes FROM memories WHERE memory_id='m1'"
        ).fetchone()[0]
        assert notes.count("第一次放行") == 1
        assert "第二次放行" not in notes


# ---------- reject：人工拒绝 ----------

class TestReject:
    def test_reject_sets_status_and_reason(self, tmp_path):
        """拒绝 = status='rejected' + reject_reason（软删，可溯源可恢复）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1")

        out = review.reject(conn, "m1", "事实有误")

        assert out.ok is True and out.data["memory_id"] == "m1"
        row = conn.execute(
            "SELECT status, reject_reason, rejected_at FROM memories WHERE memory_id='m1'"
        ).fetchone()
        assert row["status"] == "rejected"
        assert row["reject_reason"] == "事实有误"
        assert row["rejected_at"]  # 留痕：拒绝时刻
        # 拒绝后退出待审队列
        assert review.list_pending(conn).data["total"] == 0

    def test_reject_not_found(self, tmp_path):
        """不存在的记忆 → ok=False + error=not_found。"""
        conn = _make_conn(tmp_path)
        out = review.reject(conn, "ghost", "原因")

        assert out.ok is False and out.error_code == "ERR_NOT_FOUND"

    def test_reject_idempotent(self, tmp_path):
        """幂等：已拒绝的记忆再拒绝不报错、reason 不被覆盖（保留首次理由）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1")
        review.reject(conn, "m1", "第一次理由")

        out = review.reject(conn, "m1", "第二次理由")

        assert out.ok is True and out.data["memory_id"] == "m1"
        reason = conn.execute(
            "SELECT reject_reason FROM memories WHERE memory_id='m1'"
        ).fetchone()[0]
        assert reason == "第一次理由"


# ---------- auto_resolve_expired：超期保守直存 ----------

class TestAutoResolveExpired:
    def test_six_days_not_resolved(self, tmp_path):
        """6 天前的待审记忆：未超 7 天期限，不消化。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1", days_ago=6.0)

        out = review.auto_resolve_expired(conn, days=7)

        assert out.ok is True and out.data["resolved"] == 0
        row = conn.execute(
            "SELECT needs_review, notes FROM memories WHERE memory_id='m1'"
        ).fetchone()
        assert row["needs_review"] == 1
        assert row["notes"] is None  # 未动 notes

    def test_eight_days_resolved_with_mark(self, tmp_path):
        """8 天前的待审记忆：超期 → 保守直存（放行 + notes 追加标记）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1", days_ago=8.0)

        out = review.auto_resolve_expired(conn, days=7)

        assert out.ok is True and out.data["resolved"] == 1
        row = conn.execute(
            "SELECT needs_review, notes FROM memories WHERE memory_id='m1'"
        ).fetchone()
        assert row["needs_review"] == 0
        assert review.AUTO_RESOLVE_MARK in row["notes"]

    def test_resolves_only_expired_among_mixed_ages(self, tmp_path):
        """混合时长：只消化超期行，6 天的保持待审；返回计数为超期行数。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "old1", days_ago=10.0)
        _insert_memory(conn, "old2", days_ago=8.0)
        _insert_memory(conn, "young", days_ago=6.0)

        out = review.auto_resolve_expired(conn, days=7)

        assert out.ok is True and out.data["resolved"] == 2
        remaining = review.list_pending(conn)
        assert remaining.data["total"] == 1
        assert remaining.data["items"][0]["memory_id"] == "young"
        # 已消化行不再出现在任何过滤结果的待审队列
        assert review.list_pending(conn, dimension=None).data["items"]

    def test_auto_resolve_mark_is_deduped(self, tmp_path):
        """标记去重：notes 已含「未经人工确认」时不再重复追加。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1", days_ago=8.0, notes="既有备注")
        # 预置一条已带标记的超期待审（模拟上次消化被打断后重跑）
        mark = review.AUTO_RESOLVE_MARK
        conn.execute(
            "UPDATE memories SET notes = ? WHERE memory_id = 'm1'",
            (f"既有备注\n[{_iso()}] {mark}",),
        )
        conn.commit()

        out = review.auto_resolve_expired(conn, days=7)

        assert out.ok is True and out.data["resolved"] == 1
        notes = conn.execute(
            "SELECT notes FROM memories WHERE memory_id='m1'"
        ).fetchone()[0]
        assert notes.count(mark) == 1
        assert "既有备注" in notes

    def test_rerun_is_noop(self, tmp_path):
        """幂等：消化完成后重跑返回 {"resolved": 0}，notes 不变。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1", days_ago=8.0, notes="既有备注")

        first = review.auto_resolve_expired(conn, days=7)
        notes_after_first = conn.execute(
            "SELECT notes FROM memories WHERE memory_id='m1'"
        ).fetchone()[0]
        second = review.auto_resolve_expired(conn, days=7)

        assert first.ok is True and first.data["resolved"] == 1 and second.data["resolved"] == 0
        assert conn.execute(
            "SELECT notes FROM memories WHERE memory_id='m1'"
        ).fetchone()[0] == notes_after_first

    def test_default_days_is_seven(self, tmp_path):
        """days 缺省 = 7（契约默认值）。"""
        conn = _make_conn(tmp_path)
        _insert_memory(conn, "m1", days_ago=8.0)

        out = review.auto_resolve_expired(conn)

        assert out.ok is True and out.data["resolved"] == 1
