"""连接配置解析。

密钥优先级（只从环境变量或部署配置读取，绝不硬编码）：
  1. SGME_CLAUDE_KEY   宿主专属环境变量（bootstrap 时 setx 写入）
  2. ~/.sgme/claude-agent.json 部署状态文件（bootstrap 落盘）
  3. SGME_AGENT_KEY    通用兜底（不推荐长期依赖）

连接地址优先级：
  1. SGME_BASE_URL / SGME_HTTP_URL 环境变量
  2. 部署状态文件
  3. http://127.0.0.1:9910
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

STATE_FILE = Path.home() / ".sgme" / "claude-agent.json"


def _normalise_base_url(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SGME base url must use http or https")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _derive_mcp_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    mcp_port = port + 3
    host = parsed.hostname or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit((parsed.scheme, f"{host}:{mcp_port}", "/mcp", "", ""))


def load_state(path: Path | str | None = None) -> dict:
    """读取部署状态文件；缺失或损坏时静默返回空 dict。"""
    target = Path(path) if path is not None else STATE_FILE
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class Settings:
    base_url: str
    mcp_url: str
    agent_id: str = "claude-code"
    api_key: str | None = None
    api_key_source: str = "none"
    timeout: float = 10.0

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        state: Mapping[str, object] | None = None,
    ) -> Settings:
        env = os.environ if environ is None else environ
        st = dict(state) if state is not None else load_state()

        base_url = _normalise_base_url(
            str(
                env.get("SGME_BASE_URL")
                or env.get("SGME_HTTP_URL")
                or st.get("base_url")
                or "http://127.0.0.1:9910"
            )
        )
        mcp_url = str(
            env.get("SGME_MCP_URL") or st.get("mcp_url") or _derive_mcp_url(base_url)
        ).rstrip("/")

        key: str | None = None
        source = "none"
        if env.get("SGME_CLAUDE_KEY"):
            key, source = str(env["SGME_CLAUDE_KEY"]), "env"
        elif st.get("api_key"):
            key, source = str(st["api_key"]), "state"
        elif env.get("SGME_AGENT_KEY"):
            key, source = str(env["SGME_AGENT_KEY"]), "env-generic"

        agent_id = str(st.get("agent_id") or env.get("SGME_AGENT_ID") or "claude-code")
        return cls(
            base_url=base_url,
            mcp_url=mcp_url,
            agent_id=agent_id,
            api_key=key,
            api_key_source=source,
            timeout=float(env.get("SGME_TIMEOUT", "10")),
        )

    def require_agent_key(self) -> str:
        if not self.api_key:
            raise ValueError(
                "SGME agent key 未配置：请运行 scripts/bootstrap_key.py，"
                "或设置 SGME_CLAUDE_KEY"
            )
        return self.api_key

    def __repr__(self) -> str:
        return (
            "Settings("
            f"base_url={self.base_url!r}, mcp_url={self.mcp_url!r}, "
            f"agent_id={self.agent_id!r}, api_key=<redacted from {self.api_key_source}>, "
            f"timeout={self.timeout!r})"
        )
