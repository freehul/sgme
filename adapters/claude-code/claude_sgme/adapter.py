from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import Settings
from .http_client import HttpClient
from .mcp_client import McpClient


class SgmeAdapter:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http: Any | None = None,
        mcp: Any | None = None,
    ):
        self.settings = settings or Settings.from_env()
        self.http = http or HttpClient(
            self.settings.base_url,
            self.settings.api_key,
            timeout=self.settings.timeout,
        )
        self.mcp = mcp or McpClient(
            self.settings.mcp_url,
            self.settings.api_key,
            timeout=self.settings.timeout,
        )

    @classmethod
    def from_env(cls) -> SgmeAdapter:
        return cls(settings=Settings.from_env())

    def health(self) -> Any:
        return self.http.get("/v1/health", authenticated=False)

    def inject(self, *, mode: str = "daily", max_tokens: int = 800) -> Any:
        return self.http.post("/v1/inject", json={"mode": mode, "max_tokens": max_tokens})

    def search(
        self,
        query: str,
        *,
        scopes: Iterable[str] = ("memory",),
        limit: int = 10,
        include_sources: bool = True,
    ) -> Any:
        return self.http.post(
            "/v1/search",
            json={
                "query": query,
                "scopes": list(scopes),
                "limit": limit,
                "include_sources": include_sources,
            },
        )

    def skill_search(self, query: str, *, limit: int = 5) -> Any:
        return self.http.get("/v1/skills/search", params={"q": query, "limit": limit})

    def wiki_search(self, query: str, *, limit: int = 5) -> Any:
        return self.http.get("/v1/wiki/search", params={"q": query, "limit": limit})

    def append(
        self,
        *,
        session_key: str,
        started_at: str,
        content: str,
        agent_id: str | None = None,
        source_type: str = "session",
    ) -> Any:
        return self.http.post(
            "/v1/append",
            json={
                "session_key": session_key,
                "started_at": started_at,
                "agent_id": agent_id or self.settings.agent_id,
                "source_type": source_type,
                "content": content,
            },
        )

    def refine_trigger(self, *, async_mode: bool = True, limit: int = 50) -> Any:
        return self.mcp_tool("refine_trigger", async_mode=async_mode, limit=limit)

    def onboarding(self) -> Any:
        return self.mcp.call_tool("agent_onboarding", {})

    def mcp_tool(self, name: str, **arguments: Any) -> Any:
        return self.mcp.call_tool(name, arguments)

    def close(self) -> None:
        self.http.close()
        self.mcp.close()
