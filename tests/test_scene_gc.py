"""tests/test_scene_gc.py：T-97 场景主动治理单元测试。

覆盖：
- list_merge_candidates：相似对检测 + 贪心去重叠（同一场景不重复合并）
- run_scene_gc：低于 trigger_at 跳过 / 禁用跳过 / 真实合并+归档（mock LLM）
- _merge_scene_gc_config：默认值与用户覆盖
"""

from __future__ import annotations

import numpy as np
import pytest

from sgme import config as config_mod
from sgme.data import db as db_mod
from sgme.data import scene_dao
from sgme.engine import scene_gc


def _make_mem(tmp_path):
    data = tmp_path / "data"
    mem, _sess, _wiki = db_mod.init_databases(data)
    return mem


def _add_scene(mem, scene_id: str, title: str, content: str, vec: list[float]) -> None:
    """建 active 场景 + 注入向量（绕过 embedding，直接构造 float32 blob）。"""
    scene_dao.insert_scene(mem, scene_id, title, content)
    blob = np.array(vec, dtype=np.float32).tobytes()
    mem.execute(
        "INSERT INTO scene_vectors (scene_id, embedding, model, dims, embedded_at) "
        "VALUES (?,?,?,?,?)",
        (scene_id, blob, "test-embed", len(vec), "2026-01-01T00:00:00Z"),
    )
    mem.commit()


# ----------------------------------------------------------------------
# list_merge_candidates
# ----------------------------------------------------------------------

def test_candidates_detect_similar_pair(tmp_path):
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "场景A", "内容A", [1.0, 0.0, 0.0])
    _add_scene(mem, "b1", "场景B", "内容B", [0.99, 0.01, 0.0])  # 与 A 高相似
    _add_scene(mem, "c1", "场景C", "内容C", [0.0, 1.0, 0.0])    # 与 A 不相似
    cfg = {"scene_gc": {"merge_threshold": 0.80}, "l2": {}}
    cands = scene_gc.list_merge_candidates(mem, cfg)
    assert len(cands) == 1
    assert {cands[0]["scene_a"], cands[0]["scene_b"]} == {"a1", "b1"}
    assert cands[0]["sim"] >= 0.80


def test_candidates_greedy_dedup(tmp_path):
    """四场景 a-b 相似、b-c 相似、d 独立：贪心去重叠只选 a-b。"""
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "A", "内容A", [1.0, 0.0, 0.0])
    _add_scene(mem, "b1", "B", "内容B", [0.99, 0.01, 0.0])
    _add_scene(mem, "c1", "C", "内容C", [0.98, 0.02, 0.0])  # 与 b 也相似
    _add_scene(mem, "d1", "D", "内容D", [0.0, 1.0, 0.0])
    cfg = {"scene_gc": {"merge_threshold": 0.80}, "l2": {}}
    cands = scene_gc.list_merge_candidates(mem, cfg)
    assert len(cands) == 1
    assert {cands[0]["scene_a"], cands[0]["scene_b"]} == {"a1", "b1"}


def test_candidates_below_threshold_empty(tmp_path):
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "A", "内容A", [1.0, 0.0, 0.0])
    _add_scene(mem, "b1", "B", "内容B", [0.0, 1.0, 0.0])
    cfg = {"scene_gc": {"merge_threshold": 0.95}, "l2": {}}
    assert scene_gc.list_merge_candidates(mem, cfg) == []


def test_candidates_skips_scenes_without_vector(tmp_path):
    """无向量的场景不进 JOIN 结果，不参与检测。"""
    mem = _make_mem(tmp_path)
    scene_dao.insert_scene(mem, "a1", "A", "内容A")  # 无向量
    _add_scene(mem, "b1", "B", "内容B", [1.0, 0.0, 0.0])
    _add_scene(mem, "c1", "C", "内容C", [0.99, 0.01, 0.0])
    cfg = {"scene_gc": {"merge_threshold": 0.80}, "l2": {}}
    cands = scene_gc.list_merge_candidates(mem, cfg)
    ids = {c["scene_a"] for c in cands} | {c["scene_b"] for c in cands}
    assert "a1" not in ids


# ----------------------------------------------------------------------
# run_scene_gc
# ----------------------------------------------------------------------

def test_run_skips_below_trigger(tmp_path):
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "A", "内容", [1.0, 0.0, 0.0])
    cfg = {"scene_gc": {"trigger_at": 10, "enabled": True}, "l2": {}}
    res = scene_gc.run_scene_gc(mem, cfg)
    assert res.skipped_reason is not None
    assert res.active_before == 1
    assert res.merged == 0


def test_run_disabled(tmp_path):
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "A", "内容", [1.0, 0.0, 0.0])
    cfg = {"scene_gc": {"enabled": False}, "l2": {}}
    res = scene_gc.run_scene_gc(mem, cfg)
    assert res.skipped_reason == "disabled"


def test_run_merges_and_archives(tmp_path, monkeypatch):
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "A", "内容A", [1.0, 0.0, 0.0])
    _add_scene(mem, "b1", "B", "内容B", [0.99, 0.01, 0.0])
    _add_scene(mem, "c1", "C", "内容C", [0.0, 1.0, 0.0])

    cfg = {
        "scene_gc": {"trigger_at": 2, "max_merges": 5, "enabled": True,
                     "merge_threshold": 0.80},
        "l2": {"warn_thresholds": {"orange": 2}},
        "llm": {},
    }

    def fake_call(llm_cfg, prompt, chain_name=None, client=None):
        return ("# 合并标题\n合并后的正文", "zhipu", {"total_tokens": 10})

    monkeypatch.setattr(scene_gc.llm_chain, "call_with_fallback", fake_call)

    res = scene_gc.run_scene_gc(mem, cfg)
    assert res.merged == 1
    assert res.archived == 2
    assert res.active_after == 2  # 3 - 2 archived + 1 new
    # 旧场景已归档（可恢复，符合原件永不删）
    assert scene_dao.get_scene(mem, "a1")["status"] == "archived"
    assert scene_dao.get_scene(mem, "b1")["status"] == "archived"
    # 新合并场景 active
    active = scene_dao.list_active_scenes(mem)
    assert len(active) == 2  # c1 + 新合并场景


def test_run_max_merges_cap(tmp_path, monkeypatch):
    """两对相似（4 场景）但 max_merges=1：只合并 1 对。"""
    mem = _make_mem(tmp_path)
    _add_scene(mem, "a1", "A", "内容A", [1.0, 0.0, 0.0])
    _add_scene(mem, "b1", "B", "内容B", [0.99, 0.01, 0.0])
    _add_scene(mem, "x1", "X", "内容X", [0.0, 1.0, 0.0])
    _add_scene(mem, "y1", "Y", "内容Y", [0.0, 0.99, 0.01])

    cfg = {
        "scene_gc": {"trigger_at": 2, "max_merges": 1, "enabled": True,
                     "merge_threshold": 0.80},
        "l2": {"warn_thresholds": {"orange": 2}},
        "llm": {},
    }

    def fake_call(llm_cfg, prompt, chain_name=None, client=None):
        return ("# 合并标题\n合并后的正文", "zhipu", {"total_tokens": 10})

    monkeypatch.setattr(scene_gc.llm_chain, "call_with_fallback", fake_call)

    res = scene_gc.run_scene_gc(mem, cfg)
    assert res.merged == 1
    assert res.archived == 2
    assert res.active_after == 3  # 4 - 2 + 1


# ----------------------------------------------------------------------
# config
# ----------------------------------------------------------------------

def test_merge_scene_gc_config_defaults():
    cfg = config_mod._merge_scene_gc_config(None)
    assert cfg["enabled"] is True
    assert cfg["merge_threshold"] == 0.70  # B117 由 0.80 下调（收弱相似度重复场景）
    assert cfg["min_threshold"] == 0.70    # B117 新增兜底下限
    assert cfg["trigger_at"] is None
    assert cfg["max_merges"] == 20


def test_merge_scene_gc_config_user_override():
    user = config_mod._merge_scene_gc_config({"max_merges": 5, "trigger_at": 300})
    assert user["max_merges"] == 5
    assert user["trigger_at"] == 300
    assert user["merge_threshold"] == 0.70  # 未覆盖保留默认（B117 起 0.80→0.70）


def test_merge_scene_gc_config_rejects_bad_types():
    user = config_mod._merge_scene_gc_config({"enabled": "yes", "max_merges": -3})
    assert user["enabled"] is True  # 非法类型回退默认
    assert user["max_merges"] == 20
