# -*- coding: utf-8 -*-
"""D-58（2026-11-04）：Hermes 适配器能力面自检单元测试（mock 网络层）。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.hermes import SGMEProvider  # noqa: E402

AGENT_KEY = "agt_" + "sample_key_for_tests_only"
BASE_URL = "http://127.0.0.1:9910"


class _Response:
    def __init__(self, status_code: int = 200, json_body: Any = None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def json(self):
        return self._json_body

    @property
    def text(self):
        return ""


class _Client:
    def __init__(self, response=None, raise_error: bool = False):
        self._response = response or _Response()
        self._raise = raise_error
        self.calls: List[Dict[str, Any]] = []

    @property
    def is_closed(self):
        return False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._raise:
            raise ConnectionError("Connection refused")
        return self._response


def _provider(client: _Client) -> SGMEProvider:
    p = SGMEProvider(base_url=BASE_URL, agent_key=AGENT_KEY)
    p._client = client  # type: ignore[assignment]
    p._available = True
    return p


def test_capabilities_match():
    p = _provider(_Client(_Response(json_body={"api_compat": "1.x", "schema_version": 6})))
    r = p.api_compat_check()
    assert r["ok"] is True and r["reason"] is None
    assert r["schema_version"] == 6


def test_capabilities_mismatch():
    p = _provider(_Client(_Response(json_body={"api_compat": "2.x", "schema_version": 7})))
    r = p.api_compat_check()
    assert r["ok"] is False and r["reason"] == "mismatch"
    assert r["api_compat"] == "2.x"


def test_capabilities_unreachable_does_not_raise():
    p = _provider(_Client(raise_error=True))
    r = p.api_compat_check()
    assert r["ok"] is False and r["reason"] == "unreachable"


def test_probe_compat_is_silent_on_error():
    p = _provider(_Client(raise_error=True))
    p._probe_compat()  # 不应抛异常
