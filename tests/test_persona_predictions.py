# -*- coding: utf-8 -*-
"""tests/test_persona_predictions.py：D-25 预测生成链路（mock LLM）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import persona_predictions as pp


def _fake_extract(conversation, dimensions, llm_cfg):
    if "GIL" in conversation:
        return ([], "fake", {"stage": "test"})
    return ([{
        "content": "用户当天很累",
        "dimensions": ["status"],
        "memory_type": "episodic",
        "certainty": "tentative",
        "subject": "self",
        "route": "memory",
        "occurred_at": None,
    }], "fake", {"stage": "test"})


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setattr("sgme.config.load_config", lambda: {
        "dimensions": [{"id": "status", "display_name": "状态"}],
        "llm": {"chain": []},
    })
    monkeypatch.setattr("sgme.engine.l2_extraction.extract_memories", _fake_extract)


def test_predictions_schema_and_limit(tmp_path, fake_env):
    out = tmp_path / "preds.json"
    rc = pp.main(["--only", "pa-001,pa-004", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["predictions"]) == {"pa-001", "pa-004"}
    m = data["predictions"]["pa-001"]["memories"][0]
    assert m["main_dimension"] == "status"
    assert m["route"] == "memory"
    # 技术反例：fake 返回空
    assert data["predictions"]["pa-004"]["memories"] == []
    assert data["errors"] == []


def test_dry_run_does_not_call_llm(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry-run 不应调用 LLM")

    monkeypatch.setattr("sgme.engine.l2_extraction.extract_memories", boom)
    rc = pp.main(["--dry-run", "--limit", "3", "--out", str(tmp_path / "x.json")])
    assert rc == 0
    assert not (tmp_path / "x.json").exists()


def test_error_recorded_per_case(tmp_path, fake_env, monkeypatch):
    def flaky(conversation, dimensions, llm_cfg):
        if "GIL" in conversation:
            raise RuntimeError("provider down")
        return _fake_extract(conversation, dimensions, llm_cfg)

    monkeypatch.setattr("sgme.engine.l2_extraction.extract_memories", flaky)
    out = tmp_path / "preds.json"
    rc = pp.main(["--only", "pa-001,pa-004", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["errors"]) == 1
    assert data["errors"][0]["case_id"] == "pa-004"
    assert data["predictions"]["pa-004"]["memories"] == []
