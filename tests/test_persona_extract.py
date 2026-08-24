"""ST-35 T-99：实时规则特质抽取测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sgme.data import db, persona_dao
from sgme.engine import persona_extract


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db.connect_memory(tmp_path)
    yield conn
    conn.close()


def _result(memories):
    return SimpleNamespace(file_id="f-1", memories=memories, status="refined")


class TestMatchMemories:
    def test_keyword_hit(self, mem_conn):
        rules = persona_extract.DEFAULT_RULES
        hits = persona_extract._match_memories(
            [{"content": "用户坚守原件永不删底线"}], rules
        )
        assert "decision_style" in hits
        assert hits["decision_style"]["原则先于利益"] == 1

    def test_no_hit(self, mem_conn):
        hits = persona_extract._match_memories(
            [{"content": "今天天气不错"}], persona_extract.DEFAULT_RULES
        )
        assert hits == {}

    def test_same_value_capped(self, mem_conn):
        """单次提炼同一 value 最多 MAX_EVIDENCE_PER_DIM_PER_RUN 条证据。"""
        mems = [{"content": f"沉没成本不参与决策-{i}"} for i in range(5)]
        hits = persona_extract._match_memories(
            mems,
            [{"dimension": "d", "values": [{"value": "v", "keywords": ["沉没成本"]}]}],
        )
        assert hits["d"]["v"] == persona_extract.MAX_EVIDENCE_PER_DIM_PER_RUN


class TestExtractAndStore:
    def test_store_and_accumulate(self, mem_conn):
        r = _result([
            {"memory_id": "m1", "content": "用户坚持文档第一，动手前先报备方案"},
            {"memory_id": "m2", "content": "拒绝以 mock 数据替代真实链路验证"},
        ])
        stats = persona_extract.extract_and_store(r, mem_conn, {})
        assert stats["enabled"] is True
        assert stats["evidence_added"] >= 2
        traits = persona_dao.list_traits(mem_conn)
        vals = {t["value"] for t in traits}
        assert "计划驱动" in vals
        assert "真实高于效率" in vals
        # 溯源可查
        t = [t for t in traits if t["value"] == "计划驱动"][0]
        assert t["evidence_refs"] == ["refine:f-1"]

    def test_disabled(self, mem_conn):
        r = _result([{"memory_id": "m1", "content": "沉没成本不参与决策"}])
        stats = persona_extract.extract_and_store(r, mem_conn, {"persona": {"enabled": False}})
        assert stats["enabled"] is False
        assert persona_dao.list_traits(mem_conn) == []

    def test_no_memory_id_skipped(self, mem_conn):
        r = _result([{"content": "沉没成本"}])  # 未落库，无 memory_id
        stats = persona_extract.extract_and_store(r, mem_conn, {})
        assert stats["evidence_added"] == 0

    def test_custom_rules(self, mem_conn):
        cfg = {
            "persona": {
                "enabled": True,
                "rules": [
                    {"dimension": "custom", "values": [
                        {"value": "夜猫子", "keywords": ["深夜", "凌晨"]}
                    ]}
                ],
            }
        }
        r = _result([{"memory_id": "m1", "content": "凌晨三点还在写代码"}])
        persona_extract.extract_and_store(r, mem_conn, cfg)
        traits = persona_dao.list_traits(mem_conn)
        assert len(traits) == 1 and traits[0]["dimension"] == "custom"

    def test_bad_rule_tolerated(self, mem_conn):
        """规则缺 dimension 等脏配置不炸。"""
        r = _result([{"memory_id": "m1", "content": "沉没成本不参与决策"}])
        stats = persona_extract.extract_and_store(
            r, mem_conn, {"persona": {"rules": [{"values": []}]}}
        )
        assert stats["evidence_added"] == 0
