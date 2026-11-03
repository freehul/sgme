"""data/persona_dao.py：人格洞察 DAO（ST-35 T-98）。

读写 persona_traits（特质累积）/ user_mbti（自报锚点轨迹）/
persona_reports（月度校准报告）/ persona_state（月度计时状态）四表。

设计原则（2026-08-25 用户定案，见 Backlog ST-35）
------------------------------------------------
1. **倾向而非判决**：特质是带 confidence 的累积信号，不是一次性标签。
   同一 (dimension, value) 重复出现 → evidence_count +1、confidence 增长；
   单条记忆不改写整体画像。
2. **Supersession 非累积**：同一 dimension 下出现对立 value 时，
   新 value 累积到阈值后旧 value 置 status='superseded'（superseded_by 溯源），
   对齐画像 v2 的 Supersession 规则。
3. **软操作不物理删除**：废弃走 status='rejected'，原件永不删。
4. **溯源**：evidence_refs 为 JSON 数组（memory_id 列表），每条特质可回溯证据链。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

# 特质状态枚举（对齐 memories.status 语义 + supersession）
VALID_TRAIT_STATUSES: frozenset[str] = frozenset(
    {"active", "rejected", "superseded", "archived"}
)

# 特质来源枚举：rule=实时规则抽取 / llm_monthly=月度 LLM 校准 / manual=人工登记
VALID_SOURCES: frozenset[str] = frozenset({"rule", "llm_monthly", "manual"})

# MBTI 合法类型粗校验：4 字母，每位取值固定集合
_MBTI_POSITIONS: tuple[frozenset[str], ...] = (
    frozenset("EI"),
    frozenset("NS"),
    frozenset("TF"),
    frozenset("JP"),
)


def _now_iso() -> str:
    """UTC ISO 8601 时间戳（秒级）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_trait_id() -> str:
    return uuid.uuid4().hex


def validate_mbti(mbti_type: str) -> bool:
    """MBTI 类型粗校验（4 字母、各位合法取值），大小写归一为大写。"""
    t = mbti_type.strip().upper()
    if len(t) != 4:
        return False
    return all(t[i] in pos for i, pos in enumerate(_MBTI_POSITIONS))


def _row_to_trait(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
    return d


# ---------------------------------------------------------------------------
# D-48 人格沉淀四条件闸门（2026-11-04）
#
# 同一特质进人格证据链须同时满足：
#   ① 多轮 ≥3 会话（按 file_id 去重）
#   ② 跨天 ≥2（按 occurred_at 的日期去重）
#   ③ 多场景 ≥2（COUNT(DISTINCT scene_key)，排除 unknown/NULL）
#   ④ 来源可信 + 已发生（derivation ∈ fact/reconstructed 且 modality == done）
#
# 「来源可信/已发生」只对**通过前三条的可用证据**计数：不可信条目（推测/玩笑/
# 转述/计划）直接剔除，不让它们污染会话/天数/场景数。legacy 字符串证据
# （1.x 的 "refine:<file_id>"）没有日期与场景字段，**无法证明** ②③ →
# 一律不通过（与 §9.3 同口径：存量证据需新记忆补证）。
# ---------------------------------------------------------------------------

GATE_MIN_SESSIONS = 3
GATE_MIN_DAYS = 2
GATE_MIN_SCENES = 2
CREDIBLE_DERIVATIONS = ("fact", "reconstructed")
REQUIRED_MODALITY = "done"


def structured_evidence(trait: dict[str, Any]) -> list[dict[str, Any]]:
    """取结构化证据条目（dict）；1.x 字符串证据不参与四条件判定。"""
    refs = trait.get("evidence_refs") or []
    return [r for r in refs if isinstance(r, dict)]


def four_condition_report(
    trait: dict[str, Any],
    *,
    min_sessions: int = GATE_MIN_SESSIONS,
    min_days: int = GATE_MIN_DAYS,
    min_scenes: int = GATE_MIN_SCENES,
) -> dict[str, Any]:
    """四条件判定报告（只读；包含未通过原因与各项计数，供运维与测试）。"""
    evidence = structured_evidence(trait)
    credible = [
        e
        for e in evidence
        if str(e.get("derivation") or "") in CREDIBLE_DERIVATIONS
        and str(e.get("modality") or "") == REQUIRED_MODALITY
    ]
    sessions = {
        str(e.get("file_id") or e.get("session_id") or "").strip()
        for e in credible
    }
    sessions.discard("")
    days: set[str] = set()
    for e in credible:
        ts = str(e.get("occurred_at") or e.get("recorded_at") or "")
        if len(ts) >= 10:
            days.add(ts[:10])
    scenes = {
        str(e.get("scene_key")).strip()
        for e in credible
        if e.get("scene_key") and str(e.get("scene_key")).strip() != "unknown"
    }
    reasons: list[str] = []
    if len(sessions) < min_sessions:
        reasons.append(f"会话 {len(sessions)}<{min_sessions}")
    if len(days) < min_days:
        reasons.append(f"跨天 {len(days)}<{min_days}")
    if len(scenes) < min_scenes:
        reasons.append(f"多场景 {len(scenes)}<{min_scenes}")
    if not credible:
        reasons.append("无可信已发生证据")
    return {
        "passed": not reasons,
        "evidence": len(evidence),
        "credible": len(credible),
        "sessions": len(sessions),
        "days": len(days),
        "scenes": len(scenes),
        "reasons": reasons,
    }


def passes_four_conditions(trait: dict[str, Any], **thresholds: int) -> bool:
    """四条件闸门入口：True = 该特质可进人格证据链/注入块。"""
    return bool(four_condition_report(trait, **thresholds)["passed"])


# ---------------------------------------------------------------------------
# persona_traits
# ---------------------------------------------------------------------------


def upsert_trait(
    conn: sqlite3.Connection,
    dimension: str,
    value: str,
    *,
    evidence_ref: str | dict | None = None,
    scene_context: str = "general",
    source: str = "rule",
    confidence_step: float = 0.15,
) -> dict[str, Any]:
    """累积式写入一条特质信号。

    同一 (dimension, value, scene_context) 已有 active 特质 → evidence_count +1、
    confidence 增长（封顶 1.0）、evidence_refs 追加；否则新建 trait。
    返回写入后的完整特质行。
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"非法特质来源: {source}")
    now = _now_iso()
    row = conn.execute(
        """
        SELECT * FROM persona_traits
        WHERE dimension=? AND value=? AND scene_context=? AND status='active'
        """,
        (dimension, value, scene_context),
    ).fetchone()
    if row is None:
        trait_id = _gen_trait_id()
        refs = [evidence_ref] if evidence_ref else []
        conn.execute(
            """
            INSERT INTO persona_traits
              (trait_id, dimension, value, confidence, evidence_count,
               evidence_refs, scene_context, status, source, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trait_id,
                dimension,
                value,
                round(min(confidence_step, 1.0), 4),
                1 if evidence_ref else 0,
                json.dumps(refs, ensure_ascii=False),
                scene_context,
                "active",
                source,
                now,
                now,
            ),
        )
        conn.commit()
        out = conn.execute(
            "SELECT * FROM persona_traits WHERE trait_id=?", (trait_id,)
        ).fetchone()
        return _row_to_trait(out)
    # 累积路径：计数 +1、置信度增长、证据追加
    refs = json.loads(row["evidence_refs"] or "[]")
    if isinstance(evidence_ref, dict):
        # D-48 结构化证据去重：同一 memory_id 反复提炼不重复计证（同一事实不算新证据）
        mid = str(evidence_ref.get("memory_id") or "")
        if mid and any(
            isinstance(r, dict) and str(r.get("memory_id") or "") == mid for r in refs
        ):
            return _row_to_trait(row)
    if evidence_ref:
        refs.append(evidence_ref)
    new_conf = min(round(row["confidence"] + confidence_step, 4), 1.0)
    conn.execute(
        """
        UPDATE persona_traits
        SET confidence=?, evidence_count=evidence_count+?, evidence_refs=?,
            updated_at=?
        WHERE trait_id=?
        """,
        (
            new_conf,
            1 if evidence_ref else 0,
            json.dumps(refs, ensure_ascii=False),
            now,
            row["trait_id"],
        ),
    )
    conn.commit()
    out = conn.execute(
        "SELECT * FROM persona_traits WHERE trait_id=?", (row["trait_id"],)
    ).fetchone()
    return _row_to_trait(out)


def list_traits(
    conn: sqlite3.Connection,
    *,
    dimension: str | None = None,
    scene_context: str | None = None,
    status: str = "active",
    min_confidence: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按条件列特质（默认 active，confidence 降序）。"""
    sql = "SELECT * FROM persona_traits WHERE status=?"
    args: list[Any] = [status]
    if dimension:
        sql += " AND dimension=?"
        args.append(dimension)
    if scene_context:
        sql += " AND scene_context=?"
        args.append(scene_context)
    if min_confidence is not None:
        sql += " AND confidence>=?"
        args.append(min_confidence)
    sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
    args.append(limit)
    return [_row_to_trait(r) for r in conn.execute(sql, args).fetchall()]


def supersede_trait(
    conn: sqlite3.Connection,
    old_trait_id: str,
    new_trait_id: str,
) -> bool:
    """把旧特质标记为 superseded 并记录 superseded_by（非累积覆盖）。"""
    cur = conn.execute(
        """
        UPDATE persona_traits
        SET status='superseded', superseded_by=?, updated_at=?
        WHERE trait_id=? AND status='active'
        """,
        (new_trait_id, _now_iso(), old_trait_id),
    )
    conn.commit()
    return cur.rowcount > 0


def reject_trait(conn: sqlite3.Connection, trait_id: str) -> bool:
    """软删除特质（status='rejected'，不物理删除）。"""
    cur = conn.execute(
        "UPDATE persona_traits SET status='rejected', updated_at=? WHERE trait_id=?",
        (_now_iso(), trait_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# user_mbti（自报锚点轨迹）
# ---------------------------------------------------------------------------


def add_mbti_record(
    conn: sqlite3.Connection,
    mbti_type: str,
    *,
    source: str = "self_reported",
    note: str | None = None,
) -> dict[str, Any]:
    """追加一条 MBTI 记录（轨迹式，不覆盖历史）。"""
    t = mbti_type.strip().upper()
    if not validate_mbti(t):
        raise ValueError(f"非法 MBTI 类型: {mbti_type}")
    now = _now_iso()
    conn.execute(
        "INSERT INTO user_mbti (mbti_type, source, note, recorded_at) VALUES (?,?,?,?)",
        (t, source, note, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM user_mbti WHERE rowid=last_insert_rowid()"
    ).fetchone()
    return dict(row)


def get_mbti_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """MBTI 轨迹（时间正序——最早在前，便于画时间线）。"""
    rows = conn.execute("SELECT * FROM user_mbti ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def get_latest_mbti(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM user_mbti ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# persona_reports（月度报告）
# ---------------------------------------------------------------------------


def save_report(
    conn: sqlite3.Connection,
    period: str,
    report: str,
    *,
    report_id: str | None = None,
    mbti_result: str | None = None,
    trait_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """落一份月度校准报告（period 形如 2026-08）。"""
    rid = report_id or uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO persona_reports (report_id, period, report, mbti_result,
                                     trait_changes, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            rid,
            period,
            report,
            mbti_result,
            json.dumps(trait_changes or [], ensure_ascii=False),
            _now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM persona_reports WHERE report_id=?", (rid,)
    ).fetchone()
    return _report_to_dict(row)


def _report_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["trait_changes"] = json.loads(d.get("trait_changes") or "[]")
    return d


def list_reports(conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM persona_reports ORDER BY period DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_report_to_dict(r) for r in rows]


def get_report(conn: sqlite3.Connection, report_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM persona_reports WHERE report_id=?", (report_id,)
    ).fetchone()
    return _report_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# persona_state（月度计时状态，SGME 内部计时防漏跑）
# ---------------------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM persona_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO persona_state (key, value, updated_at) VALUES (?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                       updated_at=excluded.updated_at
        """,
        (key, value, _now_iso()),
    )
    conn.commit()
