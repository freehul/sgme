"""ST-40 / T-141 测试：LoCoMo 评测链路（解析 / 灌库 / GT 映射 / 检索 / J-score）。

设计原则：
- **零网络**：全部走 fixture（`eval/fixtures/locomo_mini.json`）与内存桩，
  不依赖 D:\\GitHubDownloads 的真实 2.8MB 数据，也不碰 LLM / embeddings；
- **打真实链路的坑**：多 dia_id 脏 evidence、dia_id 跨 conversation 撞号、
  缺 session 内容、时间解析失败——这几条都是实测踩出来的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlite3

from sgme import config as sgme_config
from sgme.data import db as db_mod

from eval import locomo_eval
from eval.locomo import (
    load_locomo,
    locomo_stats,
    iter_qa,
    normalize_evidence,
    parse_dia_id,
)
from eval.locomo_ingest import (
    IngestConfig,
    build_chunks,
    build_locomo_replica,
    parse_locomo_datetime,
    resolve_evidence,
)

FIXTURE = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "locomo_mini.json"


@pytest.fixture
def convs():
    return load_locomo(FIXTURE)


# ── 解析 ──

def test_load_locomo_structure(convs):
    assert len(convs) == 1
    c = convs[0]
    assert c.sample_id == "conv-mini"
    assert (c.speaker_a, c.speaker_b) == ("Caroline", "Melanie")
    assert len(c.sessions) == 2
    assert c.turn_count == 7
    assert [t.dia_id for t in c.sessions[0].turns] == ["D1:1", "D1:2", "D1:3", "D1:4"]
    assert c.sessions[0].date_time == "1:56 pm on 8 May, 2023"
    assert len(c.qas) == 5


def test_locomo_stats_main_caliber(convs):
    st = locomo_stats(convs)
    assert st["conversations"] == 1 and st["sessions"] == 2 and st["turns"] == 7
    assert st["qa_total"] == 5
    # 主口径 = 剔除 adversarial(1) 且必须有 evidence(再剔除 1 条空 evidence) → 3
    assert st["qa_main"] == 4
    assert st["qa_main_with_evidence"] == 3
    assert st["qa_by_category"]["adversarial"] == 1


def test_iter_qa_default_excludes_adversarial_and_empty(convs):
    qs = list(iter_qa(convs))
    assert len(qs) == 3
    assert all(q.category != 5 for q in qs)
    assert all(q.dia_ids for q in qs)


def test_iter_qa_can_include_adversarial(convs):
    qs = list(iter_qa(convs, include_adversarial=True))
    assert len(qs) == 4


def test_parse_dia_id():
    assert parse_dia_id("D8:17") == (8, 17)
    assert parse_dia_id("D:11") is None
    assert parse_dia_id("garbage") is None


def test_normalize_evidence_handles_dirty_tokens():
    """实测脏数据：分号/空格分隔多 dia_id + 畸形 token。"""
    assert normalize_evidence(["D1:1"]) == ["D1:1"]
    assert normalize_evidence(["D8:6; D9:17"]) == ["D8:6", "D9:17"]
    assert normalize_evidence(["D9:1 D4:4 D4:6"]) == ["D9:1", "D4:4", "D4:6"]
    assert normalize_evidence(["D:11:26", "D"]) == []          # 畸形 token 丢弃
    assert normalize_evidence(["D1:1", "D1:1"]) == ["D1:1"]    # 去重保序
    assert normalize_evidence([]) == []


def test_qa_dia_ids_property_uses_normalized(convs):
    q = [q for q in convs[0].qas if q.qa_index == 2][0]
    assert q.evidence == ["D1:4; D2:3"]          # 原始保持忠实
    assert q.dia_ids == ["D1:4", "D2:3"]         # 派生属性已拆分


def test_parse_locomo_datetime():
    assert parse_locomo_datetime("1:56 pm on 8 May, 2023") == "2023-05-08T13:56:00Z"
    assert parse_locomo_datetime("garbage") is None
    assert parse_locomo_datetime("") is None


# ── 切块 ──

def test_build_chunks_turn_granularity(convs):
    chunks = build_chunks(convs[0], IngestConfig(granularity="turn"))
    assert len(chunks) == 7
    c = [x for x in chunks if x.memory_id == "conv-mini|S1|T3"][0]
    assert "LGBTQ support group" in c.content
    assert c.content.startswith("[1:56 pm on 8 May, 2023]")   # with_date 默认开
    assert c.dia_ids == ["D1:3"]
    assert c.occurred_at == "2023-05-08T13:56:00Z"


def test_build_chunks_session_granularity(convs):
    chunks = build_chunks(convs[0], IngestConfig(granularity="session"))
    assert len(chunks) == 2
    assert chunks[0].dia_ids == ["D1:1", "D1:2", "D1:3", "D1:4"]
    assert chunks[0].memory_id == "conv-mini|S1"


def test_build_chunks_window_does_not_cross_session(convs):
    """window 不跨 session——跨天拼块会让 temporal 类的时间上下文自相矛盾。"""
    chunks = build_chunks(convs[0], IngestConfig(granularity="window", window=3, stride=3))
    # session_1 4 turns → 2 个窗口(3+1)；session_2 3 turns → 1 个窗口
    assert len(chunks) == 3
    for ck in chunks:
        sess = ck.memory_id.split("|")[1]
        for d in ck.dia_ids:
            assert f"D{sess[1:]}" == f"D{d.split(':')[0][1:]}"


def test_build_chunks_without_date(convs):
    chunks = build_chunks(convs[0], IngestConfig(granularity="turn", with_date=False))
    assert not chunks[0].content.startswith("[")


# ── 灌库与 GT 映射 ──

@pytest.fixture
def replica(tmp_path, convs):
    db_path, index = build_locomo_replica(
        tmp_path / "locomo", convs, IngestConfig(granularity="turn")
    )
    yield db_path, index
    # no-op：连接已在 build 内关闭


def test_replica_row_count_and_agent_tag(replica):
    db_path, index = replica
    assert index.memory_count == 7
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT memory_id, agent_tag FROM memories").fetchall()
        assert len(rows) == 7
        # per_conv_agent_tag 默认开 → agent_tag = conv_id（隔离检索的前提）
        assert {r["agent_tag"] for r in rows} == {"conv-mini"}
        src = conn.execute("SELECT COUNT(*) c FROM memory_sources").fetchone()["c"]
        assert src == 7
    finally:
        conn.close()


def test_index_keys_are_conv_scoped(replica):
    """dia_id 跨 conversation 撞号 → 索引 key 必须带 conv 作用域。"""
    _db, index = replica
    assert "conv-mini|D1:3" in index.dia_to_mem
    assert "D1:3" not in index.dia_to_mem          # 裸 dia_id 绝不能作为 key


def test_resolve_evidence_requires_conv_id(replica):
    _db, index = replica
    hit, miss = resolve_evidence(index, ["D1:3"], "conv-mini")
    assert hit == ["conv-mini|S1|T3"] and miss == []
    # 不带 conv_id → 解析不到（防止「忘了传」静默退化成 0 覆盖）
    hit2, miss2 = resolve_evidence(index, ["D1:3"], "")
    assert hit2 == [] and miss2 == ["D1:3"]


def test_build_gt_covers_dirty_evidence(replica, convs):
    _db, index = replica
    gt = locomo_eval.build_gt(convs, index)
    assert gt.qa_total == 3
    assert gt.qa_covered == 3
    assert gt.coverage == 1.0
    multi = [it for it in gt.items if it.qa_index == 2][0]
    # 脏 evidence "D1:4; D2:3" 必须被拆成两条，相关集 = 2
    assert multi.relevant_ids == ["conv-mini|S1|T4", "conv-mini|S2|T3"]
    assert multi.unresolved_dia == []


# ── 检索 ──

def test_run_arm_bm25_recall(replica, convs):
    db_path, index = replica
    gt = locomo_eval.build_gt(convs, index)
    cfg = locomo_eval.arm_cfg("bm25", sgme_config.load_config(), scoped=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        arm = locomo_eval.run_arm(conn, gt, cfg, limit=10, scoped=True)
    finally:
        conn.close()
    assert arm["query_count"] == 3
    assert arm["empty_result_count"] == 0
    # 「LGBTQ support group」这条问句应与 D1:3 高度重合 → recall@1 至少 1/3
    assert arm["recall_at_k"]["recall@10"] > 0
    assert set(arm["by_category"]) == {"temporal", "multi_hop"}


def test_agent_scope_isolates_conversations(tmp_path):
    """检索必须被限制在 QA 所属 conversation 内（否则 recall 被跨 conv 稀释）。"""
    json_path = tmp_path / "two.json"
    one = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    two = json.loads(json.dumps(one))
    two["sample_id"] = "conv-other"
    json_path.write_text(json.dumps([one, two], ensure_ascii=False), encoding="utf-8")

    convs = load_locomo(json_path)
    db_path, index = build_locomo_replica(
        tmp_path / "db", convs, IngestConfig(granularity="turn")
    )
    gt = locomo_eval.build_gt(convs, index)
    assert gt.qa_covered == 6          # 两个 conv 各 3 条
    # 相关集只含本 conv 的记忆（key 带 conv 作用域）
    for it in gt.items:
        for mid in it.relevant_ids:
            assert mid.startswith(it.conv_id)

    cfg = locomo_eval.arm_cfg("bm25", sgme_config.load_config(), scoped=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        arm = locomo_eval.run_arm(conn, gt, cfg, limit=10, scoped=True)
        assert arm["query_count"] == 6
        # 隔离生效：每条查询返回的都应属于同一个 conv
        for it in gt.items:
            res = locomo_eval.search_mod.search_memories(
                conn, None, query=it.question, limit=10,
                include_sources=False, cfg=cfg, agent_id=it.conv_id,
            )
            assert res, "隔离后仍应有结果"
            for r in res:
                assert r["memory_id"].startswith(it.conv_id)
    finally:
        conn.close()


# ── J-score（桩 LLM，零网络）──

def test_judge_score_with_stub_llm(replica, convs):
    db_path, index = replica
    gt = locomo_eval.build_gt(convs, index)
    cfg = locomo_eval.arm_cfg("bm25", sgme_config.load_config(), scoped=True)

    calls: list[str] = []

    def stub(prompt: str) -> str:
        calls.append(prompt)
        if prompt.startswith("You are an impartial judge"):
            return "CORRECT\nkey fact matches"
        return "7 May 2023"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        js = locomo_eval.judge_score(
            conn, gt, cfg=cfg, llm_cfg={}, sample_n=3, top_k=5,
            seed=0, scoped=True, llm_fn=stub,
        )
    finally:
        conn.close()

    assert js["sample_n"] == 3
    assert js["judged"] == 3
    assert js["correct"] == 3
    assert js["j_score"] == 1.0
    assert len(calls) == 6          # 每条 2 次调用：生成 + 判定
    assert set(js["by_category"]) == {"temporal", "multi_hop"}


def test_judge_score_counts_no_context_and_errors(replica, convs):
    """NO CONTEXT 与 LLM 故障必须分开计，绝不能混进 WRONG。"""
    db_path, index = replica
    gt = locomo_eval.build_gt(convs, index)
    cfg = locomo_eval.arm_cfg("bm25", sgme_config.load_config(), scoped=True)

    def stub(prompt: str) -> str:
        if prompt.startswith("You are an impartial judge"):
            return "WRONG"
        return "NO CONTEXT"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        js = locomo_eval.judge_score(
            conn, gt, cfg=cfg, llm_cfg={}, sample_n=3, top_k=5,
            seed=0, scoped=True, llm_fn=stub,
        )
        assert js["no_context"] == 3 and js["judged"] == 0 and js["j_score"] == 0.0

        def broken(prompt: str) -> str:
            return ""          # 模拟 LLMUnavailable

        js2 = locomo_eval.judge_score(
            conn, gt, cfg=cfg, llm_cfg={}, sample_n=3, top_k=5,
            seed=0, scoped=True, llm_fn=broken,
        )
        assert js2["errors"] == 3 and js2["judged"] == 0
    finally:
        conn.close()


# ── 报告 ──

def test_report_md_contains_both_calibers(replica, convs):
    db_path, index = replica
    gt = locomo_eval.build_gt(convs, index)
    cfg = locomo_eval.arm_cfg("bm25", sgme_config.load_config(), scoped=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        arms = {"bm25": locomo_eval.run_arm(conn, gt, cfg, scoped=True)}
    finally:
        conn.close()
    result = {
        "generated_at": "2026-08-31T00:00:00+00:00",
        "data_path": str(FIXTURE),
        "conv_ids": ["conv-mini"],
        "corpus_stats": locomo_stats(convs),
        "ingest_config": index.config,
        "index": {"memory_count": index.memory_count, "granularity": index.granularity},
        "gt": {
            "qa_total": gt.qa_total, "qa_covered": gt.qa_covered,
            "coverage": gt.coverage, "unresolved_dia_count": gt.unresolved_dia_count,
            "by_category": gt.counts_by_category(),
        },
        "scope": {"scoped": True, "mechanism": "test"},
        "arms": arms,
        "jscore": None,
    }
    md = locomo_eval.report_md(result)
    assert "recall@1/3/5/10" in md
    assert "GT 覆盖率" in md
    assert "边界" in md          # 边界声明必须在报告里，防止误读
