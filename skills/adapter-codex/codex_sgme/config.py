from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


def _normalise_base_url(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SGME_BASE_URL must use http or https")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _derive_mcp_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    mcp_port = port + 3
    host = parsed.hostname or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit((parsed.scheme, f"{host}:{mcp_port}", "/mcp", "", ""))


@dataclass(frozen=True)
class Settings:
    base_url: str
    mcp_url: str
    agent_id: str = "codex"
    api_key: str | None = None
    timeout: float = 10.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        base_url = _normalise_base_url(
            env.get("SGME_BASE_URL")
            or env.get("SGME_HTTP_URL")
            or "http://127.0.0.1:9910"
        )
        mcp_url = env.get("SGME_MCP_URL") or _derive_mcp_url(base_url)
        mcp_url = mcp_url.rstrip("/")
        return cls(
            base_url=base_url,
            mcp_url=mcp_url,
            agent_id=env.get("SGME_AGENT_ID", "codex"),
            api_key=env.get("SGME_CODEX_KEY") or env.get("SGME_AGENT_KEY") or None,
            timeout=float(env.get("SGME_TIMEOUT", "10")),
        )

    def require_agent_key(self) -> str:
        if not self.api_key:
            raise ValueError("SGME_AGENT_KEY is required for authenticated calls")
        return self.api_key

    def __repr__(self) -> str:
        return (
            "Settings("
            f"base_url={self.base_url!r}, mcp_url={self.mcp_url!r}, "
            f"agent_id={self.agent_id!r}, api_key=<redacted>, timeout={self.timeout!r})"
        )
