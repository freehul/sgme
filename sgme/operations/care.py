"""operations/care.py：角色层操作（ST-25 / T-35）。

三段式结构（照抄 wiki.py 样板）：操作函数返回 ``OperationResult`` 信息超集，
入口层（HTTP/MCP）只做协议翻译。

承接入口
--------
- HTTP ``/v1/admin/roles/*``（routes_care，T-35 新建）

数据源边界：角色卡 = roles/ 目录文件（项目内随 git）；persona = data/personas/
运行数据；画像素材 = 记忆池（memory.db，只读聚合，零物化）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.data import memory_dao
from sgme.llm import provider as llm_provider
from sgme.operations.errors import ERR_CONFLICT, ERR_INTERNAL, ERR_INVALID_ARGS, ERR_NOT_FOUND, InvalidArgs, OperationResult

logger = logging.getLogger("sgme.operations.care")

# persona 画像素材的维度白名单（静态/半静态，喂给四层扫描）
_PROFILE_DIMENSIONS = (
    "identity", "preferences", "habits", "values", "style", "skills", "family", "social",
)
_PROFILE_LIMIT = 30  # 素材记忆条数上限（防 token 爆预算）
_PROFILE_MAX_CHARS = 8000  # 素材字符上限（≤ 甜点区）


def _roles_dir() -> Path:
    return Path(sgme_config.ROLES_DIR)


def _persona_dir() -> Path:
    return Path(sgme_config.PERSONA_DIR)


# ---------- 角色卡 CRUD ----------

def list_roles() -> OperationResult:
    """角色卡列表（轻量字段，不含正文）。"""
    try:
        items = roles_mod.list_roles(_roles_dir())
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"角色列表失败: {e}")
    return OperationResult.succeed({"roles": items, "total": len(items)})


def get_role(role_id: str) -> OperationResult:
    """单张角色卡全文；不存在 → ERR_NOT_FOUND。"""
    try:
        card = roles_mod.get_role(_roles_dir(), role_id)
    except ValueError as e:
        return OperationResult.fail(ERR_INTERNAL, str(e))
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"角色读取失败: {e}")
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    return OperationResult.succeed({"role": card})


def create_role(role_id: str, data: dict[str, Any]) -> OperationResult:
    """新建/更新角色卡（幂等 upsert）。校验失败 → ERR_INTERNAL（含校验明细）。

    ``data`` 为 CC V2 的 data 段（name/description 必填）；操作层负责包装
    spec/spec_version 顶层（协议翻译，文件格式即 CC V2 完整结构）。
    """
    card = roles_mod.normalize_role_card({"data": data})
    try:
        roles_mod.save_role(_roles_dir(), role_id, card)
    except ValueError as e:
        return OperationResult.fail(ERR_INTERNAL, str(e))
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"角色保存失败: {e}")
    return OperationResult.succeed({"role_id": role_id, "status": "saved"})


def delete_role(role_id: str) -> OperationResult:
    """归档角色卡（移入 .archive/，原件永不删铁律）。"""
    try:
        ok = roles_mod.archive_role(_roles_dir(), role_id)
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"角色归档失败: {e}")
    if not ok:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    return OperationResult.succeed({"role_id": role_id, "status": "archived"})


# ---------- persona 物化（唯一物化例外） ----------

def _build_profile(mem_conn: sqlite3.Connection) -> str:
    """画像素材聚合：记忆池静态维度高优先记忆（零物化，现查现取）。"""
    parts: list[str] = []
    for dim in _PROFILE_DIMENSIONS:
        try:
            rows = memory_dao.list_memories_by_dimension(
                mem_conn, [dim], limit=_PROFILE_LIMIT, include_expired=False,
            )
        except Exception as e:
            logger.warning("画像素材维度 %s 查询失败: %s", dim, e)
            continue
        for r in rows:
            parts.append(f"[{dim}] {r.get('content', '')}")
        if len("\n".join(parts)) >= _PROFILE_MAX_CHARS:
            break
    text = "\n".join(parts)[:_PROFILE_MAX_CHARS]
    return text or "（记忆池暂无画像素材）"


def get_persona(role_id: str) -> OperationResult:
    """读取 persona 物化文件；未生成 → ERR_NOT_FOUND。"""
    text = roles_mod.load_persona(role_id, _persona_dir())
    if text is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"persona 未生成: {role_id}")
    return OperationResult.succeed({"role_id": role_id, "persona": text})


def generate_persona(
    role_id: str,
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    client: Any = None,
) -> OperationResult:
    """生成角色 persona（LLM 四层扫描）并物化。

    - 输入：角色卡（角色人设）+ 画像素材（记忆池静态维度聚合，零物化）
    - 复用提炼链 ``llm_chain.call_with_fallback``（deepseek 主链，降级链同源）
    - 产物：data/personas/<role_id>.md（物化例外；备份保留 3 份）
    - LLM 不可用 → ERR_INTERNAL（不降级直存——persona 无降级语义）
    """
    card = roles_mod.get_role(_roles_dir(), role_id)
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    try:
        from sgme.llm import chain as llm_chain

        role_name = card["data"].get("name", role_id)
        profile = _build_profile(mem_conn)
        max_chars = (cfg.get("care") or {}).get("persona_max_chars", 2000)
        prompt = roles_mod.render_persona_prompt(role_name, profile, max_chars)
        text, provider_name, _usage = llm_chain.call_with_fallback(
            cfg["llm"], prompt, chain_name="refinement", client=client,
        )
        if not text or not text.strip():
            raise ValueError("LLM 返回空 persona")
        text = text.strip()
        if len(text) > max_chars * 2:  # 超上限 2 倍视为异常（不静默截断）
            logger.warning("persona 超长 %d 字符（上限 %d），仍物化", len(text), max_chars)
        fp = roles_mod.save_persona(role_id, text, _persona_dir())
    except Exception as e:
        if isinstance(e, llm_provider.LLMUnavailable):
            return OperationResult.fail(ERR_INTERNAL, f"LLM 不可用: {e}")
        return OperationResult.fail(ERR_INTERNAL, f"persona 生成失败: {e}")
    logger.info("persona 生成完成: %s（%s，%d 字符）", role_id, provider_name, len(text))
    return OperationResult.succeed({"role_id": role_id, "path": str(fp), "provider": provider_name})


# ---------- 关怀信号（T-36） ----------

def scan_signals(mem_conn: sqlite3.Connection, cfg: dict[str, Any]) -> OperationResult:
    """执行关怀信号扫描（四类推导：待办到期/情绪/过劳/每日，幂等去重）。

    零 LLM 规则引擎——SGME 只发信号不做决策；消费方（agent）决定是否打扰。
    """
    from sgme.care import signals as signals_mod

    try:
        stats = signals_mod.scan_care_signals(mem_conn, cfg)
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"关怀信号扫描失败: {e}")
    return OperationResult.succeed({"scan": stats})


def list_signals(
    mem_conn: sqlite3.Connection,
    *,
    signal_type: str | None = None,
    unconsumed_only: bool = False,
    limit: int = 50,
) -> OperationResult:
    """拉取关怀信号（type=care_*；unconsumed_only 时只看未消费）。"""
    from sgme.care import signals as signals_mod

    try:
        items = signals_mod.list_care_signals(
            mem_conn, signal_type=signal_type,
            unconsumed_only=unconsumed_only, limit=limit,
        )
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"关怀信号拉取失败: {e}")
    return OperationResult.succeed({"signals": items, "total": len(items)})


def consume_signal(mem_conn: sqlite3.Connection, event_id: str, agent_id: str | None = None) -> OperationResult:
    """原子认领关怀信号（谁消费谁标记，ST-27 T-57）。

    - agent_id 记录认领方（不传则 consumed_by=None，仍原子防重复）
    - 返回 status=consumed（本次认领成功）；已被他人消费 → ERR_CONFLICT（409）
    - 2026-08-18 修复（兜底铁律）：合成身份（default/None）认领无归属，记 anomaly_warn
      告警——曾出现「服务端静默消费（consumed_by=default）+ signal_acks 零回执」，
      关怀永远无法到达当前会话（用户实测零感受），此告警供排障溯源
    """
    from sgme.care import signals as signals_mod
    from sgme.signal import engine as signal_engine

    try:
        ok = signals_mod.consume_signal(mem_conn, event_id, agent_id=agent_id)
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"信号消费标记失败: {e}")
    if not ok:
        # 已被他人消费（原子抢失败）→ 409 语义（区别于「不存在」404）
        return OperationResult.fail(ERR_CONFLICT, f"信号已被消费: {event_id}")
    # 2026-08-18：合成身份（env 主 key → default，或未传 → None）认领无归属 → 告警
    if agent_id in (None, "default"):
        try:
            signal_engine.publish(
                "anomaly_warn",
                "care_consume",
                {
                    "signal_event_id": event_id,
                    "agent_id": agent_id,
                    "message": "关怀信号被合成身份认领（default/None），无 agent 归属——"
                               "认领方须在会话中呈现关怀并写 signal_ack 回执，否则兜底铁律失效",
                },
                mem_conn,
            )
            logger.warning("关怀信号 %s 被合成身份认领（agent_id=%s），已记 anomaly_warn", event_id, agent_id)
        except Exception as e:  # 告警发布失败不阻塞消费（故障隔离）
            logger.warning("关怀信号合成身份告警发布失败: %s", e)
    return OperationResult.succeed({"event_id": event_id, "status": "consumed", "agent_id": agent_id})


def ack_signal(
    mem_conn: sqlite3.Connection,
    event_id: str,
    agent_id: str,
    status: str,
    result: str | None = None,
) -> OperationResult:
    """写消费回执（signal_acks 表，ST-27 T-57 三层消费模型第 3 层）。

    - status: claimed（认领未处理完）/ acked（成功）/ failed（失败）
    - 幂等 upsert：同 (event_id, agent_id) 重复回执覆盖为最新状态
    """
    from sgme.data import signal_dao

    if status not in ("claimed", "acked", "failed"):
        return OperationResult.fail(ERR_INVALID_ARGS, f"非法回执状态: {status}")
    try:
        signal_dao.ack_signal(mem_conn, event_id, agent_id, status, result)
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"回执写入失败: {e}")
    return OperationResult.succeed({"event_id": event_id, "agent_id": agent_id, "status": status})


# ---------- 角色注入装配（T-37） ----------

def assemble(
    role_id: str,
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    inject_mode: str | None = None,
) -> OperationResult:
    """角色沟通提示词装配：角色卡 + persona 物化 + 用户画像（零物化）。

    装配产物（消费方 agent 直接注入 system prompt 使用）：
    - system_prompt：角色卡 CC V2 system_prompt（含 {{char}}/{{user}} 宏占位，
      {{original}} 已替换为角色职责默认文案，供消费方拼接）
    - persona：物化文件全文（若已生成；唯一物化例外）
    - profile_blocks：用户画像（inject 模板查询，零物化——可选项，
      inject_mode 指定模板如 daily/full；None = 不带画像）
    - care_policy：extensions.sgme_care 关怀策略（问候模板/触发规则/频率档位）

    **换皮不换芯**：换角色 = 换装配输出（表达风格 + 主动策略），记忆池不动。
    """
    card = roles_mod.get_role(_roles_dir(), role_id)
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    try:
        data = card.get("data") or {}
        sys_prompt = data.get("system_prompt") or ""
        # {{original}} 占位符替换（无则忽略）
        if "{{original}}" in sys_prompt:
            default = (
                "你是{{char}}，{{user}}的专属沟通角色。"
                "你的言行基于用户的长期记忆（由记忆引擎注入），"
                "主动关怀、尊重边界、避免打扰。"
            )
            sys_prompt = sys_prompt.replace("{{original}}", default)
        persona = roles_mod.load_persona(role_id, _persona_dir())
        care_policy = (data.get("extensions") or {}).get("sgme_care")
        profile_blocks: list[dict] = []
        if inject_mode:
            from sgme.operations.inject import inject as inject_operation

            res = inject_operation(mem_conn, cfg, mode=inject_mode)
            if res.ok:
                profile_blocks = res.data.get("blocks", [])
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"角色装配失败: {e}")
    return OperationResult.succeed({
        "role_id": role_id,
        "role_name": data.get("name", role_id),
        "system_prompt": sys_prompt,
        "persona": persona,
        "profile_blocks": profile_blocks,
        "care_policy": care_policy,
    })


# ---------- 当前角色（T-40） ----------

def get_active_role() -> OperationResult:
    """读取当前沟通角色；未设置 → role_id=None。"""
    try:
        role_id = roles_mod.get_active_role(Path(sgme_config.DATA_DIR))
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"当前角色读取失败: {e}")
    return OperationResult.succeed({"role_id": role_id})


def set_active_role(role_id: str) -> OperationResult:
    """设置当前沟通角色（换皮不换芯：只换角色，记忆池不动）。

    角色必须存在（roles/ 有对应卡）；非法 id → ERR_INTERNAL。
    """
    card = roles_mod.get_role(_roles_dir(), role_id)
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    try:
        roles_mod.set_active_role(role_id, Path(sgme_config.DATA_DIR))
    except ValueError as e:
        return OperationResult.fail(ERR_INTERNAL, str(e))
    except Exception as e:
        return OperationResult.fail(ERR_INTERNAL, f"当前角色设置失败: {e}")
    return OperationResult.succeed({"role_id": role_id, "status": "active"})
