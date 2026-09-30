from codex_sgme.http_client import HttpClient, SgmeHttpError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("unexpected fake response")


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    def close(self):
        pass


def test_http_client_sends_agent_key_and_disables_proxy_environment():
    session = FakeSession(FakeResponse(payload={"status": "ok"}))
    client = HttpClient(
        "http://10.0.0.10:9910",
        "agent-secret",
        session=session,
    )

    assert client.get("/v1/health", authenticated=False) == {"status": "ok"}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "http://10.0.0.10:9910/v1/health"
    assert "X-API-Key" not in kwargs["headers"]
    assert client.trust_env is False


def test_http_client_raises_safe_error_without_echoing_key():
    session = FakeSession(FakeResponse(status_code=403, text="denied"))
    client = HttpClient("http://sgme.invalid", "agent-secret", session=session)

    try:
        client.get("/v1/search")
    except SgmeHttpError as exc:
        assert exc.status_code == 403
        assert "agent-secret" not in str(exc)
        assert "403" in str(exc)
    else:
        raise AssertionError("expected SgmeHttpError")
