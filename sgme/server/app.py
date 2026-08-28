"""server/app.py：FastAPI 应用装配 + 鉴权中间件 + 统一错误结构。

鉴权分层（§6）：
- Bearer 令牌（传输层）：env SGME_BEARER_TOKEN 设置则开启，默认关闭（localhost 旁路）
- X-API-Key（契约层）：Admin Key 调 /v1/admin/*；Agent Key 调非 Admin 端点
  - /v1/health 仅需 Bearer（若有），不强制 X-API-Key
- 401 = Bearer 缺失/无效；403 = API Key 缺失/无效/无权限

依赖注入：create_app() 接受可选 cfg/conns，便于测试隔离。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger("sgme.server")

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao


# ---------- 统一错误结构 ----------

def error_response(code: str, message: str, status_code: int, details: dict | None = None) -> JSONResponse:
    """统一错误响应：{"error":{"code","message","details":{}}}。"""
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


# 错误码 → HTTP 状态映射
ERROR_CODES = {
    "ERR_INVALID_ARGS": 400,
    "ERR_UNAUTHORIZED": 401,
    "ERR_FORBIDDEN": 403,
    "ERR_NOT_FOUND": 404,
    "ERR_CONFLICT": 409,
    "ERR_RATE_LIMITED": 429,
    "ERR_INTERNAL": 500,
    "ERR_LLM_UNAVAILABLE": 503,
    # ST-36 M3：技能写侧治理错误码
    "ERR_LINT_FAILED": 400,
    "ERR_DUPLICATE_SKILL": 409,
    "ERR_NAME_CONFLICT": 409,
    "ERR_REFERENCED_BY_USES": 409,
}


def api_error(code: str, message: str, details: dict | None = None) -> HTTPException:
    """构造统一 HTTPException（带 error 结构）。"""
    status = ERROR_CODES.get(code, 500)
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, **(details and {"details": details} or {})}},
    )


# ---------- 默认开发 Key 与来源限制（ST-22⑧） ----------

DEFAULT_AGENT_KEY = "dev-agent-key-change-me"
DEFAULT_ADMIN_KEY = "dev-admin-key-change-me"

# 本机回环来源集合：真实回环地址 + Starlette TestClient 的固定 client host
# （"testclient" 是测试工具的主机名，非网络来源——放行它保证「默认 key + 本机开发」
# 工作流在测试环境下同样成立）。
_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_localhost_source(request: Request) -> bool:
    """来源是否本机回环：``request.client.host`` 属于回环集合。

    client 信息缺失（None）→ 视为非本机来源（安全侧失败：宁可误拒不可漏放）。
    """
    client = getattr(request, "client", None)
    if client is None or not getattr(client, "host", None):
        return False
    return client.host.lower() in _LOCALHOST_HOSTS


# ---------- operations 层 → HTTP 协议翻译（v0.7 §7） ----------

def run_operation(op: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """调 operations 层操作并翻译为 HTTP 语义（所有路由共用，避免每个端点重写一遍）。

    - 成功 → 返回 ``result.data``（路由可直接 return，或先做投影裁剪）
    - ``ok=False`` / ``InvalidArgs`` / ``OperationError`` → 抛 ``api_error``，
      错误码经 ERROR_CODES 映射为状态码（400 / 404 / 500 / 503）
    - **其余异常不拦截**：保持 v0.6 行为，交给 app 的全局异常处理器兜底，
      避免抽取 operations 反而改变了非预期错误的响应形态

    Args:
        op: operations 层操作函数，返回 OperationResult。
        *args / **kwargs: 透传给操作函数的依赖与业务参数。

    Returns:
        操作成功时的 data 字典（None 时归一为 {}）。

    Raises:
        HTTPException: 操作失败时（由 api_error 构造）。
    """
    from sgme.operations.errors import InvalidArgs, OperationError

    try:
        result = op(*args, **kwargs)
    except InvalidArgs as e:
        raise api_error("ERR_INVALID_ARGS", e.message, e.details) from e
    except OperationError as e:
        raise api_error(e.error_code, e.message, e.details) from e

    if not result.ok:
        raise api_error(
            result.error_code or "ERR_INTERNAL",
            result.message or "操作失败",
            result.details,
        )
    return result.data or {}


# ---------- Agent Key 注册表 ----------

class AgentKeyStore:
    """Agent API Key 存储（内存 + 可选文件持久化）。

    - admin_key / agent_key 来自 env 或默认 dev 值（启动时告警）
    - register_agent: 签发新 Agent Key（管理员调用）
    - is_admin / is_agent: 角色判定
    """

    def __init__(self, admin_key: str | None = None, agent_key: str | None = None, store_path: Path | None = None):
        self.admin_key = admin_key or os.environ.get("SGME_ADMIN_KEY") or DEFAULT_ADMIN_KEY
        self.agent_key = agent_key or os.environ.get("SGME_AGENT_KEY") or DEFAULT_AGENT_KEY
        # 额外注册的 Agent Key（register 端点签发）
        self._extra_agents: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._store_path = store_path
        if store_path and store_path.exists():
            self._load_from_file()

    def is_default_dev_key(self, key: str | None) -> bool:
        """是否为默认开发 Key（未设置环境变量时的内置兜底值，ST-22⑧）。

        判定只看 Key 取值本身：与默认兜底值相等的 Key 一律视为开发 Key
        （即使运维恰好把环境变量设成了同值——那在语义上就是默认 Key）。
        """
        return key in (DEFAULT_AGENT_KEY, DEFAULT_ADMIN_KEY)

    def agent_exists(self, agent_id: str) -> bool:
        """agent_id 是否已注册（含 env 合成条目 default 与已签发 Key）。

        供注册端点做去重：同一 agent_id 拒绝重复签发，避免 Key 无谓累加。
        """
        if agent_id == "default":
            return True
        with self._lock:
            return any(
                rec.get("agent_id") == agent_id
                for rec in self._extra_agents.values()
            )

    def register_agent(self, agent_id: str, scope: list[str] | None = None,
                       agent_model: str | None = None) -> str:
        """签发新 Agent Key，返回明文 key（仅此一次返回）。

        agent_model（T-43）：agent 声明的提炼模型，格式 ``provider/model``
        （如 ``deepseek/deepseek-v4-flash``）——提炼动态链按此跟随 agent
        当前 LLM；用户指定 refine.llm_override 后此值降为备用。
        """
        key = f"agt_{uuid.uuid4().hex}"
        with self._lock:
            self._extra_agents[key] = {
                "agent_id": agent_id,
                "scope": scope or [],
                "role": "agent",
                "agent_model": agent_model or "",
            }
            self._save_to_file()
        return key

    def is_admin(self, key: str | None) -> bool:
        return key is not None and key == self.admin_key

    def is_agent(self, key: str | None) -> bool:
        if key is None:
            return False
        if key == self.agent_key or key == self.admin_key:
            return True
        with self._lock:
            return key in self._extra_agents

    def resolve_agent_id(self, key: str | None) -> str | None:
        """按鉴权 Key 反查绑定的 agent_id（溯源兜底，2026-08-11 B35）。

        语义：
        - env 主 agent key → "default"（与 list_agents() 合成条目一致）
        - admin key → "default"（管理员身份，无独立 agent_id）
        - 注册 agt_* key → register_agent 绑定的 agent_id
        - 未知 key / None → None（调用方保持原样）

        用途：append 请求 body 未带 agent_id 时，用鉴权 key 映射兜底，
        关掉「落 NULL/default 无法溯源」的口子（HTTP 通道）。
        """
        if key is None:
            return None
        if key == self.agent_key or key == self.admin_key:
            return "default"
        with self._lock:
            rec = self._extra_agents.get(key)
            return rec.get("agent_id") if rec else None

    def list_agents(self) -> list[dict]:
        """内部用：列出全部 Key 记录。

        🔴 返回值**含明文 Key**，仅限进程内使用（如 /v1/admin/stats 只取 agent_id/role）。
        任何对外暴露的端点一律改用 list_agents_public()。
        """
        with self._lock:
            base = [{"key": self.agent_key, "agent_id": "default", "role": "agent", "scope": []}]
            for k, v in self._extra_agents.items():
                base.append({"key": k, **v})
            return base

    @staticmethod
    def _mask_key(key: str) -> str:
        """Key 脱敏指纹：前 6 字符 + '…' + 后 2 字符。

        🔴 绝不返回明文 Key，也不返回哈希全文。
        过短的 Key（≤ 8 字符）无法安全截断，整体隐藏为 '…'。
        """
        if not key:
            return ""
        if len(key) <= 8:
            return "…"
        return f"{key[:6]}…{key[-2:]}"

    def list_agents_public(self, *, include_default: bool = False) -> list[dict]:
        """只读列出已注册 Agent（按 agent_id 聚合 + 脱敏）。

        与 list_agents() 的**唯一正确对外形态**：绝不返回明文 API Key，
        只给脱敏指纹 key_ref。纯只读，不改任何内部状态。

        约定（与 SGME-接口契约 §5.2 严格一致）：
        - 同一 agent_id 可持有多把 Key → 聚合为一条，key_count 反映把数，
          scope 取并集（保序去重），registered_at 取最早值。
        - endpoint 恒为 None：Agent 是 SGME 的客户端，SGME 从不反向呼叫 Agent，
          因此 SGME 天然不持有 endpoint。字段位保留是为将来（SGME 记录 endpoint）
          落地时无需升契约版本。
        - status 恒为 "active"：revoke_agent 是硬删除，SGME 侧无 tombstone。
        - 默认过滤合成条目 agent_id="default"（来自 env SGME_AGENT_KEY，
          非真实注册项，且其 Key 就是共享 agent key）。

        Args:
            include_default: True 时保留 "default" 合成条目（仅供本地排障）。

        Returns:
            list[dict]，按 agent_id 升序。每条含
            agent_id / role / scope / endpoint / status / registered_at /
            key_count / key_ref。
            **不含** last_seen_at —— 活跃度由路由层从 raw_files 聚合后注入。
        """
        with self._lock:
            records: list[tuple[str, dict]] = []
            if include_default:
                records.append(
                    (self.agent_key, {"agent_id": "default", "role": "agent", "scope": []})
                )
            records.extend((k, v) for k, v in self._extra_agents.items())

            agg: dict[str, dict] = {}
            for key, meta in records:
                meta = meta or {}
                agent_id = meta.get("agent_id") or ""
                if not agent_id:
                    continue  # 脏数据：无 agent_id 的记录直接跳过
                if agent_id == "default" and not include_default:
                    continue  # 过滤合成条目
                entry = agg.get(agent_id)
                if entry is None:
                    agg[agent_id] = {
                        "agent_id": agent_id,
                        "role": meta.get("role") or "agent",
                        "scope": list(meta.get("scope") or []),
                        "agent_model": meta.get("agent_model") or "",
                        "endpoint": None,
                        "status": "active",
                        "registered_at": meta.get("registered_at"),
                        "key_count": 1,
                        "key_ref": self._mask_key(key),
                    }
                    continue
                # 同一 agent_id 的第 2..N 把 Key
                entry["key_count"] += 1
                for s in meta.get("scope") or []:
                    if s not in entry["scope"]:
                        entry["scope"].append(s)
                if not entry.get("agent_model") and meta.get("agent_model"):
                    entry["agent_model"] = meta["agent_model"]
                reg = meta.get("registered_at")
                if reg and (entry["registered_at"] is None or reg < entry["registered_at"]):
                    entry["registered_at"] = reg

            return [agg[aid] for aid in sorted(agg)]

    def revoke_agent(self, agent_id: str) -> int:
        """吊销指定 agent_id 的全部 Key（§6：注册中心支持随时删除/禁用）。

        返回吊销的 Key 数量。default（env 主 key）不可吊销。
        """
        revoked = 0
        with self._lock:
            keys = [k for k, v in self._extra_agents.items() if v.get("agent_id") == agent_id]
            for k in keys:
                del self._extra_agents[k]
                revoked += 1
            if revoked:
                self._save_to_file()
        return revoked

    def resolve_agent_model(self, agent_id: str | None) -> str | None:
        """按 agent_id 查声明的提炼模型（provider/model；T-43）。

        - default（env 主 key）与未注册 agent → None（提炼回退静态链）
        - 注册 agent → 注册时声明的 agent_model（首把 Key 的非空值）
        """
        if not agent_id or agent_id == "default":
            return None
        with self._lock:
            for v in self._extra_agents.values():
                if v.get("agent_id") == agent_id and v.get("agent_model"):
                    return v["agent_model"]
        return None

    def _save_to_file(self) -> None:
        if not self._store_path:
            return
        import json
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.write_text(json.dumps(self._extra_agents, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict_file_permissions(self._store_path)

    def _load_from_file(self) -> None:
        if not self._store_path or not self._store_path.exists():
            return
        import json
        try:
            self._extra_agents = json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception:
            self._extra_agents = {}


def _restrict_file_permissions(path: Path) -> None:
    """收紧 agent_keys.json 文件权限：仅当前用户可读写（安全加固 2026-08-11）。

    - Windows：icacls 去除继承 + 仅授权当前用户（NTFS 不支持 POSIX mode bits）
    - POSIX：chmod 0600
    失败仅告警不阻断（密钥文件本身已 gitignore，权限是纵深防御）。
    """
    import logging
    import os
    import subprocess

    logger = logging.getLogger("sgme.server.app")
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            if user:
                proc = subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                    capture_output=True, text=True, errors="replace", timeout=15,
                )
                if proc.returncode != 0:
                    logger.warning("agent_keys.json 权限收紧失败: %s", proc.stderr.strip() or proc.stdout.strip())
        else:
            path.chmod(0o600)
    except Exception as e:
        logger.warning("agent_keys.json 权限收紧异常（忽略）: %s", e)


# ---------- 鉴权依赖 ----------

def _get_app_state(request: Request) -> tuple[Request, Any]:
    return request, request.app.state


def require_bearer(request: Request) -> None:
    """Bearer 令牌校验（若开启）。"""
    bearer_token: str | None = getattr(request.app.state, "bearer_token", None)
    if not bearer_token:
        return  # 旁路关闭
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise api_error(
            "ERR_UNAUTHORIZED",
            "缺失 Bearer 令牌：请设置 SGME_BEARER_TOKEN 环境变量，并在请求头携带 Authorization: Bearer <token>",
        )
    token = auth[len("Bearer "):].strip()
    if token != bearer_token:
        raise api_error(
            "ERR_UNAUTHORIZED",
            "Bearer 令牌无效：请核对请求头与 SGME_BEARER_TOKEN 环境变量是否一致",
        )


def _reject_default_key_from_remote(request: Request, key: str) -> None:
    """默认开发 Key 仅限本机回环来源（ST-22⑧）：非本机来源直接 403 并引导换 Key。

    - 默认 key（SGME_AGENT_KEY / SGME_ADMIN_KEY 未设置时的内置兜底值）+ 非本机 → 403
    - 默认 key + 本机回环（127.0.0.1 / ::1 / localhost）→ 放行（本机开发工作流不受影响）
    - 自定义 key（含 register_agent 签发的 agt_*）→ 不受限
    """
    store: AgentKeyStore = request.app.state.key_store
    if not store.is_default_dev_key(key):
        return
    if _is_localhost_source(request):
        return
    raise api_error(
        "ERR_FORBIDDEN",
        "检测到默认开发 Key 来自非本机来源：远程调用禁止使用默认 Key。"
        "请设置环境变量 SGME_AGENT_KEY / SGME_ADMIN_KEY 为自定义 Key 后重试"
        "（本机 127.0.0.1 调用不受影响）",
    )


def require_agent_key(request: Request) -> str:
    """Agent Key 校验（非 Admin 端点）。"""
    require_bearer(request)
    store: AgentKeyStore = request.app.state.key_store
    key = request.headers.get("X-API-Key")
    if not store.is_agent(key):
        raise api_error(
            "ERR_FORBIDDEN",
            "缺失或无效的 X-API-Key：请携带 Agent Key"
            "（环境变量 SGME_AGENT_KEY 或经 /v1/admin/agents 注册）",
        )
    _reject_default_key_from_remote(request, key or "")
    return key or ""


def require_admin_key(request: Request) -> str:
    """Admin Key 校验（/v1/admin/* 端点）。"""
    require_bearer(request)
    store: AgentKeyStore = request.app.state.key_store
    key = request.headers.get("X-API-Key")
    if not store.is_admin(key):
        raise api_error(
            "ERR_FORBIDDEN",
            "缺失或无权限的 X-API-Key：请携带管理员 Key（环境变量 SGME_ADMIN_KEY）",
        )
    _reject_default_key_from_remote(request, key or "")
    return key or ""


# ---------- 后台定时任务（T12 Tier0 每日摘要 / T15 心跳） ----------

async def daily_tier0_task(app) -> None:
    """每日 00:00 UTC 触发 Tier0 摘要生成（独立 LLM 任务，失败不阻塞）。"""
    from sgme.profile import tier0 as tier0_mod

    while True:
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        wait_secs = (tomorrow - now).total_seconds()
        await asyncio.sleep(wait_secs)
        try:
            summary = tier0_mod.generate_summary(app.state.mem_conn, app.state.cfg)
            if summary:
                tier0_mod.save_summary(summary)
                logger.info("Tier0 摘要自动生成成功")
            else:
                logger.warning("Tier0 摘要生成失败（LLM 不可用）")
        except Exception as e:
            logger.exception("Tier0 定时任务异常: %s", e)


async def heartbeat_task(app) -> None:
    """每 10 分钟一次心跳检查，异常产 anomaly_warn。"""
    from sgme.engine import health as health_mod

    while True:
        await asyncio.sleep(600)  # 10 分钟
        try:
            result = health_mod.check_heartbeat(
                app.state.mem_conn, app.state.session_conn, app.state.cfg,
            )
            logger.info(
                "心跳检查: ok=%s stalled=%s",
                result.get("heartbeat_ok"), result.get("stalled"),
            )
        except Exception as e:
            logger.exception("心跳任务异常: %s", e)


async def update_check_task(app) -> None:
    """ST-34：定时刷新版本检测缓存（间隔 = config update_check.interval_hours）。

    首次检查由 health 首次调用时同步完成（get_cached）；本任务只负责按
    interval_hours 周期 refresh 缓存，失败静默降级（update_check 内部已吞异常）。
    """
    from sgme.operations import update_check as update_check_mod
    from sgme.operations.health import SGME_VERSION

    while True:
        uc = (app.state.cfg.get("update_check") or {})
        interval_hours = uc.get("interval_hours", 24)
        await asyncio.sleep(interval_hours * 3600)
        try:
            result = update_check_mod.refresh(SGME_VERSION, app.state.cfg)
            logger.info(
                "版本检测刷新: update_available=%s latest=%s",
                result.get("update_available"), result.get("latest_version"),
            )
        except Exception as e:
            logger.exception("版本检测任务异常: %s", e)


def _start_batch_scan_scheduler(app) -> None:
    """启动 Batch 兜底扫描定时器（ST-23② 保底型；refine.batch_scan.enabled=true 时）。

    线程模式（engine/batch_scan.py，与 Dream/backup_scheduler 同构）而非 asyncio
    任务：提炼链路为同步阻塞调用（LLM/embedding），放事件循环会卡死整个服务；
    daemon 线程 + 幂等 ensure_scheduler，间隔/开关经 /v1/admin/config 可配。
    enabled=false 不启动（运行中改 false 则到点跳过执行）。
    """
    from sgme.engine import batch_scan as batch_scan_mod

    refine_cfg = app.state.cfg.get("refine", {}) or {}
    bs = refine_cfg.get("batch_scan", {}) or {}
    if not bs.get("enabled", True):
        logger.info("Batch 兜底扫描未启用（refine.batch_scan.enabled=false）")
        return
    batch_scan_mod.ensure_scheduler(
        app.state.cfg,
        data_dir=getattr(app.state, "data_dir", None),
    )


# ---------- 应用工厂 ----------

def create_app(
    cfg: dict | None = None,
    mem_conn: sqlite3.Connection | None = None,
    session_conn: sqlite3.Connection | None = None,
    wiki_conn: sqlite3.Connection | None = None,
    data_dir: str | Path | None = None,
    admin_key: str | None = None,
    agent_key: str | None = None,
    bearer_token: str | None = None,
    agent_store_path: str | Path | None = None,
    start_background_tasks: bool = False,
) -> FastAPI:
    """创建 FastAPI 应用实例。

    测试可注入 cfg/mem_conn/session_conn/wiki_conn 实现隔离（v0.7 三库）；
    生产由 __main__ 调用并自动初始化。
    start_background_tasks=True 时启动 Tier0 每日摘要 + 心跳定时任务
    （生产模式），并按 refine.batch_scan.enabled 拉起 Batch 兜底提炼定时器
    （daemon 线程，engine/batch_scan.py）。
    """
    if cfg is None:
        cfg = sgme_config.load_config()

    # 初始化统一日志模块（v0.7 §12）：所有 sgme.* logger 走统一 handler
    from sgme.log import setup as log_setup
    log_cfg = cfg.get("logging", {})
    log_setup(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "console"),
        output=log_cfg.get("output") or str(sgme_config.LOG_DIR / "sgme.log"),
    )

    # 连接：外部注入优先，否则按 data_dir 自动建
    own_conns = False
    if mem_conn is None or session_conn is None or wiki_conn is None:
        own_conns = True
        d = Path(data_dir) if data_dir else sgme_config.DATA_DIR
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(d)
    else:
        # 外部注入连接：调度器线程自建独立连接的目标目录（注入方负责正确性；
        # 缺省回落全局 DATA_DIR——生产 __main__ 走自建分支，测试须显式传 data_dir）
        d = Path(data_dir) if data_dir else sgme_config.DATA_DIR

    # 启动时导入注册表（幂等；YAML 为种子，DB 为运行时真相）
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # 从 DB 回刷 cfg["dimensions"]：包含 API 运行时新增/停用的维度，保证 L1 提示词与 DB 一致
    cfg["dimensions"] = memory_dao.list_dimensions(mem_conn, active_only=True)

    # 鉴权配置
    bearer = bearer_token if bearer_token is not None else os.environ.get("SGME_BEARER_TOKEN")
    store = AgentKeyStore(
        admin_key=admin_key,
        agent_key=agent_key,
        # 缺省落盘 data/agent_keys.json——注册的 Agent key 服务重启后必须恢复
        # （此前 store_path 从未接线 → 每次重启注册记录丢失，Agent 收到 403）
        store_path=Path(agent_store_path) if agent_store_path else sgme_config.DATA_DIR / "agent_keys.json",
    )
    if bearer:
        os.environ.setdefault("SGME_BEARER_TOKEN", bearer)

    # dev 默认 key 告警
    if store.admin_key == DEFAULT_ADMIN_KEY:
        print(f"[SGME auth] 警告：使用默认 admin key（{DEFAULT_ADMIN_KEY}），生产请设置 SGME_ADMIN_KEY")
    if store.agent_key == DEFAULT_AGENT_KEY:
        print(f"[SGME auth] 警告：使用默认 agent key（{DEFAULT_AGENT_KEY}），生产请设置 SGME_AGENT_KEY")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期：启动定时任务（生产模式）/ 关闭时释放自建连接。"""
        # T-23② 安装清单（ST-23⑦ 服务发现落地）：生产模式启动即生成 ~/.sgme/install.json
        if start_background_tasks:
            try:
                host = os.environ.get("SGME_HOST", "127.0.0.1")
                port = int(os.environ.get("SGME_PORT", "9910"))
                sgme_config.write_install_json(cfg, host=host, port=port)
            except Exception as e:  # 不阻断启动：清单缺失不影响核心服务
                print(f"[SGME install] 安装清单生成失败（不影响启动）: {e}")
            asyncio.create_task(daily_tier0_task(app))
            asyncio.create_task(heartbeat_task(app))
            asyncio.create_task(update_check_task(app))
            _start_batch_scan_scheduler(app)
            # ST-35 T-101：人格月度校准定时器（失败不阻断启动）
            try:
                from sgme.engine import persona_monthly
                persona_monthly.ensure_scheduler(cfg, data_dir=d)
            except Exception as e:
                print(f"[SGME persona] 月度校准定时器启动失败（不影响启动）: {e}")
            # B117（2026-08-28 治本）：Dream 夜间整理定时器。此前仅手动触发端点
            # （/v1/admin/dream/trigger）接线，容器重启后 daemon 线程死亡且永不自动
            # 复活，导致 dream 流水线自 08-15 停摆、scene_gc 从未执行、重复场景堆积
            # （active 350 > max 300）。现与生产其余 scheduler 同款接入启动，按
            # dream.schedule(03:00) 自动执行；失败不阻断启动。
            try:
                from sgme.engine import dream
                dream.ensure_scheduler(cfg, data_dir=d)
            except Exception as e:
                print(f"[SGME dream] 夜间整理定时器启动失败（不影响启动）: {e}")
        yield
        # Batch 兜底扫描定时器线程（daemon）：置位 stop 并 join（幂等，未启动无副作用）
        try:
            from sgme.engine import batch_scan as batch_scan_mod
            batch_scan_mod.stop_scheduler(timeout=2.0)
        except Exception:
            pass
        try:
            from sgme.engine import persona_monthly
            persona_monthly.stop_scheduler(timeout=2.0)
        except Exception:
            pass
        if own_conns:
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
            db_mod.close(wiki_conn)

    app = FastAPI(title="SGME", version="1.1.0", docs_url="/docs", lifespan=lifespan)

    # CORS：允许局域网来源（TackMark 等 HTML 工具需 fetch 页面内容做标注；
    # 鉴权仍由 X-API-Key 承担，开放 CORS 不降低安全性）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 挂载应用状态
    app.state.cfg = cfg
    app.state.mem_conn = mem_conn
    app.state.session_conn = session_conn
    app.state.wiki_conn = wiki_conn
    app.state.data_dir = d  # 调度器线程自建独立连接的数据库目录（2026-08-11 连接隔离修复）
    app.state.own_conns = own_conns
    app.state.key_store = store
    app.state.bearer_token = bearer
    app.state.started_at = _now_iso()

    # 初始化 FTS5（search 模块）：记忆层 + 场景层（v5）
    from sgme.data.search import init_fts, init_scenes_fts
    init_fts(mem_conn)
    init_scenes_fts(mem_conn)

    # 注册路由
    from sgme.server.routes_memory import router as memory_router
    from sgme.server.routes_admin import router as admin_router
    from sgme.server.routes_events import router as events_router
    from sgme.server.routes_backup import router as backup_router
    from sgme.server.routes_config import router as config_router
    from sgme.server.routes_registry import router as registry_router
    from sgme.server.routes_prompts import router as prompts_router
    from sgme.server.routes_ideas import router as ideas_router
    from sgme.server.routes_llm import router as llm_router
    # ST-36 M3：技能写侧管理端点（读侧 routes_skills.py 由并行代理挂载，合并时解冲突）。
    # 必须先于 routes_admin 注册：同路径 PUT/DELETE 由治理版（门禁+查重）优先接管，
    # 未命中路径（GET 列表/详情等）自然回落到后续路由器（FastAPI 按序匹配）。
    from sgme.server.routes_skills_admin import router as skills_admin_router
    app.include_router(skills_admin_router)
    app.include_router(memory_router)
    app.include_router(admin_router)
    app.include_router(events_router)
    app.include_router(backup_router)
    app.include_router(config_router)
    app.include_router(registry_router)
    app.include_router(prompts_router)
    app.include_router(ideas_router)
    app.include_router(llm_router)
    # wiki 扩展模块（v0.7 §10；wiki.enabled=false 时不挂载）
    if cfg.get("wiki", {}).get("enabled", True):
        from sgme.wiki.routes import router as wiki_router
        app.include_router(wiki_router)
    # care 扩展模块（ST-25 角色层；care.enabled=false 时不挂载）
    if cfg.get("care", {}).get("enabled", True):
        from sgme.server.routes_care import router as care_router
        app.include_router(care_router)
    # persona 扩展模块（ST-35 人格洞察；persona.enabled=false 时不挂载）
    if cfg.get("persona", {}).get("enabled", True):
        from sgme.server.routes_persona import router as persona_router
        app.include_router(persona_router)
    # skills 读侧披露模块（ST-36 M2 四级披露；skills.enabled=false 时不挂载）
    if cfg.get("skills", {}).get("enabled", True):
        from sgme.server.routes_skills import router as skills_router
        app.include_router(skills_router)

    # ---- WebUI 自动填充密钥端点（2026-08-13 用户需求）----
    # 仅限**本机回环来源**可用（不要求 key 鉴权——前端首次打开无 key 无法鉴权，
    # 需先拿到 key 才能自动填入，故以来源校验代替鉴权）。
    # key 本就存于本机 config/.env，本机回环可信；远程来源一律 403（防泄漏）。
    @app.get("/v1/admin/keys")
    def webui_keys_probe(request: Request):
        if not _is_localhost_source(request):
            raise api_error(
                "ERR_FORBIDDEN",
                "密钥自动填充仅限本机回环来源（127.0.0.1/localhost）：远程访问请手动配置 key",
            )
        store: AgentKeyStore = request.app.state.key_store
        return {
            "admin_key": store.admin_key,
            "agent_key": store.agent_key,
        }

    # 限流中间件（T-7 §6）：按 X-API-Key 滑动窗口限流（默认 120 req/min/Key；0=关闭）
    # 必须注册才能使限流生效；读取 request.app.state.cfg["server"]["rate_limit_per_min"]
    from sgme.server.ratelimit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

    # MCP Server（同进程两协议出口：HTTP API + MCP streamable HTTP）
    # 默认生产挂载；测试可通过 create_app(enable_mcp=False) 或 SGME_MCP_DISABLED=1 关闭
    if not os.environ.get("SGME_MCP_DISABLED") == "1":
        try:
            from sgme.mcp_server import mount_mcp
            mount_mcp(app)
        except Exception as e:
            logger.warning("MCP Server 启动失败（不影响 HTTP API）: %s", e)

    # 统一异常处理
    @app.exception_handler(HTTPException)
    async def http_exc_handler(request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        # 兜底：未结构化的 HTTPException（如 405 Method Not Allowed）按状态码映射统一错误码，
        # 不再一律归 ERR_INTERNAL（ST-22④：错误码语义必须可行动）
        fallback_codes = {
            401: "ERR_UNAUTHORIZED",
            403: "ERR_FORBIDDEN",
            404: "ERR_NOT_FOUND",
            405: "ERR_INVALID_ARGS",
            429: "ERR_RATE_LIMITED",
        }
        code = fallback_codes.get(exc.status_code, "ERR_INTERNAL")
        return error_response(code, f"请求处理失败: {exc.detail}", exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """422 统一错误结构（ST-22④）：FastAPI 默认 ``{"detail":[...]}`` → ERR_INVALID_ARGS + 中文说明。

        新增统一处理前，请求体校验失败返回 FastAPI 默认结构（无 ERR_* 前缀、无中文说明），
        与全局 ``{"error":{code,message,details}}`` 结构不一致——本处理器补齐差异。
        """
        errors = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            errors.append({"loc": loc, "msg": e.get("msg", ""), "type": e.get("type", "")})
        return error_response(
            "ERR_INVALID_ARGS",
            "请求参数校验失败，请检查请求体字段（详见 details）",
            422,
            details={"errors": errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logger.exception("未处理异常: %s", exc)
        return error_response(
            "ERR_INTERNAL",
            f"内部错误: {exc}（可查看服务日志 sgme.log 获取堆栈）",
            500,
        )

    # WebUI 静态托管（SGME-WebUI设计-v0.1 §1）：ui/dist 存在即挂载
    # SPA history 路由：非 /v1 的 GET 回 index.html；/v1/* 保持 API 契约
    # Path 已在模块顶部导入；FileResponse/StaticFiles 在此局部导入避免污染顶部
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    _ui_dist = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"
    if _ui_dist.is_dir():
        _assets = _ui_dist / "assets"
        if _assets.is_dir():
            app.mount("/assets", StaticFiles(directory=_assets), name="webui_assets")
        _index = _ui_dist / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _webui_spa(full_path: str):
            # 非前端路径（API/文档/健康探测）不放行到 index.html
            if full_path.startswith("v1/") or full_path in ("docs", "redoc", "openapi.json"):
                raise HTTPException(status_code=404)
            # index.html 必须 no-cache：SPA 的 HTML 若被浏览器启发式缓存，会引用旧
            # 版 JS，导致新页面（roles/signals/dashboard 等）加载旧组件而空白
            # （2026-08-13 三个页面空白根因修复）。assets 走 hash 文件名，可长缓存。
            return FileResponse(_index, headers={"Cache-Control": "no-cache"})

    return app


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
