"""operations 层：HTTP 与 MCP 共用的**唯一业务操作实现**（v0.7 §7）。

设计动机（§7.1）
----------------
改造前 ``server/routes_*.py`` 与 ``mcp_server.py`` 各自实现了一遍
"参数校验 → 调 engine/storage → 错误处理 → 序列化"。operations 层把中间三步收敛为
单一实现，两个入口退化为**纯协议翻译**：

    HTTP /v1/*  ──→ operations.xxx(params) → OperationResult → JSONResponse
    MCP  tool   ──→ operations.xxx(params) → OperationResult → json.dumps

分层铁律
--------
1. operations **不认识协议**：不 import fastapi / mcp，不知道 HTTP 状态码，
   更不知道 ``request.app.state`` 或 mcp 的 ``_app_state`` 长什么样。
   所有依赖（mem_conn / session_conn / cfg / 业务参数）由入口层**显式传入**。
2. 入口层**不写业务**：只做「从自己的状态容器取依赖 → 调 operation → 翻译错误 → 序列化」。
3. operations 向下只依赖业务层（engine / storage / search / profile / raw / signal），
   engine 是只读禁区。

新增一个操作模块的标准姿势（照抄 health.py）
--------------------------------------------
``sgme/operations/<name>.py``::

    from sgme.operations.errors import InvalidArgs, OperationResult

    def <name>(conn_or_deps..., *, 业务参数...) -> OperationResult:
        '''一句话职责。'''
        if 参数非法:
            raise InvalidArgs("说明")          # 或 return OperationResult.fail(...)
        result = 业务层调用(...)
        return OperationResult.succeed({...})   # data 为协议无关信息超集

    def http_payload(data): ...   # 仅当两端历史响应形态不一致时才需要
    def mcp_payload(data): ...    # 一致的话两端直接用 data，不写投影函数

入口层配套（已就绪，直接复用，**不要**每个模块重写一遍）：
- HTTP：``sgme.server.app.run_operation(op, *args, **kwargs) -> dict``
  成功返 ``result.data``；失败/InvalidArgs/OperationError → 抛 ``api_error``（含状态码映射）。
- MCP：``sgme.mcp_server._op_json(op, *args, **kwargs) -> str``
  成功返 ``json.dumps(result.data)``；失败 → ``{"error": "..."}``（沿用既有 MCP 错误约定）。

扩展位（§7.3，按 P2-T3 后续切片逐个补，**当前仅 health 已落地**）
------------------------------------------------------------------
=============  ==========================================  ==================
模块            承接的入口                                    主要依赖
=============  ==========================================  ==================
append.py      POST /v1/append        / MCP append          engine.pipeline
inject.py      POST /v1/inject        / MCP inject          profile.*
search.py      POST /v1/search        / MCP search          search
memory.py      GET|POST /v1/memory/*  / MCP memory_get      storage.memory_dao
refine.py      POST /v1/refine/*      / MCP refine_trigger  engine.pipeline
stats.py       GET /v1/stats          / MCP stats           storage.stats_dao
health.py      GET /v1/health         / MCP health          engine.health   ✅
config.py      GET|POST /v1/config    / MCP config_get      sgme.config
registry.py    GET|POST /v1/registry/*  —（HTTP 专属）       storage.memory_dao
prompts.py     GET|POST /v1/admin/prompts/* —（HTTP 专属）   prompts.manager
backup.py      POST|GET /v1/admin/backup/* —（HTTP 专属）    backup.manager（B30 裸连接例外）
events.py      GET /v1/events/*       —（HTTP 专属）         data.signal_dao
=============  ==========================================  ==================

注：T-8（2026-08-10）后 backup/events/registry/prompts 已全部下沉 operations 层。

导入规范（**强制**，全部模块零特例）
------------------------------------------
**本包只扁平导出 errors 里的类型，绝不扁平导出任何操作函数。**
调用方一律走完整子模块路径::

    from sgme.operations.health import health, http_payload   # ✅ 唯一正确姿势
    from sgme.operations import health                        # ❌ 禁止（此处 health 是模块，易误当函数）

这样 ``sgme.operations.<模块>`` 恒为**模块**，语义无歧义。

为什么不做 ``from .health import health`` 式扁平导出（三条理由，从强到弱）：

1. **扁平模式压根表达不了多操作模块**。``memory.py`` 的职责是 get / reject /
   unreject **三个**操作，无法坍缩成单个 ``operations.memory()``。
   ``search.py`` / ``stats.py`` 后续大概率也不止一个函数。为一部分模块开扁平口子、
   另一部分不开，是最坏的不一致。
2. **``config.py`` 扁平导出会与项目已有的 ``sgme/config.py`` 撞名**，
   ``from sgme.operations import config`` 与 ``from sgme import config`` 极易误读。
3. 扁平导出还会让包属性被同名函数遮蔽（``sgme.operations.health`` 指向函数而非模块），
   投影函数取不到。这只是附带症状，不是主要理由。

隐性契约（不写死后面必踩）
--------------------------
**操作返回的 ``data`` 不得使用顶层 ``error`` 键。**
MCP 入口的 ``_op_json`` 以 ``"error" in data`` 判定失败态，业务数据若占用该键
会被误判为错误。表达失败请用 ``OperationResult.fail(...)`` 或抛
``InvalidArgs`` / ``OperationError``。

**不要加 catch-all ``except``。** 非预期异常须按 v0.6 行为原样上抛，
交由入口层全局异常处理器兜底——就地吞掉会改变错误响应形态，与「API 契约不变」冲突。
"""
from __future__ import annotations

from sgme.operations.errors import (
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_LLM_UNAVAILABLE,
    ERR_NOT_FOUND,
    InvalidArgs,
    OperationError,
    OperationResult,
    result_from_exception,
)

# 注意：此处**刻意不导入任何操作函数**（如 health），理由见上「导入规范」。
__all__ = [
    "OperationResult",
    "InvalidArgs",
    "OperationError",
    "result_from_exception",
    "ERR_INVALID_ARGS",
    "ERR_NOT_FOUND",
    "ERR_LLM_UNAVAILABLE",
    "ERR_INTERNAL",
]
