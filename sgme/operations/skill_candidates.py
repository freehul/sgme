# -*- coding: utf-8 -*-
"""operations/skill_candidates.py：route=skill_candidate 消费管道（2.0 D-67）。

背景
----
L2 提炼会把「可复用经验」打成 ``route=skill_candidate``。``profile/inject.py``
只注入 route=memory，所以这些记忆既不进画像，也没有消费端——2026-11 生产库
积压 3296 条。本模块把「记忆 → 技能候选池 → 审核决策」这条管道补齐：

1. ``list_pending_route_memories``：列出尚未消费的 skill_candidate 记忆；
2. ``adopt_skill_candidate``：把一条记忆转成技能候选（幂等，memory_id 唯一）；
3. ``review_skill_candidate``：标记 accepted / ignored / pending / promoted（幂等）；
4. ``list_skill_candidates``：候选池分页（按状态/置信度）。

置信度口径（**规则筛选，不调 LLM**）
------------------------------------
高置信度必须同时满足：LLM 自评 ``certainty=confirmed``、``type=经验``、
正文含可执行/因果线索（必须/否则/导致/根因…）、长度 16-400、综合分 ≥ 0.8。
达不到的手动转入时状态为 ``pending``；存量脚本默认**不自动转入**低置信项，
留在待消费列表等人工/LLM 二审——**3296 条绝不一次性全自动变成正式技能**。

分层（v0.7 §7）：本模块不认识协议——不 import fastapi/mcp；SQL 只在 data 层。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.data import memory_dao, skill_candidates_dao
from sgme.operations.errors import (
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    InvalidArgs,
    OperationResult,
)

DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 200

# 高置信阈值：0.8 分 + 硬门槛（见 score_candidate）
CONFIDENCE_THRESHOLD: float = 0.8

# 候选状态机：pending=待审 / accepted=通过待落成 / ignored=忽略 / promoted=已落成
VALID_STATUSES: tuple[str, ...] = skill_candidates_dao.VALID_STATUSES

# 可执行/因果线索：经验类记忆要「能指导下一次动作」才算可复用
_ACTION_CUES: tuple[str, ...] = (
    "必须", "务必", "不要", "不能", "避免", "应该", "应当", "需要", "建议",
    "禁止", "只能", "记得", "注意", "关键", "正确", "改用", "先", "再",
    "否则", "才能", "导致", "因此", "所以", "故", "根因", "教训",
)

# 具体对象线索：含英文标识/数字/文件后缀，说明不是纯情绪表达
_CONCRETE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-/]{1,}|[0-9]+(?:\.[0-9]+)?")

# 一次性叙事线索：降低「今天/我刚才」这类不可复用记忆的权重
_ANECDOTE_CUES: tuple[str, ...] = ("今天", "昨天", "刚才", "我觉得", "我感觉", "我当时")


def _now_memory(memory: dict[str, Any]) -> dict[str, Any]:
    """统一字段访问（memory_dao 返回 dict；测试可直接传最小字典）。"""
    return {
        "content": str(memory.get("content") or "").strip(),
        "type": str(memory.get("type") or "").strip(),
        "certainty": str(memory.get("certainty") or "").strip().lower(),
        "priority": int(memory.get("priority") or 0),
        "dimensions": list(memory.get("dimensions") or memory.get("tags") or []),
    }


def _has_action_cue(content: str) -> bool:
    return any(cue in content for cue in _ACTION_CUES)


def score_candidate(memory: dict[str, Any]) -> dict[str, Any]:
    """给一条记忆打「像不像可复用技能」的规则分（0-1）。

    Returns:
        ``{"confidence": float, "high_confidence": bool, "reasons": [str, ...]}``
        reasons 是给审核人看的加分/减分依据，不参与落库。
    """
    m = _now_memory(memory)
    content = m["content"]
    score = 0.0
    reasons: list[str] = []

    if m["certainty"] == "confirmed":
        score += 0.25
        reasons.append("certainty=confirmed")
    elif m["certainty"] == "probable":
        score += 0.10
        reasons.append("certainty=probable")

    if m["type"] == "经验":
        score += 0.15
        reasons.append("type=经验")

    has_action = _has_action_cue(content)
    if has_action:
        score += 0.20
        reasons.append("含可执行/因果线索")

    if _CONCRETE_RE.search(content):
        score += 0.15
        reasons.append("含具体对象（英文标识/数字）")

    length = len(content)
    if 16 <= length <= 240:
        score += 0.10
        reasons.append("长度适中（16-240 字）")
    elif 241 <= length <= 400:
        score += 0.05
        reasons.append("长度偏长（241-400 字）")
    else:
        reasons.append("长度过短或过长")

    if m["priority"] >= 70:
        score += 0.05
        reasons.append("priority>=70")

    if m["dimensions"]:
        score += 0.05
        reasons.append("有维度标签")

    if any(cue in content for cue in _ANECDOTE_CUES) and not has_action:
        score -= 0.10
        reasons.append("一次性叙事措辞（减分）")

    confidence = round(max(0.0, min(1.0, score)), 2)
    high = (
        confidence >= CONFIDENCE_THRESHOLD
        and m["certainty"] == "confirmed"
        and m["type"] == "经验"
        and has_action
        and 16 <= length <= 400
    )
    return {
        "confidence": confidence,
        "high_confidence": high,
        "reasons": reasons,
    }


def _slug_from_memory_id(memory_id: str) -> str:
    """从 memory_id 派生 ASCII 技能名（中文正文无法可靠生成英文名）。"""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", memory_id or "").lower()
    if len(cleaned) < 6:
        cleaned = hashlib.sha256((memory_id or "").encode("utf-8")).hexdigest()
    return f"exp-{cleaned[:8]}"


def _one_line(text: str, limit: int = 100) -> str:
    """压成单行并截断（frontmatter description 不能带换行）。"""
    line = re.sub(r"\s+", " ", text or "").strip()
    return line[:limit].rstrip()


def _normalized_content_sha(content: str) -> str:
    """正文指纹：压空白后取 sha256（跨记忆去重锚点）。"""
    normalized = re.sub(r"\s+", " ", content or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_skill_draft(memory: dict[str, Any]) -> dict[str, Any]:
    """把记忆渲染成一份 SKILL.md 草稿（确定性、无 LLM）。

    草稿只是审核原料，**不落正式技能目录**；name 用 memory_id 派生，审核通过后
    由人/LLM 改成有语义的技能名。
    """
    m = _now_memory(memory)
    memory_id = str(memory.get("memory_id") or "")
    name = _slug_from_memory_id(memory_id)
    description = _one_line(m["content"]) or "从记忆提炼的可复用经验（待审核）"
    category = m["dimensions"][0] if m["dimensions"] else "experience"
    tags = list(dict.fromkeys([*m["dimensions"], "skill-candidate"]))
    content = m["content"]
    body = (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"category: {category}\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        "version: 0.1.0\n"
        "pattern: manual\n"
        "---\n\n"
        f"# {description}\n\n"
        f"{content}\n\n"
        "## 溯源\n\n"
        f"- memory_id: {memory_id}\n"
        "- route: skill_candidate\n"
        "- 状态：候选草稿（未审核，不得直接当作正式技能使用）\n"
    )
    return {
        "name": name,
        "description": description,
        "content": body,
        "category": category,
        "tags": tags,
        "content_sha256": _normalized_content_sha(content),
    }


# ---------- 参数校验 ----------


def _parse_limit(raw: int, name: str = "limit") -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidArgs(f"{name} 必须是整数: {raw!r}")
    if raw < 1 or raw > MAX_LIMIT:
        raise InvalidArgs(f"{name} 必须在 1-{MAX_LIMIT} 之间: {raw}")
    return raw


def _parse_offset(raw: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise InvalidArgs(f"offset 必须 ≥ 0: {raw!r}")
    return raw


def _parse_confidence(raw: float | None, name: str = "min_confidence") -> float | None:
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise InvalidArgs(f"{name} 必须是 0-1 的小数: {raw!r}") from None
    if not 0.0 <= val <= 1.0:
        raise InvalidArgs(f"{name} 必须在 0-1 之间: {raw}")
    return val


def _parse_status(raw: str | None, *, allow_none: bool = True) -> str | None:
    if raw is None or str(raw).strip() == "":
        if allow_none:
            return None
        raise InvalidArgs("status 不能为空")
    status = str(raw).strip().lower()
    if status not in VALID_STATUSES:
        raise InvalidArgs(f"非法候选状态: {raw!r}（合法值: {', '.join(VALID_STATUSES)}）")
    return status


# ---------- 待消费列表 ----------


def _memory_item(memory: dict[str, Any], scored: dict[str, Any]) -> dict[str, Any]:
    """待消费列表的单条投影（只给审核需要的字段）。"""
    return {
        "memory_id": memory.get("memory_id"),
        "content": memory.get("content"),
        "memory_type": memory.get("memory_type"),
        "type": memory.get("type"),
        "certainty": memory.get("certainty"),
        "priority": memory.get("priority"),
        "dimensions": list(memory.get("dimensions") or []),
        "created_at": memory.get("created_at"),
        **scored,
    }


def list_pending_route_memories(
    mem_conn: sqlite3.Connection,
    skills_conn: sqlite3.Connection | None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    min_confidence: float | None = None,
) -> OperationResult:
    """列出尚未消费的 route=skill_candidate 记忆（游标分批扫描，不一次读全表）。

    Args:
        mem_conn: memory.db 连接。
        skills_conn: skills.db 连接（候选池台账）；None → 技能模块未启用。
        limit: 页大小（1-200）。
        offset: 跳过条数（在过滤后的集合上计算）。
        min_confidence: 只返回置信度达线项（None=不过滤）。

    Returns:
        data = ``{items, count, remaining, route_total}``；remaining 是
        skill_candidate 记忆总数减去已消费数（未考虑 min_confidence 过滤）。
    """
    if skills_conn is None:
        return OperationResult.fail(ERR_INVALID_ARGS, "skills 模块未启用（skills.db 不可用）")
    limit_val = _parse_limit(limit)
    offset_val = _parse_offset(offset)
    min_conf = _parse_confidence(min_confidence)

    consumed = skill_candidates_dao.candidate_memory_ids(skills_conn)
    route_total = memory_dao.count_memories_by_route(mem_conn, "skill_candidate")

    items: list[dict[str, Any]] = []
    skipped = 0
    cursor: str | None = None
    while len(items) < limit_val:
        batch = memory_dao.list_memories_by_route(
            mem_conn, "skill_candidate", limit=200, after_memory_id=cursor
        )
        if not batch:
            break
        cursor = batch[-1]["memory_id"]
        for memory in batch:
            if memory["memory_id"] in consumed:
                continue
            scored = score_candidate(memory)
            if min_conf is not None and scored["confidence"] < min_conf:
                continue
            if skipped < offset_val:
                skipped += 1
                continue
            items.append(_memory_item(memory, scored))
            if len(items) >= limit_val:
                break

    return OperationResult.succeed(
        {
            "items": items,
            "count": len(items),
            "remaining": max(0, route_total - len(consumed)),
            "route_total": route_total,
        }
    )


# ---------- 转入候选池 ----------


def adopt_skill_candidate(
    mem_conn: sqlite3.Connection,
    skills_conn: sqlite3.Connection | None,
    memory_id: str,
    status: str | None = None,
    note: str | None = None,
    dedupe: bool = True,
    commit: bool = True,
) -> OperationResult:
    """把一条 skill_candidate 记忆转成技能候选（幂等）。

    幂等语义：memory_id 已在池中 → 直接返回既有行（created=False），
    **不覆盖**已有审核状态；重复内容（content_sha256 相同）→ 新行标记
    ``ignored`` 并指向首条，避免候选池被近似记忆灌爆。

    Args:
        status: 显式指定初始状态（pending/accepted/ignored）；None = 高置信
            accepted、其余 pending。
        dedupe: True 时按正文指纹跨记忆去重。
        commit: 批量脚本可传 False，由调用方按批提交（默认立即提交）。

    Returns:
        data = 候选行关键字段 + ``created`` / ``duplicate`` / ``duplicate_of``。
    """
    if skills_conn is None:
        return OperationResult.fail(ERR_INVALID_ARGS, "skills 模块未启用（skills.db 不可用）")
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        raise InvalidArgs("memory_id 不能为空")
    explicit = _parse_status(status)

    existing = skill_candidates_dao.get_candidate(skills_conn, memory_id)
    if existing is not None:
        return OperationResult.succeed(
            {
                "candidate_id": existing["candidate_id"],
                "memory_id": memory_id,
                "name": existing["name"],
                "status": existing["status"],
                "confidence": existing["confidence"],
                "high_confidence": existing["confidence"] >= CONFIDENCE_THRESHOLD,
                "created": False,
                "duplicate": False,
                "duplicate_of": None,
            }
        )

    memory = memory_dao.get_memory(mem_conn, memory_id)
    if memory is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"记忆不存在: {memory_id}")
    if str(memory.get("route") or "memory") != "skill_candidate":
        return OperationResult.fail(
            ERR_INVALID_ARGS,
            f"该记忆 route 不是 skill_candidate: {memory.get('route') or 'memory'}",
        )

    scored = score_candidate(memory)
    draft = build_skill_draft(memory)
    initial_status = explicit or ("accepted" if scored["high_confidence"] else "pending")
    duplicate_of = None
    duplicate = False
    reason = "；".join(scored["reasons"])
    if dedupe and draft["content_sha256"]:
        same = skill_candidates_dao.find_by_content_sha(
            skills_conn, draft["content_sha256"]
        )
        if same is not None and same["memory_id"] != memory_id:
            duplicate = True
            duplicate_of = same["memory_id"]
            initial_status = "ignored"
            note = f"重复内容，已存在候选: {duplicate_of}" + (f"\n{note}" if note else "")
            reason = f"{reason}；重复内容（{duplicate_of}）"

    candidate_id, created = skill_candidates_dao.create_candidate(
        skills_conn,
        {
            "memory_id": memory_id,
            **draft,
            "confidence": scored["confidence"],
            "status": initial_status,
            "reason": reason,
            "note": note,
            "reviewed_at": None if initial_status == "pending" else _now_iso(),
        },
    )
    if commit:
        skills_conn.commit()
    return OperationResult.succeed(
        {
            "candidate_id": candidate_id,
            "memory_id": memory_id,
            "name": draft["name"],
            "status": initial_status,
            "confidence": scored["confidence"],
            "high_confidence": scored["high_confidence"],
            "reasons": scored["reasons"],
            "created": created,
            "duplicate": duplicate,
            "duplicate_of": duplicate_of,
        }
    )


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（与 data 层同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 审核决策 ----------


def _merge_note(current: str | None, note: str | None) -> str | None:
    """备注追加式合并（不覆盖历史；重复文本不重复追加）。"""
    if not note:
        return None
    text = str(note).strip()
    if not text:
        return None
    if current and text in str(current):
        return None
    if current and str(current).strip():
        return f"{current}\n{text}"
    return text


def review_skill_candidate(
    mem_conn: sqlite3.Connection,
    skills_conn: sqlite3.Connection | None,
    memory_id: str,
    decision: str,
    note: str | None = None,
    reviewer: str | None = None,
) -> OperationResult:
    """审核一条技能候选（幂等）：accepted / ignored / pending / promoted。

    - 候选不存在时先自动转入（等同 adopt），再落决策——让「直接忽略一条待消费
      记忆」也能一次调用完成；
    - 状态未变且备注已存在 → ``changed=False``，不写库、不动时间戳；
    - 状态相同但补了新备注 → 只追加备注（changed=True，状态不变）。
    """
    status = _parse_status(decision, allow_none=False)
    assert status is not None  # allow_none=False 时必非空，供类型检查收窄
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        raise InvalidArgs("memory_id 不能为空")
    if skills_conn is None:
        return OperationResult.fail(ERR_INVALID_ARGS, "skills 模块未启用（skills.db 不可用）")

    existing = skill_candidates_dao.get_candidate(skills_conn, memory_id)
    created = False
    if existing is None:
        adopted = adopt_skill_candidate(
            mem_conn, skills_conn, memory_id, status=status, note=note
        )
        if not adopted.ok:
            return adopted
        # adopt 已写入目标状态；补 reviewer
        if reviewer:
            skill_candidates_dao.update_status(
                skills_conn, memory_id, status, reviewer=reviewer
            )
            skills_conn.commit()
        data = dict(adopted.data or {})
        data.update({"changed": True, "created": True})
        return OperationResult.succeed(data)

    if existing["status"] == status:
        merged = _merge_note(existing.get("note"), note)
        if merged is None:
            return OperationResult.succeed(
                {
                    "candidate_id": existing["candidate_id"],
                    "memory_id": memory_id,
                    "status": status,
                    "changed": False,
                    "created": False,
                }
            )
        skill_candidates_dao.update_status(
            skills_conn, memory_id, status, note=merged, reviewer=reviewer
        )
        skills_conn.commit()
        return OperationResult.succeed(
            {
                "candidate_id": existing["candidate_id"],
                "memory_id": memory_id,
                "status": status,
                "changed": True,
                "created": False,
            }
        )

    merged = _merge_note(existing.get("note"), note)
    skill_candidates_dao.update_status(
        skills_conn, memory_id, status, note=merged, reviewer=reviewer
    )
    skills_conn.commit()
    return OperationResult.succeed(
        {
            "candidate_id": existing["candidate_id"],
            "memory_id": memory_id,
            "status": status,
            "changed": True,
            "created": False,
        }
    )


# ---------- 候选池列表 ----------


def list_skill_candidates(
    skills_conn: sqlite3.Connection | None,
    status: str | None = None,
    min_confidence: float | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> OperationResult:
    """候选池分页列表（默认全部状态，按置信度降序）。"""
    if skills_conn is None:
        return OperationResult.fail(ERR_INVALID_ARGS, "skills 模块未启用（skills.db 不可用）")
    limit_val = _parse_limit(limit)
    offset_val = _parse_offset(offset)
    status_val = _parse_status(status)
    min_conf = _parse_confidence(min_confidence)
    items = skill_candidates_dao.list_candidates(
        skills_conn, status_val, min_conf, limit_val, offset_val
    )
    return OperationResult.succeed(
        {
            "items": items,
            "count": len(items),
            "total": skill_candidates_dao.count_candidates(skills_conn, status_val, min_conf),
            "status": status_val,
            "min_confidence": min_conf,
        }
    )


def candidate_summary(skills_conn: sqlite3.Connection | None) -> OperationResult:
    """候选池总览（脚本/运维面板用）：总数、按状态、高置信数。"""
    if skills_conn is None:
        return OperationResult.fail(ERR_INVALID_ARGS, "skills 模块未启用（skills.db 不可用）")
    return OperationResult.succeed(
        skill_candidates_dao.summary(skills_conn, CONFIDENCE_THRESHOLD)
    )


# 兼容测试/调用方从本模块取异常类型（入口层统一捕获）
__all__ = [
    "CONFIDENCE_THRESHOLD",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "VALID_STATUSES",
    "InvalidArgs",
    "adopt_skill_candidate",
    "build_skill_draft",
    "candidate_summary",
    "list_pending_route_memories",
    "list_skill_candidates",
    "review_skill_candidate",
    "score_candidate",
]
