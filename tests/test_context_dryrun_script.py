# -*- coding: utf-8 -*-
"""tests/test_context_dryrun_script.py：W11-W12 存量回填 dry-run 脚本测试。

覆盖（脚本 = scripts/context_backfill_dryrun.py，全部离线、零真实 DB）：
1. 文本归一 / bigram / jaccard / l2_facts_json 容错（与写入侧同口径）；
2. 只读连接（mode=ro + query_only）：写尝试必须失败、行数不变化；
3. 基线统计（active / l2 非空 / 有 causes / causes 总数 / 边分布）；
4. 存量 causes 三级可映射率（unit_strict / same_source / global_exact）与失败原因；
5. 歧义端点（同文本多条 active）不计入可映射；
6. 按钮类近重复聚类 + 保留建议（带 causes 优先、其次最长）；
7. 全库 blocking 聚类 + 抽样 N 组；
8. CLI：--json-out / --report-out / --render-json 与 mode 分支。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "context_backfill_dryrun.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("context_backfill_dryrun", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["context_backfill_dryrun"] = mod
    spec.loader.exec_module(mod)
    return mod


dry = _load_script()

SCHEMA = """
CREATE TABLE memories (
  memory_id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT,
  updated_at TEXT,
  l2_facts_json TEXT
);
CREATE TABLE memory_sources (memory_id TEXT, source_ref TEXT, source_type TEXT);
CREATE TABLE memory_edges (
  edge_id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, relation TEXT,
  weight REAL, source TEXT
);
"""


# ---------- helpers ----------

def _mkdb(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _mem(conn, mid, content, *, status="active", updated_at="2026-01-01T00:00:00Z",
         l2=None, source_ref=None) -> None:
    conn.execute(
        "INSERT INTO memories (memory_id, content, status, created_at, updated_at, l2_facts_json) "
        "VALUES (?,?,?,?,?,?)",
        (mid, content, status, updated_at, updated_at,
         json.dumps(l2, ensure_ascii=False) if l2 is not None else None),
    )
    if source_ref:
        conn.execute(
            "INSERT INTO memory_sources (memory_id, source_ref, source_type) VALUES (?,?,?)",
            (mid, source_ref, "session"),
        )


def _edge(conn, frm, to, relation="causes", weight=1.0, source="l2_extract") -> None:
    conn.execute(
        "INSERT INTO memory_edges (edge_id, from_id, to_id, relation, weight, source) "
        "VALUES (?,?,?,?,?,?)",
        (f"{frm}::{to}::{relation}", frm, to, relation, weight, source),
    )


CAUSES_MAIN = {
    "facts": [],
    "causes": [
        {"from": "深色底对比度不足", "to": "按钮统一白字"},      # c1
        {"from": "不存在的文本", "to": "按钮统一白字"},          # c2（from 缺失）
        {"from": "同文本", "to": "同文本"},                      # c3（self）
        {"from": "提交按钮反馈", "to": "按钮统一白字"},          # c4（strict 只 from 命中）
    ],
}


def _causes_db(tmp_path) -> sqlite3.Connection:
    conn = _mkdb(tmp_path)
    _mem(conn, "m-main", "提交按钮反馈", l2=CAUSES_MAIN, source_ref="f1:0")
    _mem(conn, "m-side1", "按钮统一白字", source_ref="f1:0")
    _mem(conn, "m-side2", "深色底对比度不足", source_ref="f1:0")
    _mem(conn, "m-other", "与本次单元无关的普通记忆", source_ref="f2:0")
    _mem(conn, "m-rejected", "已拒收记忆", status="rejected",
         l2={"facts": [], "causes": [{"from": "a", "to": "b"}]})
    _edge(conn, "m-side1", "m-main", "belongs_to")
    _edge(conn, "m-side1", "m-side2", "similar", source="l1_conflict")
    conn.commit()
    return conn


# ---------- 1. 纯函数 ----------

def test_normalize_text_matches_writer_contract():
    assert dry.normalize_text("  Hello,  世界。 ") == "hello, 世界"
    assert dry.normalize_text(None) == ""
    assert dry.normalize_text("   ") == ""


def test_parse_l2_facts_tolerant():
    assert dry.parse_l2_facts(None) == {"facts": [], "causes": []}
    assert dry.parse_l2_facts("not-json{{") == {"facts": [], "causes": []}
    parsed = dry.parse_l2_facts({
        "facts": [{"content": "x"}],
        "causes": [{"from": "A", "to": "B"}, {"from": "", "to": "B"}, "bad"],
    })
    assert len(parsed["facts"]) == 1
    assert parsed["causes"] == [{"from": "A", "to": "B"}]


def test_jaccard_and_bigrams():
    assert dry.jaccard(dry.bigrams("按钮白字"), dry.bigrams("按钮白字")) == 1.0
    assert dry.jaccard(dry.bigrams("按钮白字"), dry.bigrams("")) == 0.0
    assert dry.jaccard(dry.bigrams("完全无关甲"), dry.bigrams("完全无关乙")) < 1.0


# ---------- 2. 只读纪律 ----------

def test_open_db_readonly_rejects_writes(tmp_path):
    db = tmp_path / "ro.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    ro = dry.open_db_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO memories (memory_id, content, status) VALUES ('x','y','active')"
            )
        assert ro.total_changes == 0
    finally:
        ro.close()


def test_open_db_readonly_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        dry.open_db_readonly(tmp_path / "nope.db")


# ---------- 3. 基线 ----------

def test_analyze_baseline_counts(tmp_path):
    conn = _causes_db(tmp_path)
    before = conn.total_changes
    base = dry.analyze_baseline(conn)
    assert base["active"] == 4          # m-rejected 不计入 active
    assert base["l2_facts_json_non_empty"] == 2   # m-main + m-rejected
    assert base["with_causes"] == 2
    assert base["causes_total"] == 5    # main 4 + rejected 1
    assert base["edges_total"] == 2
    assert base["edges_by_relation"] == {"belongs_to": 1, "similar": 1}
    assert conn.total_changes == before      # 分析期零写入
    conn.close()


# ---------- 4/5. causes 三级可映射率 ----------

def test_analyze_causes_three_levels(tmp_path):
    conn = _causes_db(tmp_path)
    before = conn.total_changes
    out = dry.analyze_causes(conn)

    assert out["l2_rows"] == 2
    # 只统计有 causes 的记录；m-rejected 也有 1 条 causes → 2 行 + 5 条
    assert out["rows_with_causes"] == 2
    assert out["causes_total"] == 5

    strict = out["levels"]["unit_strict"]
    # strict 只有主干自身 content 可映射：c4 的 to（按钮统一白字）缺失
    assert strict["mappable"] == 0
    assert strict["rows_with_any_mappable"] == 0

    same_src = out["levels"]["same_source"]
    # c1 两端命中；c4 两端命中（main/side）；c2 from 缺失；c3 self；+ rejected 行 c? ("a"→"b") 两端缺失
    # rejected 行：a/b 都不在任何 active 内容中 → from_missing
    assert same_src["mappable"] == 2
    assert same_src["self"] == 1
    assert same_src["from_missing"] >= 1
    assert same_src["rows_with_any_mappable"] == 1

    glob = out["levels"]["global_exact"]
    assert glob["mappable"] == 2
    assert glob["rate"] == pytest.approx(2 / 5)
    assert conn.total_changes == before
    conn.close()


def test_analyze_causes_ambiguous_endpoint_not_mappable(tmp_path):
    conn = _mkdb(tmp_path)
    _mem(conn, "m1", "主干事件", l2={
        "facts": [],
        "causes": [{"from": "主干事件", "to": "重复文本"}],
    }, source_ref="f1:0")
    # 同一文本两条 active → 映射歧义（不猜）
    _mem(conn, "dup1", "重复文本", source_ref="f1:0")
    _mem(conn, "dup2", "重复文本", source_ref="f1:0")
    conn.commit()

    out = dry.analyze_causes(conn)
    glob = out["levels"]["global_exact"]
    assert glob["mappable"] == 0
    assert glob["to_ambiguous"] == 1
    conn.close()


# ---------- 6. 按钮类聚类 + 保留建议 ----------

BUTTON_A = "提交按钮黑字看不清要求改成白色配实色底禁用低对比方案统一界面规范"
BUTTON_B = "提交按钮黑字看不清要求改成白色配实色底禁用低对比方案统一界面规则"
BUTTON_D = "提交按钮黑字看不清要求改成白色配实色底禁用低对比方案统一界面细则"
DEPLOY_G = "这周的部署流水线全部通过且没有出现任何回滚事故"


def _near_dup_db(tmp_path) -> sqlite3.Connection:
    conn = _mkdb(tmp_path)
    # 按钮类近重复：A/B/D 两两 sim ≥ 0.85（B 带 causes → 应保留 B，即便 D 更长）
    _mem(conn, "btn-a", BUTTON_A, updated_at="2026-01-01T00:00:00Z")
    _mem(conn, "btn-b", BUTTON_B, updated_at="2026-01-02T00:00:00Z",
         l2={"facts": [], "causes": [{"from": "对比度不足", "to": "按钮白字"}]})
    _mem(conn, "btn-d", BUTTON_D, updated_at="2026-01-03T00:00:00Z")
    # 不含按钮关键词的精确重复（测「最长」保留规则 + 全库聚类）
    _mem(conn, "dep-g", DEPLOY_G, updated_at="2026-01-01T00:00:00Z")
    _mem(conn, "dep-h", DEPLOY_G + "。", updated_at="2026-01-02T00:00:00Z")
    # 干扰项：既非按钮类也非重复
    _mem(conn, "noise", "完全无关的天气记录内容", updated_at="2026-01-01T00:00:00Z")
    conn.commit()
    return conn


def test_near_dup_button_clusters_and_keep(tmp_path):
    conn = _near_dup_db(tmp_path)
    before = conn.total_changes
    out = dry.analyze_near_dup(conn, sample_groups=100)

    btn = out["button_class"]
    # W02 基线口径（按钮 AND 白）与配色分析口径在本数据集上同样命中 3 条
    assert btn["baseline_count"] == 3
    assert btn["baseline_whitescreen"] == 0
    assert btn["count"] == 3                     # A/B/D 命中配色词口径
    assert btn["with_causes"] == 1
    # 词法分层：A-B=0.9375（high）、A-D/B-D≈0.879（gray）→ 0.85 阈值下 1 簇 3 条
    assert btn["pair_sim_stats"]["pairs_total"] == 3
    assert btn["pair_sim_stats"]["pair_counts"]["ge_0_92"] == 1
    assert btn["cluster_stats"]["ge_0_85"]["clusters"] == 1
    assert btn["strict_cluster_count"] == 1
    # 保留优先候选 = 带 causes 的那条
    rec = btn["recommend_summary"]
    assert rec["keep_candidates_with_causes"] == 1
    assert rec["bare_candidates_without_causes"] == 2
    assert rec["auto_archive_candidates"] is None
    assert [m["memory_id"] for m in btn["keep_candidates"]] == ["btn-b"]
    # 弱分组（0.4）仍给出 keep/archive 建议，带 causes 优先
    group = btn["groups"][0]
    assert {m["memory_id"] for m in group["members"]} == {"btn-a", "btn-b", "btn-d"}
    assert group["keep"] == "btn-b"
    assert set(group["archive"]) == {"btn-a", "btn-d"}
    assert conn.total_changes == before
    conn.close()


def test_near_dup_whole_db_blocking_and_sampling(tmp_path):
    conn = _near_dup_db(tmp_path)
    before = conn.total_changes
    out = dry.analyze_near_dup(conn, sample_groups=100)

    whole = out["whole_db"]
    assert whole["clusters_found"] == 2          # 按钮簇 + 部署精确重复簇
    assert whole["size_distribution"] == {"2": 1, "3": 1}
    assert whole["sampling"]["sampled"] == 2
    # 部署簇：无 causes → 取最长（dep-h 带句号更长）
    dep = [g for g in whole["groups"] if "dep-h" in {m["memory_id"] for m in g["members"]}][0]
    assert dep["keep"] == "dep-h"
    assert dep["tier"] == "high"
    assert set(dep["archive"]) == {"dep-g"}
    rec = whole["recommend_summary"]
    assert rec["archive_candidates"] == 3
    assert rec["tier_split"]["high"] == {"groups": 1, "archive_candidates": 1}
    assert rec["tier_split"]["gray"] == {"groups": 1, "archive_candidates": 2}
    assert conn.total_changes == before
    conn.close()


# ---------- 7. CLI ----------

def test_cli_json_and_report_outputs(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    _mem(conn, "btn-a", BUTTON_A)
    _mem(conn, "btn-b", BUTTON_B,
         l2={"facts": [], "causes": [{"from": "对比度不足", "to": "按钮白字"}]})
    _mem(conn, "other", "普通记忆内容")
    conn.commit()
    conn.close()

    json_out = tmp_path / "out" / "dryrun.json"
    md_out = tmp_path / "out" / "dryrun.md"
    rc = dry.main([
        "--db", str(db_path), "--mode", "all",
        "--json-out", str(json_out), "--report-out", str(md_out),
    ])
    assert rc == 0
    assert json_out.is_file() and md_out.is_file()
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert set(report.keys()) == {"generated_at", "db", "baseline", "causes_mapping", "near_dup"}
    assert report["db"]["mode"] == "readonly"
    md = md_out.read_text(encoding="utf-8")
    assert "存量上下文回填 dry-run 报告" in md
    assert "未执行任何写入" in md
    assert "保留" in md

    # --render-json：只读渲染，输出可复现
    md2 = tmp_path / "out" / "dryrun2.md"
    assert dry.main(["--render-json", str(json_out), "--report-out", str(md2)]) == 0
    assert md2.read_text(encoding="utf-8") == md


def test_cli_mode_causes_only(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _mkdb(tmp_path)
    _mem(conn, "m1", "主干", l2=CAUSES_MAIN, source_ref="f1:0")
    conn.commit()
    conn.close()
    json_out = tmp_path / "causes.json"
    assert dry.main(["--db", str(db_path), "--mode", "causes",
                     "--json-out", str(json_out)]) == 0
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert "causes_mapping" in report
    assert "near_dup" not in report
