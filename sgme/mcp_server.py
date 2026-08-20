"""sgme/mcp_server.py：SGME MCP Server（Model Context Protocol）。

与 HTTP API 同一套业务逻辑（复用 engine/storage 业务层，模块化重构 B30 后
不再依赖 server/routes_*），提供 MCP 协议出口（streamable HTTP transport），
SCSM / 其他 Agent 可经 MCP 调用。

统一服务原则（2026-08-04 用户决策）：
- HTTP API 与 MCP 两套接口都提供服务，功能等价
- MCP 工具：append / inject / search / memory_get / memory_reject / refine_trigger /
  refine_batch / refine_status / stats / health / config_get / config_update / agent_onboarding
- 鉴权：MCP 工具内自行校验 X-API-Key（读环境变量 SGME_AGENT_KEY / SGME_ADMIN_KEY）

Trae 通知兼容（ST-23⑤，2026-08-11）：
- 官方 SDK 把 ClientNotification 定义为 method 字面量严格枚举的判别联合，
  Trae 等客户端发送的非标准通知（如 notifications/trae/session_stop）校验失败，
  每来一条刷一条 WARNING 日志、事件语义丢失。
- 本模块在 build_mcp_server 时对 mcp.types.ClientNotification 打一次宽容补丁
  （联合末尾追加 method 任意字符串的兜底成员）：已知通知仍走原类型，
  未知通知静默接受——协议层宽容，符合 MCP「通知可扩展」的精神。

模块化重构 B30（2026-08-07）：MCP 曾反向 import server/routes_* 复用处理函数
（append_session/_persist_memories/CONFIG_SECTIONS），形成 server ↔ mcp_server 包级环。
现全部归位业务层：append → engine.pipeline.append_l0；refine → engine.pipeline；
stats → storage.stats_dao；config → sgme.config。依赖方向：mcp_server → 业务层，
与 HTTP 路由平级（入口不依赖入口）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict

from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import Context

logger = logging.getLogger("sgme.mcp")

# 允许外部注入 app 状态（create_app 时设置）
_app_state: Dict[str, Any] = {}

# Trae 通知宽容补丁的幂等开关（ST-23⑤，进程级只打一次）
_NOTIFICATION_PATCHED: bool = False


def bind_app_state(state: Dict[str, Any]) -> None:
    """绑定 FastAPI app.state（cfg/mem_conn/session_conn/wiki_conn），供 MCP 工具复用。"""
    _app_state.clear()
    _app_state.update(state)


def _require_admin() -> str:
    """校验管理员 Key（环境变量 SGME_ADMIN_KEY，与 HTTP 鉴权对齐）。"""
    return os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """MCP streamable-http 传输层鉴权中间件（PR#1，2026-08-11）。

    与 HTTP 通道 require_agent_key 同规则、同设施（AgentKeyStore.is_agent）：
    - X-API-Key 缺失或无效 → 403 ERR_FORBIDDEN
    - env agent key / admin key / 注册 agt_* key → 放行
    - 校验通过后把 key 存入 request.state.api_key（工具内反查溯源用，PR#2）

    动机：FastMCP 1.28 的 AuthSettings 是 OAuth 模型（需外部授权服务器），
    不适用 API key 场景；mcp.run() 自托管会忽略 streamable_http_app 上
    附加的中间件（LibreChat 踩坑实录），故 mount_mcp 改为手动 uvicorn
    跑 streamable_http_app() + 本中间件。
    """
    def __init__(self, app, key_store):
        super().__init__(app)
        self._key_store = key_store

    async def dispatch(self, request, call_next):
        key = request.headers.get("X-API-Key")
        if not self._key_store.is_agent(key):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={"error": {
                    "code": "ERR_FORBIDDEN",
                    "message": "缺失或无效的 X-API-Key：请携带 Agent Key"
                               "（环境变量 SGME_AGENT_KEY 或经 /v1/admin/agents 注册）",
                }},
            )
        # 供工具内 resolve_agent_id 反查（PR#2）
        request.state.api_key = key
        return await call_next(request)


def run_mcp_server(mcp, key_store, host: str = "127.0.0.1", port: int = 9913) -> None:
    """手动启动 MCP streamable-http server（带鉴权中间件，PR#1）。

    替代 mcp.run(transport="streamable-http")——自托管会忽略中间件，
    无法挂 ApiKeyMiddleware。此处显式构建 Starlette app → 加中间件 →
    uvicorn 独立线程跑（与 mount_mcp 原行为一致：daemon 线程、同进程）。
    """
    import threading
    import uvicorn

    starlette_app = mcp.streamable_http_app()
    starlette_app.add_middleware(ApiKeyMiddleware, key_store=key_store)

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    logger.info("MCP Server 已启动（鉴权启用）: http://%s:%s/mcp（streamable HTTP，独立线程）", host, port)


def _patch_lenient_notifications() -> None:
    """宽容处理未知通知类型（ST-23⑤：Trae notifications/trae/session_stop 等）。

    官方 SDK 的 ``ClientNotification`` 是 method 字面量严格枚举的判别联合
    （``mcp.types.ClientNotificationType``），未知通知（如 Trae 会话结束事件）
    校验失败 → 共享会话层每来一条刷一条 ``Failed to validate notification``
    WARNING，且事件语义丢失。

    方案：把联合末尾追加一个宽松兜底成员（``method: str`` 任意字符串），
    已知通知仍命中原字面量类型（行为不变），未知通知静默接受——
    MCP 协议本身允许客户端发送任意通知（服务端自行决定处理与否）。

    注意：
    - **进程级全局补丁**（替换 ``mcp.types.ClientNotification``），幂等只打一次；
      SGME 进程内只有本 MCP Server，无副作用面。
    - 必须早于 ``mcp.run()`` 生效——``ServerSession.__init__`` 在每次连接时
      取 ``types.ClientNotification`` 属性，故 build_mcp_server 时打好即可。
    - 只放宽**通知**（无响应消息）；未知**请求**仍按 JSON-RPC 错误响应，
      那是协议正确行为，不在本次兼容范围内。
    """
    global _NOTIFICATION_PATCHED
    if _NOTIFICATION_PATCHED:
        return
    from mcp import types as mcp_types
    from pydantic import RootModel
    from typing import Any

    class _LenientNotification(mcp_types.Notification[Any, str]):
        """兜底通知：method 任意字符串（未知通知的落点）。"""

        method: str
        params: Any = None

    class _LenientClientNotification(
        RootModel[mcp_types.ClientNotificationType | _LenientNotification]
    ):
        pass

    mcp_types.ClientNotification = _LenientClientNotification
    _NOTIFICATION_PATCHED = True
    logger.info("MCP 通知宽容补丁已生效（未知通知类型静默接受）")


# agent_onboarding（ST-23①）的能力清单：与下方 @mcp.tool 一一对应。
# 测试会断言本清单与 list_tools() 实际工具集一致，防止清单与实现漂移。
ONBOARDING_TOOLS: tuple[dict[str, str], ...] = (
    {"name": "agent_onboarding", "description": "连接即发现：SGME 版本/能力清单/快速上手指引（本工具）"},
    {"name": "append", "description": "L0 捕获：写入原始会话（幂等），content 需 # {ISO时间戳} {role} 格式；可选 agent_id 标注来源（溯源）"},
    {"name": "inject", "description": "记忆注入：按模式模板查询记忆池，返回注入块（画像视图）"},
    {"name": "search", "description": "混合检索：BM25 + 向量 + RRF，带溯源（记忆池）"},
    {"name": "wiki_search", "description": "检索 wiki 知识库（wiki_pages 知识文档，FTS5 BM25 + 兜底）"},
    {"name": "wiki_pages", "description": "wiki 页面列表（updated_at 降序；category 可选过滤；不含正文）"},
    {"name": "wiki_page", "description": "wiki 页面详情（标题/正文/分类/来源/更新时间）"},
    {"name": "wiki_page_add", "description": "wiki 页面直接写入（原样入库，不走 LLM 提炼；幂等 upsert，返回 page_id+status）"},
    {"name": "wiki_page_update", "description": "wiki 页面按 id 更新/追加（自进化写回主通道：append 默认追加 ADD-only + entry hash 去重幂等；description 默认不动，W3）"},
    {"name": "wiki_evolve_trigger", "description": "自进化触发（W4）：会话 → 经验 → 写回 wiki 手册（费用门禁 + 规则闸门 + 独立游标 wiki_evolve）"},
    {"name": "memory_get", "description": "单条记忆详情（内容/维度/TTL + 溯源 + 归档链）"},
    {"name": "memory_reject", "description": "标记记忆「不采用」（不删除、可恢复），带纠错理由"},
    {"name": "refine_trigger", "description": "触发提炼：单文件或扫 status=new 批量（async_mode 分流同步/异步）"},
    {"name": "refine_batch", "description": "批量提炼：显式文件列表或扫全部未提炼，异步排队即返"},
    {"name": "refine_status", "description": "提炼进度：待提炼/已完成/失败计数 + 水位 + 最近失败"},
    {"name": "stats", "description": "统计：记忆数/维度分布/原始文件状态/水位"},
    {"name": "health", "description": "健康检查：LLM 可用性/提炼水位/心跳"},
    {"name": "config_get", "description": "读取 SGME 运行时配置（section 可选：l1/l2/refine/search/backup）"},
    {"name": "config_update", "description": "更新 SGME 配置段（热生效 + 落盘 sgme.yaml）"},
    {"name": "idea_add", "description": "人工添加创意（用户主动提出才记录；自动打 ideas 标签 + 长期保存 ttl=NULL）"},
    {"name": "demand_create", "description": "新建待办/需求（跨项目统一待办池；可指定 project_id 标记过滤）"},
    {"name": "project_register", "description": "登记/创建项目（用户主动立项；project_meta upsert，二次登记=更新）"},
    {"name": "signal_pull", "description": "拉取未消费关怀信号（type=care_*；会话开始主动消费，ST-27）"},
    {"name": "signal_claim", "description": "原子认领信号（谁消费谁标记；已被他人消费返回 claimed=false，ST-27）"},
    {"name": "signal_ack", "description": "写消费回执（claimed/acked/failed；认领后报告处理结果，ST-27）"},
    {"name": "role_list", "description": "列出可用角色模板（管家/伴侣/朋友/导师，含人设摘要；换皮不换芯，ST-29）"},
    {"name": "role_assemble", "description": "装配角色沟通提示词（角色卡 system_prompt + care_policy + 画像，可选 inject_mode；ST-29）"},
    {"name": "role_active_get", "description": "读取当前沟通角色（未设置返回 role_id=null；ST-29）"},
    {"name": "role_active_set", "description": "设置当前沟通角色（换皮不换芯，只换角色不换记忆池；ST-29）"},
)


# ---------- operations 层 → MCP 协议翻译（v0.7 §7） ----------

def _op_json(op: Callable[..., Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """调 operations 层操作并翻译为 MCP 语义（所有工具共用，避免每个工具重写一遍）。

    - 成功 → 返回 ``result.data`` 字典（调用方按需投影后 json.dumps）
    - ``ok=False`` / ``InvalidArgs`` / ``OperationError`` → 返回 ``{"error": "..."}``
      （沿用既有 MCP 错误约定：扁平 error 字符串，非 HTTP 的嵌套 error 对象）
    - **其余异常不拦截**：保持既有行为，交由 FastMCP 处理

    ⚠️ 约定：operations 层的 data **不得**使用顶层 ``error`` 键，
    否则会与失败态混淆（调用方按 ``"error" in d`` 判定）。后续 8 个模块同此约定。

    Returns:
        成功为 data 字典；失败为 ``{"error": message}``。调用方用 ``"error" in d`` 判定。
    """
    from sgme.operations.errors import InvalidArgs, OperationError

    try:
        result = op(*args, **kwargs)
    except InvalidArgs as e:
        return {"error": e.message}
    except OperationError as e:
        return {"error": e.message}

    if not result.ok:
        return {"error": result.message or result.error_code or "操作失败"}
    return result.data or {}


def build_mcp_server():
    """构建 FastMCP 实例（工具集与 HTTP API 等价）。

    streamable_http_path 默认 '/mcp'（自托管时客户端访问 http://host:9913/mcp）。
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # ST-23⑤：宽容处理 Trae 等客户端的非标准通知（幂等，进程级只打一次）
    _patch_lenient_notifications()

    mcp = FastMCP(
        "SGME",
        instructions=(
            "SGME 记忆引擎 MCP 接口。记忆写入（append）、注入（inject）、"
            "检索（search，记忆池）、wiki 知识库（wiki_search/wiki_pages/wiki_page）、"
            "记忆查看/纠错（memory_get/memory_reject）、"
            "提炼（refine_trigger/refine_batch/refine_status）、"
            "统计（stats）、健康检查（health）、配置读写（config_get/config_update）、"
            "创意/待办/项目管理（idea_add/demand_create/project_register，2026-08-13："
            "创意由用户主动提出、需求池改为跨项目待办池、项目由用户主动立项）、"
            "连接即发现（agent_onboarding）。"
        ),
        # 2026-08-16 NAS 容器部署：MCP 绑 0.0.0.0 时 FastMCP 默认仅对本机
        # 开 DNS 防重绑（allowed_hosts 只含 localhost）→ 外部访问 421。
        # 非本机部署显式关闭该附加层（SGME 自身 ApiKeyMiddleware 鉴权不降级）。
        transport_security=(
            None
            if os.environ.get("SGME_MCP_HOST", "127.0.0.1") in ("127.0.0.1", "localhost", "::1")
            else TransportSecuritySettings(enable_dns_rebinding_protection=False)
        ),
    )

    # ---------- 记忆核心 ----------

    @mcp.tool()
    def append(session_key: str, started_at: str, content: str, source_type: str = "session", agent_id: str | None = None, ctx: Context | None = None) -> str:
        """L0 捕获：写入原始会话（幂等）。content 需 # {ISO时间戳} {role} 格式。

        B35/PR#2（2026-08-11）：agent_id 解析优先级 = 显式参数 > 鉴权 key 反查
        （ApiKeyMiddleware 存入 request.state.api_key → resolve_agent_id）> None。
        与 HTTP 通道同语义：注册 key 落绑定 agent_id，env 主 key 落 default。
        直调（无 HTTP 上下文）时 ctx 为 None → 仅显式参数生效。
        """
        import json
        import sqlite3

        from sgme.operations.append import append_l0 as append_l0_operation

        # 鉴权 key 反查兜底（PR#2）：ctx.request_context.request.state.api_key
        # 由 ApiKeyMiddleware 写入（HTTP 传输层）；直调时 request 不可用 → 跳过
        if agent_id is None and ctx is not None:
            try:
                req = ctx.request_context.request
                if req is not None:
                    key = getattr(req.state, "api_key", None)
                    key_store = _app_state.get("key_store")
                    if key_store is not None:
                        agent_id = key_store.resolve_agent_id(key)
            except Exception:
                agent_id = None  # 反查失败不阻断写入（溯源退化为 None）

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]
        cfg = _app_state["cfg"]
        data = _op_json(
            append_l0_operation,
            session_key=session_key,
            started_at=started_at,
            content=content,
            source_type=source_type,
            agent_id=agent_id,
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def inject(mode: str = "daily", max_tokens: int = 800) -> str:
        """记忆注入：按模式模板查询记忆池，返回注入块（画像视图）。"""
        from sgme.operations.inject import inject as inject_operation
        import json
        import sqlite3

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        cfg = _app_state["cfg"]
        data = _op_json(inject_operation, mem_conn, cfg, mode=mode)
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def search(query: str, limit: int = 5) -> str:
        """混合检索：BM25 + 向量 + RRF，带溯源。"""
        from sgme.operations.search import mcp_payload as search_mcp_payload
        from sgme.operations.search import search as search_operation
        import json
        import sqlite3

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]
        cfg = _app_state["cfg"]
        data = _op_json(
            search_operation,
            mem_conn,
            session_conn,
            cfg,
            query=query,
            limit=min(limit, 20),
            scopes=["memory"],
        )
        return json.dumps(search_mcp_payload(data), ensure_ascii=False)

    @mcp.tool()
    def memory_get(memory_id: str) -> str:
        """单条记忆详情（内容/维度/TTL + 溯源 + 归档链）。

        v0.7：业务逻辑已下沉 sgme.operations.memory，本工具只做协议翻译。
        输出与 v0.6 逐字段等价——成功态是**裸记忆对象**（非 HTTP 的三键包裹体），
        失败态是固定文案 ``记忆不存在``（不带 id），两者均由投影函数还原。
        """
        import json
        import sqlite3

        # 规范：operations 一律走完整子模块路径导入（详见 operations/__init__.py）
        from sgme.operations.memory import get_mcp_error_payload
        from sgme.operations.memory import get_mcp_payload
        from sgme.operations.memory import get_memory as get_memory_operation

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]

        data = _op_json(get_memory_operation, mem_conn, memory_id)
        if "error" in data:
            return json.dumps(get_mcp_error_payload(data), ensure_ascii=False)
        return json.dumps(get_mcp_payload(data), ensure_ascii=False)

    @mcp.tool()
    def memory_reject(memory_id: str, reason: str | None = None) -> str:
        """标记记忆「不采用」：用户纠错，不删除、可恢复（memory_id + 纠错理由）。

        ST-22⑥：接线 operations.memory.reject_memory（HTTP ``POST /v1/memory/{id}/reject``
        同一实现），输出与 HTTP 语义一致——成功态 ``{memory_id, status: "rejected",
        reject_reason}``；reason 空值回落缺省「用户纠错」；记忆不存在返回
        ``{"error": ...}``（MCP 扁平错误约定）。
        """
        import json
        import sqlite3

        # 规范：operations 一律走完整子模块路径导入（详见 operations/__init__.py）
        from sgme.operations.memory import reject_memory as reject_memory_operation

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]

        data = _op_json(reject_memory_operation, mem_conn, memory_id, reason=reason)
        return json.dumps(data, ensure_ascii=False)

    # ---------- 管理 ----------

    @mcp.tool()
    def refine_trigger(file_id: str | None = None, limit: int = 50, async_mode: bool = True) -> str:
        """触发提炼。async_mode=true 用后台线程立即返回（推荐）；false 同步等待完成。

        编排统一走 engine.pipeline（B30：同步 refine_one/refine_many，
        异步 async_refine_worker 逐文件容错——修复版，比旧批量收集更稳）。
        """
        from sgme.operations.refine import mcp_payload as refine_mcp_payload
        from sgme.operations.refine import refine_trigger as refine_trigger_operation
        from sgme.operations.refine import refine_trigger_async as refine_trigger_async_operation
        import json
        import sqlite3

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]
        cfg = _app_state["cfg"]
        op = refine_trigger_async_operation if async_mode else refine_trigger_operation
        data = _op_json(op, mem_conn, session_conn, cfg, file_id=file_id, limit=limit)
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps(refine_mcp_payload(data), ensure_ascii=False)

    @mcp.tool()
    def refine_batch(file_ids: list[str] | None = None, limit: int = 50, async_mode: bool = True) -> str:
        """批量文件提炼：显式文件列表，或不传 file_ids 扫全部未提炼（status=new）。

        ST-22⑥：接线 operations.refine.refine_batch（新操作，无历史契约，
        data 即响应）。async_mode=true 后台线程逐文件容错执行、立即返回排队任务
        （triggered/status/scope/file_ids/limit/note）；false 同步执行返回
        triggered/requested/processed/total_memories/results。
        """
        from sgme.operations.refine import refine_batch as refine_batch_operation
        import json
        import sqlite3

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]
        cfg = _app_state["cfg"]
        data = _op_json(
            refine_batch_operation,
            mem_conn,
            session_conn,
            cfg,
            file_ids=file_ids,
            limit=limit,
            async_mode=async_mode,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def refine_status() -> str:
        """提炼进度：待提炼/已完成/失败计数 + 提炼水位 + 最近失败。

        ST-22⑥：接线 operations.refine.refine_status。数据来源：
        raw_files 状态计数与 last_refined_at（stats_dao.raw_files_summary）、
        水位年龄（health 共用口径）、最近失败（refine_runs status='error'
        按 started_at DESC 取最新一条）。
        """
        from sgme.operations.refine import refine_status as refine_status_operation
        import json
        import sqlite3

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]
        data = _op_json(refine_status_operation, mem_conn, session_conn)
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def stats() -> str:
        """统计：记忆数/维度分布/原始文件状态/水位。

        v0.7：业务逻辑已下沉 sgme.operations.stats，本工具只做协议翻译。
        输出与 v0.6 逐字段等价——顶层第 2/3 键（dimension_distribution /
        raw_files）与 HTTP 互换、refinement 是单键版、无 agents，
        这些历史差异全部由 mcp_payload 投影还原。
        """
        import json
        import sqlite3

        # 规范：operations 一律走完整子模块路径导入（详见 operations/__init__.py）
        from sgme.operations.stats import mcp_payload
        from sgme.operations.stats import stats as stats_operation

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]

        data = _op_json(stats_operation, mem_conn, session_conn)
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps(mcp_payload(data), ensure_ascii=False)

    @mcp.tool()
    def health() -> str:
        """健康检查：LLM 可用性/提炼水位/心跳。

        v0.7：业务逻辑已下沉 sgme.operations.health，本工具只做协议翻译。
        输出与 v0.6 逐字段等价——refinement 仍是 engine 原始透传版
        （与 HTTP 的重组超集版存在历史差异，v0.8 待统一，故用 mcp_payload 投影）。
        """
        import json
        import sqlite3

        # 规范：operations 一律走完整子模块路径导入（详见 operations/__init__.py）
        from sgme.operations.health import health as health_operation
        from sgme.operations.health import mcp_payload

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        session_conn: sqlite3.Connection = _app_state["session_conn"]
        cfg = _app_state["cfg"]

        data = _op_json(health_operation, mem_conn, session_conn, cfg)
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps(mcp_payload(data), ensure_ascii=False)

    @mcp.tool()
    def wiki_search(query: str, limit: int = 5) -> str:
        """检索 wiki 知识库（wiki_pages 知识页面，FTS5 BM25 + LIKE 兜底）。

        T-22：检索经 ingest 提炼入库的知识文档（对称 HTTP /v1/wiki/search）；
        返回 [{page_id, title, snippet}]。注意：记忆引擎的 L2 场景检索用 search 工具。
        """
        import json
        import sqlite3

        from sgme.operations.wiki import search as wiki_search_operation

        conn: sqlite3.Connection | None = _app_state.get("wiki_conn")
        if conn is None:
            return json.dumps({"error": "wiki 扩展未启用"}, ensure_ascii=False)
        data = _op_json(
            wiki_search_operation, conn,
            query=query, limit=min(limit, 20),
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def wiki_pages(category: str | None = None, limit: int = 20, offset: int = 0) -> str:
        """wiki 页面列表（updated_at 降序；category 可选过滤；不含正文）。

        T-22：浏览入口——先列表定位，再 wiki_page 取正文。
        """
        import json
        import sqlite3

        from sgme.operations.wiki import list_pages as list_pages_operation

        conn: sqlite3.Connection | None = _app_state.get("wiki_conn")
        if conn is None:
            return json.dumps({"error": "wiki 扩展未启用"}, ensure_ascii=False)
        data = _op_json(
            list_pages_operation, conn,
            category=category, limit=limit, offset=offset,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def wiki_page(page_id: str) -> str:
        """wiki 页面详情（标题/正文全文/分类/标签/来源/更新时间）。"""
        import json
        import sqlite3

        from sgme.operations.wiki import get_page as get_page_operation

        conn: sqlite3.Connection | None = _app_state.get("wiki_conn")
        if conn is None:
            return json.dumps({"error": "wiki 扩展未启用"}, ensure_ascii=False)
        data = _op_json(get_page_operation, conn, page_id)
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def wiki_page_add(
        title: str,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
        source_type: str = "text",
        source_url: str | None = None,
        source_file: str | None = None,
        description: str | None = None,
        author: str | None = None,
    ) -> str:
        """wiki 页面直接写入（原样入库，不走 LLM 提炼；幂等 upsert，T-55）。

        page_id 由「标题 slug + 内容哈希」自动生成——同 title+content 重复调用
        命中同一 page_id 更新（status=updated），不重复建页；写入后立即可被
        wiki_search / wiki_page 检索到（FTS 触发器 + 幂等 init 兜底）。
        """
        import json
        import sqlite3

        from sgme.operations.wiki import create_page as create_page_operation

        conn: sqlite3.Connection | None = _app_state.get("wiki_conn")
        if conn is None:
            return json.dumps({"error": "wiki 扩展未启用"}, ensure_ascii=False)
        data = _op_json(
            create_page_operation, conn,
            title=title, content=content, category=category, tags=tags,
            source_type=source_type, source_url=source_url, source_file=source_file,
            description=description, author=author,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def wiki_page_update(
        page_id: str,
        content: str,
        append: bool = True,
        author: str | None = None,
        description: str | None = None,
    ) -> str:
        """按 page_id 更新/追加 wiki 页面（自进化写回主通道，W3）。

        append=true（默认）：追加到正文末尾（ADD-only + entry hash 去重幂等，
        content 同 hash 重复提交返回 noop）；description 默认不动（显式传才更新）。
        """
        import json
        import sqlite3

        from sgme.operations.wiki import update_page as update_page_operation

        conn: sqlite3.Connection | None = _app_state.get("wiki_conn")
        if conn is None:
            return json.dumps({"error": "wiki 扩展未启用"}, ensure_ascii=False)
        data = _op_json(
            update_page_operation, conn, page_id,
            content=content, append=append, author=author, description=description,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def wiki_evolve_trigger(
        session_key: str | None = None,
        min_rounds: int = 5,
        limit: int = 5,
    ) -> str:
        """自进化触发（W4）：会话 → 经验 → 写回 wiki 手册。

        流程：费用门禁（消息块 ≥ min_rounds）→ LLM 提炼 → 规则闸门 →
        写入（append 踩坑记录 / create 新手册页）→ 审计（wiki_evolve）。
        """
        import json
        import sqlite3

        from sgme.operations.evolve import evolve_trigger as evolve_operation

        conn: sqlite3.Connection | None = _app_state.get("wiki_conn")
        session_conn: sqlite3.Connection | None = _app_state.get("session_conn")
        if conn is None or session_conn is None:
            return json.dumps({"error": "wiki/session 扩展未启用"}, ensure_ascii=False)
        # 传 llm 段（含顶层 chains）：降级链 call_with_fallback 读 cfg["chains"]，完整 cfg 的 chains 在 cfg["llm"] 下
        data = _op_json(
            evolve_operation, conn, session_conn, _app_state.get("cfg", {}).get("llm"),
            session_key=session_key, limit=limit, min_rounds=min_rounds,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def config_get(section: str | None = None) -> str:
        """读取 SGME 运行时配置（section 可选：l1/l2/refine/search/backup）。

        v0.7：业务逻辑已下沉 sgme.operations.config，本工具只做协议翻译。
        输出与 v0.6 逐字段等价——沿用**宽松版** get_config（未知段不报错，
        直接回 config: null），且全量读**不带** writable_sections（HTTP 才有）。
        """
        import json

        # 规范：operations 一律走完整子模块路径导入（详见 operations/__init__.py）
        # ⚠️ sgme.operations.config ≠ sgme.config，故用别名区分。
        from sgme.operations.config import get_config as get_config_operation
        from sgme.operations.config import get_mcp_payload as config_get_mcp_payload

        cfg = _app_state["cfg"]

        data = _op_json(get_config_operation, cfg, section=section)
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps(config_get_mcp_payload(data), ensure_ascii=False)

    @mcp.tool()
    def config_update(section: str, values: dict) -> str:
        """更新 SGME 配置段（热生效 + 落盘 sgme.yaml）。SCSM 经此接口远程设置。

        v0.7：业务逻辑已下沉 sgme.operations.config，本工具只做协议翻译。
        走 **MCP 版** update_config_section（未知段文案不带可用段列表、
        落盘失败转 ``{"error": "配置落盘失败: ..."}``），与 v0.6 逐字段等价。
        """
        import json

        # ⚠️ sgme.operations.config ≠ sgme.config，故用别名区分。
        from sgme.operations.config import update_config_section as update_config_operation
        from sgme.operations.config import update_payload as config_update_payload

        cfg = _app_state["cfg"]

        data = _op_json(update_config_operation, cfg, section=section, values=values)
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        _app_state["cfg"] = cfg
        return json.dumps(config_update_payload(data), ensure_ascii=False)

    # ---------- 创意 / 待办 / 项目（2026-08-13 用户定：用户主动驱动，agent 执行） ----------

    @mcp.tool()
    def idea_add(content: str, priority: int | None = None, source_ref: str | None = None) -> str:
        """人工添加创意（用户主动提出才记录，提炼 LLM 不再自动打标）。

        T-56：写入独立 ideas 表（创意长期保存，无 TTL）。删除/编辑/升格
        走 HTTP API（/v1/admin/ideas*）或 WebUI。
        """
        import json

        from sgme.operations.idea import add_idea as add_idea_operation

        mem_conn = _app_state["mem_conn"]
        data = _op_json(
            add_idea_operation, mem_conn,
            content=content, priority=priority, source_ref=source_ref,
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def demand_create(
        title: str,
        content: str | None = None,
        priority: int | None = None,
        project_id: str | None = None,
        source_ref: str | None = None,
    ) -> str:
        """新建待办/需求（跨项目统一待办池，backlog 化）。

        可指定 project_id 标记所属项目（过滤查询用）；时间戳（加入/完成）
        由服务端自动落库。状态流转走 HTTP API（PUT /v1/admin/demands/{id}/status）。
        """
        import json

        from sgme.operations.demand import create_demand as create_demand_operation

        mem_conn = _app_state["mem_conn"]
        data = _op_json(
            create_demand_operation, mem_conn,
            body={
                "title": title,
                **({"content": content} if content is not None else {}),
                **({"priority": priority} if priority is not None else {}),
                **({"project_id": project_id} if project_id is not None else {}),
                **({"source_ref": source_ref} if source_ref is not None else {}),
            },
        )
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def project_register(
        project_id: str,
        path: str | None = None,
        name: str | None = None,
        git_repo: str | None = None,
        milestone: str | None = None,
    ) -> str:
        """登记/创建项目（用户主动立项；upsert，二次登记=更新）。

        project_id 纯英文（必填）；新建时 path 必填（NOT NULL 列）。
        """
        import json

        from sgme.operations.project import register_project as register_project_operation

        mem_conn = _app_state["mem_conn"]
        data = _op_json(
            register_project_operation, mem_conn,
            project_id=project_id, path=path, name=name,
            git_repo=git_repo, milestone=milestone,
        )
        return json.dumps(data, ensure_ascii=False)

    # ---------- 信号消费（ST-27 T-60：agent 成为消费者，谁消费谁标记） ----------

    @mcp.tool()
    def signal_pull(signal_type: str | None = None, limit: int = 20) -> str:
        """拉取未消费关怀信号（type=care_* 等，ST-27）。

        会话开始主动消费：拉取未消费的关怀信号，决定是否关怀用户。
        signal_type 可选过滤（care_todo_due/care_mood/care_overwork/care_daily）；
        None 拉全部 care_* 未消费信号。
        """
        import json
        import sqlite3

        from sgme.care import signals as signals_mod

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        items = signals_mod.list_care_signals(
            mem_conn, signal_type=signal_type, unconsumed_only=True, limit=min(limit, 50),
        )
        return json.dumps({"signals": items, "total": len(items)}, ensure_ascii=False)

    @mcp.tool()
    def signal_claim(event_id: str, ctx: Context | None = None) -> str:
        """原子认领信号（谁消费谁标记，ST-27）。

        认领成功返回 claimed=true；已被他人消费返回 claimed=false（并发抢失败，
        跳过即可）。agent_id 从鉴权 key 反查（MCP 上下文）。
        """
        import json
        import sqlite3

        from sgme.signal import engine as signal_engine

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        agent_id = None
        if ctx is not None:
            try:
                req = ctx.request_context.request
                if req is not None:
                    key = getattr(req.state, "api_key", None)
                    key_store = _app_state.get("key_store")
                    if key_store is not None:
                        agent_id = key_store.resolve_agent_id(key)
            except Exception:
                agent_id = None
        ok = signal_engine.claim(mem_conn, event_id, agent_id or "unknown")
        return json.dumps(
            {"event_id": event_id, "claimed": ok, "agent_id": agent_id}, ensure_ascii=False,
        )

    @mcp.tool()
    def signal_ack(
        event_id: str,
        status: str,
        result: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """写消费回执（ST-27）：claimed / acked / failed。

        认领后处理完调用，报告处理结果（供溯源 + 释放认领语义）。
        agent_id 从鉴权 key 反查（MCP 上下文）。
        """
        import json
        import sqlite3

        from sgme.data import signal_dao

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        agent_id = None
        if ctx is not None:
            try:
                req = ctx.request_context.request
                if req is not None:
                    key = getattr(req.state, "api_key", None)
                    key_store = _app_state.get("key_store")
                    if key_store is not None:
                        agent_id = key_store.resolve_agent_id(key)
            except Exception:
                agent_id = None
        if status not in ("claimed", "acked", "failed"):
            return json.dumps({"error": f"非法回执状态: {status}"}, ensure_ascii=False)
        signal_dao.ack_signal(mem_conn, event_id, agent_id or "unknown", status, result)
        return json.dumps(
            {"event_id": event_id, "agent_id": agent_id, "status": status}, ensure_ascii=False,
        )

    # ---------- 角色模板（ST-29：agent 发现并调用角色，换皮不换芯） ----------

    @mcp.tool()
    def role_list() -> str:
        """列出可用角色模板（ST-29）：管家/伴侣/朋友/导师，含人设摘要。

        换皮不换芯——角色只是沟通外皮，记忆池（芯）不动。agent 会话开始或
        用户指定角色时调用，按用户当前需求选择；选后调 role_assemble 拿人设。
        """
        import json

        from sgme.operations.care import list_roles as list_roles_operation

        data = _op_json(list_roles_operation)
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        # 附加当前角色 id（未设置 → None），供 agent 判断是否需要重选
        from sgme.operations.care import get_active_role as get_active_operation
        active = _op_json(get_active_operation)
        data["active_role"] = active.get("role_id") if isinstance(active, dict) else None
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def role_assemble(role_id: str, inject_mode: str | None = None) -> str:
        """装配角色沟通提示词（ST-29）：角色卡 system_prompt + care_policy + 画像。

        - 角色不存在 → {"error": "..."}（先 role_list 确认 role_id）
        - inject_mode 可选（daily/full 等模板名）：带上则附加用户画像块（零物化）
        - 产物供 agent 直接注入 system prompt 使用（{{char}}/{{user}} 宏保留）
        """
        import json
        import sqlite3

        from sgme.operations.care import assemble as assemble_operation

        mem_conn: sqlite3.Connection = _app_state["mem_conn"]
        cfg = _app_state["cfg"]
        data = _op_json(
            assemble_operation, role_id, mem_conn, cfg, inject_mode=inject_mode,
        )
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        # 精简返回：system_prompt / care_policy / persona / 画像块（role_id/role_name 冗余保留）
        return json.dumps({
            "role_id": data.get("role_id"),
            "role_name": data.get("role_name"),
            "system_prompt": data.get("system_prompt"),
            "care_policy": data.get("care_policy"),
            "persona": data.get("persona"),
            "profile_blocks": data.get("profile_blocks", []),
        }, ensure_ascii=False)

    @mcp.tool()
    def role_active_get() -> str:
        """读取当前沟通角色（ST-29）；未设置返回 role_id=null。"""
        import json

        from sgme.operations.care import get_active_role as get_active_operation

        data = _op_json(get_active_operation)
        return json.dumps(data, ensure_ascii=False)

    @mcp.tool()
    def role_active_set(role_id: str) -> str:
        """设置当前沟通角色（ST-29，换皮不换芯：只换角色，记忆池不动）。

        角色必须存在（role_list 可见）；非法/不存在 id → {"error": "..."}。
        """
        import json

        from sgme.operations.care import set_active_role as set_active_operation

        data = _op_json(set_active_operation, role_id)
        return json.dumps(data, ensure_ascii=False)

    # ---------- 连接即发现（ST-23①） ----------

    @mcp.tool()
    def agent_onboarding() -> str:
        """连接即发现（self-serve）：SGME 版本、能力清单（全部工具）、快速上手指引。

        ST-23①：agent 连接后先调本工具即可完成接入——无需人工配置即可
        知道「我是谁 / 能干什么 / 怎么开始」。版本取 ``sgme.__version__``
        （新工具无历史契约，版本保持最新即可）；能力清单为 ONBOARDING_TOOLS
        （与 @mcp.tool 一一对应，测试断言防漂移）；指引覆盖
        注册（append 即接入）/ 提炼（refine_trigger/refine_batch/refine_status）/
        回忆（search/inject）三条主线。
        """
        import json

        import sgme

        return json.dumps({
            "server": "SGME 拾光记忆引擎",
            "version": sgme.__version__,
            "tools": [dict(t) for t in ONBOARDING_TOOLS],
            "quickstart": {
                "register": "无独立注册步骤：调用 append 写入第一条会话即完成接入（每次会话用同一 session_key 延续）",
                "append": "append(session_key, started_at, content, source_type='session')；content 首行须为 # {ISO时间戳} {role}",
                "refine": "refine_trigger(async_mode=true) 或 refine_batch() 触发提炼；refine_status() 查进度与水位",
                "recall": "search(query) 混合检索带溯源；inject(mode='daily') 注入当日画像",
                "manage": "memory_reject 纠错；stats/health 看引擎状态；config_get/config_update 读写配置",
            },
            "self_config": {
                "requirement": "接入完成标准：你的身份文件（SOUL.md / AGENTS.md / CLAUDE.md 等，按你的工具机制选择）中必须包含本模板（版本标记 SGME-ONBOARDING-v2）。SGME 不替你改文件——由你自助完成，只要求结果、不限制过程。若 health 返回的 model_config 提示模型 Key 缺失（如 ZHIPU_API_KEY / SILICONFLOW_API_KEY），请提醒主人按 docs/guide/免费模型Key申请指南.md 申请免费 Key（智谱 GLM-4.7-Flash 永久免费 / 硅基流动 bge-m3）。",
                "steps": [
                    "1. 自查：读你的身份文件，grep 标记「SGME-ONBOARDING」；",
                    "2. 已存在且版本 >= v1 → 配置已完成，跳过；",
                    "3. 缺失或版本旧 → 把下方 template 写入你的身份文件（写入位置/方式按你工具的文件组织与加载机制自定）；",
                    "4. 写入后读回验证内容完整，并汇报（写了哪个文件、哪个位置）；",
                    "5. 无法写入（权限/沙箱/审批限制）→ 如实报告主人并给出建议，禁止谎称已完成。",
                ],
                "version": "SGME-ONBOARDING-v2",
                "template": (
                    "# SGME 接入纪律（SGME-ONBOARDING-v2）\n"
                    "你有一个长期记忆引擎 SGME（ShiGuang Memory Engine），运行在本机或 NAS。"
                    "它的职责：把我们的会话提炼成标签化记忆，按场景注入回来，让你不再失忆。\n"
                    "**服务发现**（找不到时按序）：1) 探测 http://<sgme-host>:9910/v1/health（host 取环境变量 SGME_HTTP_HOST 或 ~/.sgme/install.json 的 http.host，默认 localhost）；"
                    "2) 失败读 ~/.sgme/install.json（地址/端口/Key 引用）；3) 仍失败 → 向主人报告「SGME 未发现」。\n"
                    "**使用纪律**（五条铁律）：\n"
                    "1. 每轮对话结束 append 当前轮次——纯落盘零 LLM 成本，崩溃不丢；\n"
                    "2. 会话结束 refine_trigger(async_mode=true) 触发提炼；\n"
                    "3. 对话开始时 inject 按场景取画像 / search 检索相关记忆；\n"
                    "4. 主动关怀靠消费信号——**信号消费=主动关怀，谁消费谁标记**：拿到 care_* 信号后 signal_claim 原子认领 → 关怀用户 → signal_ack 回执（认领失败=已被其他 agent 消费，跳过）。获取信号有两条路：\n"
                    "   - 短连接（无常驻进程的 agent）：每次对话开始 signal_pull 拉未消费信号；\n"
                    "   - 长连接（有常驻能力的 agent，**主动关怀首选**）：挂 SSE 事件流 GET http://<sgme-host>:9910/v1/events/stream?subscriber_id=<你的agent_id>（请求头带 X-API-Key），事件实时推送，care_*/memory_updated/anomaly_warn 一产生即到达 → 立即 claim→关怀→ack；断线重连带 Last-Event-ID 头补偿，不丢事件。\n"
                    "5. 对话开始时（或用户指定角色时）role_list 看可用角色，role_assemble(role_id) 拿人设并按其说话——换皮不换芯，角色只是沟通外皮，记忆池不动。\n"
                    "**强制查询**：涉及用户/项目历史事实的问题（之前/以前/上次/还记得…），必须先 search 再回答，不得直接说「不知道」。\n"
                    "**写入格式**：append 的 content 首行必须是 `# {ISO时间戳} {role}`（user/assistant），否则返回 422。\n"
                    "**批量提炼纪律**：≥20 文件必须分批（每批≤20）+ 批间 30–60 秒；429 不立即重试（交服务端 batch_scan 兜底）；永远 async 模式。\n"
                    "**待办主动登记**（2026-08-18 强化）：会话中遇到用户要办的事 / 项目要做的任务 / 待跟进事项，主动调 demand_create 登记（title + project_id 关联），不要只留在对话里；完成时标记 done。\n"
                    "**事件对接**（主动关怀的触发源，常驻 agent 必读）：SGME 事件分三类 care_*（关怀：情绪/待办到期/过劳/每日）、memory_updated（记忆更新）、anomaly_warn（异常）。三种接法任选：①SSE 长连 GET /v1/events/stream?subscriber_id=<agent_id>（实时推送，Last-Event-ID 断线补偿）；②游标拉取 GET /v1/events/pull?subscriber_id=<agent_id>（持久游标，适合定时任务轮询）；③MCP signal_pull（会话内短连接）。SSE/pull 走 HTTP :9910 带 X-API-Key；signal_pull 走 MCP。\n"
                    "**通信渠道**（兜底铁律）：当前会话是兜底通信渠道——任何主动消息（关怀/提醒/告警等），无论是否已发送到其它通道（微信/飞书/Telegram 等），都必须在当前会话也发一条；此兜底持续生效，直到用户明确取消。\n"
                    "**向量引擎接入**（health() 返回 vector.available=false 或 vector.connectivity=false 时执行；策略：本地优先、云端免费降级——本地 Ollama bge-m3 1024 维，fallback 硅基流动 BAAI/bge-m3 同样 1024 维免费）：\n"
                    "Step1 诊断：health() 看 vector.available / vector.connectivity / model_config.missing_keys，定位是缺引擎还是缺 Key；\n"
                    "Step2 探测本机向量引擎（按序）：Ollama `curl http://127.0.0.1:11434/api/tags`；LM Studio `curl http://127.0.0.1:1234/v1/models`——响应里有 bge-m3 即可用；不推荐 llama.cpp（模型状态无人维护，用户清理显存后即失联，麻烦）；\n"
                    "Step3 引导部署：已装但无 bge-m3 → `ollama pull bge-m3`（或 LM Studio 搜索下载 bge-m3）；都没装 → 推荐安装 Ollama（跨平台一键）或 LM Studio（有 GUI），二选一；\n"
                    "Step4 配置写入：直接改 config/sgme.yaml 的 search.vector 段（改后需重启 SGME 生效），主 provider 指本地、fallbacks 列表指云端免费降级：\n"
                    "  search:\n"
                    "    vector:\n"
                    "      enabled: true\n"
                    "      provider: local          # 本地优先\n"
                    "      model: bge-m3\n"
                    "      base_url: http://127.0.0.1:11434/v1   # LM Studio 用 http://127.0.0.1:1234/v1\n"
                    "      # api_key_env 留空——本地 Ollama/LM Studio 无需鉴权\n"
                    "      fallbacks:\n"
                    "        - provider: siliconflow   # 云端免费降级备用\n"
                    "          model: BAAI/bge-m3\n"
                    "          base_url: https://api.siliconflow.cn/v1\n"
                    "          api_key_env: SILICONFLOW_API_KEY\n"
                    "  注意：本地与云端都用 bge-m3（1024 维），切换不重灌索引；\n"
                    "Step5 验证闭环：再 health() → vector.connectivity=true → 报告「✅ 本地向量引擎已就绪」；失败按端口/模型名/代理劫持排障，仍失败如实上报。\n"
                    "边界：本流程要求 agent 能操作宿主（curl/装软件/写配置）——宿主机 agent 可执行；云端 agent 无权限时给用户操作建议，禁止谎称已完成。\n"
                    "**接口**：HTTP API http://<sgme-host>:9910 ｜ MCP http://<sgme-host>:9913/mcp，请求头 X-API-Key（key 由主人配置：config/.env 的 SGME_ADMIN_KEY/SGME_AGENT_KEY，或管理员签发的 agt_* key；host 解析见「服务发现」）。"
                ),
            },
        }, ensure_ascii=False)

    return mcp


def mount_mcp(app, start_server: bool = True):
    """把 MCP Server 挂到 SGME Server 进程（同进程两协议出口）。

    - HTTP API:  FastAPI :9910（uvicorn 主循环）
    - MCP:       FastMCP streamable-http :9913（独立线程，鉴权中间件）
    两协议共享同一 cfg/mem_conn/session_conn/wiki_conn，功能等价。
    start_server=False 时只构建不启动线程（测试用）。
    返回 FastMCP 实例（测试用）。
    """
    import threading

    from sgme.mcp_server import bind_app_state

    bind_app_state({
        "cfg": app.state.cfg,
        "mem_conn": app.state.mem_conn,
        "session_conn": app.state.session_conn,
        "wiki_conn": app.state.wiki_conn,
        "key_store": app.state.key_store,
    })
    mcp = build_mcp_server()
    # 测试/CI 环境跳过启动（SGME_MCP_PORT=0 或 MCP_DISABLED=1）
    if os.environ.get("SGME_MCP_DISABLED") == "1" or not start_server:
        logger.info("MCP Server 已跳过启动（SGME_MCP_DISABLED=1 或 start_server=False）")
        return mcp
    # 手动 uvicorn 跑 streamable_http_app + 鉴权中间件（PR#1，替代 mcp.run()）
    from sgme.mcp_server import run_mcp_server

    run_mcp_server(
        mcp,
        key_store=app.state.key_store,
        host=os.environ.get("SGME_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("SGME_MCP_PORT", "9913")),
    )
    return mcp
