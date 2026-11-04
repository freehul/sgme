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
        self.trust_env = False
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
            raise ValueError("SGME_AGENT_KEY is required for authenticated calls")
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


    def capabilities(self) -> Any:
        """D-58（2026-11-04）：GET /v1/capabilities（服务端能力广告）。"""
        return self.get("/v1/capabilities")

    def api_compat_check(self, supported: str = "1.x") -> dict[str, Any]:
        """比较服务端 api_compat；不匹配/端点缺失只返回结构化结果，不抛异常。"""
        try:
            caps = self.capabilities() or {}
        except Exception as exc:  # 旧版 404 / 网络错误都按不可达处理
            return {"ok": False, "reason": "unreachable", "supported": supported,
                    "error": str(exc)[:120]}
        api_compat = caps.get("api_compat")
        schema = caps.get("schema_version")
        if api_compat != supported:
            return {"ok": False, "reason": "mismatch", "supported": supported,
                    "api_compat": api_compat, "schema_version": schema}
        return {"ok": True, "reason": None, "supported": supported,
                "api_compat": api_compat, "schema_version": schema}

    def close(self) -> None:
        self.session.close()
