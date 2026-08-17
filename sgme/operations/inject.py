"""operations/inject.py：记忆注入操作（v0.7 §7 operations 层样板模块之一）。

承接入口：
- HTTP ``POST /v1/inject``（routes_memory.inject_memories，Agent Key）
- MCP ``inject`` 工具（mcp_server.inject，当前仅 mode 分支，无 custom_filter）

两分支语义（与 v0.6 路由逐行等价，抽取时不得改动任何字段与文案）：
1. ``mode`` 分支：``template.load_template(mode, dimensions)`` → 逐 section
   ``profile.inject.query_section``（纯 SQL 零 LLM）→ ``build_inject_blocks``
   → 响应附加 tier0 字段 ``{"present": bool, "content": 摘要或 None}``（契约 4.2）。
2. ``custom_filter`` 分支：自定义过滤查询（dimensions/match/limit，
   先校验维度 id 已注册）→ 拼装为单个 section → ``build_inject_blocks``
   → tier0 字段 + ``stats.mode="custom"``。

异常翻译（照 v0.6 路由，逐条一致）：
- ``template.TemplateError`` → ``fail(ERR_INVALID_ARGS, "模板加载失败: {e}")``
- custom_filter 未指定 dimensions → ``fail(ERR_INVALID_ARGS, "custom_filter 需指定 dimensions")``
- custom_filter 含未注册维度 id → ``fail(ERR_INVALID_ARGS, "未注册的维度 id: {d}")``
- mode 与 custom_filter 均未指定 → ``fail(ERR_INVALID_ARGS, "需指定 mode 或 custom_filter")``
- 其余意外异常（pipeline/DAO 层）→ ``fail(ERR_INTERNAL, ...)``

依赖：只调 profile.template / profile.inject / profile.tier0 / storage.memory_dao
（业务层，全部只读；engine 是禁区）。副作用：无——inject 不写库、不调 LLM、不发信号。

投影函数说明：HTTP 与 MCP 两端对注入响应的历史契约形态**本就一致**
（路由原样返回 response，MCP 直接 json.dumps(response)），因此按
operations/__init__.py 约定**不写** http_payload / mcp_payload，两端直接使用 ``data``。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from sgme.operations.errors import ERR_INTERNAL, ERR_INVALID_ARGS, OperationResult
from sgme.profile import inject as inject_mod
from sgme.profile import template as template_mod
from sgme.profile import tier0 as tier0_mod
from sgme.data import memory_dao


def _attach_key_missing_note(response: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """模型 Key 缺失引导（T-53）：提炼/向量端点缺 Key 时在 stats.note 附申请提醒。

    只在缺失时附加（缺失=降级，提醒有行动价值）；Key 齐全零噪音。
    复用 llm.model_keys_notice（只读 os.environ，无副作用）。
    """
    from sgme.operations.llm import model_keys_notice

    notice = model_keys_notice(cfg)
    if notice:
        current = response.get("stats", {}).get("note", "")
        response["stats"]["note"] = (current + "\n" + notice).strip()
    return response


def _attach_empty_note(response: dict[str, Any]) -> dict[str, Any]:
    """空结果引导（ST-22④）：所有 block 均无 items 时在 ``stats.note`` 附加可行动提示。

    新手体验加固：注入返回全空 block 会让调用方误以为功能异常。
    附加中文引导（先 append 沉淀记忆 / 检查维度标签），不改变任何既有字段。
    """
    total = sum(len(b.get("items", [])) for b in response.get("blocks", []))
    if total == 0:
        response["stats"]["note"] = (
            "暂无相关记忆，注入结果为空：请先通过 POST /v1/append 记录会话，"
            "或检查维度标签是否已注册；记忆沉淀后将自动出现在注入结果中"
        )
    return response


def inject(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    mode: str | None = None,
    custom_filter: dict[str, Any] | None = None,
) -> OperationResult:
    """记忆注入：mode 模板查询 或 custom_filter 自定义查询 → blocks + tier0。

    签名刻意**只收业务依赖**：operations 层不认识 ``request.app.state``
    或 mcp 的 ``_app_state``（那是入口层的协议细节），由入口层取出后显式传入。
    mode 与 custom_filter 二选一，mode 优先（照 v0.6 路由的判断顺序）。

    Args:
        mem_conn: memory.db 连接（只读查询）。
        cfg: 运行时配置（``dimensions`` 维度注册表）。
        mode: 模式模板名（``templates/{mode}.yaml``，不含扩展名）。
        custom_filter: 自定义过滤条件 dict，可含
            ``dimensions``/``memory_types``（二选一）、``match``（默认 any）、
            ``limit``（默认 10）。

    Returns:
        OperationResult(ok=True)，``data`` 为注入响应（HTTP/MCP 共用形态）：
        - blocks: 注入块列表（Tier0 摘要块 + 各 section 块，含 present 标注）
        - stats: {mode, queries, tokens_est, tier0_present}
        - tier0: {"present": bool, "content": 摘要或 None}（契约 4.2）

        失败态（错误码与文案照 v0.6 路由）：
        - 模板加载失败（TemplateError）→ ERR_INVALID_ARGS「模板加载失败: {e}」
        - custom_filter 缺 dimensions / 含未注册维度 → ERR_INVALID_ARGS
        - 未指定 mode 与 custom_filter → ERR_INVALID_ARGS
        - 其余意外异常（pipeline/DAO 层）→ ERR_INTERNAL
    """
    dimensions: list[dict[str, Any]] = cfg["dimensions"]

    try:
        # ---------- 分支 1：mode 模板注入 ----------
        if mode:
            try:
                tmpl = template_mod.load_template(mode, dimensions)
            except template_mod.TemplateError as e:
                return OperationResult.fail(ERR_INVALID_ARGS, f"模板加载失败: {e}")
            section_results = [
                inject_mod.query_section(mem_conn, s, dimensions)
                for s in tmpl.get("sections", [])
            ]
            tier0_summary = tier0_mod.load_summary()
            response = inject_mod.build_inject_blocks(
                tmpl, section_results, tier0_summary=tier0_summary,
            )
            # 契约 4.2：tier0 字段
            response["tier0"] = {
                "present": tier0_summary is not None,
                "content": tier0_summary,
            }
            return OperationResult.succeed(_attach_key_missing_note(_attach_empty_note(response), cfg))

        # ---------- 分支 2：custom_filter 自定义查询 ----------
        if custom_filter:
            cf = custom_filter
            dims = cf.get("dimensions") or cf.get("memory_types") or []
            if not dims:
                return OperationResult.fail(
                    ERR_INVALID_ARGS, "custom_filter 需指定 dimensions",
                )
            # 校验维度 id 已注册
            registered = {d["id"] for d in dimensions}
            for d in dims:
                if d not in registered:
                    return OperationResult.fail(
                        ERR_INVALID_ARGS, f"未注册的维度 id: {d}",
                    )
            match = cf.get("match", "any")
            limit = cf.get("limit", 10)
            results = memory_dao.list_memories_by_dimension(
                mem_conn, dims, match=match, limit=limit,
                include_expired=False,
            )
            # 记录注入统计（best-effort）
            from sgme.data.memory_stats_dao import record_inject
            for r in results:
                record_inject(mem_conn, r["memory_id"])
            # 拼装为单个 section
            tmpl = {
                "name": "custom",
                "sections": [{"title": "自定义查询", "query": {"dimensions": dims}}],
            }
            tier0_summary = tier0_mod.load_summary()
            response = inject_mod.build_inject_blocks(
                tmpl, [results], tier0_summary=tier0_summary,
            )
            response["tier0"] = {
                "present": tier0_summary is not None,
                "content": tier0_summary,
            }
            response["stats"]["mode"] = "custom"
            return OperationResult.succeed(_attach_key_missing_note(_attach_empty_note(response), cfg))

        # 两个分支都未指定 → 参数非法（照 v0.6 路由兜底文案）
        return OperationResult.fail(ERR_INVALID_ARGS, "需指定 mode 或 custom_filter")
    except Exception as e:
        # 意外异常（pipeline/DAO 层）→ ERR_INTERNAL，照 v0.6 路由 api_error 兜底
        return OperationResult.fail(ERR_INTERNAL, f"注入失败: {e}")
