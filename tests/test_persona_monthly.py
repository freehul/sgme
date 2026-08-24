"""ST-35 T-100：月度人格校准测试（LLM 全 mock，零真实调用）。"""

from __future__ import annotations

import pytest

from sgme.data import db, persona_dao
from sgme.engine import persona_monthly


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db.connect_memory(tmp_path)
    yield conn
    conn.close()


def _seed_traits(conn):
    persona_dao.upsert_trait(
        conn, "decision_style", "价值观驱动", evidence_ref="m1"
    )


class TestIsDue:
    def test_never_run_is_due(self, mem_conn):
        assert persona_monthly.is_due(mem_conn) is True

    def test_current_period_not_due(self, mem_conn):
        period = persona_monthly._current_period()
        persona_dao.set_state(mem_conn, "last_run", period)
        assert persona_monthly.is_due(mem_conn) is False


class TestRunCalibration:
    def _patch_extract(self, monkeypatch, data):
        import importlib
        extract_mod = importlib.import_module("sgme.refinery.extract")

        def fake_extract(prompt, schema, llm_cfg, client=None, **kw):
            return data
        monkeypatch.setattr(extract_mod, "extract", fake_extract)

    def test_success_flow(self, mem_conn, monkeypatch):
        _seed_traits(mem_conn)
        self._patch_extract(monkeypatch, {
            "mbti": "INFJ",
            "traits": [
                {"dimension": "decision_style", "value": "价值观驱动",
                 "action": "adjust", "confidence_delta": 0.1},
                {"dimension": "work_style", "value": "计划驱动",
                 "action": "confirm"},
            ],
            "report": "本月倾向稳定。",
            "changes": [],
        })
        result = persona_monthly.run_calibration(mem_conn, {})
        assert result["status"] == "done"
        # 报告落库 + MBTI 锚点 + last_run 状态
        reports = persona_dao.list_reports(mem_conn)
        assert len(reports) == 1 and reports[0]["mbti_result"] == "INFJ"
        hist = persona_dao.get_mbti_history(mem_conn)
        assert hist[-1]["source"] == "llm_monthly"
        assert persona_dao.get_state(mem_conn, "last_run") == result["period"]
        # adjust 生效、confirm 不动
        traits = {t["value"]: t for t in persona_dao.list_traits(mem_conn)}
        assert traits["价值观驱动"]["confidence"] > 0.15

    def test_invalid_mbti_tolerated(self, mem_conn, monkeypatch):
        self._patch_extract(monkeypatch, {
            "mbti": "XXXX", "traits": [], "report": "r", "changes": [],
        })
        result = persona_monthly.run_calibration(mem_conn, {})
        assert result["status"] == "done"
        assert result["mbti"] is None
        assert persona_dao.get_mbti_history(mem_conn) == []

    def test_reentry_lock(self, mem_conn, monkeypatch):
        acquired = persona_monthly.RUN_LOCK.acquire(blocking=False)
        try:
            r = persona_monthly.run_calibration(mem_conn, {})
            assert r["status"] == "running"
        finally:
            persona_monthly.RUN_LOCK.release()

    def test_change_confirmation_two_months(self, mem_conn, monkeypatch):
        """连续两月同向变化才确认。"""
        changes = [{"dimension": "decision_style", "from": "逻辑优先", "to": "价值优先"}]
        # 第一月：observation only
        self._patch_extract(monkeypatch, {
            "mbti": "", "traits": [], "report": "r1", "changes": changes,
        })
        r1 = persona_monthly.run_calibration(mem_conn, {})
        assert r1["confirmed_changes"] == 0
        # 第二月：同向 → confirmed
        persona_dao.set_state(mem_conn, "last_run", "")  # 解锁重跑
        r2 = persona_monthly.run_calibration(mem_conn, {})
        assert r2["confirmed_changes"] == 1
