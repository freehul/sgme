"""ST-35 T-98：persona_dao 单元测试。

覆盖：表迁移幂等 / 特质累积 upsert / supersession / 软删除 /
MBTI 校验与轨迹 / 报告存取 / persona_state 计时状态。
"""

from __future__ import annotations

import pytest

from sgme.data import db, persona_dao


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db.connect_memory(tmp_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


class TestMigration:
    def test_tables_created(self, mem_conn):
        names = {
            r[0]
            for r in mem_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "persona_traits",
            "user_mbti",
            "persona_reports",
            "persona_state",
        } <= names

    def test_migration_idempotent(self, tmp_path):
        conn1 = db.connect_memory(tmp_path)
        conn1.close()
        # 重开连接重跑迁移不抛异常、无重复副作用
        conn2 = db.connect_memory(tmp_path)
        cnt = conn2.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='persona_traits'"
        ).fetchone()[0]
        conn2.close()
        assert cnt == 1

    def test_state_table_upsert(self, mem_conn):
        persona_dao.set_state(mem_conn, "last_run", "2026-08")
        persona_dao.set_state(mem_conn, "last_run", "2026-09")
        assert persona_dao.get_state(mem_conn, "last_run") == "2026-09"
        assert persona_dao.get_state(mem_conn, "missing") is None


# ---------------------------------------------------------------------------
# 特质累积
# ---------------------------------------------------------------------------


class TestTraits:
    def test_new_trait_then_accumulate(self, mem_conn):
        t1 = persona_dao.upsert_trait(
            mem_conn,
            "decision_style",
            "价值观驱动",
            evidence_ref="mem-001",
        )
        assert t1["evidence_count"] == 1
        assert t1["confidence"] == pytest.approx(0.15)
        t2 = persona_dao.upsert_trait(
            mem_conn,
            "decision_style",
            "价值观驱动",
            evidence_ref="mem-002",
            scene_context="general",
        )
        assert t2["trait_id"] == t1["trait_id"]
        assert t2["evidence_count"] == 2
        assert t2["confidence"] == pytest.approx(0.30)
        assert t2["evidence_refs"] == ["mem-001", "mem-002"]

    def test_scene_context_isolates(self, mem_conn):
        a = persona_dao.upsert_trait(
            mem_conn, "expression", "内敛", scene_context="work"
        )
        b = persona_dao.upsert_trait(
            mem_conn, "expression", "外显", scene_context="value"
        )
        assert a["trait_id"] != b["trait_id"]
        traits = persona_dao.list_traits(mem_conn, scene_context="value")
        assert len(traits) == 1 and traits[0]["value"] == "外显"

    def test_confidence_caps_at_one(self, mem_conn):
        for i in range(10):
            t = persona_dao.upsert_trait(
                mem_conn, "plan", "计划驱动", evidence_ref=f"m-{i}"
            )
        assert t["confidence"] == 1.0

    def test_invalid_source_rejected(self, mem_conn):
        with pytest.raises(ValueError):
            persona_dao.upsert_trait(mem_conn, "d", "v", source="bogus")

    def test_supersede(self, mem_conn):
        old = persona_dao.upsert_trait(mem_conn, "info", "细节导向")
        new = persona_dao.upsert_trait(mem_conn, "info", "宏观直觉")
        ok = persona_dao.supersede_trait(mem_conn, old["trait_id"], new["trait_id"])
        assert ok
        rows = persona_dao.list_traits(mem_conn, dimension="info")
        assert len(rows) == 1 and rows[0]["trait_id"] == new["trait_id"]
        all_rows = mem_conn.execute(
            "SELECT status, superseded_by FROM persona_traits WHERE trait_id=?",
            (old["trait_id"],),
        ).fetchone()
        assert all_rows["status"] == "superseded"
        assert all_rows["superseded_by"] == new["trait_id"]

    def test_reject_soft_delete(self, mem_conn):
        t = persona_dao.upsert_trait(mem_conn, "d", "v")
        assert persona_dao.reject_trait(mem_conn, t["trait_id"])
        assert persona_dao.list_traits(mem_conn) == []
        still = mem_conn.execute(
            "SELECT status FROM persona_traits WHERE trait_id=?", (t["trait_id"],)
        ).fetchone()
        assert still["status"] == "rejected"  # 原件仍在

    def test_min_confidence_filter(self, mem_conn):
        persona_dao.upsert_trait(mem_conn, "a", "x", confidence_step=0.15)
        hi = None
        for i in range(5):
            hi = persona_dao.upsert_trait(mem_conn, "b", "y", evidence_ref=f"m-{i}")
        got = persona_dao.list_traits(mem_conn, min_confidence=0.6)
        assert [t["trait_id"] for t in got] == [hi["trait_id"]]


# ---------------------------------------------------------------------------
# MBTI 锚点
# ---------------------------------------------------------------------------


class TestMbti:
    def test_add_and_history(self, mem_conn):
        persona_dao.add_mbti_record(mem_conn, "intj", note="2024 测")
        persona_dao.add_mbti_record(mem_conn, "INFJ")
        hist = persona_dao.get_mbti_history(mem_conn)
        assert [h["mbti_type"] for h in hist] == ["INTJ", "INFJ"]
        latest = persona_dao.get_latest_mbti(mem_conn)
        assert latest["mbti_type"] == "INFJ"

    def test_invalid_type(self, mem_conn):
        with pytest.raises(ValueError):
            persona_dao.add_mbti_record(mem_conn, "ABCD")
        with pytest.raises(ValueError):
            persona_dao.add_mbti_record(mem_conn, "INT")

    def test_monthly_source_allowed(self, mem_conn):
        r = persona_dao.add_mbti_record(
            mem_conn, "INFJ", source="llm_monthly", note="月度校准"
        )
        assert r["source"] == "llm_monthly"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


class TestReports:
    def test_save_and_list(self, mem_conn):
        r1 = persona_dao.save_report(
            mem_conn,
            "2026-07",
            "报告正文",
            mbti_result="INTJ",
            trait_changes=[{"dim": "decision_style"}],
        )
        r2 = persona_dao.save_report(mem_conn, "2026-08", "八月报告")
        assert r1["mbti_result"] == "INTJ"
        assert r1["trait_changes"] == [{"dim": "decision_style"}]
        listed = persona_dao.list_reports(mem_conn)
        assert [r["period"] for r in listed] == ["2026-08", "2026-07"]
        got = persona_dao.get_report(mem_conn, r2["report_id"])
        assert got["report"] == "八月报告"
        assert persona_dao.get_report(mem_conn, "nope") is None
