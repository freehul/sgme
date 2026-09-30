# Codex SGME Adapter

Thin, host-neutral client layer for connecting a Codex session to the SGME gateway.

This directory is the repository source of truth for the Codex adapter. The
standalone deployment copy remains rebuildable from this source.

## Scope

- Safe environment-based configuration.
- HTTP transport for health, inject, search, skills, and Wiki.
- Streamable HTTP MCP transport for SGME self-discovery and future full capability access.
- Lifecycle bridge for session start, per-turn append, and session-end refinement.
- A host-neutral CLI and manifest; it does not modify global Codex settings.

## Configuration

Copy `.env.example` into the host's environment configuration and provide an agent key
without committing it:

```text
SGME_BASE_URL=http://<SGME_HOST>:9910
SGME_MCP_URL=http://<SGME_HOST>:9913/mcp
SGME_AGENT_ID=codex
SGME_CODEX_KEY=<issued-by-sgme-admin>
```

The client disables proxy environment inheritance (`trust_env=False`) so local/NAS
traffic is not redirected by an unrelated proxy configuration.

## Development

The repository uses `uv` because this Windows host does not expose a system `python`
command:

```powershell
uv run --with pytest --with httpx python -m pytest
uv run --with ruff ruff check .
```

The live smoke check is read-only and requires `SGME_AGENT_KEY` in the process
environment:

```powershell
$env:SGME_BASE_URL = 'http://<SGME_HOST>:9910'
$env:SGME_MCP_URL = 'http://<SGME_HOST>:9913/mcp'
uv run --with httpx python -c "from codex_sgme.adapter import SgmeAdapter; a=SgmeAdapter.from_env(); print(a.health()); print(a.onboarding()); a.close()"
```

## Host integration protocol

Generate a reviewable manifest:

```powershell
uv run python install.py --output .codex-sgme.json
```

The lifecycle CLI commands are:

```powershell
python -m codex_sgme.cli start --session-key <session> --cursor-file <cursor>
python -m codex_sgme.cli turn --session-key <session> --cursor-file <cursor> < turn.json
python -m codex_sgme.cli end --session-key <session> --cursor-file <cursor>
```

`turn.json` is either a message array or an object containing `messages` and an
optional `started_at`. Only `user` and `assistant` messages are sent to SGME;
system messages and tool receipts are discarded before append. The cursor stores
only the last exported timestamp and count, never conversation text.

The current Codex host does not expose a public lifecycle-hook registration API,
so the manifest is intentionally declarative. It can be wired into a future host
hook without changing the SGME protocol or the adapter's data model.

## Codex MCP registration

The adapter includes a local stdio proxy because SGME authenticates with
`X-API-Key`, while Codex's direct remote registration path is Bearer-token based.
After setting the dedicated `SGME_CODEX_KEY` in the Codex process environment,
register the proxy:

```powershell
uv run python install.py --register --output .codex-sgme.json
```

The command writes only the server command, endpoint variables, and `codex` agent
identity into the Codex MCP configuration. The key is inherited by name and is
never written. Restart or reload Codex after registration; the proxy exposes the
full SGME MCP tool list, including `append`, `refine_trigger`, and
`agent_onboarding`.

## UTF-8 stdio contract

MCP stdio is UTF-8. On Windows, Python otherwise inherits the active ANSI code
page (often GBK), which can crash `tools/list` when a tool description contains
characters outside that code page. The proxy explicitly reconfigures stdin and
stdout to UTF-8 at startup, and `install.py --register` also records
`PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` for defence in depth.

## Safety boundary

The adapter never embeds or prints credentials. Append/refine behavior is covered
by dedicated tests and is only activated when the host invokes the lifecycle CLI.
