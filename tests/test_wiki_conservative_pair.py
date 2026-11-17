# -*- coding: utf-8 -*-
"""tests/test_wiki_conservative_pair.py：A-3 保守配对器（T-402，dry-run 只读）。

判据矩阵（依据 docs/plans/T-388-A-3-wiki取代范围定义-20261116.md 判据纪律）：
1. 同题精确相等 = 一级判据；同前缀仅召回（跨题候选一律 low 档）
2. 标题含日期 token 的跨日期台账 → 禁止配对（历史序列，真链在同题同日内）
3. 指针核对：显式指向=strong；缺失/自指=候选补链（medium）；指向第三页=skip（R1 审计管）
4. 方向校验用 ingested_at（旧页必须更早）
5. 家族前缀防误配：同前缀不同题且相似度不过阈 → skip
6. draft 不是退休态，不参与配对
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wiki_conservative_pair.py"
_spec = importlib.util.spec_from_file_location("wiki_conservative_pair", _SCRIPT)
cp = importlib.util.module_from_spec(_spec)
sys.modules["wiki_conservative_pair"] = cp
_spec.loader.exec_module(cp)


def _page(pid, title, status="superseded", supersedes="", ingested="2026-01-01T00:00:00Z",
          content="正文内容。"):
    return {
        "page_id": pid, "title": title, "status": status, "supersedes": supersedes,
        "ingested_at": ingested, "content": content,
    }


# ---------- 判据矩阵 ----------

def test_exact_title_explicit_pointer_is_high():
    """同题 + 显式指针指向 + 方向正确 → high 档候选对。"""
    old = _page("old", "同题页", "superseded", "new", "2026-01-01T00:00:00Z")
    new = _page("new", "同题页", "active", "", "2026-01-02T00:00:00Z")
    pairs, skipped = cp.pair_pages([old, new])
    assert len(pairs) == 1 and not skipped
    p = pairs[0]
    assert p["title_match"] == "exact" and p["pointer_state"] == "explicit"
    assert p["confidence"] == "high" and (p["old_page_id"], p["new_page_id"]) == ("old", "new")


def test_exact_title_missing_pointer_is_medium():
    """同题但旧页无指针（候选补链）→ medium，不静默跳过。"""
    old = _page("old", "同题页", "superseded", "", "2026-01-01T00:00:00Z")
    new = _page("new", "同题页", "active", "", "2026-01-02T00:00:00Z")
    pairs, _ = cp.pair_pages([old, new])
    assert len(pairs) == 1
    assert pairs[0]["pointer_state"] == "missing" and pairs[0]["confidence"] == "medium"


def test_exact_title_self_pointer_with_active_is_medium():
    """同题但旧页自指且组内有 active（链断裂嫌疑）→ medium。"""
    old = _page("old", "同题页", "superseded", "old", "2026-01-01T00:00:00Z")
    new = _page("new", "同题页", "active", "", "2026-01-02T00:00:00Z")
    pairs, _ = cp.pair_pages([old, new])
    assert len(pairs) == 1
    assert pairs[0]["pointer_state"] == "self" and pairs[0]["confidence"] == "medium"


def test_cross_date_ledger_titles_are_skipped():
    """标题含不同日期 token（跨日期台账=历史序列）→ 禁止配对。"""
    a = _page("a", "github-ledger-2026-09-20", "superseded", "", "2026-09-20T01:00:00Z")
    b = _page("b", "github-ledger-2026-09-22", "active", "", "2026-09-22T01:00:00Z")
    pairs, skipped = cp.pair_pages([a, b])
    assert pairs == []
    assert any(s["reason"] == "date_token_conflict" for s in skipped)


def test_same_date_ledger_chain_pairs():
    """同题同日台账（同日期 token）→ 允许配对（真链在同题同日内）。"""
    a = _page("a", "github-ledger-2026-09-29", "superseded", "b", "2026-09-29T10:00:00Z")
    b = _page("b", "github-ledger-2026-09-29", "superseded", "", "2026-09-29T10:00:46Z")
    pairs, _ = cp.pair_pages([a, b])
    assert len(pairs) == 1 and pairs[0]["title_match"] == "exact"


def test_family_prefix_low_similarity_skipped():
    """同前缀不同题且正文相似度不过阈（家族兄弟技能）→ skip。"""
    a = _page("a", "skill:parallel-dev-orchestration", "superseded", "", "2026-01-01T00:00:00Z", content="共享骨架：# 标题\n## 用法\n" + "alpha 独有第X段排障步骤与参数说明。" * 25)
    b = _page("b", "skill:parallel-execution-optimizer", "active", "", "2026-01-02T00:00:00Z", content="共享骨架：# 标题\n## 用法\n" + "beta 独有第X段安装流程与环境变量。" * 25)
    pairs, skipped = cp.pair_pages([a, b])
    assert pairs == []
    assert any(s["reason"] == "similarity_below_threshold" for s in skipped)


def test_family_prefix_high_similarity_is_low_candidate():
    """同前缀不同题但正文高度相似（伪重复嫌疑）→ low 档候选（宁漏不错下仍报出供人工）。"""
    body = "完全相同的正文内容。" * 40
    a = _page("a", "skill:hyperframes-cli", "superseded", "", "2026-01-01T00:00:00Z", content=body + "A尾部微差")
    b = _page("b", "skill:hyperframes-core", "active", "", "2026-01-02T00:00:00Z", content=body + "B尾部微差")
    pairs, _ = cp.pair_pages([a, b])
    assert len(pairs) == 1
    assert pairs[0]["title_match"] == "prefix" and pairs[0]["confidence"] == "low"


def test_pointer_to_third_party_skipped():
    """旧页指针指向第三页（非本组候选）→ skip（错链归 R1 审计管）。"""
    old = _page("old", "同题页", "superseded", "other", "2026-01-01T00:00:00Z")
    new = _page("new", "同题页", "active", "", "2026-01-02T00:00:00Z")
    other = _page("other", "同题页", "superseded", "new", "2026-01-01T06:00:00Z")
    pairs, skipped = cp.pair_pages([old, new, other])
    # old 的显式指针指向 other → (old,new) 不配；old 与 other 正常配对；other 与 new 配对
    assert ("old", "new") not in {(p["old_page_id"], p["new_page_id"]) for p in pairs}
    assert ("old", "other") in {(p["old_page_id"], p["new_page_id"]) for p in pairs}
    assert any(s["reason"] == "pointer_points_elsewhere" for s in skipped)


def test_wrong_direction_skipped():
    """旧页 ingested_at 晚于新页（方向校验失败）→ skip。"""
    old = _page("old", "同题页", "superseded", "new", "2026-01-03T00:00:00Z")
    new = _page("new", "同题页", "active", "", "2026-01-02T00:00:00Z")
    pairs, skipped = cp.pair_pages([old, new])
    assert pairs == []
    assert any(s["reason"] == "direction_invalid" for s in skipped)


def test_draft_pages_excluded():
    """draft 不是退休态，不参与配对（T-392E 口径）。"""
    old = _page("old", "同题页", "superseded", "", "2026-01-01T00:00:00Z")
    d = _page("d", "同题页", "draft", "", "2026-01-03T00:00:00Z")
    pairs, _ = cp.pair_pages([old, d])
    assert pairs == []


def test_retired_group_no_pair():
    """C1 整组自指下线（无 active 后继）→ 不产出配对（无后继即无对）。"""
    a = _page("a", "skill:iot-hardware-projects", "superseded", "a", "2026-08-16T14:41:00Z")
    b = _page("b", "skill:iot-hardware-projects", "superseded", "b", "2026-08-16T14:42:00Z")
    pairs, _ = cp.pair_pages([a, b])
    assert pairs == []


def test_three_version_chain_pairs_adjacent():
    """三版本同题链：每个旧页各产一对（链式），方向均正确。"""
    v1 = _page("v1", "链页", "superseded", "v2", "2026-01-01T00:00:00Z")
    v2 = _page("v2", "链页", "superseded", "v3", "2026-01-02T00:00:00Z")
    v3 = _page("v3", "链页", "active", "", "2026-01-03T00:00:00Z")
    pairs, _ = cp.pair_pages([v1, v2, v3])
    assert {(p["old_page_id"], p["new_page_id"]) for p in pairs} == {("v1", "v2"), ("v2", "v3")}


# ---------- 加载与输出契约 ----------

def test_load_pages_reads_content(tmp_path):
    """load_pages 从 sqlite 读全量页（含 content 供相似度）。"""
    db = tmp_path / "wiki.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT, status TEXT,"
        " supersedes TEXT, ingested_at TEXT, content TEXT)"
    )
    conn.execute(
        "INSERT INTO wiki_pages VALUES ('p1','t','active','','2026-01-01T00:00:00Z','hello')"
    )
    conn.commit()
    pages = cp.load_pages(conn)
    conn.close()
    assert len(pages) == 1 and pages[0]["content"] == "hello"


def test_report_stable_fields(tmp_path):
    """输出字段契约稳定（喂 T-404 的 JSON 契约）。"""
    old = _page("old", "同题页", "superseded", "new", "2026-01-01T00:00:00Z")
    new = _page("new", "同题页", "active", "", "2026-01-02T00:00:00Z")
    pairs, _ = cp.pair_pages([old, new])
    required = {
        "old_page_id", "old_title", "old_status", "old_ingested_at",
        "new_page_id", "new_title", "new_status", "new_ingested_at",
        "title_match", "shared_prefix_len", "content_similarity",
        "pointer_state", "confidence",
    }
    assert required <= set(pairs[0].keys())
