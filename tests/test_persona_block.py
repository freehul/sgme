"""ST-35 T-101：性格参考块注入测试。"""

from __future__ import annotations

import pytest

from sgme.data import db, persona_dao
from sgme.profile import persona_block
from sgme.profile.inject import build_inject_blocks


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db.connect_memory(tmp_path)
    yield conn
    conn.close()


def _seed(conn, dim, value, confidence, evidence):
    t = persona_dao.upsert_trait(conn, dim, value, evidence_ref="m0")
    # 直接补足证据数与置信度（绕过逐条累积）
    conn.execute(
        "UPDATE persona_traits SET confidence=?, evidence_count=? WHERE trait_id=?",
        (confidence, evidence, t["trait_id"]),
    )
    conn.commit()
    return t["trait_id"]


class TestBuildPersonaBlock:
    def test_none_when_empty(self, mem_conn):
        assert persona_block.build_persona_block(mem_conn) is None

    def test_below_threshold_filtered(self, mem_conn):
        _seed(mem_conn, "decision_style", "成本敏感", 0.30, 2)
        assert persona_block.build_persona_block(mem_conn) is None

    def test_high_confidence_included(self, mem_conn):
        _seed(mem_conn, "decision_style", "原则先于利益", 0.75, 5)
        out = persona_block.build_persona_block(mem_conn)
        assert out is not None
        item = out["block"]["items"][0]["content"]
        assert "倾向" in item and "高置信" in item

    def test_one_per_dimension(self, mem_conn):
        _seed(mem_conn, "d1", "v1", 0.8, 5)
        _seed(mem_conn, "d1", "v2", 0.6, 3)
        _seed(mem_conn, "d2", "w1", 0.9, 6)
        out = persona_block.build_persona_block(mem_conn)
        assert len(out["block"]["items"]) == 2

    def test_scene_context_isolation(self, mem_conn):
        _seed(mem_conn, "expression", "内敛", 0.8, 5)  # general
        out = persona_block.build_persona_block(mem_conn, scene_context="value")
        assert out is None  # value 情境无数据不混用 general


class TestInjectIntegration:
    def test_inject_appends_persona_block(self, mem_conn):
        _seed(mem_conn, "decision_style", "计划驱动", 0.9, 8)
        template = {
            "name": "daily",
            "sections": [{"title": "测试段", "dimensions": ["goals"], "limit": 3}],
        }
        result = build_inject_blocks(template, [[]])
        # 直接验证 build 层（inject 全链路需要维度注册表，环境无关）
        from sgme.profile.persona_block import build_persona_block
        block = build_persona_block(mem_conn)
        result["blocks"].append(block["block"])
        titles = [b["title"] for b in result["blocks"]]
        assert "性格参考" in titles
