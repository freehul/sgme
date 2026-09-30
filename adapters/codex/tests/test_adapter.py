from codex_sgme.adapter import SgmeAdapter


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, path, *, params=None, authenticated=True):
        self.calls.append(("GET", path, params, authenticated))
        if path == "/v1/health":
            return {"status": "ok"}
        return {"results": []}

    def post(self, path, *, json=None, authenticated=True):
        self.calls.append(("POST", path, json, authenticated))
        return {"blocks": [], "results": []}


class FakeMcp:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "onboarding"}]}


def test_adapter_exposes_read_capabilities_without_write_side_effects():
    http = FakeHttp()
    mcp = FakeMcp()
    adapter = SgmeAdapter(http=http, mcp=mcp)

    assert adapter.health() == {"status": "ok"}
    adapter.inject(mode="daily", max_tokens=400)
    adapter.search("Codex", scopes=("memory", "wiki"), limit=3)
    adapter.skill_search("Python", limit=2)
    adapter.wiki_search("SGME", limit=2)
    assert adapter.onboarding()["content"][0]["text"] == "onboarding"

    assert not any(path == "/v1/append" for _, path, _, _ in http.calls)
