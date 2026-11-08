# -*- coding: utf-8 -*-
"""engine/l2_edges.py：L2 单元上下文边（belongs_to / causes）写入。

W01 契约落地（docs/design/SGME2.0-上下文归属与因果边-v0.1.md §2-§4，A2 / W03-W05）：

- **归属边**：单元内「侧事实（side）→ 主事件（main）」建 belongs_to，
  解决「用户看到裸事实、没有父事件」；
- **因果边**：unit.causes 的 {from, to} 两端都能在**同一单元内**映射到最终
  memory_id 才建 causes——确定性映射，**不猜**（契约 §1「不新增 LLM 调用」）；
- **因果端点登记**（W05b，配 v022 提示词）：单元内文本登记三类来源——
  侧事实记录（role=side）、非 split fact 文本（role=fact，落到主干 id）、
  主干正文（role=main）；同文本命中多条时按 **side > fact > main** 取，
  让 causes 连到「更具体的那个记忆」而不是主干摘要（防因果错连）。
- 落库口径：source='l2_extract'、weight=1.0、edge_id=from::to::relation 由
  edge_dao.create_edge 幂等保证（重跑不重复）。

输入是**已落库**的 record 列表（每条带最终 memory_id + ephemeral _l2_unit），
由 l3_adjudicate.resolve_conflicts（四动作）与 pipeline._fallback_direct_store
（降级直存）在 memory_id 确定后统一调用。

失败不阻塞落库（契约 §1.4）：任一条边/任一个单元异常只 WARNING，记忆照常入库。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import string
from typing import Any, Iterable

from sgme.data import edge_dao

logger = logging.getLogger("sgme.engine.l2_edges")

#: 单元内上下文元数据的 ephemeral 键（W01 §2）。
#: ⚠️ 只在 L2 解析 → L3 落库之间传递；**绝不能**进 memories——落库白名单
#: （l3_adjudicate._EXTRA_KEYS / pipeline._fallback_direct_store 的 extra 列表 /
#: memory_dao._EXTRA_COLUMNS）刻意都不含它。
UNIT_META_KEY = "_l2_unit"

#: 边来源（可溯源：edge_dao.delete_edges_by_source(conn, "l2_extract") 一键回滚该路）
EDGE_SOURCE = "l2_extract"
EDGE_WEIGHT = 1.0
REL_BELONGS_TO = "belongs_to"
REL_CAUSES = "causes"
ROLE_MAIN = "main"
ROLE_SIDE = "side"
#: 非 split fact 的文本登记角色（W05b）——它没有独立记忆，落到主干 id 上
ROLE_FACT = "fact"

#: 同一归一文本命中多条记忆时的解析优先级（W05b）：
#: side > fact > main。理由：侧事实/非 split fact 比主干摘要更具体，
#: 优先落到它们可避免「因果关系连到主干」的错连（v022 起 causes 端点逐字复制
#: facts 原文，正是要连到那条具体事实）。同优先级内按 record 顺序取先出现者。
_ROLE_PRIORITY = {ROLE_SIDE: 0, ROLE_FACT: 1, ROLE_MAIN: 2}
_ROLE_PRIORITY_UNKNOWN = 3

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_WS_RE = re.compile(r"\s+")
#: 去首尾标点字符集（契约 §3.1：中英文句号/逗号/分号/引号，另含常见成对符号）。
_TRIM_PUNCT = (
    " \t\r\n\u3000.,;:!?。，；：！？、…·"
    "\"'“”‘’「」『』（）()[]【】《》〈〉"
)


def normalize_unit_text(text: Any) -> str:
    """单元内文本归一（契约 §3.1，纯确定性、零 LLM）。

    顺序：strip → 连续空白折叠为单空格 → 去首尾标点 → 小写（**仅 ASCII**）。

    只做 ASCII 小写是刻意的：.lower() 会把土耳其语 İ、希腊语 Σ 等改成不同字符，
    导致「同一文本」在两处归一后不一致；中文/其它脚本本无大小写，无需处理。
    """
    s = str(text) if text is not None else ""
    if not s:
        return ""
    s = _WS_RE.sub(" ", s).strip()
    s = s.strip(_TRIM_PUNCT)
    return s.translate(_ASCII_LOWER)


def unit_meta(record: Any) -> dict[str, Any] | None:
    """取出 record 上的 ephemeral 单元元数据（无/非法形态 → None）。"""
    meta = record.get(UNIT_META_KEY) if isinstance(record, dict) else None
    return meta if isinstance(meta, dict) else None


class _UnitTextIndex:
    """单元内「归一文本 → memory_id」索引。

    同文本对应多条记忆时按 ``side > fact > main`` 取（见 _ROLE_PRIORITY），
    同优先级内按 record 顺序取先出现者——两种取法都只为了「连到更具体的那条」，
    绝不跨单元猜测。
    """

    def __init__(self) -> None:
        self._cands: dict[str, list[dict[str, Any]]] = {}

    def add(self, text: Any, memory_id: str | None, role: str, order: int) -> bool:
        """登记一条文本；返回是否产生**歧义**（同归一文本对应多个 memory_id）。"""
        key = normalize_unit_text(text)
        if not key or not memory_id:
            return False
        bucket = self._cands.setdefault(key, [])
        for cand in bucket:
            if cand["memory_id"] == memory_id:
                return False  # 同一记忆的 content / main_content 重复登记，不算歧义
        bucket.append({"memory_id": memory_id, "role": role, "order": order})
        return len({c["memory_id"] for c in bucket}) > 1

    def resolve_key(self, key: str) -> str | None:
        bucket = self._cands.get(key) or []
        if not bucket:
            return None
        return min(
            bucket,
            key=lambda c: (_ROLE_PRIORITY.get(c["role"], _ROLE_PRIORITY_UNKNOWN), c["order"]),
        )["memory_id"]

    def resolve(self, text: Any) -> str | None:
        return self.resolve_key(normalize_unit_text(text))


def _new_stats() -> dict[str, int]:
    return {
        "units": 0,
        "records": 0,
        "belongs_to": 0,
        "causes": 0,
        "causes_unmapped": 0,
        "causes_self": 0,
        "belongs_to_skipped": 0,
        #: 已登记到主干 id 的非 split fact 文本数（W05b：causes 端点的可映射源）
        "facts_mapped": 0,
        "ambiguous": 0,
        "no_meta": 0,
        "no_id": 0,
        "errors": 0,
    }


def _resolve_role(meta: dict[str, Any], default: str) -> str:
    role = str(meta.get("role") or "").strip().lower()
    return role if role in (ROLE_MAIN, ROLE_SIDE) else default


def _write_one_unit(
    conn: sqlite3.Connection,
    unit_key: str,
    rows: list[tuple[int, dict[str, Any], dict[str, Any]]],
    stats: dict[str, int],
) -> int:
    """处理一个单元：建 side→main 归属边 + causes 因果边。返回本次写入的边数。"""
    written = 0
    index = _UnitTextIndex()
    main_id: str | None = None
    tagged: list[tuple[int, dict[str, Any], str]] = []
    for order, rec, meta in rows:
        role = _resolve_role(meta, ROLE_MAIN if not tagged else ROLE_SIDE)
        mid = rec.get("memory_id")
        tagged.append((order, rec, role))
        if index.add(rec.get("content"), mid, role, order):
            stats["ambiguous"] += 1
            logger.warning(
                "L2 单元边文本歧义（同归一文本对应多条记忆，已按 side > fact > main 取）: "
                "unit_key=%s text=%r",
                unit_key, normalize_unit_text(rec.get("content")),
            )
        # 主干全文也登记一份：content 可能被 MAIN_MAX_CHARS 截断，而 causes 的
        # from/to 引用的是主干原句——只能靠同一 memory_id 映射到主干（不算猜）。
        if role == ROLE_MAIN and meta.get("main_content"):
            if index.add(meta.get("main_content"), mid, role, order):
                stats["ambiguous"] += 1
                logger.warning(
                    "L2 单元边文本歧义（main_content 与侧事实/facts 同文本）: "
                    "unit_key=%s text=%r",
                    unit_key, normalize_unit_text(meta.get("main_content")),
                )
        if role == ROLE_MAIN and main_id is None and mid:
            main_id = mid

    # 0.5) 非 split facts 的文本登记到**主干 id**（role=fact，W05b）：
    # v022 起 causes 端点逐字复制 facts 原文；非 split fact 没有独立记忆，落到
    # 主干是它唯一的正确落点。split fact 不在此登记——它已有自己的 side record，
    # 由上面的记录登记覆盖（避免同一事实同时挂到主干而造成因果错连）。
    # 主干未落库 → facts 无归属，不登记（不猜）。
    if main_id:
        seen_fact_texts: set[str] = set()
        for _order, _rec, meta in rows:
            raw_facts = meta.get("facts")
            if not isinstance(raw_facts, list):
                continue
            for fact in raw_facts:
                if not isinstance(fact, dict) or fact.get("split"):
                    continue
                fkey = normalize_unit_text(fact.get("content"))
                if not fkey or fkey in seen_fact_texts:
                    continue
                seen_fact_texts.add(fkey)
                stats["facts_mapped"] += 1
                if index.add(fact.get("content"), main_id, ROLE_FACT, len(tagged)):
                    stats["ambiguous"] += 1
                    logger.warning(
                        "L2 单元边文本歧义（非 split fact 与其它记忆同文本，"
                        "已按 side > fact > main 取）: unit_key=%s text=%r",
                        unit_key, fkey,
                    )

    # 1) belongs_to：side → main（主事件未落库 → 不建父边，记 WARNING，不猜）
    for order, rec, role in tagged:
        if role != ROLE_SIDE:
            continue
        mid = rec.get("memory_id")
        if not main_id:
            stats["belongs_to_skipped"] += 1
            logger.warning(
                "L2 belongs_to 未建（主事件未落库，侧事实不建父边）: unit_key=%s "
                "side_memory_id=%s content=%r",
                unit_key, mid, (rec.get("content") or "")[:120],
            )
            continue
        if mid == main_id:
            continue  # 防御：side 与 main 同 id（映射退化）→ 自环无意义
        edge_dao.create_edge(
            conn, mid, main_id, REL_BELONGS_TO, weight=EDGE_WEIGHT, source=EDGE_SOURCE,
        )
        stats["belongs_to"] += 1
        written += 1

    # 2) causes：单元内 from/to 两端都能映射才建边（任一端缺失 → 保留 JSON + WARNING）
    causes: list[dict[str, Any]] = []
    seen_causes: set[tuple[str, str]] = set()
    for _order, _rec, meta in rows:
        raw = meta.get("causes")
        if not isinstance(raw, list):
            continue
        for c in raw:
            if not isinstance(c, dict):
                continue
            nf = normalize_unit_text(c.get("from"))
            nt = normalize_unit_text(c.get("to"))
            if not nf or not nt:
                continue
            if (nf, nt) in seen_causes:
                continue
            seen_causes.add((nf, nt))
            causes.append({"from": c.get("from"), "to": c.get("to"), "_nf": nf, "_nt": nt})

    for c in causes:
        nf, nt = c["_nf"], c["_nt"]
        if nf == nt:
            stats["causes_self"] += 1
            continue  # from == to（同文本）→ 不自环
        from_id = index.resolve_key(nf)
        to_id = index.resolve_key(nt)
        if not from_id or not to_id:
            stats["causes_unmapped"] += 1
            missing = "from" if not from_id else "to"
            logger.warning(
                "L2 causes 边未建（端点未在单元内映射，保留 l2_facts_json）: "
                "unit_key=%s from=%r to=%r missing=%s",
                unit_key, c["from"], c["to"], missing,
            )
            continue
        if from_id == to_id:
            stats["causes_self"] += 1
            continue  # 两端映射到同一条记忆 → 自环无意义
        edge_dao.create_edge(
            conn, from_id, to_id, REL_CAUSES, weight=EDGE_WEIGHT, source=EDGE_SOURCE,
        )
        stats["causes"] += 1
        written += 1
    return written


def write_unit_edges(
    conn: sqlite3.Connection,
    records: Iterable[dict[str, Any]] | None,
    *,
    source_ref: str | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """按 L2 单元为一批**已落库**记忆建 belongs_to / causes 边（幂等 + fail-open）。

    Args:
        conn: memory.db 连接。
        records: 记录列表，每条形如
            {content, memory_id, _l2_unit: {unit_key, role, main_content, causes, facts}}。
            facts 为 W05b 新增的单元内事实文本清单（[{content, type, split}]）：
            split=False 的文本登记到主干 id（role=fact），split=True 的由各自侧
            事实记录承载。
            无 memory_id 的（skip / 隔离 / 幂等跳过 / 上游未落库）**一律跳过**——
            skip 与 quarantine 不建边（契约 §4）。无 _l2_unit 的（1.x 路径）同样跳过。
        source_ref: 单元键缺失时的兜底前缀（仅用于分组/日志，不落库）。
        commit: 写过边后是否提交（create_edge 是裸 INSERT，会留下隐式事务；
            后续 insert_memory 要显式 BEGIN，不提交会撞
            "cannot start a transaction within a transaction"）。

    Returns:
        统计 dict（units / records / belongs_to / causes / causes_unmapped /
        causes_self / belongs_to_skipped / facts_mapped / ambiguous / no_meta /
        no_id / errors）。

    ⚠️ **绝不抛异常**：边是增强项，任何异常只 WARNING（契约 §1.4「失败不阻塞落库」）。
    """
    stats = _new_stats()
    if not records:
        return stats

    grouped: dict[str, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {}
    for order, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        meta = unit_meta(rec)
        if not meta:
            stats["no_meta"] += 1  # 1.x 记录 / 已被剥离 → 无单元上下文可建边
            continue
        mid = rec.get("memory_id")
        if not mid:
            stats["no_id"] += 1  # skip / 隔离 / 幂等跳过：本次没有最终 memory_id
            continue
        key = str(meta.get("unit_key") or "").strip() or str(source_ref or "") or f"unit@{order}"
        grouped.setdefault(key, []).append((order, rec, meta))

    written = 0
    for unit_key, rows in grouped.items():
        try:
            written += _write_one_unit(conn, unit_key, rows, stats)
        except Exception as e:  # noqa: BLE001 —— 单个单元失败不影响其它单元与落库主链
            stats["errors"] += 1
            logger.warning(
                "L2 单元边写入异常（不影响落库）: unit_key=%s err=%s", unit_key, e,
            )
            try:
                conn.rollback()  # 丢弃该单元半程写入，避免留下不完整事务
            except Exception:  # noqa: BLE001
                logger.warning("L2 单元边异常后 rollback 失败: unit_key=%s", unit_key)

    if written and commit:
        try:
            conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("L2 单元边提交失败（边可能未落库，不影响记忆）: %s", e)

    stats["records"] = sum(len(v) for v in grouped.values())
    stats["units"] = len(grouped)
    if (written or stats["belongs_to_skipped"] or stats["causes_unmapped"]
            or stats["ambiguous"] or stats["facts_mapped"]):
        logger.info(
            "L2 单元边: units=%d records=%d belongs_to=%d causes=%d "
            "facts=%d unmapped=%d self=%d parent_skipped=%d ambiguous=%d errors=%d",
            stats["units"], stats["records"], stats["belongs_to"], stats["causes"],
            stats["facts_mapped"], stats["causes_unmapped"], stats["causes_self"],
            stats["belongs_to_skipped"], stats["ambiguous"], stats["errors"],
        )
    return stats


def count_unit_edges(conn: sqlite3.Connection) -> dict[str, int]:
    """对账：本路（source='l2_extract'）按关系计数（运维/验收/测试断言用）。"""
    out = {REL_BELONGS_TO: 0, REL_CAUSES: 0}
    for row in conn.execute(
        "SELECT relation, COUNT(*) AS c FROM memory_edges WHERE source=? GROUP BY relation",
        (EDGE_SOURCE,),
    ):
        out[row["relation"]] = row["c"]
    return out
