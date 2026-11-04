# -*- coding: utf-8 -*-
"""D-61（2026-11-04）：care_watch 消费侧关怀档位过滤测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import care_watch as cw  # noqa: E402
from sgme_client import SGME  # noqa: E402


def _events():
    return {"events": [
        {"event_id": "a", "type": "care_todo_due", "title": "待办到期"},
        {"event_id": "b", "type": "care_daily", "title": "每日问候"},
        {"event_id": "c", "type": "anomaly_warn", "title": "异常"},
    ]}


def test_silent_suppresses_care_output():
    out = cw.fmt_pull(_events(), "care_", "silent")
    assert "care_todo_due" not in out and "care_daily" not in out
    assert "silent" in out


def test_push_keeps_only_key_care_types():
    out = cw.fmt_pull(_events(), "care_", "push")
    assert "care_todo_due" in out
    assert "care_daily" not in out


def test_force_none_shows_all_care():
    out = cw.fmt_pull(_events(), "care_", None)
    assert "care_todo_due" in out and "care_daily" in out


def test_client_care_level_reads_role_card():
    c = SGME(base_url="http://127.0.0.1:9910", key="agt_" + "placeholder")

    def fake(method, path, body=None, **kwargs):
        if "active-role" in path:
            return {"role_id": "butler"}
        if "/roles/butler" in path:
            return {"role": {"data": {"extensions": {"sgme_care": {"care_level": "push"}}}}}
        return {}

    c._http = fake
    assert c.care_level() == "push"


def test_client_care_level_no_role_is_silent():
    c = SGME(base_url="http://127.0.0.1:9910", key="agt_" + "placeholder")
    c._http = lambda *a, **k: {"role_id": None}
    assert c.care_level() == "silent"


def test_client_care_level_error_falls_back_session():
    c = SGME(base_url="http://127.0.0.1:9910", key="agt_" + "placeholder")

    def boom(*a, **k):
        raise RuntimeError("gateway down")

    c._http = boom
    assert c.care_level() == "session"
