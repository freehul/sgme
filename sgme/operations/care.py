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
import re
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
# T-203 G2：原含 "style"，该维度已停用并并入 preferences（preferences 已在列，
# 迁移后的原 style 记忆由它覆盖），保留会查不到任何记忆导致画像素材缺失。
_PROFILE_DIMENSIONS = (
    "identity", "preferences", "habits", "values", "skills", "family", "social",
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


# ---------- 人设懒补全与写回（D-76 / T-306） ----------

PERSONA_FILL_PROMPT = """你是角色「{role_name}」的档案补全器。用户在追问角色人生中\
没有写进档案的细节。按规则补一条背景，或判定补不了。
硬性规则：
- 允许类型级补充：「一所普通中学」「一家做外贸的小公司」「比我小三岁的表弟」
- 允许感受与偏好：「那几年过得不轻松」
- 必须与已有设定兼容
- 禁止可核查专名：具体校名/公司名/地名+年份组合
- 禁止数字断言：「2015 年、月薪 8000」
- 若补一条必然与已有设定冲突 → 只输出 UNSET，不要硬编
输出格式（两行，不要输出其他内容）：
回答正文（第一人称、口语、不超过 40 字）
KEYS: 与该回答相关的 2 到 4 个关键词，用逗号分隔
已有档案条目：
{entries}
追问细节：{question}
"""


def _book_entries_text(card: dict[str, Any]) -> str:
    """把 character_book 条目拼成提示词素材（无条目给占位说明）。"""
    book = (card.get("data") or {}).get("character_book") or {}
    lines = [f"- {e.get('content', '')}" for e in book.get("entries") or [] if e.get("content")]
    return "\n".join(lines) if lines else "（档案暂无人生条目）"


def _parse_fill_output(text: str, question: str) -> tuple[str | None, list[str]]:
    """解析补全输出：UNSET/空 → (None, [])；否则 (正文, keys)。

    LLM 没给 KEYS 时回退用追问原文做 key——保证写回条目必带可命中的 keys。
    """
    t = (text or "").strip()
    if not t or t.upper().startswith("UNSET"):
        return None, []
    keys: list[str] = []
    m = re.search(r"KEYS?\s*[:：]\s*(.+)", t)
    if m:
        keys = [k.strip() for k in re.split(r"[,，、]", m.group(1)) if k.strip()][:4]
        t = t[:m.start()].strip()
    answer = t.strip().strip('"「」\'')
    if not answer:
        return None, []
    if not keys:
        keys = [question.strip()[:8]]
    return answer, keys


def persona_fill(
    role_id: str,
    question: str,
    cfg: dict[str, Any],
    client: Any = None,
) -> OperationResult:
    """D-76 人设懒补全四步闭环（顺序不可反）：追问 → ①查角色档案 → ②命中直接用
    → ③未命中：生成+冲突检查+立即落盘 → ④再回答。

    - ①② 命中 character_book 条目：零 LLM，返回条目原文（措辞可变、事实不变）
    - ③ 未命中走 LLM（三条硬规则写在提示词里：允许类型级/感受偏好、禁专名/
      数字断言、冲突宁可不补）；LLM 判冲突输出 UNSET → 不写回，答「这个我没提过」
    - ④ 正常补全立即写回角色卡 character_book（``generated: true`` + keys），
      同一问题再问命中同一条目 → 事实不会每次变一个版本
    - **隔离铁律**：角色人生事实只写角色卡（roles/<id>.json），永不写 memory.db
      ——本函数不接收 mem_conn，没有触碰记忆池的代码路径
    - 角色不存在 → ERR_NOT_FOUND；LLM 不可用 → ERR_INTERNAL（不降级硬编）
    """
    card = roles_mod.get_role(_roles_dir(), role_id)
    if card is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    if not (question or "").strip():
        return OperationResult.fail(ERR_INVALID_ARGS, "question 不能为空")
    # ①② 查档案：命中直接用（零 LLM、零写盘）
    hit = roles_mod.find_book_entry(card, question)
    if hit is not None:
        return OperationResult.succeed({
            "role_id": role_id, "status": "hit",
            "answer": hit.get("content", ""),
            "generated": bool(hit.get("generated")),
        })
    # ③ 未命中：LLM 生成 + 冲突检查
    try:
        from sgme.llm import chain as llm_chain

        role_name = card["data"].get("name", role_id)
        prompt = PERSONA_FILL_PROMPT.format(
            role_name=role_name,
            entries=_book_entries_text(card),
            question=question.strip(),
        )
        text, provider_name, _usage = llm_chain.call_with_fallback(
            cfg["llm"], prompt, chain_name="refinement", client=client,
        )
        answer, keys = _parse_fill_output(text or "", question)
        if answer is None:
            # 冲突/补不了 → 不写回，宁可说「没提过」
            return OperationResult.succeed({
                "role_id": role_id, "status": "unset",
                "answer": "这个我没提过。",
                "provider": provider_name,
            })
        # ④ 立即落盘（角色卡 character_book）
        entry = roles_mod.append_book_entry(_roles_dir(), role_id, keys, answer)
    except Exception as e:
        if isinstance(e, llm_provider.LLMUnavailable):
            return OperationResult.fail(ERR_INTERNAL, f"LLM 不可用: {e}")
        return OperationResult.fail(ERR_INTERNAL, f"人设补全失败: {e}")
    if entry is None:  # 并发下角色被归档（理论不可达，兜底可见失败）
        return OperationResult.fail(ERR_NOT_FOUND, f"角色不存在: {role_id}")
    logger.info("人设懒补全: %s（keys=%s，%s）", role_id, keys, provider_name)
    return OperationResult.succeed({
        "role_id": role_id, "status": "filled", "answer": answer,
        "keys": entry["keys"], "insertion_order": entry["insertion_order"],
        "generated": True, "provider": provider_name,
    })


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

_MACRO_RE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


def _render_macros(text: str, char_name: str, user_name: str | None = None) -> str:
    """替换角色卡 CC V2 宏：``{{char}}`` → 角色名、``{{user}}`` → 用户显示名。

    ``user_name`` 缺省回落「你」——不猜真实姓名（数据卫生）。
    未知宏**保留原文并告警**，不静默吞掉：便于发现新宏漏网。
    """
    s = (text or "").replace("{{char}}", char_name)
    s = s.replace("{{user}}", user_name or "你")
    left = sorted(set(_MACRO_RE.findall(s)))
    if left:
        logger.warning("角色装配渲染后仍残留未知宏（未替换）：%s", left)
    return s


def assemble(
    role_id: str,
    mem_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    inject_mode: str | None = None,
    render: bool = False,
    user_name: str | None = None,
) -> OperationResult:
    """角色沟通提示词装配：角色卡 + persona 物化 + 用户画像（零物化）。

    装配产物（消费方 agent 直接注入 system prompt 使用）：
    - system_prompt：角色卡 CC V2 system_prompt（``{{original}}`` 已替换为角色职责
      默认文案）。``{{char}}``/``{{user}}`` 宏**默认保留**（供消费方拼接）；
      ``render=True`` 时就地替换为角色名与 ``user_name``（缺省「你」）——
      8 个适配器历史上都没拼接，导致用户可见「早安，{{user}}。」，故提供服务端渲染。
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
        if render:
            sys_prompt = _render_macros(
                sys_prompt, str(data.get("name") or role_id), user_name
            )
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
    空串/空白 = 取消当前角色（T-211 面板「不使用角色」语义；幂等）。
    """
    role_id = str(role_id or "").strip()
    if not role_id:
        try:
            roles_mod.clear_active_role(Path(sgme_config.DATA_DIR))
        except Exception as e:
            return OperationResult.fail(ERR_INTERNAL, f"取消当前角色失败: {e}")
        return OperationResult.succeed({"role_id": None, "status": "cleared"})
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
