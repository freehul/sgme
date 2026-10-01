from __future__ import annotations

from typing import Any

import httpx


class SgmeHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"SGME HTTP {status_code}: {message[:500]}")


class HttpClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 10.0,
        session: Any | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # SGME 铁律：httpx 调用必须等价 trust_env=False，防代理劫持内网请求
        self.session = session or httpx.Client(timeout=timeout, trust_env=False)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        if authenticated and not self.api_key:
            raise ValueError("SGME agent key 未配置（SGME_CLAUDE_KEY）")
        headers = {"Accept": "application/json"}
        if authenticated and self.api_key:
            headers["X-API-Key"] = self.api_key
        response = self.session.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            headers=headers,
            json=json,
            params=params,
        )
        if response.status_code >= 400:
            detail = getattr(response, "text", "request failed") or "request failed"
            raise SgmeHttpError(response.status_code, detail)
        return response.json()

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        return self.request("GET", path, params=params, authenticated=authenticated)

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        return self.request("POST", path, json=json, authenticated=authenticated)

    def close(self) -> None:
        self.session.close()
