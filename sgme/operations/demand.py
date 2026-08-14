"""operations/demand.py：需求池业务操作（0.8 ST-15）。

演化链定位（`SGME-创意池与需求池设计-v0.1.md` §2 ②）：
创意池 ① →（人工升格）→ **需求池 ②** →（立项关联）→ 项目 ③ → 问题 ④ → PR ⑤。
需求 = 宽泛需求，无标准无时限；状态流转 未立项 → 已立项 → 部分解决 → 已解决。

分层（v0.7 §7）：本模块不认识协议——不 import fastapi、不知道 HTTP 状态码；
入口层 `server/routes_admin.py` 只做协议翻译，SQL 全部在 `data/demand_dao.py`（B30）。

业务判断（四个判断题的落点，均在本模块）：
1. **不加外键**：project_id / origin_idea_id 是裸 TEXT，跨表存在性走软校验
   （`_check_project` 条件生效；origin_idea_id 一律不校验，见 §「升格链路」）。
2. **转出 done 清空 resolved_at**：维持不变式 ``resolved_at IS NOT NULL ⟺ status='done'``。
3. **转 planned 不强制 project_id**：不拦截，只回 ``warnings=['planned_without_project']``。
4. **不限制流转方向**：四态间任意流转合法（含 done→pending 回退），非法的是**状态值**不是路径。

升格链路（与 ST-14 的接口约定）：本模块只负责写入/查询 ``demands.origin_idea_id``，
**不校验该 memory_id 是否存在、不写创意侧 memories.custom_flag**——
创意侧标记 `promoted` 由 ST-14 负责，跨模块耦合留到集成阶段。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.data import demand_dao
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult

# ---------- 枚举与默认值 ----------

# 合法状态（数据模型文档 demands.status）
DEMAND_STATUSES: tuple[str, ...] = ("pending", "planned", "partial", "done")

# 展示层中文映射（数据模型文档："展示层映射中文"）——由 API 直接给出，
# 避免 WebUI / SCSM / CLI 各写一份映射表导致术语漂移
STATUS_LABELS: dict[str, str] = {
    "pending": "未立项",
    "planned": "已立项",
    "partial": "部分解决",
    "done": "已解决",
}

DEFAULT_STATUS: str = "pending"
DEFAULT_PRIORITY: int = 50
PRIORITY_MIN: int = 0
PRIORITY_MAX: int = 100

DEFAULT_PAGE: int = 1
DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 200          # 硬上限，对齐契约 §5.3（防查询放大）
DEFAULT_SORT: str = "updated_at"
DEFAULT_ORDER: str = "desc"

# 创建时可接受的入参键（多余键即视为参数非法，避免拼错字段被静默吞掉）
_CREATE_KEYS: frozenset[str] = frozenset(
    {"title", "content", "status", "priority", "project_id", "origin_idea_id", "source_ref"}
)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与 data 层同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 参数解析（全部抛 InvalidArgs → 入口层翻译为 400 ERR_INVALID_ARGS） ----------

def _parse_int(raw: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    """解析整数参数并做区间校验。

    接受 int 与纯数字字符串；显式拒绝 bool（Python 中 bool 是 int 子类，
    ``priority=true`` 若放行会被静默存成 1）。
    """
    if isinstance(raw, bool):
        raise InvalidArgs(f"{name} 必须是整数: {raw}")
    if isinstance(raw, int):
        val = raw
    else:
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            raise InvalidArgs(f"{name} 必须是整数: {raw}") from None
    if val < minimum:
        raise InvalidArgs(f"{name} 必须 ≥ {minimum}: {val}")
    if maximum is not None and val > maximum:
        raise InvalidArgs(f"{name} 不得超过 {maximum}: {val}")
    return val


def _parse_page(raw: Any) -> int:
    """page：≥1，默认 1。"""
    if raw is None or raw == "":
        return DEFAULT_PAGE
    return _parse_int(raw, "page", 1)


def _parse_limit(raw: Any) -> int:
    """limit：1-200，默认 50；>200 → 400（硬上限，不静默截断）。"""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    return _parse_int(raw, "limit", 1, MAX_LIMIT)


def _parse_priority(raw: Any) -> int:
    """priority：0-100，默认 50。"""
    if raw is None or raw == "":
        return DEFAULT_PRIORITY
    return _parse_int(raw, "priority", PRIORITY_MIN, PRIORITY_MAX)


def _parse_status(raw: Any) -> str:
    """单个状态值校验（非法 → 400，错误文案列出全部合法值）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidArgs(f"status 必须是非空字符串，合法值: {list(DEMAND_STATUSES)}")
    val = raw.strip()
    if val not in DEMAND_STATUSES:
        raise InvalidArgs(f"非法状态: {val}（合法值: {list(DEMAND_STATUSES)}）")
    return val


def _parse_statuses(raw: str | None) -> list[str] | None:
    """状态过滤：逗号分隔多值；None/空 → 不过滤（返回 None）。

    ⚠️ 与 §5.3 memories 的「默认仅 active」不同——需求池默认**展示全部状态**：
    需求的四态全都是活跃业务态（含 done，需要看"已解决"作为成果盘点），
    没有 rejected/expired 这类"应默认隐藏"的语义。
    """
    if raw is None or raw.strip() == "":
        return None
    parts = [p.strip() for p in raw.split(",")]
    values = [p for p in parts if p]
    if not values:
        raise InvalidArgs("status 过滤值为空")
    out: list[str] = []
    for v in values:
        sv = _parse_status(v)
        if sv not in out:
            out.append(sv)
    return out


def _parse_sort(raw: str | None) -> str:
    """排序字段：priority / updated_at / created_at，默认 updated_at。"""
    if raw is None or raw.strip() == "":
        return DEFAULT_SORT
    val = raw.strip()
    if val not in demand_dao.SORT_FIELDS:
        raise InvalidArgs(
            f"未知排序字段: {val}（合法值: {list(demand_dao.SORT_FIELDS)}）"
        )
    return val


def _parse_order(raw: str | None) -> str:
    """排序方向：desc / asc，默认 desc。"""
    if raw is None or raw.strip() == "":
        return DEFAULT_ORDER
    val = raw.strip().lower()
    if val not in demand_dao.ORDER_DIRECTIONS:
        raise InvalidArgs(
            f"未知排序方向: {val}（合法值: {list(demand_dao.ORDER_DIRECTIONS)}）"
        )
    return val


def _parse_ts(raw: str | None, name: str) -> str | None:
    """解析 ISO8601 时间参数并归一为库内定宽格式 ``%Y-%m-%dT%H:%M:%SZ``。

    库内时间戳为定宽 UTC 字符串，字典序 == 时间序，故归一后可直接参与 SQL 比较。
    含时区的输入先换算到 UTC；不带时区的按 UTC 解释（与写入侧一致）。
    """
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise InvalidArgs(f"{name} 不是合法 ISO8601 时间: {text}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_text(raw: Any, name: str, required: bool = False) -> str | None:
    """文本参数：去首尾空白；required 时空值 → 400。"""
    if raw is None:
        if required:
            raise InvalidArgs(f"{name} 不能为空")
        return None
    if not isinstance(raw, str):
        raise InvalidArgs(f"{name} 必须是字符串: {raw!r}")
    val = raw.strip()
    if not val and required:
        raise InvalidArgs(f"{name} 不能为空")
    return val


def _parse_nullable_ref(raw: Any, name: str) -> str | None:
    """可空引用类字段（project_id / origin_idea_id / source_ref）。

    显式 null 或空串一律归一为 None（= 解绑），非字符串 → 400。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidArgs(f"{name} 必须是字符串或 null: {raw!r}")
    val = raw.strip()
    return val or None


# ---------- 投影与软校验 ----------

def _to_item(row: dict[str, Any]) -> dict[str, Any]:
    """DB 行 → API 条目（补 status_label 中文展示名，字段序固定）。"""
    status = row.get("status") or ""
    return {
        "demand_id": row.get("demand_id"),
        "title": row.get("title"),
        "content": row.get("content"),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "priority": row.get("priority"),
        "project_id": row.get("project_id"),
        "origin_idea_id": row.get("origin_idea_id"),
        "source_ref": row.get("source_ref"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "resolved_at": row.get("resolved_at"),
    }


def _check_project(conn: sqlite3.Connection, project_id: str | None) -> str | None:
    """project_id 软校验（2026-08-13 语义变更：待办池化后为**自由标记**，不阻断）。

    - project_meta 表**未就绪**（ST-16 未合并）→ 无警告。
    - 表就绪但 project_id 不存在 → 返回 warning（**不再 400**）——
      需求池=跨项目待办池，待办可能先于项目注册出现（用户先记待办、后主动立项），
      project_id 仅作过滤标记；未知项目提示 agent 可顺手注册。

    为什么不用外键：memory.db 连接开了 ``PRAGMA foreign_keys=ON``，
    若 DDL 里写 ``REFERENCES project_meta(project_id)`` 而该表尚未创建，
    对 demands 的任何 DML 都会直接抛 "no such table: main.project_meta"，
    需求池在 ST-16 合并前完全不可用；且项目记录被清理时外键会连带阻塞/级联，
    与"溯源信息应尽量保留"相悖（参见设计文档 §2 ④「归档 FK 崩溃」教训）。

    Returns:
        未知项目时返回 warning 文案；无警告返回 None。
    """
    if project_id is None:
        return None
    if not demand_dao.project_meta_available(conn):
        return None
    if not demand_dao.project_exists(conn, project_id):
        return f"project_id 未登记: {project_id}（可先登记项目或忽略该标记）"
    return None


def _warnings_for(status: str, project_id: str | None) -> list[str]:
    """非阻断提示（判断题③的落地）。

    "已立项（关联项目）"的字面语义确实暗示 planned 应带 project_id，但**不强制**：
    真实工作流是「用户先拍板要做（planned）→ 再跑 scripts/project_init.py 建项目」，
    强制会造成先有鸡还是先有蛋；且设计文档 §8 待决问题 3「'已立项'由谁标记」尚未定案，
    此时把规则写死属于越权。折中：回一条机器可读 warning，UI 据此提示补全。
    """
    if status == "planned" and not project_id:
        return ["planned_without_project"]
    return []


# ---------- 操作：列表 ----------

def list_demands(
    mem_conn: sqlite3.Connection,
    page: Any = None,
    limit: Any = None,
    status: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> OperationResult:
    """需求池分页列表。

    Args:
        mem_conn: memory.db 连接。
        page: 页码（≥1，默认 1）。
        limit: 页大小（1-200，默认 50）。
        status: 逗号分隔多值过滤；缺省不过滤（四态全展示）。
        project_id: 精确匹配关联项目。
        q: 标题/内容子串匹配（通配符已转义）。
        since / until: ISO8601 时间范围，作用于 sort 时间列（sort=priority 时回落 updated_at）。
        sort: priority / updated_at / created_at，默认 updated_at。
        order: desc / asc，默认 desc。

    Returns:
        OperationResult，data = ``{items, count, total, page, limit, generated_at}``。

    Raises:
        InvalidArgs: 任一参数非法（入口层 → 400 ERR_INVALID_ARGS）。
    """
    page_val = _parse_page(page)
    limit_val = _parse_limit(limit)
    statuses = _parse_statuses(status)
    sort_val = _parse_sort(sort)
    order_val = _parse_order(order)
    since_val = _parse_ts(since, "since")
    until_val = _parse_ts(until, "until")
    project_val = _parse_nullable_ref(project_id, "project_id")
    q_val = _parse_text(q, "q")

    rows, total = demand_dao.list_demands(
        mem_conn,
        statuses=statuses,
        project_id=project_val,
        q=q_val,
        since=since_val,
        until=until_val,
        sort=sort_val,
        order=order_val,
        page=page_val,
        limit=limit_val,
    )
    items = [_to_item(r) for r in rows]
    return OperationResult.succeed({
        "items": items,
        "count": len(items),
        "total": total,
        "page": page_val,
        "limit": limit_val,
        "generated_at": _now_iso(),
    })


# ---------- 操作：详情 ----------

def get_demand(mem_conn: sqlite3.Connection, demand_id: str) -> OperationResult:
    """单条需求详情；不存在 → ERR_NOT_FOUND（入口层 → 404）。"""
    did = _parse_text(demand_id, "demand_id", required=True) or ""
    row = demand_dao.get_demand(mem_conn, did)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"需求不存在: {did}")
    return OperationResult.succeed(_to_item(row))


# ---------- 操作：新建 ----------

def create_demand(
    mem_conn: sqlite3.Connection,
    body: dict[str, Any] | None = None,
) -> OperationResult:
    """新建需求（支持从创意升格：传 origin_idea_id）。

    Body 键：
        title (必填) / content / status（默认 pending）/ priority（0-100，默认 50）/
        project_id / origin_idea_id / source_ref。

    Returns:
        OperationResult，data = 条目字段 + ``warnings``。

    Raises:
        InvalidArgs: 未知键、title 为空、status/priority 非法、project_id 软校验失败。
    """
    b = body or {}
    if not isinstance(b, dict):
        raise InvalidArgs("请求体必须是 JSON 对象")
    unknown = sorted(set(b) - _CREATE_KEYS)
    if unknown:
        raise InvalidArgs(f"未知字段: {unknown}（可用: {sorted(_CREATE_KEYS)}）")

    title = _parse_text(b.get("title"), "title", required=True) or ""
    content = _parse_text(b.get("content"), "content") or ""
    status = _parse_status(b["status"]) if b.get("status") is not None else DEFAULT_STATUS
    priority = _parse_priority(b.get("priority"))
    project_id = _parse_nullable_ref(b.get("project_id"), "project_id")
    # origin_idea_id 刻意不校验存在性：创意侧（memories.custom_flag）归 ST-14，
    # 跨模块存在性耦合留到集成阶段（边界约定，见模块 docstring）
    origin_idea_id = _parse_nullable_ref(b.get("origin_idea_id"), "origin_idea_id")
    source_ref = _parse_nullable_ref(b.get("source_ref"), "source_ref")

    _check_project(mem_conn, project_id)

    now = _now_iso()
    demand_id = demand_dao.new_demand_id()
    demand_dao.insert_demand(
        mem_conn,
        demand_id=demand_id,
        title=title,
        content=content,
        status=status,
        priority=priority,
        project_id=project_id,
        origin_idea_id=origin_idea_id,
        source_ref=source_ref,
        created_at=now,
        updated_at=now,
        # 建时即 done（历史需求补录）也要落 resolved_at，维持状态不变式
        resolved_at=now if status == "done" else None,
    )
    row = demand_dao.get_demand(mem_conn, demand_id)
    if row is None:  # 理论不可达：插入成功后立即读不到说明底层异常
        return OperationResult.fail(message=f"需求创建后读取失败: {demand_id}")
    data = _to_item(row)
    warnings = _warnings_for(status, project_id)
    unknown = _check_project(mem_conn, project_id)
    if unknown:
        warnings.append(unknown)
    data["warnings"] = warnings
    return OperationResult.succeed(data)


# ---------- 操作：编辑 ----------

def update_demand(
    mem_conn: sqlite3.Connection,
    demand_id: str,
    body: dict[str, Any] | None = None,
) -> OperationResult:
    """编辑需求（PATCH：只改传入字段）。

    可改字段：title / content / priority / project_id / source_ref。
    ``project_id: null`` 表示解绑项目；未传该键则保持原值。
    **status 不在此端点**——状态流转有 resolved_at 联动，走 PUT /status 单一出口。

    Raises:
        InvalidArgs: 无可改字段、字段非法（含试图在此改 status）、软校验失败。
    """
    did = _parse_text(demand_id, "demand_id", required=True) or ""
    b = body or {}
    if not isinstance(b, dict):
        raise InvalidArgs("请求体必须是 JSON 对象")

    if "status" in b:
        raise InvalidArgs("status 不可经 PATCH 修改，请用 PUT /v1/admin/demands/{id}/status")
    unknown = sorted(set(b) - set(demand_dao.EDITABLE_FIELDS))
    if unknown:
        raise InvalidArgs(
            f"未知字段: {unknown}（可改: {list(demand_dao.EDITABLE_FIELDS)}）"
        )

    existing = demand_dao.get_demand(mem_conn, did)
    if existing is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"需求不存在: {did}")

    changes: dict[str, Any] = {}
    if "title" in b:
        changes["title"] = _parse_text(b["title"], "title", required=True)
    if "content" in b:
        changes["content"] = _parse_text(b["content"], "content") or ""
    if "priority" in b:
        if b["priority"] is None:
            raise InvalidArgs("priority 不可为 null（0-100 整数）")
        changes["priority"] = _parse_priority(b["priority"])
    if "project_id" in b:
        changes["project_id"] = _parse_nullable_ref(b["project_id"], "project_id")
    if "source_ref" in b:
        changes["source_ref"] = _parse_nullable_ref(b["source_ref"], "source_ref")

    if not changes:
        raise InvalidArgs(
            f"未提供任何可更新字段（可改: {list(demand_dao.EDITABLE_FIELDS)}）"
        )

    demand_dao.update_demand_fields(mem_conn, did, fields=changes, updated_at=_now_iso())
    row = demand_dao.get_demand(mem_conn, did)
    if row is None:  # 理论不可达
        return OperationResult.fail(ERR_NOT_FOUND, f"需求不存在: {did}")
    data = _to_item(row)
    data["updated_fields"] = sorted(changes)
    warnings = _warnings_for(row.get("status") or "", row.get("project_id"))
    unknown = _check_project(mem_conn, row.get("project_id"))
    if unknown:
        warnings.append(unknown)
    data["warnings"] = warnings
    return OperationResult.succeed(data)


# ---------- 操作：状态流转（核心） ----------

def set_demand_status(
    mem_conn: sqlite3.Connection,
    demand_id: str,
    body: dict[str, Any] | None = None,
) -> OperationResult:
    """需求状态流转（pending / planned / partial / done）。

    规则（本任务核心验收点）：
    - 状态值必须在四态枚举内，否则 400 ERR_INVALID_ARGS；
    - **不限制流转方向**（判断题④）：四态间任意迁移合法，含 done→pending 回退。
      设计文档未规定方向约束，且 §2 明确"越上游越不设限"——需求池是第②层，
      现实中需求被重开（范围扩大 / 方案回退）是常态，禁止回退等于逼用户造新条目、
      切断溯源链。非法的是**状态值**，不是路径；
    - 进入 done 落 ``resolved_at``；转出 done **清空** ``resolved_at``（判断题②）：
      resolved_at 定义是"状态=done 时刻"，属当前状态的派生属性而非事件日志。
      若保留旧值，不变式 ``resolved_at IS NOT NULL ⟺ status='done'`` 被破坏，
      任何"本期解决了多少需求"的区间统计都会把已重开的需求算进去。
      流转历史需要审计的话，应另建 demand_events 审计表，而不是靠一个过期时间戳兼职；
    - done→done 视为幂等重申，保留原 resolved_at（不刷新为当前时刻）；
    - 每次调用都刷新 ``updated_at``（含同态重申），``changed`` 标识状态值是否真的变化。

    Body：``{"status": "planned"}``。

    Raises:
        InvalidArgs: status 缺失或非法。
    """
    did = _parse_text(demand_id, "demand_id", required=True) or ""
    b = body or {}
    if not isinstance(b, dict):
        raise InvalidArgs("请求体必须是 JSON 对象")
    if "status" not in b:
        raise InvalidArgs(f"缺少 status（合法值: {list(DEMAND_STATUSES)}）")
    unknown = sorted(set(b) - {"status"})
    if unknown:
        raise InvalidArgs(f"未知字段: {unknown}（本端点只接受 status）")

    new_status = _parse_status(b["status"])

    existing = demand_dao.get_demand(mem_conn, did)
    if existing is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"需求不存在: {did}")

    previous_status = existing.get("status") or ""
    now = _now_iso()
    if new_status == "done":
        # 已经是 done → 幂等重申，保留首次解决时刻；否则记录本次解决时刻
        resolved_at = existing.get("resolved_at") if previous_status == "done" else now
    else:
        resolved_at = None  # 转出 done：清空，维持状态不变式

    demand_dao.update_demand_status(
        mem_conn, did, status=new_status, resolved_at=resolved_at, updated_at=now,
    )
    row = demand_dao.get_demand(mem_conn, did)
    if row is None:  # 理论不可达
        return OperationResult.fail(ERR_NOT_FOUND, f"需求不存在: {did}")

    data = _to_item(row)
    data["previous_status"] = previous_status
    data["previous_status_label"] = STATUS_LABELS.get(previous_status, previous_status)
    data["changed"] = previous_status != new_status
    data["warnings"] = _warnings_for(new_status, row.get("project_id"))
    return OperationResult.succeed(data)
