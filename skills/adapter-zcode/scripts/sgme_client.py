# -*- coding: utf-8 -*-
"""
sgme_client.py — SGME × ZCode（ZCode CLI Agent）适配器客户端
=============================================================

ZCode 官方适配器（adapters/zcode）的运行时客户端。两层通道：

  HTTP 层（agent key 直接可用，标准库零依赖）：
    health / append / inject / search / answer / memory_get / memory_reject /
    memory_unreject / events_pull / events_after / skill_*（读+写）/ wiki_*（读+写）/
    fetch（跨机取技能完整包：下载 → MANIFEST 校验 → 安全解包）

  MCP 层（需 mcp 库，用 SGME 项目 venv 执行）：
    agent_onboarding / refine_* / review_* / stats / config_get / config_update / signal_* /
    role_* / demand_create / project_register / idea_add / wiki_evolve_trigger /
    skill_put / skill_delete / skill_rename

地址解析（六级，构造时求值；部署后无需改代码）：
  ① 调用参数：--base-url（HTTP）、--mcp-url（MCP）
  ② 环境变量：SGME_HTTP_URL 或 SGME_BASE_URL（HTTP）、SGME_MCP_URL（MCP）
  ③ 同目录部署配置：`client.env`（由 install.py 部署时写入部署副本；仓库内不含真值）
  ④ 身份文件：~/.sgme/zcode-agent.json（http / mcp / agent_id 字段）
  ⑤ ZCode 自己的 MCP 配置：~/.zcode/cli/config.json 的 `sgme` server（零配置继承）
  ⑥ 回环默认：http://127.0.0.1:9910（MCP 端点由 HTTP 主机 :端口+3 推导）
  只给 HTTP 地址即可——MCP 端点自动按同主机「端口 +3 / 路径 /mcp」推导（9910→9913）。

密钥（只读环境变量或本机部署产物，不硬编码、不回显；部署副本 .env 在仓库外）：
  SGME_ZCODE_KEY —— ZCode 专用 agent key（最高优先；install.py 注册 agt_* key 后写入部署副本 .env）
  SGME_ADMIN_KEY —— 写侧·管理能力（skill_put / skill_delete / skill_rename 服务端强制管理员 Key）
  SGME_AGENT_KEY —— 通用兜底（本机可能是其它 agent 的 key，附警示）

用法（能力面 = CLI 命令面；全部基准能力都有对应子命令）：
  python sgme_client.py health
  python sgme_client.py append --session 2026-01-01-zcode --text "……" [--role user|assistant]
  python sgme_client.py inject --mode daily
  python sgme_client.py search "某项目关键词" --limit 5
  python sgme_client.py answer "我最近在做什么项目？"
  python sgme_client.py events-pull --subscriber zcode
  python sgme_client.py skill-search "数字人"
  python sgme_client.py fetch <name> <dest_dir>  # 跨机取技能完整包（下载→校验→安全解包）
  python sgme_client.py wiki-search "接入"
  python sgme_client.py stats / config-get / refine-status / review-list / signal-pull / role-list
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
try:  # 分发包内 scripts/ 是平铺目录（脚本直跑）：同级导入
    import skill_cache
except ImportError:  # pragma: no cover - 以包方式导入时的兜底
    from . import skill_cache  # type: ignore[no-redef]

from pathlib import Path

# ---------- 常量与地址解析 ----------

KEY_ENV = "SGME_AGENT_KEY"
ADMIN_KEY_ENV = "SGME_ADMIN_KEY"
# ZCode 专用密钥变量（最高优先）。存在的理由：本机 SGME_AGENT_KEY 常常已被
# 其它 agent 占用——实测 .env 的 SGME_AGENT_KEY 绑定 agent_id=dsh，若直接优先使用
# 会把 ZCode 的 L0 打上 dsh 的 agent_tag，污染多 Agent 溯源与隔离（T-140）。
ZCODE_KEY_ENV = "SGME_ZCODE_KEY"
AGENT_ID = "zcode"

# ZCode 本机接入产物（相对 $HOME）
ZCODE_IDENTITY_PATH = ".sgme/zcode-agent.json"
ZCODE_CONFIG_PATH = ".zcode/cli/config.json"
# 部署副本 .env（install.py 注册 agent 后写入 SGME_ZCODE_KEY；仓库外，不入 git）
DEPLOY_ENV_NAME = "scripts/.env"
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


def load_zcode_identity(path: str | Path | None = None) -> dict:
    """读取 ZCode 本机身份文件 ~/.sgme/zcode-agent.json（不含密钥回显）。

    字段：api_key / http / mcp / agent_id。文件不存在或不可读返回 {}。
    仓库内永不出现该文件真值；仅部署侧本地使用。
    """
    f = Path(path) if path else Path.home() / ".sgme" / "zcode-agent.json"
    try:
        if not f.is_file():
            return {}
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_zcode_mcp_config(path: str | Path | None = None) -> dict:
    """从 ZCode 的用户配置发现已注册的 SGME MCP 条目（本适配器特有）。

    场景：install.py 已把 `sgme` server（type=http，含 url 与 X-API-Key 头）写进
    `~/.zcode/cli/config.json` 的 `mcp.servers`。CLI 客户端直接继承它即可
    **零配置接入**——地址与密钥都不必再填一遍，也不会多签一把 key。

    返回 `{"api_key": str, "url": str}`（缺哪项不出哪项）；未配置/不可读返回 {}。
    🔴 只读、绝不回显密钥明文（调用方仅可判空）。
    """
    f = Path(path) if path else Path.home() / ZCODE_CONFIG_PATH
    try:
        if not f.is_file():
            return {}
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers = (data.get("mcp") or {}).get("servers")
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


def load_deploy_key(path: str | Path | None = None) -> str:
    """读部署副本 scripts/.env 里的 SGME_ZCODE_KEY（install.py 注册 agent 后写入）。

    仅部署侧本地使用；仓库内不含该文件（.gitignore）。缺文件/缺字段返回空串。
    """
    f = Path(path) if path else _HERE / DEPLOY_ENV_NAME
    try:
        if not f.is_file():
            return ""
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() == ZCODE_KEY_ENV:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def resolve_agent_key() -> tuple[str, str]:
    """解析 agent 能力面密钥，返回 `(key, 来源说明)`；全无则返回 `("", "")`。

    顺序（ZCode 口径）：

    ① 环境变量 `SGME_ZCODE_KEY` —— 专用变量，最高优先
    ② 部署副本 `scripts/.env` 的 `SGME_ZCODE_KEY`（install.py 注册后写入）
    ③ 身份文件 `~/.sgme/zcode-agent.json` 的 `api_key`（用户手动放置时）
    ④ `~/.zcode/cli/config.json` 里 `sgme` server 的 `X-API-Key`（零配置继承）
    ⑤ 环境变量 `SGME_AGENT_KEY` —— 通用兜底，**附警示**

    为什么把 `SGME_AGENT_KEY` 降到兜底：实测本机 `.env` 的该变量绑定
    `agent_id=dsh`。若照搬「环境变量优先」，ZCode 写入的 L0 会被打上 dsh 的
    `agent_tag`，污染溯源与多 Agent 隔离（T-140 灰度）。
    宁可多一步显式配置，也不接受静默错标。
    """
    v = _env_str(ZCODE_KEY_ENV)
    if v:
        return v, f"环境变量 {ZCODE_KEY_ENV}"
    v = load_deploy_key()
    if v:
        return v, f"部署副本 {DEPLOY_ENV_NAME}"
    ident = load_zcode_identity()
    v = str(ident.get("api_key") or "").strip()
    if v:
        return v, f"身份文件 ~/{ZCODE_IDENTITY_PATH}"
    v = str(load_zcode_mcp_config().get("api_key") or "").strip()
    if v:
        return v, f"ZCode MCP 配置 ~/{ZCODE_CONFIG_PATH}"
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

    顺序：显式参数 → 环境变量 → 同目录部署配置 → 身份文件 → ZCode MCP 配置
    （由 `~/.zcode/cli/config.json` 的 sgme server 反推）→ 回环默认。
    """
    cfg = load_deploy_config() if deploy_cfg is None else deploy_cfg
    if http_url:
        return http_url.rstrip("/"), (mcp_url or derive_mcp_url(http_url)).rstrip("/"), "调用参数"
    env_http = _env_str("SGME_HTTP_URL") or _env_str("SGME_BASE_URL")
    cfg_http = cfg.get("SGME_HTTP_URL") or cfg.get("SGME_BASE_URL")
    ident = load_zcode_identity()
    ident_http = (ident.get("http") or "").strip()
    mcp_cfg_url = str(load_zcode_mcp_config().get("url") or "").strip()
    if env_http:
        http, src = env_http, "环境变量 SGME_HTTP_URL/SGME_BASE_URL"
    elif cfg_http:
        http, src = cfg_http, f"部署配置 {DEPLOY_CONFIG_NAME}"
    elif ident_http:
        http, src = ident_http, f"身份文件 ~/{ZCODE_IDENTITY_PATH}"
    elif mcp_cfg_url:
        http, src = derive_http_from_mcp(mcp_cfg_url), f"ZCode MCP 配置 ~/{ZCODE_CONFIG_PATH}"
    else:
        http, src = DEFAULT_HTTP_URL, "回环默认"
    ident_mcp = (ident.get("mcp") or "").strip()
    mcp = (mcp_url or _env_str("SGME_MCP_URL") or cfg.get("SGME_MCP_URL")
           or ident_mcp or mcp_cfg_url or derive_mcp_url(http))
    return http.rstrip("/"), mcp.rstrip("/"), src


# ---------- MCP 基准工具清单（权威基准：sgme/mcp_server.py，49 个） ----------

BASELINE_TOOLS: tuple[str, ...] = (
    "agent_onboarding", "append", "inject", "search", "answer",
    "wiki_search", "wiki_pages", "wiki_page", "wiki_page_add", "wiki_page_update",
    "wiki_evolve_trigger", "memory_get", "memory_reject", "memory_unreject",
    "refine_trigger", "refine_batch", "refine_status",
    "review_list", "review_approve", "review_reject", "review_auto_resolve",
    "stats", "health",
    "config_get", "config_update", "idea_add", "demand_create", "project_register",
    "signal_pull", "signal_claim", "signal_ack", "signal_clear",
    "role_list", "role_assemble", "role_active_get", "role_active_set", "persona_fill",
    "guard_reply",
    "skill_search", "skill_digest", "skill_get", "skill_materialize", "skill_list",
    "skill_coldstart", "skill_put", "skill_delete", "skill_rename",
    "skill_catalog_version", "skill_manifest", "skill_file_get", "skill_file_put", "skill_lint",
    "credential_list", "credential_get",
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
    # D-30 待审队列（T-327）：agent 平级读写，走 MCP
    "review_list": ("review_list", "review-list", "mcp", "agent"),
    "review_approve": ("review_approve", "review-approve", "mcp", "agent"),
    "review_reject": ("review_reject", "review-reject", "mcp", "agent"),
    "review_auto_resolve": ("review_auto_resolve", "review-auto-resolve", "mcp", "agent"),
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
    "persona_fill": ("persona_fill", "persona-fill", "mcp", "agent"),
    "guard_reply": ("guard_reply", "guard-reply", "mcp", "agent"),
    "skill_search": ("skill_search", "skill-search", "http", "agent"),
    "skill_digest": ("skill_digest", "skill-digest", "http", "agent"),
    "skill_get": ("skill_get", "skill-get", "http", "agent"),
    "skill_materialize": ("skill_materialize", "skill-materialize", "http", "agent"),
    "skill_list": ("skill_list", "skill-list", "http", "agent"),
    "skill_coldstart": ("skill_coldstart", "skill-coldstart", "http", "agent"),
    "skill_put": ("skill_put", "skill-put", "mcp", "admin"),
    "skill_delete": ("skill_delete", "skill-delete", "mcp", "admin"),
    "skill_rename": ("skill_rename", "skill-rename", "mcp", "admin"),
    "skill_catalog_version": ("skill_catalog_version", "skill-catalog-version", "http", "agent"),
    "skill_manifest": ("skill_manifest", "skill-manifest", "http", "agent"),
    "skill_file_get": ("skill_file_get", "skill-file-get", "http", "agent"),
    "skill_file_put": ("skill_file_put", "skill-file-put", "http", "admin"),
    "skill_lint": ("skill_lint", "skill-lint", "http", "admin"),
    # EP-11 凭据读侧（T-277）：agent 平级读，走 HTTP
    "credential_list": ("credential_list", "credential-list", "http", "agent"),
    "credential_get": ("credential_get", "credential-get", "http", "agent"),
}


class SGMEError(Exception):
    pass


class SGME:
    """SGME HTTP/MCP 客户端（ZCode 适配器运行时）。"""

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
                f"缺少密钥：请设置环境变量 {ZCODE_KEY_ENV} 或 {KEY_ENV}"
                "（管理员签发的 agt_* key；或重跑 adapters/zcode/install.py 自动注册）"
                f"，或写入 ~/{ZCODE_IDENTITY_PATH} 的 api_key 字段"
                f"（ZCode 已配好 MCP 时会自动继承 ~/{ZCODE_CONFIG_PATH}）"
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
            "agent_key_env": ZCODE_KEY_ENV,
            "admin_key_env": ADMIN_KEY_ENV,
            "admin_key_set": bool(_env_str(ADMIN_KEY_ENV)),
        }

    # ---------- 底层 ----------
    def _http(self, method: str, path: str, body: dict | None = None, timeout: int = 60,
              key: str | None = None):
        """HTTP 调用；key 给定时按能力范围选密钥（admin 写侧），缺省用 agent key。"""
        url = self.base_url + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self._key_for(key) if key else self.key)
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

    # ---------- HTTP 层：技能缓存 / 附件 / 预检（2.0 D-59 + T-404） ----------

    def skill_catalog_version(self):
        """技能目录版本（两级缓存第 1 级，<100 字节）：批量比对前先调，相同即零请求复用本地目录。"""
        return self._http("GET", "/v1/skills/catalog-version")

    def skill_manifest(self, name: str):
        """单技能清单（两级缓存第 2 级）：package_sha256 相同即复用本地副本；files[] 列附件。"""
        return self._http("GET", f"/v1/skills/{urllib.parse.quote(name)}/manifest")

    def skill_file_get(self, name: str, relpath: str):
        """读技能附件原文（octet-stream 原字节 + X-File-Sha256）。

        返回 {"name","path","size","sha256","binary","content"/"hint"}：文本附件给全文
        （超 64 KiB 截断并给 hint）；二进制只给摘要并指路 HTTP 单文件/包通道——
        二进制字节不进对话上下文。
        """
        seg = "/".join(urllib.parse.quote(p) for p in relpath.split("/") if p)
        raw, headers = self._http_raw(
            f"/v1/skills/{urllib.parse.quote(name)}/files/{seg}"
        )
        body = raw or b""
        payload = {
            "name": name, "path": relpath, "size": len(body),
            "sha256": headers.get("x-file-sha256", ""),
        }
        text = None
        if b"\x00" not in body:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is None:
            payload["binary"] = True
            payload["hint"] = (
                f"二进制附件不进对话；原字节走 HTTP GET /v1/skills/{name}/files/{relpath}"
                f"（octet-stream），或完整包 GET /v1/skills/{name}/package（tar.gz）"
            )
            return payload
        limit = 64 * 1024
        truncated = len(body) > limit
        if truncated:
            text = body[:limit].decode("utf-8", "ignore")
            payload["hint"] = "内容超过 64 KiB 已截断；完整原文走 HTTP 单文件通道"
        payload.update({"binary": False, "content": text, "truncated": truncated})
        return payload

    def skill_file_put(self, name: str, relpath: str, content: str):
        """写技能附件（PUT /v1/admin/skills/{name}/files/{relpath}；白名单目录；需管理员 Key）。"""
        seg = "/".join(urllib.parse.quote(p) for p in relpath.split("/") if p)
        return self._http(
            "PUT", f"/v1/admin/skills/{urllib.parse.quote(name)}/files/{seg}",
            {"content": content}, key="admin",
        )

    def skill_lint(self, name: str, content: str):
        """技能格式预检（GET /v1/admin/skills/{name}/lint；只校验不入库；需管理员 Key）。

        GET 带 JSON body（urllib 允许）；服务端同时支持 body 与 ?content= 两种入参。
        """
        return self._http(
            "GET", f"/v1/admin/skills/{urllib.parse.quote(name)}/lint",
            {"content": content}, key="admin",
        )

    def fetch(self, name: str, dest_dir: str) -> dict:
        """跨机取技能完整包并按规则落盘（包通道，T-283；方法 + CLI 双面）。

        `GET /v1/skills/<name>/package`（tar.gz）→ 包内 MANIFEST.sha256 逐文件校验
        → 安全解包 → 原子落盘；失败即中止且不留残件，任何输出不打印密钥。
        返回 {name, path, sha256, files}。同机快速路径仍用 skill_materialize。
        """
        return _fetch_skill_package(self, name, dest_dir)

    def _http_raw(self, path: str, timeout: int = 300) -> tuple[bytes, dict]:
        """原始 GET（二进制安全）：返回 (响应体 bytes, 响应头小写化 dict)。

        `_http` 走 JSON 解码，不适合二进制包下载；错误包装与其同口径。
        """
        url = self.base_url + path
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-API-Key", self.key)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.read(), {str(k).lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (404, 405):
                detail = ("技能不存在，或服务端版本不足（技能包通道需支持 "
                          "/v1/skills/{name}/package 的 SGME 版本）；可先 skill-get 核对技能名。"
                          + detail[:200])
            raise SGMEError(f"HTTP {e.code} {e.reason}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise SGMEError(f"连接失败 {url}: {e.reason}") from e

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

    def review_list(self, limit: int = 50, offset: int = 0, dimension: str | None = None):
        """待审记忆列表（needs_review=1 不进主注入，需人工放行/拒绝；D-30 七天未处理自动消化）。"""
        args: dict = {"limit": limit, "offset": offset}
        if dimension:
            args["dimension"] = dimension
        return self._mcp("review_list", args)

    def review_approve(self, memory_id: str, note: str | None = None):
        """放行一条待审记忆（needs_review 置 0，恢复进主注入）。"""
        args: dict = {"memory_id": memory_id}
        if note:
            args["note"] = note
        return self._mcp("review_approve", args)

    def review_reject(self, memory_id: str, reason: str):
        """拒绝一条待审记忆（软删 status=rejected，可恢复，不物理删除）。"""
        return self._mcp("review_reject", {"memory_id": memory_id, "reason": reason})

    def review_auto_resolve(self, days: int = 7):
        """触发超期自动消化（D-30：超 N 天未处理的待审记忆按保守直存放行）。"""
        return self._mcp("review_auto_resolve", {"days": days})

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

    def role_assemble(self, role_id: str, inject_mode: str | None = None, render: bool = True):
        """装配角色提示词；render=True（默认，T-303）由服务端替换 {{char}}/{{user}} 宏。"""
        return self._mcp("role_assemble", {"role_id": role_id, "render": render}
                         | ({"inject_mode": inject_mode} if inject_mode else {}))

    def role_active_get(self):
        return self._mcp("role_active_get", {})

    def role_active_set(self, role_id: str):
        return self._mcp("role_active_set", {"role_id": role_id})

    def persona_fill(self, role_id: str, question: str):
        """人设懒补全（D-76 / T-306）：查档案命中直接用；未命中生成类型级背景+冲突检查+写回角色卡。

        status=hit/filled/unset；unset 表示冲突补不了（答「这个我没提过」）。
        角色人生事实只写角色卡，永不写记忆池。
        """
        return self._mcp("persona_fill", {"role_id": role_id, "question": question})

    def guard_reply(self, role_id: str, reply: str, session_id: str = ""):
        """越界三级后果（D-77 / T-307）：角色回复发出前自检并改写越界表述。

        status=clean 原样返回；rewritten 时须用返回的 reply 发送（不要发 original）；
        同类同会话 ≥2 返回 constraint（后续须更正/收回），同类当日 ≥3 发 anomaly_warn。
        越界只写角色卡，不进记忆池。
        """
        args: dict = {"role_id": role_id, "reply": reply}
        if session_id:
            args["session_id"] = session_id
        return self._mcp("guard_reply", args)

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

    # ---------- HTTP 层：凭据（EP-11 读侧，agent 平级） ----------
    def credential_list(self):
        """凭据列表：键名 + 元数据，**不含值**（需服务端 credentials.enabled 已启用）。"""
        return self._http("GET", "/v1/credentials")

    def credential_get(self, name: str):
        """取凭据单值（服务端记 read 审计）。

        ⚠️ 返回值含明文凭据——**不得进入对话正文**：标准姿势是客户端注入
        （scripts/sgme_cred.py run，值不落盘不进对话）；本方法仅限「必须显式查看」
        场景，取用后勿复述值（值经会话上下文将沿 L0 与云端模型链路扩散，不可回收）。
        """
        return self._http("GET", f"/v1/credentials/{urllib.parse.quote(name, safe='')}")

    def mcp_raw(self, tool: str, args: dict | None = None, scope: str = "agent"):
        return self._mcp(tool, args or {}, scope=scope)


# ================= 技能完整包（跨机获取通道，T-283 / D-59 落盘缓存） =================

_PKG_TIMEOUT = 300


class _ClientTransport:
    """把 SGME 客户端适配成 SkillCache 传输层（复用其鉴权、代理绕过与错误文案）。"""

    def __init__(self, client):
        self._client = client

    def get_json(self, path: str):
        """目录版本 / 单技能清单（JSON）。失败统一转 SkillCacheError → 缓存层降级。"""
        try:
            return self._client._http("GET", path)
        except Exception as e:  # SGMEError 等：缓存是优化，降级为直接拉包
            raise skill_cache.SkillCacheError(str(e)) from e

    def get_bytes(self, path: str):
        """技能包（二进制）。沿用 _http_raw 的错误文案（含「服务端版本不足」提示）。"""
        return self._client._http_raw(path, timeout=_PKG_TIMEOUT)


def _fetch_skill_package(client, name: str, dest_dir: str) -> dict:
    """技能包获取：两级缓存（D-59）→ 命中零传输，未命中下载校验后原子落盘。

    缓存逻辑统一在 ``skill_cache.py``（adapters/_shared 真源的随包副本）：
    先 catalog_version 目录级短路，再 package_sha256 单技能比对；未命中才
    ``GET /v1/skills/{name}/package``，按包内 MANIFEST.sha256 校验后
    「临时目录 → rename」原子替换。失败即中止且不留残件；输出不打印密钥。

    返回 {name, path, sha256, files, cached}；``cached=True`` 表示本次零包传输。
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise SGMEError(f"技能名非法: {name!r}")
    root = Path(dest_dir).expanduser()
    cache = skill_cache.SkillCache(_ClientTransport(client), state_dir=root)
    try:
        path = cache.ensure_skill(name, root)
    except skill_cache.SkillCacheError as e:
        raise SGMEError(str(e)) from e
    manifest = cache.last_manifest or {}
    return {
        "name": name,
        "path": str(path),
        "sha256": manifest.get("content_hash", ""),
        "files": len(manifest.get("files") or []),
        "cached": bool(cache.last_hit),
    }


# ================= 能力矩阵 =================

def capability_report() -> list[dict]:
    """逐条核对 49 个基准能力的方法层与 CLI 层覆盖度（离线，不发网络请求）。"""
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
    print(f"SGME × ZCode 基准能力矩阵（{ok}/{len(rows)}）\n")
    for r in rows:
        mark = "✅" if (r["method_ok"] and r["cli_ok"]) else "❌"
        print(f"  {mark} {r['tool']:<22} {r['layer']:<4} 方法={r['method']:<22} "
              f"命令=sgme_client.py {r['cli']}")
    print("\n另含适配器自有扩展命令：fetch（跨机取技能完整包：下载 → 校验 → 解包）"
          " / events-pull / events-after / wiki-raw / mcp（逃生口） / capabilities / env-info")


def _print_env_info(_c, _a):
    http, mcp, src = resolve_addresses()
    key, key_src = resolve_agent_key()
    print("SGME × ZCode 客户端生效配置（不打印密钥值）")
    print(f"  HTTP 端点  : {http}")
    print(f"  MCP  端点  : {mcp}")
    print(f"  地址来源   : {src}")
    print(f"  agent_id   : {AGENT_ID}")
    print(f"  agent key  : {'已取到' if key else '未取到'}（来源: {key_src or '—'}）")
    for env_name, desc in ((ZCODE_KEY_ENV, "agent 能力面（专用，推荐）"),
                           (KEY_ENV, "agent 能力面（通用兜底）"),
                           (ADMIN_KEY_ENV, "写侧/管理能力")):
        print(f"  {env_name:<20}: {'已设置' if _env_str(env_name) else '未设置'}（{desc}）")
    ident = load_zcode_identity()
    mcp_cfg = load_zcode_mcp_config()
    print(f"  身份文件   : ~/{ZCODE_IDENTITY_PATH} "
          f"{'已加载（' + str(sorted(ident)) + '）' if ident else '未找到'}")
    print(f"  MCP 配置   : ~/{ZCODE_CONFIG_PATH} "
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
            {"dest": "scopes", "flag": "scopes", "help": "逗号分隔：memory,skills,wiki,sessions（sessions=L0 原文正文检索，T-207）"},
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
    {
        "cli": "fetch",
        "help": "跨机取技能完整包：下载 → MANIFEST 校验 → 安全解包落盘（失败不留残件）",
        "positionals": [
            {"dest": "name", "help": "技能名"},
            {"dest": "dest_dir", "help": "落盘目标目录（产出 <dest_dir>/<name>/）"},
        ],
        "run": lambda c, a: c.fetch(a.name, a.dest_dir),
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
    {
        "cli": "review-list", "help": "待审记忆列表（needs_review=1 不进主注入，需人工放行/拒绝）",
        "options": [
            {"dest": "limit", "flag": "limit", "type": int, "default": 50, "help": "条数"},
            {"dest": "offset", "flag": "offset", "type": int, "default": 0, "help": "偏移"},
            {"dest": "dimension", "flag": "dimension", "help": "按维度过滤（如 identity）"},
        ],
        "run": lambda c, a: c.review_list(a.limit, a.offset, a.dimension),
    },
    {
        "cli": "review-approve", "help": "放行一条待审记忆（恢复进主注入）",
        "positionals": [{"dest": "memory_id", "help": "记忆 ID"}],
        "options": [{"dest": "note", "flag": "note", "help": "放行备注（可选）"}],
        "run": lambda c, a: c.review_approve(a.memory_id, a.note),
    },
    {
        "cli": "review-reject", "help": "拒绝一条待审记忆（软删，可恢复）",
        "positionals": [{"dest": "memory_id", "help": "记忆 ID"}],
        "options": [{"dest": "reason", "flag": "reason", "required": True, "help": "拒绝原因"}],
        "run": lambda c, a: c.review_reject(a.memory_id, a.reason),
    },
    {
        "cli": "review-auto-resolve", "help": "触发超期自动消化（超 N 天未处理按保守直存放行）",
        "options": [{"dest": "days", "flag": "days", "type": int, "default": 7, "help": "超期天数阈值"}],
        "run": lambda c, a: c.review_auto_resolve(a.days),
    },
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
    {
        "cli": "persona-fill", "help": "人设懒补全（D-76）：追问没写过的人生细节，命中直接用/生成写回角色卡",
        "positionals": [
            {"dest": "role_id", "help": "角色 ID"},
            {"dest": "question", "help": "用户追问的人生细节问题"},
        ],
        "run": lambda c, a: c.persona_fill(a.role_id, a.question),
    },
    {
        "cli": "guard-reply", "help": "越界三级后果（D-77）：回复发送前自检，命中则改写/记角色卡",
        "positionals": [
            {"dest": "role_id", "help": "角色 ID"},
            {"dest": "reply", "help": "待发送的角色回复原文"},
        ],
        "options": [
            {"dest": "session_id", "flag": "session-id", "help": "会话 ID（同类计数按会话聚合，可选）"},
        ],
        "run": lambda c, a: c.guard_reply(a.role_id, a.reply, a.session_id),
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
    # ---- 技能缓存与附件（2.0 D-59 / T-404） ----
    {"cli": "skill-catalog-version", "help": "技能目录版本（两级缓存第 1 级，<100 字节）",
     "run": lambda c, a: c.skill_catalog_version()},
    {
        "cli": "skill-manifest", "help": "单技能清单（package_sha256 同判 + 附件清单）",
        "positionals": [{"dest": "name", "help": "技能名"}],
        "run": lambda c, a: c.skill_manifest(a.name),
    },
    {
        "cli": "skill-file-get", "help": "读技能附件原文（二进制只回摘要，不进对话）",
        "positionals": [
            {"dest": "name", "help": "技能名"},
            {"dest": "relpath", "help": "相对路径，如 references/x.md"},
        ],
        "run": lambda c, a: c.skill_file_get(a.name, a.relpath),
    },
    {
        "cli": "skill-file-put", "help": "写技能附件（references/scripts/assets；需管理员 Key）",
        "positionals": [
            {"dest": "name", "help": "技能名"},
            {"dest": "relpath", "help": "相对路径，如 references/x.md"},
        ],
        "options": [
            {"dest": "content", "flag": "content", "help": "文件全文（与 --file 二选一）"},
            {"dest": "file", "flag": "file", "help": "从文件读（与 --content 二选一）"},
        ],
        "run": lambda c, a: c.skill_file_put(
            a.name, a.relpath, _read_text_arg(a.content, a.file, "skill-file-put")),
    },
    {
        "cli": "skill-lint", "help": "技能格式预检（只校验不入库；需管理员 Key）",
        "positionals": [{"dest": "name", "help": "技能名"}],
        "options": [
            {"dest": "content", "flag": "content", "help": "SKILL.md 全文（与 --file 二选一）"},
            {"dest": "file", "flag": "file", "help": "从文件读（与 --content 二选一）"},
        ],
        "run": lambda c, a: c.skill_lint(
            a.name, _read_text_arg(a.content, a.file, "skill-lint")),
    },
    # ---- 凭据读侧（EP-11，T-277） ----
    {"cli": "credential-list", "help": "凭据列表（键名+元数据，不含值）",
     "run": lambda c, a: c.credential_list()},
    {
        "cli": "credential-get",
        "help": "取凭据单值（⚠️ 值不得进入对话正文——标准姿势 sgme_cred.py run 注入）",
        "positionals": [{"dest": "name", "help": "凭据键名（credential-list 可见）"}],
        "run": lambda c, a: c.credential_get(a.name),
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
        description="SGME × ZCode 适配器客户端（HTTP 层零依赖 + MCP 层）",
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
