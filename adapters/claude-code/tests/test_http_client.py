# -*- coding: utf-8 -*-
"""HTTP 客户端：错误传播、缺 key 边界、免鉴权端点。"""
import pytest

from claude_sgme.http_client import HttpClient, SgmeHttpError


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"ok": True}


class _Session:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self.resp

    def close(self):
        pass


def test_error_status_raises_with_code():
    client = HttpClient("http://x", "agt_test", session=_Session(_Resp(403, "forbidden")))
    with pytest.raises(SgmeHttpError) as ei:
        client.get("/v1/health")
    assert ei.value.status_code == 403


def test_missing_key_rejected_before_network():
    session = _Session(_Resp(200, "{}"))
    client = HttpClient("http://x", None, session=session)
    with pytest.raises(ValueError):
        client.post("/v1/search", json={"query": "x"})
    assert session.calls == []


def test_unauthenticated_endpoint_does_not_require_key():
    session = _Session(_Resp(200, "{}"))
    client = HttpClient("http://x", None, session=session)
    assert client.get("/v1/health", authenticated=False) == {"ok": True}
