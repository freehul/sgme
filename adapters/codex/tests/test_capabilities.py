# -*- coding: utf-8 -*-
"""D-58（2026-11-04）：能力面自检（GET /v1/capabilities）单元测试。"""
from __future__ import annotations

import pytest

from codex_sgme.http_client import HttpClient, SgmeHttpError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    def close(self):
        pass


def _client(resp):
    return HttpClient("http://127.0.0.1:9910", "agent-secret",
                      session=FakeSession(resp))


def test_capabilities_match():
    client = _client(FakeResponse(payload={"api_compat": "1.x", "schema_version": 6}))
    r = client.api_compat_check()
    assert r["ok"] is True and r["reason"] is None
    assert r["schema_version"] == 6
    assert client.capabilities()["api_compat"] == "1.x"


def test_capabilities_mismatch():
    client = _client(FakeResponse(payload={"api_compat": "2.x", "schema_version": 7}))
    r = client.api_compat_check()
    assert r["ok"] is False and r["reason"] == "mismatch"
    assert r["api_compat"] == "2.x"


def test_capabilities_unreachable():
    client = _client(FakeResponse(status_code=404, payload={}, text="not found"))
    r = client.api_compat_check()
    assert r["ok"] is False and r["reason"] == "unreachable"
