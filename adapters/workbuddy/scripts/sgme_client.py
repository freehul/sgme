# -*- coding: utf-8 -*-
"""
sgme_client.py — SGME × WorkBuddy（WorkBuddy 桌面端）适配器客户端
=============================================================

WorkBuddy官方适配器（adapters/workbuddy）的运行时客户端。两层通道：

  HTTP 层（agent key 直接可用，标准库零依赖）：
    health / append / inject / search / answer / memory_get / memory_reject /
    memory_unreject / events_pull / events_after / skill_*（读+写）/ wiki_*（读+写）

  MCP 层（需 mcp 库，用 SGME 项目 venv 执行）：
    agent_onboarding / refine_* / stats / config_get / config_update / signal_* /
    role_* / demand_create / project_register / idea_add / wiki_evolve_trigger /
    skill_put / skill_delete / skill_rename

地址解析（三级，构造时求值；部署后无需改代码）：
  ① 环境变量：SGME_HTTP_URL 或 SGME_BASE_URL（HTTP）、SGME_MCP_URL（MCP）
  ② 同目录部署配置：`client.env`（由 install.py 部署时写入部署副本；仓库内不含真值）
  ③ 回环默认：http://127.0.0.1:9910（MCP 端点由 HTTP 主机 :端口+3 推导）
  只给 HTTP 地址即可——MCP 端点自动按同主机「端口 +3 / 路径 /mcp」推导（9910→9913）。

密钥（只读环境变量或本机身份文件，不硬编码、不落盘、不回显）：
  SGME_AGENT_KEY —— agent 能力面（记忆/检索/技能读/wiki/信号/角色…）
  SGME_ADMIN_KEY —— 写侧·管理能力（skill_put / skill_delete / skill_rename 服务端强制管理员 Key）
  兜底：~/.sgme/workbuddy-agent.json（WorkBuddy 身份文件：api_key / http / mcp 字段）

用法（能力面 = CLI 命令面；全部基准能力都有对应子命令）：
  python sgme_client.py health
  python sgme_client.py append --session 2026-01-01-workbuddy --text "……" [--role user|assistant]
  python sgme_client.py inject --mode daily
  python sgme_client.py search "某项目关键词" --limit 5
  python sgme_client.py answer "我最近在做什么项目？"
  python sgme_client.py events-pull --subscriber workbuddy
  python sgme_client.py skill-search "数字人"
  python sgme_client.py wiki-search "接入"
  python sgme_client.py stats / config-get / refine-status / signal-pull / role-list
  python sgme_client.py demand-create "待办标题" --project-id <id>
  python sgme_client.py capabilities            # 打印基准能力矩阵（离线）
  python sgme_client.py env-info                # 打印生效端点（离线，不打印密钥）
  python sgme_client.py mcp <tool> k=v …        # 逃生口：直接调任意 MCP 工具

铁律：
  - 密钥只读环境变量（SGME_AGENT_KEY / SGME_ADMIN_KEY），不硬编码、不回显明文
  - 忽略代理环境变量（防代理劫持内网，等价 httpx trust_env=False）
  - append content 首行必须为 `# {ISO时间戳} {role}`，脚本自动生成
  - 提炼永远 async；批量 ≥20 分批 + 批间 30–60s；429 不立即重试
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------- 常量与地址解析 ----------

KEY_ENV = "SGME_AGENT_KEY"
ADMIN_KEY_ENV = "SGME_ADMIN_KEY"
# WorkBuddy 专用密钥变量（最高优先）。存在的理由：本机 SGME_AGENT_KEY 常常已被
# 其它 agent 占用——实测 .env 的 SGME_AGENT_KEY 绑定 agent_id=dsh，若直接优先使用
# 会把 WorkBuddy 的 L0 打上 dsh 的 agent_tag，污染多 Agent 溯源与隔离（T-140）。
WORKBUDDY_KEY_ENV = "SGME_WORKBUDDY_KEY"
AGENT_ID = "workbuddy"

# WorkBuddy 本机接入产物（相对 $HOME）
WORKBUDDY_IDENTITY_PATH = ".sgme/workbuddy-agent.json"
WORKBUDDY_MCP_JSON_PATH = ".workbuddy/mcp.json"
MCP_SERVER_NAME = "sgme"

# 回环默认（仓库内不出现任何真实设备地址；生产地址由环境变量/部署配置注入）
LOOPBACK_HTTP_URL = "http://127.0.0.1:9910"
LOOPBACK_MCP_URL = "http://127.0.0.1:9913/mcp"
DEFAULT_HTTP_URL = LOOPBACK_HTTP_URL
DEFAULT_MCP_URL = LOOPBACK_MCP_URL

# 部署配置文件名（install.py 写入部署副本 scripts/client.env）
DEPLOY_CONFIG_NAME = "client.env"
_HERE = Path(__file__).resolve().parent

# MCP 端点推导：同主机、端口 +3、路径 /mcp（9910→9913 / 9930→9933）
_MCP_PORT_DELTA = 3

_PROXY_ENVS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")


def _env_str(name: str) -> str | None:
    """读环境变量；空白视为未设置。"""
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def load_deploy_config(path: str | Path | None = None) -> dict:
    """读取同目录部署配置 client.env（install.py 生成）；不存在返回 {}。

    查找顺序：显式 path → <脚本目录>/client.env → <技能目录>/client.env。
    格式：`KEY=VALUE` 行，`#` 注释；只含地址与「密钥环境变量名」，不含密钥值。
    """
    cands = [Path(path)] if path else [
        _HERE / DEPLOY_CONFIG_NAME,
        _HERE.parent / DEPLOY_CONFIG_NAME,
    ]
    cfg: dict = {}
    for f in cands:
        try:
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            continue
    return cfg


def load_workbuddy_identity(path: str | Path | None = None) -> dict:
    """读取 WorkBuddy 本机身份文件 ~/.sgme/workbuddy-agent.json（不含密钥回显）。

    字段：api_key / http / mcp / agent_id。文件不存在或不可读返回 {}。
    仓库内永不出现该文件真值；仅部署侧本地使用。
    """
    f = Path(path) if path else Path.home() / ".sgme" / "workbuddy-agent.json"
    try:
        if not f.is_file():
            return {}
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_workbuddy_mcp_json(path: str | Path | None = None) -> dict:
    """从 WorkBuddy 的 mcp.json 发现已配置的 SGME MCP 条目（本适配器特有）。

    场景：WorkBuddy 用户在装本适配器之前，通常已经在 `~/.workbuddy/mcp.json`
    里配好了 `sgme` server（含 URL 与 X-API-Key）。直接继承它即可**零配置接入**
    ——地址与密钥都不必再填一遍，也不会多签一把 key。

    返回 `{"api_key": str, "url": str}`（缺哪项不出哪项）；未配置/不可读返回 {}。
    🔴 只读、绝不回显密钥明文（调用方仅可判空）。
    """
    f = Path(path) if path else Path.home() / WORKBUDDY_MCP_JSON_PATH
    try:
        if not f.is_file():
            return {}
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    entry = servers.get(MCP_SERVER_NAME)
    if not isinstance(entry, dict):
        return {}
    out: dict = {}
    headers = entry.get("headers")
    if isinstance(headers, dict):
        for name in ("X-API-Key", "x-api-key", "X-Api-Key"):
            val = headers.get(name)
            if isinstance(val, str) and val.strip():
                out["api_key"] = val.strip()
                break
    url = entry.get("url") or entry.get("serverUrl")
    if isinstance(url, str) and url.strip():
        out["url"] = url.strip()
    return out


def resolve_agent_key() -> tuple[str, str]:
    """解析 agent 能力面密钥，返回 `(key, 来源说明)`；全无则返回 `("", "")`。

    顺序（WorkBuddy 口径，与 mimo/豆包不同，理由见下）：

    ① 环境变量 `SGME_WORKBUDDY_KEY` —— 专用变量，最高优先
    ② 身份文件 `~/.sgme/workbuddy-agent.json` 的 `api_key`
    ③ `~/.workbuddy/mcp.json` 里 `sgme` server 的 `X-API-Key`（零配置继承）
    ④ 环境变量 `SGME_AGENT_KEY` —— 通用兜底，**附警示**

    为什么把 `SGME_AGENT_KEY` 降到兜底：实测本机 `.env` 的该变量绑定
    `agent_id=dsh`。若照搬 mimo/豆包的「环境变量优先」，WorkBuddy 写入的 L0
    会被打上 dsh 的 `agent_tag`，污染溯源与多 Agent 隔离（T-140 灰度）。
    宁可多一步显式配置，也不接受静默错标。
    """
    v = _env_str(WORKBUDDY_KEY_ENV)
    if v:
        return v, f"环境变量 {WORKBUDDY_KEY_ENV}"
    ident = load_workbuddy_identity()
    v = str(ident.get("api_key") or "").strip()
    if v:
        return v, f"身份文件 ~/{WORKBUDDY_IDENTITY_PATH}"
    v = str(load_workbuddy_mcp_json().get("api_key") or "").strip()
    if v:
        return v, f"WorkBuddy MCP 配置 ~/{WORKBUDDY_MCP_JSON_PATH}"
    v = _env_str(KEY_ENV)
    if v:
        return v, f"环境变量 {KEY_ENV}（兜底：请确认它属于 agent_id={AGENT_ID}）"
    return "", ""


def derive_http_from_mcp(mcp_url: str) -> str:
    """由 MCP 端点反推 HTTP 端点（同主机、端口 -3、去掉 /mcp 路径）。"""
    u = urllib.parse.urlsplit(mcp_url)
    host = u.hostname or "127.0.0.1"
    port = (u.port or 9913) - _MCP_PORT_DELTA
    return f"{u.scheme or 'http'}://{host}:{port}"


def derive_mcp_url(http_url: str) -> str:
    """由 HTTP 地址推导 MCP 端点（同主机、端口 +3、路径 /mcp）。"""
    u = urllib.parse.urlsplit(http_url)
    host = u.hostname or "127.0.0.1"
    port = (u.port or 9910) + _MCP_PORT_DELTA
    return f"{u.scheme or 'http'}://{host}:{port}/mcp"


def resolve_addresses(http_url: str | None = None, mcp_url: str | None = None,
                      deploy_cfg: dict | None = None) -> tuple[str, str, str]:
    """解析生效端点，返回 (http_url, mcp_url, 来源说明)。

    顺序：显式参数 → 环境变量 → 同目录部署配置 → 身份文件 → WorkBuddy MCP 配置
    （由 `~/.workbuddy/mcp.json` 的 sgme server 反推）→ 回环默认。
    """
    cfg = load_deploy_config() if deploy_cfg is None else deploy_cfg
    if http_url:
        return http_url.rstrip("/"), (mcp_url or derive_mcp_url(http_url)).rstrip("/"), "调用参数"
    env_http = _env_str("SGME_HTTP_URL") or _env_str("SGME_BASE_URL")
    cfg_http = cfg.get("SGME_HTTP_URL") or cfg.get("SGME_BASE_URL")
    ident = load_workbuddy_identity()
    ident_http = (ident.get("http") or "").strip()
    mcp_cfg_url = str(load_workbuddy_mcp_json().get("url") or "").strip()
    if env_http:
        http, src = env_http, "环境变量 SGME_HTTP_URL/SGME_BASE_URL"
    elif cfg_http:
        http, src = cfg_http, f"部署配置 {DEPLOY_CONFIG_NAME}"
    elif ident_http:
        http, src = ident_http, f"身份文件 ~/{WORKBUDDY_IDENTITY_PATH}"
    elif mcp_cfg_url:
        http, src = derive_http_from_mcp(mcp_cfg_url), f"WorkBuddy MCP 配置 ~/{WORKBUDDY_MCP_JSON_PATH}"
    else:
        http, src = DEFAULT_HTTP_URL, "回环默认"
    ident_mcp = (ident.get("mcp") or "").strip()
    mcp = (mcp_url or _env_str("SGME_MCP_URL") or cfg.get("SGME_MCP_URL")
           or ident_mcp or mcp_cfg_url or derive_mcp_url(http))
    return http.rstrip("/"), mcp.rstrip("/"), src


# ---------- MCP 基准工具清单（权威基准：sgme/mcp_server.py，41 个） ----------

BASELINE_TOOLS: tuple[str, ...] = (
    "agent_onboarding", "append", "inject", "search", "answer",
    "wiki_search", "wiki_pages", "wiki_page", "wiki_page_add", "wiki_page_update",
    "wiki_evolve_trigger", "memory_get", "memory_reject", "memory_unreject",
    "refine_trigger", "refine_batch", "refine_status", "stats", "health",
    "config_get", "config_update", "idea_add", "demand_create", "project_register",
    "signal_pull", "signal_claim", "signal_ack", "signal_clear",
    "role_list", "role_assemble", "role_active_get", "role_active_set",
    "skill_search", "skill_digest", "skill_get", "skill_materialize", "skill_list",
    "skill_coldstart", "skill_put", "skill_delete", "skill_rename",
)

# 基准工具 → (客户端方法名, CLI 命令, 层, 密钥范围)
BASELINE_SPEC: dict[str, tuple[str, str, str, str]] = {
    "agent_onboarding": ("agent_onboarding", "agent-onboarding", "mcp", "agent"),
    "append": ("append", "append", "http", "agent"),
    "inject": ("inject", "inject", "http", "agent"),
    "search": ("search", "search", "http", "agent"),
    "answer": ("answer", "answer", "http", "agent"),
    "wiki_search": ("wiki_search", "wiki-search", "http", "agent"),
    "wiki_pages": ("wiki_pages", "wiki-pages", "http", "agent"),
    "wiki_page": ("wiki_page", "wiki-page", "http", "agent"),
    "wiki_page_add": ("wiki_page_add", "wiki-page-add", "http", "agent"),
    "wiki_page_update": ("wiki_page_update", "wiki-page-update", "http", "agent"),
    "wiki_evolve_trigger": ("wiki_evolve_trigger", "wiki-evolve-trigger", "mcp", "auto"),
    "memory_get": ("memory_get", "memory-get", "http", "agent"),
    "memory_reject": ("memory_reject", "memory-reject", "http", "agent"),
    "memory_unreject": ("memory_unreject", "memory-unreject", "http", "agent"),
    "refine_trigger": ("refine_trigger", "refine-trigger", "mcp", "agent"),
    "refine_batch": ("refine_batch", "refine-batch", "mcp", "agent"),
    "refine_status": ("refine_status", "refine-status", "mcp", "agent"),
    "stats": ("stats", "stats", "mcp", "agent"),
    "health": ("health", "health", "http", "agent"),
    "config_get": ("config_get", "config-get", "mcp", "agent"),
    "config_update": ("config_update", "config-update", "mcp", "auto"),
    "idea_add": ("idea_add", "idea-add", "mcp", "agent"),
    "demand_create": ("demand_create", "demand-create", "mcp", "agent"),
    "project_register": ("project_register", "project-register", "mcp", "auto"),
    "signal_pull": ("signal_pull", "signal-pull", "mcp", "agent"),
    "signal_claim": ("signal_claim", "signal-claim", "mcp", "agent"),
    "signal_ack": ("signal_ack", "signal-ack", "mcp", "agent"),
    "signal_clear": ("signal_clear", "signal-clear", "mcp", "auto"),
    "role_list": ("role_list", "role-list", "mcp", "agent"),
    "role_assemble": ("role_assemble", "role-assemble", "mcp", "agent"),
    "role_active_get": ("role_active_get", "role-active-get", "mcp", "agent"),
    "role_active_set": ("role_active_set", "role-active-set", "mcp", "agent"),
    "skill_search": ("skill_search", "skill-search", "http", "agent"),
    "skill_digest": ("skill_digest", "skill-digest", "http", "agent"),
    "skill_get": ("skill_get", "skill-get", "http", "agent"),
    "skill_materialize": ("skill_materialize", "skill-materialize", "http", "agent"),
    "skill_list": ("skill_list", "skill-list", "http", "agent"),
    "skill_coldstart": ("skill_coldstart", "skill-coldstart", "http", "agent"),
    "skill_put": ("skill_put", "skill-put", "mcp", "admin"),
    "skill_delete": ("skill_delete", "skill-delete", "mcp", "admin"),
    "skill_rename": ("skill_rename", "skill-rename", "mcp", "admin"),
}


class SGMEError(Exception):
    pass


class SGME:
    """SGME HTTP/MCP 客户端（WorkBuddy适配器运行时）。"""

    def __init__(self, base_url: str | None = None, key: str | None = None,
                 mcp_url: str | None = None):
        http, mcp, src = resolve_addresses(base_url, mcp_url)
        self.base_url = http
        self.mcp_url = mcp
        self.source = src
        if key:
            self.key, self.key_source = key, "调用参数"
        else:
            self.key, self.key_source = resolve_agent_key()
        if not self.key:
            raise SGMEError(
                f"缺少密钥：请设置环境变量 {WORKBUDDY_KEY_ENV} 或 {KEY_ENV}"
                "（管理员签发的 agt_* key）"
                f"，或写入 ~/{WORKBUDDY_IDENTITY_PATH} 的 api_key 字段"
                f"（WorkBuddy 已配好 MCP 时会自动继承 ~/{WORKBUDDY_MCP_JSON_PATH}）"
            )
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def describe(self) -> dict:
        """端点与密钥可用性摘要（只报「是否存在」，绝不回显密钥值）。"""
        return {
            "http_url": self.base_url,
            "mcp_url": self.mcp_url,
            "address_source": self.source,
            "agent_id": AGENT_ID,
            "agent_key_source": self.key_source,
            "agent_key_set": bool(self.key),
            "agent_key_env": WORKBUDDY_KEY_ENV,
            "admin_key_env": ADMIN_KEY_ENV,
            "admin_key_set": bool(_env_str(ADMIN_KEY_ENV)),
        }

    # ---------- 底层 ----------
    def _http(self, method: str, path: str, body: dict | None = None, timeout: int = 60):
        url = self.base_url + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self.key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise SGMEError(f"HTTP {e.code} {e.reason}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise SGMEError(f"连接失败 {url}: {e.reason}") from e

    def _key_for(self, scope: str) -> str:
        """按能力范围选密钥：agent（默认）｜admin（服务端强制）｜auto（有管理员 Key 用管理员）。"""
        if scope == "admin":
            k = _env_str(ADMIN_KEY_ENV)
            if not k:
                raise SGMEError(
                    f"该能力需管理员 Key：请设置环境变量 {ADMIN_KEY_ENV}"
                    "（写侧/管理类能力服务端强制校验管理员 Key）"
                )
            return k
        if scope == "auto":
            return _env_str(ADMIN_KEY_ENV) or self.key
        return self.key

    def _mcp(self, tool: str, args: dict | None = None, scope: str = "agent"):
        """MCP 层调用（lazy import；需 mcp 库，建议用 SGME 项目 venv）。

        先校验密钥范围再 import mcp——密钥问题直接报密钥错，不被「缺 mcp 库」掩盖。
        """
        key = self._key_for(scope)
        try:
            import asyncio
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as e:
            raise SGMEError(
                "MCP 层需要 mcp 库。请用 SGME 项目 venv 执行："
                "<project-root>/.venv/Scripts/python.exe sgme_client.py mcp <tool>"
            ) from e

        mcp_url = self.mcp_url

        async def _run():
            async with streamablehttp_client(mcp_url, headers={"X-API-Key": key}) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(tool, args or {})
                    texts = [getattr(c, "text", "") or "" for c in res.content]
                    return "\n".join(texts), res.isError

        # 防代理劫持内网：调用期间摘掉代理环境变量（mcp/httpx 默认 trust_env=True）
        saved = {k: os.environ.pop(k, None) for k in _PROXY_ENVS}
        try:
            out, is_err = asyncio.run(_run())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        if is_err:
            raise SGMEError(f"MCP {tool} 返回错误: {out[:300]}")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    # ---------- HTTP 层：记忆 ----------
    def health(self):
        return self._http("GET", "/v1/health")

    def append(self, session_key: str, text: str, role: str = "user",
               agent_id: str = AGENT_ID, started_at: str | None = None) -> dict:
        ts = started_at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content = f"# {ts} {role}\n{text}"
        return self._http("POST", "/v1/append", {
            "session_key": session_key, "started_at": ts,
            "content": content, "agent_id": agent_id,
        })

    def inject(self, mode: str = "daily", max_tokens: int | None = None):
        body = {"mode": mode}
        if max_tokens:
            body["max_tokens"] = max_tokens
        return self._http("POST", "/v1/inject", body)

    def search(self, query: str, limit: int = 5, scopes: list[str] | None = None):
        body = {"query": query, "limit": limit}
        if scopes:
            body["scopes"] = scopes
        return self._http("POST", "/v1/search", body)

    def answer(self, query: str, limit: int = 5):
        return self._http("POST", "/v1/answer", {"query": query, "limit": limit})

    def memory_get(self, memory_id: str):
        return self._http("GET", f"/v1/memory/{urllib.parse.quote(memory_id)}")

    def memory_reject(self, memory_id: str, reason: str):
        return self._http("POST", f"/v1/memory/{urllib.parse.quote(memory_id)}/reject", {"reason": reason})

    def memory_unreject(self, memory_id: str):
        return self._http("POST", f"/v1/memory/{urllib.parse.quote(memory_id)}/unreject")

    # ---------- HTTP 层：事件 ----------
    def events_pull(self, subscriber_id: str = AGENT_ID, limit: int | None = None):
        """拉取未消费事件（服务端无类型过滤，类型筛选由调用方在客户端完成）。"""
        params = {"subscriber_id": subscriber_id}
        if limit:
            params["limit"] = limit
        q = urllib.parse.urlencode(params)
        return self._http("GET", f"/v1/events/pull?{q}")

    def events_after(self, after_ts: str, limit: int = 50):
        q = urllib.parse.urlencode({"after": after_ts, "limit": limit})
        return self._http("GET", f"/v1/events?{q}")

    # ---------- HTTP 层：技能 ----------
    def skill_list(self, offset: int = 0, limit: int | None = None):
        params = {"offset": offset}
        if limit:
            params["limit"] = limit
        q = urllib.parse.urlencode(params)
        return self._http("GET", f"/v1/skills?{q}")

    def skill_coldstart(self):
        return self._http("GET", "/v1/skills/coldstart")

    def skill_search(self, query: str, limit: int = 5):
        q = urllib.parse.urlencode({"q": query, "limit": limit})
        return self._http("GET", f"/v1/skills/search?{q}")

    def skill_digest(self, name: str):
        return self._http("GET", f"/v1/skills/{urllib.parse.quote(name)}/digest")

    def skill_get(self, name: str, section: str | None = None):
        """技能全文（section 可选：按章节只取一段）。"""
        path = f"/v1/skills/{urllib.parse.quote(name)}"
        if section:
            path += f"?section={urllib.parse.quote(section)}"
        return self._http("GET", path)

    def skill_materialize(self, name: str, dest_dir: str):
        """技能物化落盘，返回 path + sha256。"""
        return self._http("POST", f"/v1/skills/{urllib.parse.quote(name)}/materialize",
                          {"dest_dir": dest_dir})

    # ---------- HTTP 层：wiki ----------
    def wiki_search(self, query: str, limit: int = 5):
        q = urllib.parse.urlencode({"q": query, "limit": limit})
        return self._http("GET", f"/v1/wiki/search?{q}")

    def wiki_pages(self, category: str | None = None):
        path = "/v1/wiki/pages"
        if category:
            path += "?" + urllib.parse.urlencode({"category": category})
        return self._http("GET", path)

    def wiki_page(self, page_id: str):
        return self._http("GET", f"/v1/wiki/pages/{urllib.parse.quote(page_id)}")

    def wiki_page_add(self, title: str, content: str, category: str | None = None):
        body = {"title": title, "content": content}
        if category:
            body["category"] = category
        return self._http("POST", "/v1/wiki/pages", body)

    def wiki_page_update(self, page_id: str, content: str):
        return self._http("PATCH", f"/v1/wiki/pages/{urllib.parse.quote(page_id)}",
                          {"content": content})

    def wiki_raw(self, file_hash: str):
        return self._http("GET", f"/v1/wiki/raw/{urllib.parse.quote(file_hash)}")

    # ---------- MCP 层：接入 / 提炼 / 统计 ----------
    def agent_onboarding(self):
        return self._mcp("agent_onboarding", {})

    def refine_trigger(self, file_id: str | None = None, limit: int = 50,
                       async_mode: bool = True):
        args = {"async_mode": async_mode, "limit": limit}
        if file_id:
            args["file_id"] = file_id
        return self._mcp("refine_trigger", args)

    def refine_batch(self, file_ids: list[str] | None = None, limit: int = 50,
                     async_mode: bool = True):
        args = {"async_mode": async_mode, "limit": limit}
        if file_ids:
            args["file_ids"] = file_ids
        return self._mcp("refine_batch", args)

    def refine_status(self):
        return self._mcp("refine_status", {})

    def stats(self):
        return self._mcp("stats", {})

    # ---------- MCP 层：配置 ----------
    def config_get(self, section: str | None = None):
        return self._mcp("config_get", {"section": section} if section else {})

    def config_update(self, section: str, values: dict, scope: str = "auto"):
        """更新配置段（values 为 dict）；有管理员 Key 时自动走管理员 Key。"""
        return self._mcp("config_update", {"section": section, "values": values}, scope=scope)

    # ---------- MCP 层：创意 / 待办 / 项目 ----------
    def idea_add(self, content: str, priority: int | None = None,
                 source_ref: str | None = None):
        args: dict = {"content": content}
        if priority is not None:
            args["priority"] = priority
        if source_ref:
            args["source_ref"] = source_ref
        return self._mcp("idea_add", args)

    def demand_create(self, title: str, content: str | None = None,
                      priority: int | None = None, project_id: str | None = None,
                      source_ref: str | None = None,
                      origin_idea_id: str | None = None):
        args: dict = {"title": title}
        for k, v in (("content", content), ("priority", priority),
                     ("project_id", project_id), ("source_ref", source_ref),
                     ("origin_idea_id", origin_idea_id)):
            if v is not None:
                args[k] = v
        return self._mcp("demand_create", args)

    def project_register(self, project_id: str, path: str | None = None,
                         name: str | None = None, git_repo: str | None = None,
                         milestone: str | None = None):
        """登记/更新项目（project_id 纯英文必填；新建时 path 必填）。"""
        args: dict = {"project_id": project_id}
        for k, v in (("path", path), ("name", name),
                     ("git_repo", git_repo), ("milestone", milestone)):
            if v is not None:
                args[k] = v
        return self._mcp("project_register", args)

    # ---------- MCP 层：信号闭环 ----------
    def signal_pull(self, signal_type: str | None = None, limit: int = 20):
        args: dict = {"limit": limit}
        if signal_type:
            args["signal_type"] = signal_type
        return self._mcp("signal_pull", args)

    def signal_claim(self, event_id: str):
        return self._mcp("signal_claim", {"event_id": event_id})

    def signal_ack(self, event_id: str, status: str = "acked",
                   result: str | None = None):
        args: dict = {"event_id": event_id, "status": status}
        if result:
            args["result"] = result
        return self._mcp("signal_ack", args)

    def signal_clear(self, signal_type: str | None = None,
                     subscriber_id: str | None = None, scope: str = "auto"):
        """批量清空未消费信号（幂等；管理操作，有管理员 Key 时自动走管理员 Key）。"""
        args: dict = {}
        if signal_type:
            args["signal_type"] = signal_type
        if subscriber_id:
            args["subscriber_id"] = subscriber_id
        return self._mcp("signal_clear", args, scope=scope)

    # ---------- MCP 层：角色 ----------
    def role_list(self):
        return self._mcp("role_list", {})

    def role_assemble(self, role_id: str, inject_mode: str | None = None):
        return self._mcp("role_assemble", {"role_id": role_id}
                         | ({"inject_mode": inject_mode} if inject_mode else {}))

    def role_active_get(self):
        return self._mcp("role_active_get", {})

    def role_active_set(self, role_id: str):
        return self._mcp("role_active_set", {"role_id": role_id})

    # ---------- MCP 层：自进化 / 技能写侧 ----------
    def wiki_evolve_trigger(self, session_key: str | None = None, min_rounds: int = 5,
                            limit: int = 5):
        args: dict = {"min_rounds": min_rounds, "limit": limit}
        if session_key:
            args["session_key"] = session_key
        return self._mcp("wiki_evolve_trigger", args)

    def skill_put(self, name: str, content: str):
        """写入/覆盖技能（content 为 SKILL.md 全文）；服务端强制管理员 Key。"""
        return self._mcp("skill_put", {"name": name, "content": content}, scope="admin")

    def skill_delete(self, name: str, hard: bool = False, force: bool = False):
        """删除技能（默认软删；hard=True 物理删；有入向引用需 force=True）；需管理员 Key。"""
        return self._mcp("skill_delete", {"name": name, "hard": hard, "force": force},
                         scope="admin")

    def skill_rename(self, name: str, new_name: str):
        """技能改名（墓碑制）；需管理员 Key。"""
        return self._mcp("skill_rename", {"name": name, "new_name": new_name},
                         scope="admin")

    def mcp_raw(self, tool: str, args: dict | None = None, scope: str = "agent"):
        return self._mcp(tool, args or {}, scope=scope)


# ================= 能力矩阵 =================

def capability_report() -> list[dict]:
    """逐条核对 41 个基准能力的方法层与 CLI 层覆盖度（离线，不发网络请求）。"""
    rows = []
    for tool in BASELINE_TOOLS:
        method, cli, layer, scope = BASELINE_SPEC[tool]
        rows.append({
            "tool": tool, "method": method, "cli": cli, "layer": layer, "scope": scope,
            "method_ok": hasattr(SGME, method),
            "cli_ok": cli in CLI_NAMES,
        })
    return rows


# ================= CLI =================

def _print(obj):
    if obj is None:
        return
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _split_csv(s: str | None) -> list[str] | None:
    if not s:
        return None
    items = [x.strip() for x in s.split(",") if x.strip()]
    return items or None


def _parse_kv_pairs(pairs: list[str] | None) -> dict:
    """`k=v` 列表 → dict（自动识别 bool/int）。"""
    out: dict = {}
    for kv in pairs or []:
        k, _, v = kv.partition("=")
        if not k:
            continue
        low = v.lower()
        if low in ("true", "false"):
            out[k] = low == "true"
        elif v.lstrip("-").isdigit():
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _fmt_signal_pull(resp, types_filter: str | None = None):
    """把 events_pull 结果格式化为可读清单；types_filter 为前缀过滤（如 'care_'）。"""
    if isinstance(resp, dict) and ("events" in resp or "signals" in resp):
        items = resp.get("events") or resp.get("signals") or []
        if types_filter:
            items = [it for it in items
                     if str(it.get("type") or it.get("event_type") or "").startswith(types_filter)]
        if not items:
            print("暂无未消费信号 ✅")
            return
        for it in items:
            eid = it.get("event_id") or it.get("id") or "?"
            typ = it.get("type") or it.get("event_type") or "?"
            title = it.get("title") or it.get("summary") or it.get("content") or ""
            print(f"[{typ}] {eid}  {str(title)[:120]}")
        return
    _print(resp)


def _print_matrix(_c, _a):
    rows = capability_report()
    ok = sum(1 for r in rows if r["method_ok"] and r["cli_ok"])
    print(f"SGME × WorkBuddy 基准能力矩阵（{ok}/{len(rows)}）\n")
    for r in rows:
        mark = "✅" if (r["method_ok"] and r["cli_ok"]) else "❌"
        print(f"  {mark} {r['tool']:<22} {r['layer']:<4} 方法={r['method']:<22} "
              f"命令=sgme_client.py {r['cli']}")
    print("\n另含适配器自有扩展命令：events-pull / events-after / wiki-raw / mcp（逃生口）"
          " / capabilities / env-info")


def _print_env_info(_c, _a):
    http, mcp, src = resolve_addresses()
    key, key_src = resolve_agent_key()
    print("SGME × WorkBuddy 客户端生效配置（不打印密钥值）")
    print(f"  HTTP 端点  : {http}")
    print(f"  MCP  端点  : {mcp}")
    print(f"  地址来源   : {src}")
    print(f"  agent_id   : {AGENT_ID}")
    print(f"  agent key  : {'已取到' if key else '未取到'}（来源: {key_src or '—'}）")
    for env_name, desc in ((WORKBUDDY_KEY_ENV, "agent 能力面（专用，推荐）"),
                           (KEY_ENV, "agent 能力面（通用兜底）"),
                           (ADMIN_KEY_ENV, "写侧/管理能力")):
        print(f"  {env_name:<20}: {'已设置' if _env_str(env_name) else '未设置'}（{desc}）")
    ident = load_workbuddy_identity()
    mcp_cfg = load_workbuddy_mcp_json()
    print(f"  身份文件   : ~/{WORKBUDDY_IDENTITY_PATH} "
          f"{'已加载（' + str(sorted(ident)) + '）' if ident else '未找到'}")
    print(f"  MCP 配置   : ~/{WORKBUDDY_MCP_JSON_PATH} "
          f"{'已发现 sgme server（' + str(sorted(mcp_cfg)) + '）' if mcp_cfg else '未发现 sgme server'}")
    cfg = load_deploy_config()
    print(f"  部署配置   : {DEPLOY_CONFIG_NAME} "
          f"{'已加载（' + str(sorted(cfg)) + '）' if cfg else '未找到（回落到环境变量/回环默认）'}")


def _read_text_arg(inline: str | None, file_path: str | None, what: str) -> str:
    if inline and file_path:
        raise SGMEError(f"{what}：--text/--content 与 --file 只能二选一")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    if inline:
        return inline
    raise SGMEError(f"{what}：必须提供内容（内联参数或 --file）")


def _run_mcp_kv(c, a):
    args = _parse_kv_pairs(a.kwargs)
    if a.json_args:
        try:
            extra = json.loads(a.json_args)
        except json.JSONDecodeError as e:
            raise SGMEError(f"--json-args 不是合法 JSON: {e}") from e
        if not isinstance(extra, dict):
            raise SGMEError("--json-args 必须是 JSON 对象")
        args.update(extra)
    return c.mcp_raw(a.tool, args, scope="admin" if a.admin else "agent")


# 命令表：cli → {help, offline?, positionals, options, run}
CMD_SPECS: list[dict] = [
    # ---- HTTP 层 ----
    {"cli": "health", "help": "健康检查（服务可达）", "run": lambda c, a: c.health()},
    {
        "cli": "append", "help": "写入原始会话（L0 捕获）",
        "options": [
            {"dest": "session", "flag": "session", "required": True, "help": "会话键（同一会话延续）"},
            {"dest": "text", "flag": "text", "help": "本轮文本（与 --file 二选一）"},
            {"dest": "file", "flag": "file", "help": "从文件读本轮文本（与 --text 二选一）"},
            {"dest": "role", "flag": "role", "default": "user",
             "choices": ["user", "assistant"], "help": "角色（默认 user）"},
            {"dest": "agent", "flag": "agent", "default": AGENT_ID, "help": "agent_id"},
        ],
        "run": lambda c, a: c.append(a.session, _read_text_arg(a.text, a.file, "append"),
                                     a.role, a.agent),
    },
    {
        "cli": "inject", "help": "记忆注入（画像块）",
        "options": [
            {"dest": "mode", "flag": "mode", "default": "daily",
             "choices": ["daily", "coding", "work", "full"], "help": "注入场景"},
            {"dest": "max_tokens", "flag": "max-tokens", "type": int, "help": "注入 token 上限"},
        ],
        "run": lambda c, a: c.inject(a.mode, a.max_tokens),
    },
    {
        "cli": "search", "help": "混合检索（带溯源）",
        "positionals": [{"dest": "query", "help": "检索关键词"}],
        "options": [
            {"dest": "limit", "flag": "limit", "type": int, "default": 5, "help": "条数"},
            {"dest": "scopes", "flag": "scopes", "help": "逗号分隔：memory,skills,wiki,sessions"（sessions=L0 原文正文检索，T-207）},
        ],
        "run": lambda c, a: c.search(a.query, a.limit, _split_csv(a.scopes)),
    },
    {
        "cli": "answer", "help": "聚合问答（记忆+知识库）",
        "positionals": [{"dest": "query", "help": "自然语言问题"}],
        "options": [{"dest": "limit", "flag": "limit", "type": int, "default": 5, "help": "条数"}],
        "run": lambda c, a: c.answer(a.query, a.limit),
    },
    {
        "cli": "memory-get", "help": "单条记忆详情",
        "positionals": [{"dest": "memory_id", "help": "记忆 ID"}],
        "run": lambda c, a: c.memory_get(a.memory_id),
    },
    {
        "cli": "memory-reject", "help": "标记记忆不采用（纠错）",
        "positionals": [{"dest": "memory_id", "help": "记忆 ID"}],
        "options": [{"dest": "reason", "flag": "reason", "required": True, "help": "不采用原因"}],
        "run": lambda c, a: c.memory_reject(a.memory_id, a.reason),
    },
    {
        "cli": "memory-unreject", "help": "撤销「不采用」",
        "positionals": [{"dest": "memory_id", "help": "记忆 ID"}],
        "run": lambda c, a: c.memory_unreject(a.memory_id),
    },
    {
        "cli": "events-pull", "help": "拉取未消费事件/信号（--types 客户端前缀过滤）",
        "options": [
            {"dest": "subscriber", "flag": "subscriber", "default": AGENT_ID, "help": "订阅者 ID"},
            {"dest": "types", "flag": "types", "help": "类型前缀过滤，如 care_；不传看全部"},
            {"dest": "limit", "flag": "limit", "type": int, "help": "条数"},
        ],
        "run": lambda c, a: _fmt_signal_pull(c.events_pull(a.subscriber, a.limit), a.types),
    },
    {
        "cli": "events-after", "help": "按游标增量拉取事件（扩展）",
        "positionals": [{"dest": "after_ts", "help": "ISO 时间戳（含）之后的事件"}],
        "options": [{"dest": "limit", "flag": "limit", "type": int, "default": 50, "help": "条数"}],
        "run": lambda c, a: c.events_after(a.after_ts, a.limit),
    },
    # ---- HTTP 层：技能 ----
    {
        "cli": "skill-list", "help": "技能索引",
        "options": [
            {"dest": "offset", "flag": "offset", "type": int, "default": 0, "help": "偏移"},
            {"dest": "limit", "flag": "limit", "type": int, "help": "条数"},
        ],
        "run": lambda c, a: c.skill_list(a.offset, a.limit),
    },
    {"cli": "skill-coldstart", "help": "技能冷启动包",
     "run": lambda c, a: c.skill_coldstart()},
    {
        "cli": "skill-search", "help": "技能检索",
        "positionals": [{"dest": "query", "help": "检索词"}],
        "options": [{"dest": "limit", "flag": "limit", "type": int, "default": 5, "help": "条数"}],
        "run": lambda c, a: c.skill_search(a.query, a.limit),
    },
    {
        "cli": "skill-digest", "help": "技能摘要（一级披露）",
        "positionals": [{"dest": "name", "help": "技能名"}],
        "run": lambda c, a: c.skill_digest(a.name),
    },
    {
        "cli": "skill-get", "help": "技能全文（--section 只取一章）",
        "positionals": [{"dest": "name", "help": "技能名"}],
        "options": [{"dest": "section", "flag": "section", "help": "章节名（可选）"}],
        "run": lambda c, a: c.skill_get(a.name, a.section),
    },
    {
        "cli": "skill-materialize", "help": "技能物化落盘（返回 path + sha256）",
        "positionals": [
            {"dest": "name", "help": "技能名"},
            {"dest": "dest_dir", "help": "落盘目标目录"},
        ],
        "run": lambda c, a: c.skill_materialize(a.name, a.dest_dir),
    },
    # ---- HTTP 层：wiki ----
    {
        "cli": "wiki-search", "help": "检索知识库",
        "positionals": [{"dest": "query", "help": "检索词"}],
        "options": [{"dest": "limit", "flag": "limit", "type": int, "default": 5, "help": "条数"}],
        "run": lambda c, a: c.wiki_search(a.query, a.limit),
    },
    {
        "cli": "wiki-pages", "help": "知识库页面列表",
        "options": [{"dest": "category", "flag": "category", "help": "分类（可选）"}],
        "run": lambda c, a: c.wiki_pages(a.category),
    },
    {
        "cli": "wiki-page", "help": "知识库页面全文",
        "positionals": [{"dest": "page_id", "help": "页面 ID"}],
        "run": lambda c, a: c.wiki_page(a.page_id),
    },
    {
        "cli": "wiki-page-add", "help": "写入知识库页面",
        "options": [
            {"dest": "title", "flag": "title", "required": True, "help": "标题"},
            {"dest": "content", "flag": "content", "help": "正文（与 --file 二选一）"},
            {"dest": "file", "flag": "file", "help": "从文件读正文"},
            {"dest": "category", "flag": "category", "help": "分类（可选）"},
        ],
        "run": lambda c, a: c.wiki_page_add(
            a.title, _read_text_arg(a.content, a.file, "wiki-page-add"), a.category),
    },
    {
        "cli": "wiki-page-update", "help": "追加更新知识库页面",
        "options": [
            {"dest": "page_id", "flag": "page-id", "required": True, "help": "页面 ID"},
            {"dest": "content", "flag": "content", "help": "追加正文（与 --file 二选一）"},
            {"dest": "file", "flag": "file", "help": "从文件读追加正文"},
        ],
        "run": lambda c, a: c.wiki_page_update(
            a.page_id, _read_text_arg(a.content, a.file, "wiki-page-update")),
    },
    {
        "cli": "wiki-raw", "help": "取原始文件内容（扩展）",
        "positionals": [{"dest": "file_hash", "help": "原始文件 hash"}],
        "run": lambda c, a: c.wiki_raw(a.file_hash),
    },
    # ---- MCP 层：接入 / 提炼 / 统计 ----
    {"cli": "agent-onboarding", "help": "MCP 层接入指引（能力面/坑位清单）",
     "run": lambda c, a: c.agent_onboarding()},
    {
        "cli": "refine-trigger", "help": "触发提炼（默认 async；--sync 强制同步）",
        "options": [
            {"dest": "file_id", "flag": "file-id", "help": "只提炼指定原始文件"},
            {"dest": "limit", "flag": "limit", "type": int, "default": 50, "help": "最多文件数"},
            {"dest": "sync", "flag": "sync", "action": "store_true", "help": "同步模式（不推荐）"},
        ],
        "run": lambda c, a: c.refine_trigger(a.file_id, a.limit, not a.sync),
    },
    {
        "cli": "refine-batch", "help": "批量提炼（--file-ids 逗号分隔；永远 async）",
        "options": [
            {"dest": "file_ids", "flag": "file-ids", "help": "逗号分隔的 file_id 列表"},
            {"dest": "limit", "flag": "limit", "type": int, "default": 50, "help": "最多文件数"},
            {"dest": "sync", "flag": "sync", "action": "store_true", "help": "同步模式（不推荐）"},
        ],
        "run": lambda c, a: c.refine_batch(_split_csv(a.file_ids), a.limit, not a.sync),
    },
    {"cli": "refine-status", "help": "提炼进度（队列/水位）",
     "run": lambda c, a: c.refine_status()},
    {"cli": "stats", "help": "引擎统计（记忆数/维度分布/水位）",
     "run": lambda c, a: c.stats()},
    # ---- MCP 层：配置 ----
    {
        "cli": "config-get", "help": "读取配置段",
        "options": [{"dest": "section", "flag": "section", "help": "段名（l1/l2/refine/search/backup）"}],
        "run": lambda c, a: c.config_get(a.section),
    },
    {
        "cli": "config-update", "help": "更新配置段（热生效 + 落盘；管理类能力）",
        "positionals": [{"dest": "section", "help": "段名"}],
        "options": [
            {"dest": "values", "flag": "values", "help": 'JSON 对象，如 \'{"search":{"limit":5}}\''},
            {"dest": "set", "flag": "set", "action": "append", "help": "k=v（可重复，与 --values 合并）"},
        ],
        "run": lambda c, a: c.config_update(a.section, _merge_values(a)),
    },
    # ---- MCP 层：创意 / 待办 / 项目 ----
    {
        "cli": "idea-add", "help": "登记创意（用户主动提出）",
        "positionals": [{"dest": "content", "help": "创意内容"}],
        "options": [
            {"dest": "priority", "flag": "priority", "type": int, "help": "优先级"},
            {"dest": "source_ref", "flag": "source-ref", "help": "来源引用（可选）"},
        ],
        "run": lambda c, a: c.idea_add(a.content, a.priority, a.source_ref),
    },
    {
        "cli": "demand-create", "help": "登记待办/需求",
        "positionals": [{"dest": "title", "help": "待办标题"}],
        "options": [
            {"dest": "content", "flag": "content", "help": "详情（可选）"},
            {"dest": "priority", "flag": "priority", "type": int, "help": "优先级"},
            {"dest": "project_id", "flag": "project-id", "help": "所属项目 ID"},
            {"dest": "source_ref", "flag": "source-ref", "help": "来源引用"},
            {"dest": "origin_idea_id", "flag": "origin-idea-id", "help": "从创意升格时的创意 ID"},
        ],
        "run": lambda c, a: c.demand_create(a.title, a.content, a.priority, a.project_id,
                                            a.source_ref, a.origin_idea_id),
    },
    {
        "cli": "project-register", "help": "登记/更新项目（project_id 纯英文；新建需 --path）",
        "positionals": [{"dest": "project_id", "help": "项目 ID（纯英文，一律大写）"}],
        "options": [
            {"dest": "path", "flag": "path", "help": "项目路径（新建时必填）"},
            {"dest": "name", "flag": "name", "help": "项目显示名"},
            {"dest": "git_repo", "flag": "git-repo", "help": "仓库地址"},
            {"dest": "milestone", "flag": "milestone", "help": "当前里程碑"},
        ],
        "run": lambda c, a: c.project_register(a.project_id, a.path, a.name, a.git_repo,
                                               a.milestone),
    },
    # ---- MCP 层：信号闭环 ----
    {
        "cli": "signal-pull", "help": "拉取未消费关怀信号（MCP）",
        "options": [
            {"dest": "signal_type", "flag": "signal-type", "help": "类型过滤（如 care_daily）"},
            {"dest": "limit", "flag": "limit", "type": int, "default": 20, "help": "条数"},
        ],
        "run": lambda c, a: c.signal_pull(a.signal_type, a.limit),
    },
    {
        "cli": "signal-claim", "help": "原子认领信号（关怀前）",
        "positionals": [{"dest": "event_id", "help": "事件 ID"}],
        "run": lambda c, a: c.signal_claim(a.event_id),
    },
    {
        "cli": "signal-ack", "help": "写消费回执（claimed/acked/failed）",
        "positionals": [{"dest": "event_id", "help": "事件 ID"}],
        "options": [
            {"dest": "status", "flag": "status", "default": "acked",
             "choices": ["claimed", "acked", "failed"], "help": "回执状态"},
            {"dest": "result", "flag": "result", "help": "处理结果说明（可选）"},
        ],
        "run": lambda c, a: c.signal_ack(a.event_id, a.status, a.result),
    },
    {
        "cli": "signal-clear", "help": "批量清空未消费信号（幂等；管理类能力）",
        "options": [
            {"dest": "signal_type", "flag": "signal-type", "help": "类型精确过滤（如 care_daily）"},
            {"dest": "subscriber", "flag": "subscriber", "help": "同步推进该订阅者游标"},
        ],
        "run": lambda c, a: c.signal_clear(a.signal_type, a.subscriber),
    },
    # ---- MCP 层：角色 ----
    {"cli": "role-list", "help": "可用沟通角色清单",
     "run": lambda c, a: c.role_list()},
    {
        "cli": "role-assemble", "help": "装配角色人设（换皮不换芯）",
        "positionals": [{"dest": "role_id", "help": "角色 ID"}],
        "options": [{"dest": "inject_mode", "flag": "inject-mode", "help": "注入场景（可选）"}],
        "run": lambda c, a: c.role_assemble(a.role_id, a.inject_mode),
    },
    {"cli": "role-active-get", "help": "读取当前沟通角色（未设置返回 null）",
     "run": lambda c, a: c.role_active_get()},
    {
        "cli": "role-active-set", "help": "设置当前沟通角色（只换角色不换记忆池）",
        "positionals": [{"dest": "role_id", "help": "角色 ID"}],
        "run": lambda c, a: c.role_active_set(a.role_id),
    },
    # ---- MCP 层：自进化 / 技能写侧 ----
    {
        "cli": "wiki-evolve-trigger", "help": "自进化：会话经验写回知识库手册",
        "options": [
            {"dest": "session_key", "flag": "session-key", "help": "会话键（可选）"},
            {"dest": "min_rounds", "flag": "min-rounds", "type": int, "default": 5,
             "help": "费用门禁：最少消息块"},
            {"dest": "limit", "flag": "limit", "type": int, "default": 5, "help": "最多处理会话数"},
        ],
        "run": lambda c, a: c.wiki_evolve_trigger(a.session_key, a.min_rounds, a.limit),
    },
    {
        "cli": "skill-put", "help": "写入/覆盖技能（SKILL.md 全文；需管理员 Key）",
        "positionals": [{"dest": "name", "help": "技能名"}],
        "options": [
            {"dest": "content", "flag": "content", "help": "SKILL.md 全文（与 --file 二选一）"},
            {"dest": "file", "flag": "file", "help": "从文件读 SKILL.md（与 --content 二选一）"},
        ],
        "run": lambda c, a: c.skill_put(a.name, _read_text_arg(a.content, a.file, "skill-put")),
    },
    {
        "cli": "skill-delete", "help": "删除技能（默认软删；需管理员 Key）",
        "positionals": [{"dest": "name", "help": "技能名"}],
        "options": [
            {"dest": "hard", "flag": "hard", "action": "store_true", "help": "物理删除"},
            {"dest": "force", "flag": "force", "action": "store_true", "help": "有入向引用时强制删"},
        ],
        "run": lambda c, a: c.skill_delete(a.name, a.hard, a.force),
    },
    {
        "cli": "skill-rename", "help": "技能改名（墓碑制；需管理员 Key）",
        "positionals": [
            {"dest": "name", "help": "旧技能名"},
            {"dest": "new_name", "help": "新技能名"},
        ],
        "run": lambda c, a: c.skill_rename(a.name, a.new_name),
    },
    # ---- 诊断 / 逃生口 ----
    {"cli": "capabilities", "help": "打印基准能力矩阵（离线核对，不发请求）",
     "offline": True, "run": _print_matrix},
    {"cli": "env-info", "help": "打印生效端点与密钥可用性（离线，不打印密钥值）",
     "offline": True, "run": _print_env_info},
    {
        "cli": "mcp", "help": "逃生口：直接调任意 MCP 工具（k=v 或 --json-args）",
        "positionals": [
            {"dest": "tool", "help": "MCP 工具名"},
            {"dest": "kwargs", "nargs": "*", "help": "key=value 参数"},
        ],
        "options": [
            {"dest": "json_args", "flag": "json-args", "help": "JSON 对象参数（与 k=v 合并）"},
            {"dest": "admin", "flag": "admin", "action": "store_true",
             "help": "用管理员 Key（SGME_ADMIN_KEY）调用"},
        ],
        "run": _run_mcp_kv,
    },
]


def _merge_values(a) -> dict:
    """config-update 取值：--values JSON 对象 + 可重复 --set k=v。"""
    values: dict = {}
    if a.values:
        try:
            parsed = json.loads(a.values)
        except json.JSONDecodeError as e:
            raise SGMEError(f"--values 不是合法 JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise SGMEError("--values 必须是 JSON 对象")
        values.update(parsed)
    values.update(_parse_kv_pairs(a.set))
    if not values:
        raise SGMEError("config-update 需要 --values <JSON> 或 --set k=v 至少一项")
    return values


CMD_BY_NAME: dict[str, dict] = {}
for _spec in CMD_SPECS:
    CMD_BY_NAME[_spec["cli"]] = _spec
    for _alias in _spec.get("aliases", []):
        CMD_BY_NAME[_alias] = _spec
CLI_NAMES: frozenset[str] = frozenset(s["cli"] for s in CMD_SPECS)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sgme_client.py",
        description="SGME × WorkBuddy 适配器客户端（HTTP 层零依赖 + MCP 层）",
        epilog="能力矩阵：sgme_client.py capabilities ｜ 端点自检：sgme_client.py env-info",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for spec in CMD_SPECS:
        sp = sub.add_parser(spec["cli"], help=spec["help"], aliases=spec.get("aliases", []))
        for pos in spec.get("positionals", []):
            kw = {}
            if pos.get("nargs"):
                kw["nargs"] = pos["nargs"]
            sp.add_argument(pos["dest"], help=pos.get("help", ""), **kw)
        for opt in spec.get("options", []):
            kw = {"dest": opt["dest"], "help": opt.get("help", "")}
            if opt.get("action"):
                kw["action"] = opt["action"]
            else:
                kw["default"] = opt.get("default")
                if opt.get("type"):
                    kw["type"] = opt["type"]
                if opt.get("required"):
                    kw["required"] = True
                if opt.get("choices"):
                    kw["choices"] = opt["choices"]
            sp.add_argument("--" + opt["flag"], **kw)
    return p


def main(argv=None) -> int:
    parser = _build_parser()
    a = parser.parse_args(argv)
    spec = CMD_BY_NAME[a.cmd]

    if spec.get("offline"):
        try:
            _print(spec["run"](None, a))
        except SGMEError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        return 0

    try:
        c = SGME()
    except SGMEError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    try:
        _print(spec["run"](c, a))
    except SGMEError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
