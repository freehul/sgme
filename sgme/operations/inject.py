"""operations/inject.py：记忆注入操作（v0.7 §7 operations 层样板模块之一）。

承接入口：
- HTTP ``POST /v1/inject``（routes_memory.inject_memories，Agent Key）
- MCP ``inject`` 工具（mcp_server.inject，当前仅 mode 分支，无 custom_filter）

两分支语义（与 v0.6 路由逐行等价，抽取时不得改动任何字段与文案）：
1. ``mode`` 分支：``template.load_template(mode, dimensions)`` → 逐 section
   ``profile.inject.query_section``（纯 SQL 零 LLM）→ ``build_inject_blocks``。
   （Tier0 摘要字段已于 D-79 整体砍除，2026-11-05；画像由 core 记忆卡承担）
2. ``custom_filter`` 分支：自定义过滤查询（dimensions/match/limit，
   先校验维度 id 已注册）→ 拼装为单个 section → ``build_inject_blocks``
   → ``stats.mode="custom"``。

异常翻译（照 v0.6 路由，逐条一致；T-120 起新增 mode 缺省回落）：
- ``template.TemplateError`` → ``fail(ERR_INVALID_ARGS, "模板加载失败: {e}")``
- custom_filter 未指定 dimensions → ``fail(ERR_INVALID_ARGS, "custom_filter 需指定 dimensions")``
- custom_filter 含未注册维度 id → ``fail(ERR_INVALID_ARGS, "未注册的维度 id: {d}")``
- mode 与 custom_filter 均未指定（T-120）→ 回落 ``DEFAULT_INJECT_MODE`` 注入，
  并在 ``stats.note`` 注明回落与可用模板清单；显式传空值（非缺省态）仍走
  ``fail(ERR_INVALID_ARGS, "需指定 mode 或 custom_filter")`` 兜底
- 其余意外异常（pipeline/DAO 层）→ ``fail(ERR_INTERNAL, ...)``

依赖：只调 profile.template / profile.inject / profile.core_cards / storage.memory_dao
（业务层，全部只读；engine 是禁区）。副作用：不调 LLM、不发信号；唯一写操作是
统计类（均 best-effort 全静默，失败不影响注入）——既有 memory_stats.record_inject
（注入计数 sidecar，原样保留）+ T-252 记忆调用统计 ``memory_usage_daily``
（source='inject'，A2 政策放宽仅限本表，2026-10-10 用户批）。

D-72（2.0 对话冷场修复接线）：``inject`` 增加**可选** ``history`` / ``already_repaired``。
调用方传入会话历史时，先 ``engine.conversation_state.assess`` 判冷场，
``should_repair`` 放行则在 ``blocks`` 末尾追加一条修复提示块
（``kind="coldstart_repair"``，带 ``reason`` / ``signals`` 供调用方识别）。
不传 history → 响应与接线前逐字段一致（向后兼容）；engine 在此只用于纯函数判断，
不读写任何库。

D-49（2.0 注入预算门控，2026-11-05）：inject 增加**可选** max_tokens——
显式传入且 stats.tokens_est 超限时，按既定优先级 core > 模板 section >
persona_block 裁剪（core 永不裁、persona 先丢、section 从最后一个块尾部丢 item），
裁后重算 tokens_est 并把 budget_* 字段写进 stats；不传（None）→ 行为完全不变。

E7（2026-11-05，去重计量）：跨 section 去重 _dedupe_sections 的丢弃条数不再丢弃——
写入 stats.dedupe_dropped 并打一行 INFO 日志，供去重前后 token 对比。

投影函数说明：HTTP 与 MCP 两端对注入响应的历史契约形态**本就一致**
（路由原样返回 response，MCP 直接 json.dumps(response)），因此按
operations/__init__.py 约定**不写** http_payload / mcp_payload，两端直接使用 ``data``。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from sgme.data import memory_dao
from sgme.engine import conversation_state
from sgme.operations.errors import ERR_INTERNAL, ERR_INVALID_ARGS, OperationResult
from sgme.operations.memory_usage import record_memory_usage
from sgme.profile import core_cards as core_cards_mod
from sgme.profile import inject as inject_mod
from sgme.profile import template as template_mod

# mode 缺省回落目标（T-120：2026-08-30 接入实测定，daily 为最常用画像注入模式）
DEFAULT_INJECT_MODE = "daily"

logger = logging.getLogger("sgme.operations.inject")


def _content_key(text: str | None) -> str:
    """内容归一化键：去空白与标点，用于注入去重。

    与 search 侧 seen_content 去重同口径（精确比对归一化后的正文）。
    不做近重复判定——注入是 zero-LLM 路径，语义去重放不进来。
    """
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def _dedupe_sections(section_results: list[list[dict]]) -> tuple[list[list[dict]], int]:
    """跨 section 内容去重：同一段内容只保留最靠前出现的那次。

    背景（2026-11-05 用户提出）：注入走模板查询——每个 section 一条独立 SQL。
    profile.inject.build_inject_blocks 已有同日层去重（2026-08-18：memory_id 判等 +
    content 前 50 字去尾部标点），但仍漏两类：①同内容不同 memory_id（跨维度重复落库）；
    ②前 50 字内部的空白/标点差异。本函数按**全文去空白与标点**归一化再补一层，
    覆盖 build_inject_blocks 判不出的同义重复；两层互补，不替代（search 侧
    data/search/__init__.py 的 seen_content 是同一归一化口径）。

    保序：先出现的保留——section 顺序即模板里定义的优先级顺序，前面的块更重要。
    返回 (去重后的 section_results, 被丢弃条数)。
    """
    seen: set[str] = set()
    out: list[list[dict]] = []
    dropped = 0
    for sr in section_results:
        keep: list[dict] = []
        for r in sr:
            key = _content_key(r.get("content"))
            if key and key in seen:
                dropped += 1
                continue
            if key:
                seen.add(key)
            keep.append(r)
        out.append(keep)
    return out, dropped


def _log_dedupe(dropped: int, kept: int, sections: int) -> None:
    """E7（2026-11-05）：注入去重计量——每次注入打一行 INFO，供前后对比。

    dropped 是跨 section 归一化去重丢弃条数，与 stats.dedupe_dropped 同口径；
    注入每轮一次、只多一行日志，INFO 级保留便于线上观察（此前 _dupe_dropped 被丢弃）。
    """
    logger.info(
        "注入去重：丢弃 %d 条 / 保留 %d 条（sections=%d，含跨 section 同文）",
        dropped, kept, sections,
    )


def _log_budget(response: dict[str, Any]) -> None:
    """D-49：预算裁剪/未达标时打一行 INFO（未触发不产日志，避免噪声）。"""
    stats = response.get("stats") or {}
    if stats.get("budget_trimmed") or stats.get("budget_unmet"):
        logger.info(
            "注入预算：max_tokens=%s, tokens %s→%s, persona_dropped=%s, "
            "dropped_items=%s, budget_unmet=%s",
            stats.get("budget_max_tokens"),
            stats.get("budget_tokens_before"),
            stats.get("tokens_est"),
            stats.get("persona_dropped"),
            stats.get("dropped_items"),
            stats.get("budget_unmet"),
        )


def _read_filters(cfg: dict[str, Any]) -> dict[str, bool]:
    """D-55 阶段 4（2026-11-04）：按 migration.read_mode 决定读口径过滤器。

    - new：2.0 读口径（生命周期谓词 / 排除锚点 / 排除有效期内到期）；
    - legacy / shadow：1.x 读口径（三项全关，锚点块不挂）。
    """
    from sgme.migration import new_read_enabled

    new_read = new_read_enabled(cfg)
    return {
        "lifecycle_active": new_read,
        "exclude_anchor": new_read,
        "exclude_valid_expired": new_read,
    }


def _attach_migration_blocks(
    response: dict[str, Any], mem_conn: sqlite3.Connection, cfg: dict[str, Any]
) -> dict[str, Any]:
    """D-55 阶段 4：锚点块按 read_mode 服务；shadow 只算差异不服务。"""
    from sgme.migration import new_read_enabled, read_mode, shadow_enabled

    if new_read_enabled(cfg):
        response = _attach_anchor_block(response, mem_conn, cfg)
    elif shadow_enabled(cfg):
        try:
            shadow = _attach_anchor_block({"blocks": [], "stats": {}}, mem_conn, cfg)
            items = sum(len(b.get("items") or []) for b in shadow.get("blocks", []))
            logger.info("migration shadow：锚点块将新增 %d 项（未服务）", items)
        except Exception:  # noqa: BLE001 - shadow 只观察，绝不阻塞
            logger.warning("migration shadow 计算失败（忽略）", exc_info=True)
    response.setdefault("stats", {})["read_mode"] = read_mode(cfg)
    return response


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


def _attach_fallback_note(response: dict[str, Any], mode: str) -> dict[str, Any]:
    """mode 缺省回落提示（T-120）：mode 未指定回落默认模式时在 ``stats.note`` 注明。

    报错自解释：附可用模板清单，调用方无需猜测可切换的模板名。
    与 ``_attach_key_missing_note`` 同款 append 语义，不覆盖既有 note；
    调用方须保证本函数在 ``_attach_empty_note`` 之后执行（后者会整体覆盖 note）。
    """
    notice = (
        f"mode 未指定，已回落 {mode}"
        f"（可用模板: {', '.join(template_mod.list_templates())}）"
    )
    current = response.get("stats", {}).get("note", "")
    response["stats"]["note"] = (current + "\n" + notice).strip()
    return response


def _attach_coldstart_repair(
    response: dict[str, Any],
    history: list[dict[str, Any]] | None,
    already_repaired: bool,
) -> dict[str, Any]:
    """D-72：对话冷场修复提示——冷场且未修补过时在 ``blocks`` 末尾附加一条。

    - history：会话消息列表（时间正序，``{role, content}``）；None/空 → 原样返回
    - already_repaired：调用方声明「本次冷场信号已修补过」→ 由修补闸拦下，不重复提示
    - 命中时附加块 ``{"title", "items", "present", "kind": "coldstart_repair",
      "reason", "signals"}``，``kind`` 是调用方识别修复提示的标记；
      位置在末尾，既有 block 顺序与时序不变
    - 脏数据容错：非 dict 条目跳过、缺失 content 视作空串（判冷场不因此抛异常）

    调用时机：必须在 ``_attach_empty_note`` 之后（空注入提示按**记忆条数**判断，
    修复块不是记忆，不能把「暂无相关记忆」抵掉）。
    """
    if not history or not isinstance(history, list):
        return response
    messages = [
        {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
        for m in history
        if isinstance(m, dict)
    ]
    state = conversation_state.assess(messages)
    if not conversation_state.should_repair(state, already_repaired):
        return response
    hint = conversation_state.repair_hint(state)
    if not hint:
        return response
    response.setdefault("blocks", []).append({
        "title": "对话冷场修复",
        "items": [{"content": hint}],
        "present": True,
        "kind": "coldstart_repair",
        "reason": state.get("reason", ""),
        "signals": state.get("signals", {}),
    })
    return response


def _attach_last_mentions(
    response: dict[str, Any],
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """D-70 记忆回指：在 blocks 末尾追加「上次提到的事」。

    - 开关 ``inject.last_mentions.enabled``（**默认 off**，用户 10-30 拍板首月关闭）
    - 取数规则见 ``profile/last_mentions.py``（focus/status、最近 7 天、
      排除遗忘请求与他人事实、预算截断）
    - 任何异常都跳过（回指是加分项，不能拖垮注入）
    """
    conf = ((cfg or {}).get("inject") or {}).get("last_mentions") or {}
    if not conf.get("enabled", False):
        return response
    try:
        from sgme.profile import last_mentions as lm_mod

        items = lm_mod.pick(
            mem_conn,
            limit=int(conf.get("limit", 2)),
            days=int(conf.get("days", 7)),
            char_budget=int(conf.get("char_budget", 70)),
        )
        if not items:
            return response
        response.setdefault("blocks", []).append({
            "title": "上次提到",
            "items": [
                {"content": f"{lm_mod._fmt_date(it.get('updated_at'))} {it['content']}"}
                for it in items
            ],
            "present": True,
            "kind": "last_mentions",
        })
    except Exception as e:  # noqa: BLE001——回指失败不影响注入
        logger.warning("last_mentions 附加失败（已忽略）: %s", e)
    return response


def _attach_anchor_block(
    response: dict[str, Any], mem_conn: sqlite3.Connection, cfg: dict[str, Any]
) -> dict[str, Any]:
    """D-52 档 2/3：追加长期锚点块（inject_tier='anchor'，独立预算，失败不阻塞）。"""
    anchor_cfg = (cfg.get("inject") or {}).get("anchor") or {}
    try:
        from sgme.profile.inject import build_anchor_block

        block = build_anchor_block(mem_conn, anchor_cfg)
    except Exception:  # noqa: BLE001 - 锚点块是兜底可见性，失败绝不能拖垮注入
        block = None
    if block is None:
        response.setdefault("stats", {})["anchor_present"] = False
        return response
    response.setdefault("blocks", []).append(block)
    stats = response.setdefault("stats", {})
    stats["anchor_present"] = True
    stats["anchor_items"] = len(block.get("items") or [])
    if block.get("note"):
        stats["anchor_note"] = block["note"]
    return response


def _attach_persona_block(
    response: dict[str, Any], mem_conn: sqlite3.Connection, cfg: dict[str, Any]
) -> dict[str, Any]:
    """D-48（2026-11-04）：追加「性格参考」块（四条件闸门，失败不阻塞注入）。

    此前该块只在 profile.inject.inject() 便捷函数里拼装，operations 主路径
    （HTTP/MCP 共用）从未调用——等于生产注入看不到性格参考。本函数补上接线，
    并强制走 persona_dao 的四条件闸门（多轮/跨天/多场景/来源可信）。
    """
    pcfg = cfg.get("persona") or {}
    if not (pcfg.get("inject_block") or {}).get("enabled", True):
        response.setdefault("stats", {})["persona_present"] = False
        return response
    try:
        from sgme.profile.persona_block import build_persona_block

        info = build_persona_block(mem_conn, gate=pcfg.get("gate"))
    except Exception:  # noqa: BLE001 - 性格参考是附加块，失败绝不能拖垮注入
        info = None
    if not info:
        response.setdefault("stats", {})["persona_present"] = False
        return response
    # D-49（2026-11-05）：块打 kind 标记 + 记 tokens——预算门控据此识别并整块丢弃
    # 本块；tokens 直接沿用 persona_block 的估算结果（每条 25），不在此另立公式。
    block = dict(info["block"])
    block["kind"] = "persona"
    response.setdefault("blocks", []).append(block)
    stats = response.setdefault("stats", {})
    stats["persona_present"] = True
    stats["persona_tokens"] = int(info.get("tokens", 0))
    stats["tokens_est"] = int(stats.get("tokens_est", 0)) + stats["persona_tokens"]
    return response


def inject(
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    mode: str | None = None,
    custom_filter: dict[str, Any] | None = None,
    caller: str | None = None,
    history: list[dict[str, Any]] | None = None,
    already_repaired: bool = False,
    max_tokens: int | None = None,
) -> OperationResult:
    """记忆注入：mode 模板查询 或 custom_filter 自定义查询 → blocks。

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
        caller: 调用方标识（T-252；入口层按鉴权 key 反查 agent_id），仅供
            记忆调用统计（source='inject'）记录使用，None → ``unknown``。
        history: D-72 会话历史（时间正序 ``{role, content}``）。传入时判冷场，
            冷场且未修补过则额外附加一条 ``kind="coldstart_repair"`` 的修复提示块。
            不传（None/空）→ 行为与接线前完全一致。
        already_repaired: D-72 修补闸入参——调用方已消费过本次冷场修复提示时传 True，
            避免同一信号重复占预算。
        max_tokens: D-49 注入预算（token 口径见 profile.inject.estimate_tokens）。
            显式传入且超限时按 core > 模板 section > persona_block 裁剪：core 永不裁，
            persona 先整块丢，section 从最后一个块尾部逐条丢 item；None（缺省）→
            不裁剪，行为与接线前完全一致（向后兼容）。非正数按 0 处理（只保留 core）。

    Returns:
        OperationResult(ok=True)，``data`` 为注入响应（HTTP/MCP 共用形态）：
        - blocks: 注入块列表（core 记忆卡块排最前，随后各 section 块，含 present 标注）
        - stats: {mode, queries, tokens_est, core_present, dedupe_dropped, ...}
          （传 max_tokens 时另有 budget_max_tokens / budget_tokens_before /
          budget_trimmed / persona_dropped / dropped_items / budget_unmet）
        - （Tier0 摘要字段已于 D-79 砍除，2026-11-05）

        失败态（错误码与文案照 v0.6 路由）：
        - 模板加载失败（TemplateError）→ ERR_INVALID_ARGS「模板加载失败: {e}」
        - custom_filter 缺 dimensions / 含未注册维度 → ERR_INVALID_ARGS
        - 均未指定 mode 与 custom_filter → 回落 DEFAULT_INJECT_MODE（T-120，
          ``stats.note`` 注明回落与可用模板清单）；显式传空值仍 → ERR_INVALID_ARGS
        - 其余意外异常（pipeline/DAO 层）→ ERR_INTERNAL
    """
    dimensions: list[dict[str, Any]] = cfg["dimensions"]
    # T-120：mode 缺省回落——两者都缺省（None，非显式空值）时回落默认模式，
    # 显式传入 custom_filter（哪怕空 dict）时行为完全不变
    fallback_mode: str | None = None
    if mode is None and custom_filter is None:
        fallback_mode = DEFAULT_INJECT_MODE
        mode = fallback_mode

    try:
        # ---------- 分支 1：mode 模板注入 ----------
        if mode:
            try:
                tmpl = template_mod.load_template(mode, dimensions)
            except template_mod.TemplateError as e:
                return OperationResult.fail(ERR_INVALID_ARGS, f"模板加载失败: {e}")
            section_results = [
                inject_mod.query_section(mem_conn, s, dimensions, **_read_filters(cfg))
                for s in tmpl.get("sections", [])
            ]
            # 2026-11-05：跨 section 内容去重（此前缺失，同义记忆会被多个维度各塞一次）
            section_results, dupe_dropped = _dedupe_sections(section_results)
            # E7（2026-11-05）：去重条数进 stats + INFO 日志（此前 _dupe_dropped 被丢弃）
            _log_dedupe(
                dupe_dropped,
                sum(len(sr) for sr in section_results),
                len(section_results),
            )
            # T-252：记忆调用统计（双源之二：画像注入）——section_results 即注入集
            # （T-203 G1 在案）；跨 section 去重由 DAO 兜底（同批一次 upsert）。
            record_memory_usage(
                mem_conn,
                [r["memory_id"] for sr in section_results for r in sr if r.get("memory_id")],
                "inject",
                caller,
            )
            # D-79（2026-11-05）：Tier0 摘要整体砍除——画像由 core 记忆卡承担
            response = inject_mod.build_inject_blocks(tmpl, section_results)
            # E7：跨 section 去重条数（0 也记——无重复是正常态，字段恒定便于观察）
            response.setdefault("stats", {})["dedupe_dropped"] = dupe_dropped
            # D-79（2026-11-05）：core 记忆卡——每次必注，排最前（稳定前缀利于缓存）。
            # 零 LLM（纯表查询 + 模板拼接）；空卡不渲染、不占位。
            core_block = core_cards_mod.build_core_block(mem_conn)
            if core_block:
                response.setdefault("blocks", []).insert(0, core_block["block"])
                stats = response.setdefault("stats", {})
                stats["core_present"] = True
                # D-49：core 的 tokens 单独记账——预算裁剪时按同一口径重算（core 永不裁）
                stats["core_tokens"] = int(core_block["tokens"])
                stats["tokens_est"] = int(stats.get("tokens_est") or 0) + stats["core_tokens"]
            response = _attach_key_missing_note(_attach_empty_note(response), cfg)
            if fallback_mode:
                # 回落提示最后追加（_attach_empty_note 会整体覆盖 note，须先走既有链路）
                response = _attach_fallback_note(response, fallback_mode)
            # D-70：记忆回指块（画像块末尾；默认 off）
            response = _attach_last_mentions(response, mem_conn, cfg)
            # D-52 档 2/3 + D-55 阶段 4：锚点块按 read_mode 服务
            response = _attach_migration_blocks(response, mem_conn, cfg)
            # D-48：性格参考块（四条件闸门）
            response = _attach_persona_block(response, mem_conn, cfg)
            # D-72：冷场修复块最后追加（不参与空注入/回落 note 判断）
            response = _attach_coldstart_repair(response, history, already_repaired)
            # D-49（2026-11-05）：预算门控最后执行——core/section/persona 三段已齐备；
            # max_tokens=None 时原样返回（不传预算的调用方行为完全不变，向后兼容）
            response = inject_mod.apply_inject_budget(response, max_tokens)
            _log_budget(response)
            return OperationResult.succeed(response)

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
            rf = _read_filters(cfg)
            results = memory_dao.list_memories_by_dimension(
                mem_conn, dims, match=match, limit=limit,
                include_expired=not rf["exclude_valid_expired"],
                # D-55 阶段 4：read_mode=legacy/shadow 时不做 2.0 生命周期谓词
                lifecycle_active=rf["lifecycle_active"],
                # 2.0 D-67：技能/知识候选不进画像（与 profile/inject 口径一致）
                routes=("memory",),
            )
            # 2026-11-05：与 mode 分支同口径去重（自定义查询同样可能命中同义记忆）
            results, dupe_dropped = _dedupe_sections([results])
            results = results[0]
            # E7：与 mode 分支同口径计量（单 section，跨 section 去重通常为 0）
            _log_dedupe(dupe_dropped, len(results), 1)
            # 记录注入统计（best-effort）
            from sgme.data.memory_stats_dao import record_inject
            for r in results:
                record_inject(mem_conn, r["memory_id"])
            # T-252：记忆调用统计（source='inject'；既有 memory_stats 链路保持原样）
            record_memory_usage(
                mem_conn,
                [r["memory_id"] for r in results],
                "inject",
                caller,
            )
            # 拼装为单个 section
            tmpl = {
                "name": "custom",
                "sections": [{"title": "自定义查询", "query": {"dimensions": dims}}],
            }
            response = inject_mod.build_inject_blocks(tmpl, [results])
            response["stats"]["mode"] = "custom"
            response["stats"]["dedupe_dropped"] = dupe_dropped
            response = _attach_key_missing_note(_attach_empty_note(response), cfg)
            # D-70：记忆回指块（默认 off）
            response = _attach_last_mentions(response, mem_conn, cfg)
            # D-52 档 2/3 + D-55 阶段 4：锚点块按 read_mode 服务
            response = _attach_migration_blocks(response, mem_conn, cfg)
            # D-48：性格参考块（四条件闸门）
            response = _attach_persona_block(response, mem_conn, cfg)
            # D-72：冷场修复块最后追加（同上）
            response = _attach_coldstart_repair(response, history, already_repaired)
            # D-49：与 mode 分支同款预算门控（custom 分支无 core 块，persona/section 照裁）
            response = inject_mod.apply_inject_budget(response, max_tokens)
            _log_budget(response)
            return OperationResult.succeed(response)

        # 显式传空值（mode=""/custom_filter={} 等非缺省态）→ 参数非法（照 v0.6 路由兜底文案；
        # 真正的缺省回落已在函数入口处理，T-120）
        return OperationResult.fail(ERR_INVALID_ARGS, "需指定 mode 或 custom_filter")
    except Exception as e:
        # 意外异常（pipeline/DAO 层）→ ERR_INTERNAL，照 v0.6 路由 api_error 兜底
        return OperationResult.fail(ERR_INTERNAL, f"注入失败: {e}")
