import io
import json

from codex_sgme import __version__, cli


class FakeAdapter:
    def __init__(self):
        self.settings = type("Settings", (), {"agent_id": "codex"})()
        self.calls = []

    def health(self):
        return {"status": "ok"}

    def onboarding(self):
        return {"content": [{"type": "text", "text": "onboarding"}]}

    def inject(self, **kwargs):
        self.calls.append(("inject", kwargs))
        return {"mode": kwargs["mode"]}

    def append(self, **kwargs):
        self.calls.append(("append", kwargs))
        return {"status": "new"}

    def search(self, query, *, scopes, limit):
        self.calls.append(
            ("search", {"query": query, "scopes": scopes, "limit": limit})
        )
        return {"results": []}

    def mcp_tool(self, name, **arguments):
        self.calls.append((name, arguments))
        return {"status": "queued"} if name == "refine_trigger" else {"signals": []}

    def close(self):
        self.calls.append(("close", {}))


def test_cli_lifecycle_reads_turn_messages_from_stdin(monkeypatch, tmp_path, capsys):
    adapter = FakeAdapter()
    monkeypatch.setattr(cli.SgmeAdapter, "from_env", lambda: adapter)
    cursor = tmp_path / "cursor.json"

    assert (
        cli.main(["start", "--session-key", "codex-cli", "--cursor-file", str(cursor)])
        == 0
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "started_at": "2026-09-26T10:00:00Z",
                    "messages": [{"role": "user", "content": "hello"}],
                }
            )
        ),
    )
    assert (
        cli.main(["turn", "--session-key", "codex-cli", "--cursor-file", str(cursor)])
        == 0
    )
    assert (
        cli.main(["end", "--session-key", "codex-cli", "--cursor-file", str(cursor)])
        == 0
    )

    output = capsys.readouterr().out
    assert '"status": "new"' in output
    assert any(name == "append" for name, _ in adapter.calls)
    assert any(name == "refine_trigger" for name, _ in adapter.calls)


def test_cli_search_does_not_require_a_session(monkeypatch, capsys):
    adapter = FakeAdapter()
    monkeypatch.setattr(cli.SgmeAdapter, "from_env", lambda: adapter)

    assert cli.main(["search", "Codex", "--scopes", "memory,wiki", "--limit", "2"]) == 0

    assert '"status": "ok"' not in capsys.readouterr().out


def test_manifest_has_no_credential_or_machine_specific_path(tmp_path):
    from install import build_manifest, write_manifest

    path = tmp_path / "codex-sgme.json"
    write_manifest(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert manifest == build_manifest()
    serialized = json.dumps(manifest)
    assert "SGME_AGENT_KEY" not in serialized
    assert "C:\\Users" not in serialized
    assert manifest["adapter"] == "codex-sgme"
    assert manifest["version"] == __version__ == "0.1.2"


def test_register_command_does_not_include_a_secret(tmp_path):
    from install import register_codex_mcp

    calls = []
    register_codex_mcp(
        codex_executable="codex.exe",
        project_dir=tmp_path,
        base_url="http://10.0.0.10:9910",
        mcp_url="http://10.0.0.10:9913/mcp",
        runner=lambda command, check: calls.append((command, check)),
    )

    command, check = calls[0]
    assert check is True
    assert command[:4] == ["codex.exe", "mcp", "add", "sgme"]
    assert "SGME_AGENT_KEY" not in " ".join(command)
    assert "SGME_CODEX_KEY" not in " ".join(command)
    assert "PYTHONUTF8=1" in command
    assert "PYTHONIOENCODING=utf-8" in command
    assert command[-2:] == ["-m", "codex_sgme.mcp_proxy"]
