"""operations/memory.py：单条记忆操作（v0.7 §7，多操作模块）。

本模块是「一个模块承载多个操作」的第一例——正是 ``operations/__init__.py``
「导入规范」理由 1 所说的场景：``get_memory`` / ``reject_memory`` /
``unreject_memory`` 三个操作无法坍缩成单个 ``operations.memory()``，
因此包级**不做扁平导出**，调用方一律 ``from sgme.operations.memory import <op>``。

三段式结构（照抄 health.py 样板）：
1. 常量/私有工具（本模块内聚）
2. ``xxx(...) -> OperationResult`` 操作函数：返回协议无关的信息超集
3. ``*_payload(...)`` 投影函数：把超集裁剪成各入口的历史契约形态

承接的入口
----------
========================================  ===================================
入口                                       操作
========================================  ===================================
HTTP ``GET  /v1/memory/{memory_id}``       ``get_memory`` + ``get_http_payload``
HTTP ``POST /v1/memory/{id}/reject``       ``reject_memory``（无投影，data 即响应）
HTTP ``POST /v1/memory/{id}/unreject``     ``unreject_memory``（无投影，data 即响应）
MCP  ``memory_get``                        ``get_memory`` + ``get_mcp_payload``
MCP  ``memory_reject``                     ``reject_memory``（无投影，data 即响应）
========================================  ===================================

⚠️ 历史契约差异（v0.8 待统一，现在**不得**合并）
--------------------------------------------------
``get_memory`` 的两个入口形态完全不同：

- HTTP：``{"memory": {...}, "sources": [...], "archive_chain": [...]}``
  —— 三键包裹体，``sources`` 是 ``memory.sources`` 的**冗余提升**（两处同值）。
- MCP：``dict(memory)`` **裸记忆对象**（内含 tags/sources），无包裹、无归档链。

W06（2026-11-08，契约《SGME2.0-上下文归属与因果边-v0.1》§5）纯增量：
- 记忆对象顶层补 ``facts``（2.0 起取 ``l2_facts_json.facts``，此前恒空；1.x 三元组回落）
  与 ``causes``（``l2_facts_json.causes``）；
- 两个入口都新增 ``context``（``main_event`` / ``facts`` / ``causes`` /
  ``parent_memory_id`` / ``edges``）——HTTP 挂响应顶层，MCP 挂裸对象；
- 旧字段一个不删、语义不变（``memory`` / ``sources`` / ``archive_chain`` / ``pages``）。

失败文案也不同：HTTP 是 ``"记忆不存在: {memory_id}"``（带 id），
MCP 是固定串 ``"记忆不存在"``（不带 id）。差异全部收敛在投影函数里
（``get_http_payload`` / ``get_mcp_payload`` / ``get_mcp_error_payload``），
操作函数本身**不做 if-else 分流**。

副作用差异说明：MCP 的 v0.6 实现不查归档链，本模块统一查（多一次
``get_archive_chain`` 轻量 SELECT）。这不改变 MCP 响应（``get_mcp_payload``
把归档链裁掉），只为让操作层返回单一信息超集，符合 §7.2 设计。

依赖：只调 ``sgme.data.memory_dao``（v0.7 期 storage 尚未更名 data，
禁止提前引用 ``sgme.data``）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from sgme.operations.errors import (
    ERR_FORBIDDEN,
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    OperationResult,
)
from sgme.data import edge_dao, facts_dao, memory_dao
from sgme.data.search import parse_l2_facts_json

logger = logging.getLogger("sgme.operations.memory")

# 「不采用」的缺省理由：v0.6 写在 routes_memory.reject_memory 内联表达式里
# （``(body or {}).get("reason") or "用户纠错"``），v0.7 收敛到此单点常量。
DEFAULT_REJECT_REASON: str = "用户纠错"

# D-44 / D-54（2026-11-04）：删除两态。
DELETE_MODE_LOGICAL = "logical"
DELETE_MODE_PURGE = "purge"
LOGICAL_DELETE_REASON = "user_delete"
PURGE_CONFIRM_PHRASE = "彻底删除"
# 结果页必须诚实列出「删不干净的副本」（D-44 ③）
PURGE_REMAINING_COPIES: tuple[str, ...] = (
    "本机备份快照（增量最长保留 30 天；可用 --purge-backups 二次确认后清理）",
    "L0 原件（原件永不删；段落 redact 需人工执行，本次未改动）",
)

# MCP ``memory_get`` 的历史「不存在」文案：**固定串、不带 memory_id**。
# 与 HTTP 的 ``记忆不存在: {id}`` 是既有差异，v0.8 统一后删除本常量。
MCP_GET_NOT_FOUND_MESSAGE: str = "记忆不存在"

# ---------- W06（W01 契约 §5）：事件上下文读侧 ----------
# 进入 context.edges 的关系白名单：只暴露「归属边 + 因果边」。
# similar / contradicts / supersedes / evolves_from 属图召回语义，不进详情面板。
CONTEXT_RELATIONS: tuple[str, ...] = ("belongs_to", "causes")
# 归属边（侧事实 → 主事件）的唯一合法 source（W01 §1/§3；system 场景共现边不算父子）。
L2_EDGE_SOURCE: str = "l2_extract"
# 过渡兜底：list_edges_for_memory 缺失时全表扫描的条数上限（正常路径不走这里）。
CONTEXT_EDGE_SCAN_LIMIT: int = 50_000


def _not_found(memory_id: str) -> OperationResult:
    """构造「记忆不存在」失败结果（HTTP 历史文案为规范形态）。

    ``details`` 刻意留空：v0.6 的 404 响应体只有 ``code``/``message`` 两键，
    入口层 ``api_error`` 一旦收到 details 就会多渲染一个 ``details`` 键，
    破坏「响应逐字节等价」硬约束。MCP 侧的差异文案由
    ``get_mcp_error_payload`` 投影还原，不走 details 通道。
    """
    return OperationResult.fail(ERR_NOT_FOUND, f"记忆不存在: {memory_id}")


# ---------- 事件上下文装配（W06 / W01 §5） ----------

def empty_context() -> dict[str, Any]:
    """「事件上下文」的空形态（无数据也返回同一结构，UI 不制造空面板差异）。"""
    return {
        "main_event": None,
        "facts": [],
        "causes": [],
        "parent_memory_id": None,
        "edges": [],
    }


def _context_edge_rows(mem_conn: sqlite3.Connection, memory_id: str) -> list[dict]:
    """取一条记忆的 belongs_to / causes 边（**双向**：from_id 或 to_id 命中）。

    数据访问走 ``edge_dao.list_edges_for_memory``（W06 与 backend-edges 约定的
    只读 helper）。极端情况下 helper 缺失（旧版 data 层）退化为全表扫描——
    正确性不变，只是 O(全表) 的过渡路径，正常不会走到。
    """
    if not memory_id:
        return []
    lister = getattr(edge_dao, "list_edges_for_memory", None)
    if callable(lister):
        rows = lister(mem_conn, memory_id, relations=CONTEXT_RELATIONS)
    else:
        logger.warning(
            "edge_dao.list_edges_for_memory 缺失，context 边退化为全表扫描（过渡路径）"
        )
        rows = [
            r for r in edge_dao.list_edges(mem_conn, limit=CONTEXT_EDGE_SCAN_LIMIT)
            if r.get("from_id") == memory_id or r.get("to_id") == memory_id
        ]
    # 双保险过滤：关系 + source 语义 + 确实与本条记忆相连。
    # belongs_to 只认 l2_extract——backfill 的场景共现 belongs_to（source='system'）
    # 是结构联想而非父子归属，进上下文面板会把「同场景」误导成「归属」；
    # causes 不限 source（l2_extract / l1_conflict / llm 的因果判定都是因果）。
    return [
        r for r in rows
        if _is_context_edge(r)
        and (r.get("from_id") == memory_id or r.get("to_id") == memory_id)
    ]


def _is_context_edge(row: dict) -> bool:
    """边是否属于「事件上下文」：causes（任意 source）+ l2_extract 的 belongs_to。"""
    relation = row.get("relation")
    if relation == "causes":
        return True
    return relation == "belongs_to" and (row.get("source") or "") == L2_EDGE_SOURCE


def _edge_projection(row: dict) -> dict[str, Any]:
    """边行 → 契约形态（只保留 §5 声明的五键；weight 为原始值，不做关系级缩放）。"""
    return {
        "from_id": row.get("from_id"),
        "to_id": row.get("to_id"),
        "relation": row.get("relation"),
        "weight": float(row.get("weight") or 0.0),
        "source": row.get("source") or "",
    }


def _resolve_parent_memory_id(edges: list[dict], memory_id: str) -> str | None:
    """从归属边解析父（主事件）id。

    方向约定（W01 §3 rule 3）：``side --belongs_to--> main``，即当前记忆作
    ``from_id`` 的那条边，``to_id`` 才是父。反向的 belongs_to 只说明「我是别人的
    主事件」，不能当父边。source 必须是 ``l2_extract``——backfill 的场景共现
    belongs_to（source='system'）是结构联想，不是父子归属。
    """
    for e in edges:
        if (
            e["relation"] == "belongs_to"
            and e["source"] == L2_EDGE_SOURCE
            and e["from_id"] == memory_id
        ):
            return e["to_id"]
    return None


def _top_level_facts_causes(memory: dict) -> tuple[list[dict], list[dict]]:
    """记忆对象顶层 facts / causes（W06 修复「顶层 facts 恒空」）。

    - 2.0 解析链把单元事实只写 ``l2_facts_json``（``facts_json`` 三元组列在 2.0
      路径上恒空）→ 优先取 ``l2_facts_json.facts``；
    - 1.x 存量记忆没有 l2 列 → 回落 ``facts_json`` 三元组，旧语义不变；
    - ``causes`` 为纯新增键（l2 无数据 → []）。
    """
    l2 = parse_l2_facts_json(memory.get("l2_facts_json"))
    facts = l2["facts"] or facts_dao.parse_facts_json(memory.get("facts_json"))
    return facts, l2["causes"]


def build_context(mem_conn: sqlite3.Connection, memory: dict) -> dict[str, Any]:
    """装配 ``context``（主事件 / 事实 / 因果 / 父 id / 关联边）。

    语义要点：
    - ``edges``：仅 belongs_to + causes，双向；每行五键（from_id/to_id/relation/
      weight/source）；
    - ``parent_memory_id`` / ``main_event``：只认 ``l2_extract`` 的归属边，父记忆
      行取不到（已清理）时保留 id、``main_event`` 为 null（不猜正文）；
    - 侧事实自身没有 ``l2_facts_json``（整单元的 facts/causes 只写主干）→ 事实/因果
      为空时回流到主干，否则拆出的侧事实永远只剩裸 content（正是本批要修的现象）。
    """
    memory_id = memory.get("memory_id") or ""
    l2 = parse_l2_facts_json(memory.get("l2_facts_json"))
    edges = [_edge_projection(r) for r in _context_edge_rows(mem_conn, memory_id)]
    parent_id = _resolve_parent_memory_id(edges, memory_id)
    main_event: dict[str, Any] | None = None
    facts = list(l2["facts"])
    causes = list(l2["causes"])
    if parent_id:
        parent = memory_dao.get_memory(mem_conn, parent_id)
        if parent:
            main_event = {"memory_id": parent_id, "content": parent.get("content")}
            if not facts or not causes:
                parent_l2 = parse_l2_facts_json(parent.get("l2_facts_json"))
                if not facts:
                    facts = list(parent_l2["facts"])
                if not causes:
                    causes = list(parent_l2["causes"])
    return {
        "main_event": main_event,
        "facts": facts,
        "causes": causes,
        "parent_memory_id": parent_id,
        "edges": edges,
    }


# ---------- 操作 1：读取单条记忆 ----------

def get_memory(mem_conn: sqlite3.Connection, memory_id: str) -> OperationResult:
    """读取单条记忆 + 溯源 + 归档链。

    刻意**不校验** ``memory_id`` 非空：v0.6 两个入口都未校验，
    空串会走到「记忆不存在」分支。加校验会把 MCP 的失败文案从
    ``记忆不存在`` 改成参数错误文案，破坏契约等价。

    Args:
        mem_conn: memory.db 连接。
        memory_id: 记忆 id。

    Returns:
        - 成功：``OperationResult(ok=True)``，data 为信息超集
          ``{"memory": {...}, "archive_chain": [...], "pages": [...], "context": {...}}``。
        - 不存在：``OperationResult(ok=False, error_code=ERR_NOT_FOUND)``。

        sqlite 故障等非预期异常**原样上抛**（不加 catch-all），
        由入口层全局异常处理器兜底——与 v0.6 行为一致。
    """
    mem = memory_dao.get_memory(mem_conn, memory_id)
    if not mem:
        return _not_found(memory_id)
    archive_chain = memory_dao.get_archive_chain(mem_conn, memory_id)
    # 关联页/技能（memory_pages）：技术细节沉淀在知识库/技能库，记忆只留索引。
    # WebUI 记忆详情页需要它展示「详情见 …」；此前只有 DAO 没有端点暴露。
    pages = memory_dao.get_memory_pages(mem_conn, memory_id)
    # W06：顶层 facts/causes（2.0 起 facts 取 l2_facts_json，修复此前恒空）
    mem["facts"], mem["causes"] = _top_level_facts_causes(mem)
    # W06：事件上下文（主事件 / 事实 / 因果 / 父边 / 关联边）
    context = build_context(mem_conn, mem)
    return OperationResult.succeed(
        {
            "memory": mem,
            "archive_chain": archive_chain,
            "pages": pages,
            "context": context,
        }
    )


def get_http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 HTTP ``GET /v1/memory/{id}`` 的历史契约形态（v0.6 逐字段等价）。

    ``sources`` 与 ``memory["sources"]`` 同值冗余——v0.6 如此，保持不变。
    """
    memory: dict[str, Any] = data["memory"]
    return {
        "memory": memory,
        "sources": memory.get("sources", []),
        "archive_chain": data["archive_chain"],
        # 2.0：关联页/技能索引（memory_pages）——WebUI 详情页渲染「详情见 …」
        "pages": data.get("pages", []),
        # W06：事件上下文（主事件/事实/因果/父边/关联边）——纯增量键，
        # 老客户端忽略未知键即可（§5「旧客户端字段不删不改语义」）
        "context": data.get("context") or empty_context(),
    }


def get_mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``memory_get`` 的历史契约形态（v0.6 逐字段等价）。

    v0.6 返回 ``json.dumps(dict(m))`` —— 裸记忆对象的浅拷贝，
    无包裹层、无 archive_chain。此处照抄 ``dict(...)`` 浅拷贝语义，
    避免调用方改动泄漏回操作层的 data。

    W06：``context`` 直接挂裸对象（MCP 无包裹层，contract §5 要求 MCP 也能读到）；
    其余键（含记忆对象顶层 facts/causes）原样透传。
    """
    body = dict(data["memory"])
    body["context"] = data.get("context") or empty_context()
    return body


def get_mcp_error_payload(err: dict[str, Any]) -> dict[str, Any]:
    """投影为 MCP ``memory_get`` 的历史 error 形态（v0.6 逐字节等价）。

    ``get_memory`` 唯一的可预期失败是「记忆不存在」（其余异常原样上抛、
    不经本函数），因此这里整体折叠为固定文案，不做字符串匹配式还原。

    Args:
        err: ``_op_json`` 产出的失败字典（``{"error": "记忆不存在: xxx"}``）。
            入参保留是为签名与其它投影函数一致、且便于将来出现第二种失败码时扩展。

    Returns:
        ``{"error": "记忆不存在"}``——MCP 历史文案，不带 memory_id。
    """
    return {"error": MCP_GET_NOT_FOUND_MESSAGE}


# ---------- 操作 2：标记「不采用」 ----------

def reject_memory(
    mem_conn: sqlite3.Connection,
    memory_id: str,
    *,
    reason: str | None = None,
) -> OperationResult:
    """用户纠错「不采用」：标记记忆为 rejected（不删除、可恢复）。

    v0.6 语义逐行保留：
    1. 先 ``get_memory`` 判存在 → 不存在返 ERR_NOT_FOUND（**不是**靠 rowcount）；
    2. reason 空值（None / 空串）回落到 ``DEFAULT_REJECT_REASON``；
    3. DAO 返回 False（存在但更新 0 行，罕见竞态）→ ERR_INTERNAL「标记失败」；
    4. 幂等：重复 reject 更新 reason。

    HTTP 与 MCP 两入口共享同一响应形态（``memory_id``/``status``/``reject_reason``），
    故不设投影函数——data 即响应体（见 ``operations/__init__.py``：
    两端形态一致时不写投影函数）。

    Args:
        mem_conn: memory.db 连接。
        memory_id: 记忆 id。
        reason: 纠错原因；None / 空串回落缺省值。

    Returns:
        成功时 data 为 ``{"memory_id", "status", "reject_reason"}``（键序即 v0.6 响应序）。
    """
    mem = memory_dao.get_memory(mem_conn, memory_id)
    if not mem:
        return _not_found(memory_id)
    final_reason: str = reason or DEFAULT_REJECT_REASON
    ok: bool = memory_dao.reject_memory(mem_conn, memory_id, final_reason)
    if not ok:
        return OperationResult.fail(ERR_INTERNAL, "标记失败")
    return OperationResult.succeed({
        "memory_id": memory_id,
        "status": "rejected",
        "reject_reason": final_reason,
    })


# ---------- 操作 3：撤销「不采用」 ----------

def unreject_memory(mem_conn: sqlite3.Connection, memory_id: str) -> OperationResult:
    """撤销「不采用」：恢复为 active（rejected 误操作时用）。

    v0.6 语义逐行保留：**不预查存在性**，直接按 DAO 的 rowcount 判定——
    更新 0 行即视为「记忆不存在」（ERR_NOT_FOUND）。这与 reject 的
    「先查后改」不对称，是既有实现的真实形态，抽取时不得"顺手对齐"。

    本操作**无 MCP 入口**，故不设投影函数——data 即 HTTP 响应体。

    Args:
        mem_conn: memory.db 连接。
        memory_id: 记忆 id。

    Returns:
        成功时 data 为 ``{"memory_id", "status"}``（键序即 v0.6 响应序）。
    """
    ok: bool = memory_dao.unreject_memory(mem_conn, memory_id)
    if not ok:
        return _not_found(memory_id)
    return OperationResult.succeed({"memory_id": memory_id, "status": "active"})


# ---------- 操作 4：删除两态（D-44 / D-54） ----------

def delete_memory(
    mem_conn: sqlite3.Connection,
    memory_id: str,
    *,
    mode: str = DELETE_MODE_LOGICAL,
    reason: str | None = None,
    triggered_by: str = "user",
    agent_id: str | None = None,
    admin: bool = False,
    confirm_phrase: str | None = None,
) -> OperationResult:
    """删除两态（D-44）：logical（默认，可恢复）/ purge（不可恢复）。

    纪律（D-54）：
    - agent 只能 logical；purge 必须 admin，且入口层要求用户在现场确认 →
      操作层同时校验 admin 与确认词「彻底删除」，两道都过才执行；
    - SGME 内部任何自动动作（TTL/归档/梦境/保留策略）永不调用本函数；
    - 逻辑删除：status='rejected' + reject_reason='user_delete'，数据完整保留；
    - 彻底删除：物理清主表/归档/子表/边，审计只留删除事件 + 删除前快照
      （deletion_audit），结果带「剩余副本」清单（备份/L0 原件）。
    """
    if mode not in (DELETE_MODE_LOGICAL, DELETE_MODE_PURGE):
        return OperationResult.fail(
            ERR_INVALID_ARGS, f"非法 mode: {mode!r}（只允许 logical / purge）"
        )
    row = memory_dao.get_memory(mem_conn, memory_id)
    if row is None:
        return _not_found(memory_id)

    if mode == DELETE_MODE_LOGICAL:
        final_reason = reason or LOGICAL_DELETE_REASON
        if row.get("status") == "rejected" and row.get("reject_reason") == final_reason:
            # 幂等：重复逻辑删除不再刷审计
            return OperationResult.succeed({
                "memory_id": memory_id,
                "status": "deleted",
                "mode": DELETE_MODE_LOGICAL,
                "recoverable": True,
            })
        ok = memory_dao.reject_memory(mem_conn, memory_id, final_reason)
        if not ok:
            return _not_found(memory_id)
        memory_dao.record_deletion_audit(
            mem_conn,
            memory_id=memory_id,
            mode=DELETE_MODE_LOGICAL,
            triggered_by=triggered_by,
            agent_id=agent_id,
            reason=final_reason,
        )
        return OperationResult.succeed({
            "memory_id": memory_id,
            "status": "deleted",
            "mode": DELETE_MODE_LOGICAL,
            "reason": final_reason,
            "recoverable": True,
        })

    # purge
    if not admin:
        return OperationResult.fail(
            ERR_FORBIDDEN,
            "彻底删除只能由管理员在 WebUI 现场确认后触发（agent 不得自行 purge）",
        )
    if (confirm_phrase or "").strip() != PURGE_CONFIRM_PHRASE:
        return OperationResult.fail(
            ERR_INVALID_ARGS,
            f"彻底删除需输入确认词「{PURGE_CONFIRM_PHRASE}」",
        )
    purged = memory_dao.purge_memory(
        mem_conn, memory_id,
        triggered_by=triggered_by, agent_id=agent_id, reason=reason,
    )
    if purged is None:
        return _not_found(memory_id)
    return OperationResult.succeed({
        "memory_id": memory_id,
        "status": "purged",
        "mode": DELETE_MODE_PURGE,
        "recoverable": False,
        "content_sha256": purged["content_sha256"],
        "remaining_copies": list(PURGE_REMAINING_COPIES),
    })
