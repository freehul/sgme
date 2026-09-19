"""operations/skill_usage.py：技能消费统计操作（T-174，2026-09-19）。

三段式（照 ``operations/usage.py`` 样板；本模块响应即最终形态，单入口无需协议投影）。

承接的入口
----------
- HTTP 中间件（``server/app.py`` ``UsageMiddleware``）→ ``digest`` / ``get`` 两层
- HTTP 端点（``server/routes_skills.py``）→ ``search`` / ``materialize`` 两层
- MCP 中间件（``mcp_server.py`` ``ApiKeyMiddleware``）→ ``digest`` / ``get`` / ``materialize``
- MCP 工具（``mcp_server.py`` ``skill_search``）→ ``search``
- HTTP ``GET /v1/admin/skills/usage``（管理员 Key）→ ``query_skill_usage``

埋点分工（为什么不在中间件一把抓）
--------------------------------
- ``digest`` / ``get``：层与技能名可由「路由模板 + 路径参数」推出 → 中间件记；
- ``search``：要记命中数与首条命中，只有端点 / 工具拿得到结果 → 业务侧记；
- ``materialize``：要记目标目录**类型**（Windows 盘符路径落到 Linux 服务端是
  真实缺陷信号），而 HTTP 侧 body 在纯 ASGI 中间件里不可读 → HTTP 业务侧记，
  MCP 侧 body 可读 → 中间件记。
同一次调用只产生一条记录，不重复计数。

依赖：SQL 一律走 ``sgme.data.skill_usage_dao``（data 层唯一出口）；本层只做
参数校验与响应组装。``record_skill_usage`` 的「全静默」由调用方兜底——统计是
旁路，任何失败不得影响请求。
"""

from __future__ import annotations

import re
import sqlite3

from sgme.data import skill_usage_dao
from sgme.operations.errors import OperationResult

#: 层白名单（与 data 层一致）
LAYERS = skill_usage_dao.LAYERS

MAX_DAYS = 400
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

#: 检索词在 note 中的截断长度（防不可控输入；note 总长由 DAO 再兜一层）
QUERY_MAX = 80

#: HTTP 中间件可推导的两层：路由模板 → 层
_MIDDLEWARE_ROUTE_LAYERS = {
    "/v1/skills/{name}/digest": "digest",
    "/v1/skills/{name}": "get",
}

#: MCP 中间件可推导的三层：工具名 → 层（``skill_search`` 由工具侧记，故不在此表）
MCP_MIDDLEWARE_TOOL_LAYERS = {
    "skill_digest": "digest",
    "skill_get": "get",
    "skill_materialize": "materialize",
}

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_UNC = re.compile(r"^\\\\")


def dest_kind(dest_dir: str | None) -> str:
    """物化目标目录类型（**只记类型，不记真实路径**——数据卫生铁律）。

    - ``windows``：``D:/…`` 或 ``\\\\host\\share``（**跨机调用时是缺陷信号**：
      Linux 服务端会把盘符当相对路径，在容器里造出 ``/app/D:/…`` 垃圾目录）
    - ``absolute``：``/…``（服务端绝对路径）
    - ``home``：``~`` 开头
    - ``relative``：相对路径
    - ``empty``：空值
    """
    if not isinstance(dest_dir, str) or not dest_dir.strip():
        return "empty"
    d = dest_dir.strip()
    if _WIN_DRIVE.match(d) or _WIN_UNC.match(d):
        return "windows"
    if d.startswith("~"):
        return "home"
    if d.startswith("/"):
        return "absolute"
    return "relative"


def search_note(query: str, hits: int, top: str | None = None) -> str:
    """检索摘要（note）：``q=<检索词>;hits=<命中数>;top=<首条命中>``。

    检索词截断到 ``QUERY_MAX`` 且折叠换行（note 是聚合列，不得被长输入撑大）。
    """
    q = " ".join(str(query or "").split())[:QUERY_MAX]
    return f"q={q};hits={int(hits)};top={top or '-'}"


def extract_middleware_skill_call(
    route_path: str | None, path_params: dict | None
) -> tuple[str, str] | None:
    """从 HTTP 路由模板 + 路径参数提取 ``(layer, skill)``；非技能端点 → None。

    ``search`` / ``materialize`` 不在此列：前者要命中结果、后者要请求体，
    均由业务侧记录（见模块 docstring 的埋点分工）。
    """
    if not route_path:
        return None
    layer = _MIDDLEWARE_ROUTE_LAYERS.get(route_path)
    if layer is None:
        return None
    skill = (path_params or {}).get("name") or "-"
    return layer, str(skill)


def extract_mcp_middleware_call(
    tool_name: str | None, arguments: dict | None
) -> tuple[str, str, str | None] | None:
    """从 MCP ``tools/call`` 的「工具名 + 参数」提取 ``(layer, skill, note)``。

    非技能工具 → None。``note`` 仅物化层有值（目标目录类型）。
    """
    layer = MCP_MIDDLEWARE_TOOL_LAYERS.get(tool_name or "")
    if layer is None:
        return None
    args = arguments if isinstance(arguments, dict) else {}
    skill = str(args.get("name") or "-")
    note = f"dest={dest_kind(args.get('dest_dir'))}" if layer == "materialize" else None
    return layer, skill, note


def caller_from_state(state) -> str:
    """从请求 state 取调用方（HTTP 中间件写入 ``usage_caller``；缺省 ``unknown``）。

    兼容两种形态：ASGI ``scope["state"]``（dict）与 FastAPI ``request.state``
    （starlette ``State`` 对象，属性访问）。
    """
    if state is None:
        return "unknown"
    if isinstance(state, dict):
        value = state.get("usage_caller")
    else:
        value = getattr(state, "usage_caller", None)
    return str(value or "unknown")


def record_skill_usage(
    conn: sqlite3.Connection,
    layer: str,
    skill: str,
    caller: str,
    note: str | None = None,
    ip: str | None = None,
) -> None:
    """记录一次技能消费（layer 必须在白名单内；异常由调用方静默兜底）。"""
    if layer not in LAYERS:
        raise ValueError(f"未知层: {layer!r}（可选: {'/'.join(LAYERS)}）")
    skill_usage_dao.record_skill_usage(conn, layer, skill, caller, note=note, ip=ip)


def query_skill_usage(
    conn: sqlite3.Connection,
    days: int = DEFAULT_DAYS,
    layer: str | None = None,
    skill: str | None = None,
    caller: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> OperationResult:
    """近 N 天技能消费统计：按「层 × 技能 × 调用方」聚合（次数降序）。

    回答「四级披露哪一层真的在被用 / 哪个技能被取用 / 谁在用 / 物化目标是否异常」。
    """
    try:
        days_v = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    except (TypeError, ValueError):
        return OperationResult.fail("ERR_INVALID_ARGS", f"days 需为整数（1~{MAX_DAYS}）")
    try:
        limit_v = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        return OperationResult.fail(
            "ERR_INVALID_ARGS", f"limit 需为整数（1~{MAX_LIMIT}）"
        )
    if layer is not None and layer not in LAYERS:
        return OperationResult.fail(
            "ERR_INVALID_ARGS", f"layer 仅支持 {'/'.join(LAYERS)}"
        )
    items = skill_usage_dao.query_skill_usage(
        conn, days=days_v, layer=layer, skill=skill, caller=caller, limit=limit_v
    )
    return OperationResult.succeed(
        {
            "days": days_v,
            "layer": layer,
            "skill": skill,
            "caller": caller,
            "limit": limit_v,
            "total_rows": len(items),
            "layers": list(LAYERS),
            "items": items,
        }
    )
