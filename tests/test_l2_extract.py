"""L2 提取·2.0 输出解析测试（D-23 / D-66 / D-67 / D-21·D-22）。

覆盖：主干+侧面摊平、拆条规则落地、discard 分流、降级打待审标、1.x 向后兼容。
"""

from __future__ import annotations

import json

from sgme.engine import l1
from sgme.engine.l2_extract import parse_l2_output, records_to_items

DIMS = [
    {"id": "preferences", "display_name": "偏好", "active": 1, "time_velocity": "static"},
    {"id": "habits", "display_name": "习惯", "active": 1, "time_velocity": "dynamic"},
    {"id": "status", "display_name": "状态", "active": 1, "time_velocity": "dynamic"},
]


def _unit(**kw):
    base = {
        "main_event": {
            "content": "用户昨天在诊所做了例行复查，结果正常",
            "occurred_at": "2026-10-20",
            "feeling": "安心",
        },
        "facts": [],
        "causes": [],
        "trait": None,
        "dimensions": {"primary": "status"},
        "route": "memory",
    }
    base.update(kw)
    return [base]


def test_main_plus_side_records():
    """主干一条 + 偏好拆成独立一条；状态留主干不拆。"""
    unit = _unit(facts=[
        {"type": "偏好", "content": "用户偏好上午就诊", "subject": "self", "certainty": "confirmed"},
        {"type": "状态", "content": "用户本周腰疼", "subject": "self", "certainty": "tentative"},
    ])
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)

    assert len(recs) == 2
    main, side = recs
    # 含「偏好」这类长期属性事实 → 主干按 persona（episodic 会被强制改动态维度）
    assert main["memory_type"] == "persona"
    assert main["extra"]["feeling"] == "安心"
    assert main["occurred_at"] == "2026-10-20"
    assert main["extra"]["needs_review"] == 0
    # 状态未拆 → 留在主干 l2_facts_json 里
    kept = json.loads(main["extra"]["l2_facts_json"])["facts"]
    assert [f["type"] for f in kept] == ["状态"]

    assert side["memory_type"] == "persona"
    assert side["extra"]["type"] == "偏好"
    assert side["extra"]["subject"] == "self"
    assert "必拆" in side["extra"]["split_reason"]


def test_causes_and_trait_kept_on_main():
    unit = _unit(causes=[{"from": "复查结果正常", "to": "用户安心"}], trait="重视健康")
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    assert len(recs) == 1
    assert recs[0]["extra"]["trait"] == "重视健康"
    payload = json.loads(recs[0]["extra"]["l2_facts_json"])
    assert payload["causes"] == [{"from": "复查结果正常", "to": "用户安心"}]


def test_discard_route_drops_whole_unit():
    unit = _unit(route="discard")
    assert parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS) == []


def test_experience_routes_to_skill():
    unit = _unit(facts=[{"type": "经验", "content": "先分段摘要再总装"}])
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    side = [r for r in recs if r["extra"].get("type") == "经验"][0]
    assert side["route"] == "skill_candidate"


def test_missing_main_content_falls_back_and_flags_review():
    unit = _unit(main_event={"content": "", "occurred_at": None},
                 facts=[{"type": "偏好", "content": "用户喜欢安静的诊室"}])
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    assert recs[0]["extra"]["needs_review"] == 1
    assert recs[0]["extra"]["derivation"] == "inferred"
    assert recs[0]["content"] == "用户喜欢安静的诊室"


def test_missing_dimensions_flags_review():
    unit = _unit(dimensions={})
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    assert recs[0]["extra"]["needs_review"] == 1
    assert recs[0]["dimensions"] == []


def test_overlong_fields_are_clipped_not_dropped():
    unit = _unit(main_event={"content": "主" * 300, "occurred_at": None}, trait="特" * 80)
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    assert len(recs[0]["content"]) == 200
    assert len(recs[0]["extra"]["trait"]) == 50


def test_1_x_payload_is_delegated():
    """1.x 平铺数组 → 交给 parse_l1_output，行为不变。"""
    old = [{"content": "用户拥有三台相机", "dimensions": ["preferences"],
            "memory_type": "persona", "priority": 70}]
    recs = parse_l2_output(json.dumps(old, ensure_ascii=False), DIMS)
    assert recs[0]["content"] == "用户拥有三台相机"
    assert "extra" not in recs[0]


def test_invalid_json_raises_refine_error():
    try:
        parse_l2_output("not json", DIMS)
    except l1.RefineError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应当抛 RefineError")


# ---------- records_to_items：接进既有落库路径 ----------

def test_records_to_items_preserves_2_0_fields():
    unit = _unit(facts=[{"type": "偏好", "content": "用户偏好上午就诊", "subject": "self"}])
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    items = records_to_items(recs, DIMS)
    assert len(items) == 2
    side = [i for i in items if i.get("type") == "偏好"][0]
    assert side["subject"] == "self"
    assert side["route"] == "memory"
    assert side["needs_review"] == 0
    main = [i for i in items if i["content"] == "用户昨天在诊所做了例行复查，结果正常"][0]
    assert main["feeling"] == "安心"


def test_records_to_items_falls_back_dimension_for_pending():
    """缺维度的待审项仍要落库：给兜底维度 + needs_review=1。"""
    unit = _unit(dimensions={})
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    items = records_to_items(recs, DIMS)
    assert len(items) == 1
    assert items[0]["needs_review"] == 1
    assert items[0]["dimensions"]  # 有兜底维度


def test_schema_switch_defaults_to_1_x(monkeypatch):
    """开关未设时行为不变（1.x 解析）。"""
    from sgme.engine import l1

    monkeypatch.delenv("SGME_L2_SCHEMA", raising=False)
    old = json.dumps([{"content": "用户喜欢喝茶", "dimensions": ["preferences"],
                       "memory_type": "persona", "priority": 70}], ensure_ascii=False)
    items = l1._parse_extraction_output(old, DIMS)
    assert items and "extra" not in items[0]


def test_schema_switch_2_0_parses_2_0_payload(monkeypatch):
    from sgme.engine import l1

    monkeypatch.setenv("SGME_L2_SCHEMA", "2.0")
    payload = json.dumps(_unit(), ensure_ascii=False)
    items = l1._parse_extraction_output(payload, DIMS)
    assert items and items[0].get("feeling") == "安心"


def test_main_without_static_facts_is_fact():
    """没有长期属性事实的主干用 fact —— 也是唯一不会被强制改维度的另一类型。"""
    unit = _unit(facts=[{"type": "状态", "content": "用户本周腰疼"}])
    recs = parse_l2_output(json.dumps(unit, ensure_ascii=False), DIMS)
    assert recs[0]["memory_type"] == "fact"


def test_main_event_as_string_is_tolerated():
    """模型偶尔把 main_event 写成字符串，不能因此把整个文件判失败。

    2026-10-31 实测：全量重建 320 个文件里 21 个因 AttributeError 整file丢失。
    """
    import json as _json

    dims = [{"id": "preferences", "display_name": "偏好"}]
    payload = _json.dumps([{
        "main_event": "用户决定删除操作一律走回收站",
        "dimensions": ["preferences"],
        "route": "memory",
        "facts": [],
    }], ensure_ascii=False)
    recs = parse_l2_output(payload, dims)
    assert len(recs) == 1
    assert "回收站" in recs[0]["content"]
