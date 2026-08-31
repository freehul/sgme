"""#32 / T-129 真实库副本回归基线——专项验收测试。

覆盖：
- 合成 mini 副本结构完整（FTS 索引 + scene/supersession 边）
- sample_memories 确定性（同 seed 可复现）
- multi_hop_pairs 产出 scene 簇（相关集 ≥2）+ supersession（live 后继）
- build_realdb_gt 形态 + to_ground_truth 格式
- GT 保存/加载往返一致
- run_realdb 端到端 + 可复现（两次 recall@k 逐字段相等）
- 0-token：真实库基线不写 data/（零生产污染）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eval.models import EvalResult, EvalSummary, RRFMetrics
from eval.realdb import (
    RealDbGt,
    build_realdb_gt,
    make_mini_replica,
    multi_hop_pairs,
    open_replica,
    replica_corpus_stats,
    sample_memories,
)
from eval.runner import EvalRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _snapshot_dir(path: Path) -> frozenset:
    if not path.exists():
        return frozenset()
    return frozenset(
        (p.name, p.stat().st_size, p.stat().st_mtime_ns)
        for p in path.iterdir() if p.is_file()
    )


class TestMiniReplica:
    """合成 mini 副本：结构完整，可直接当真实库副本用。"""

    def test_builds_fts_and_edges(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        assert db.exists()
        conn = open_replica(db, readonly=True)
        try:
            size = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status != 'rejected'"
            ).fetchone()[0]
            # mini#0 被归档移出 memories → 剩 11 条 live
            assert size == 11

            arc = conn.execute(
                "SELECT COUNT(*) FROM memory_archive WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            assert arc == 1

            sm = conn.execute("SELECT COUNT(*) FROM scene_memories").fetchone()[0]
            assert sm == 2

            # FTS 索引已建：能按关键词召回
            rows = conn.execute(
                "SELECT m.memory_id FROM memories_fts f "
                "JOIN memories m ON m.rowid=f.rowid WHERE memories_fts MATCH ?",
                ('"深圳"',),
            ).fetchall()
            assert len(rows) >= 1, "mini 副本 FTS 未就绪，召回为空"
        finally:
            conn.close()


class TestSampleMemories:
    """抽样确定性（可复现评测前提）。"""

    def test_deterministic_by_seed(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            a = sample_memories(conn, 5, seed=0)
            b = sample_memories(conn, 5, seed=0)
            assert [m["memory_id"] for m in a] == [m["memory_id"] for m in b]
            c = sample_memories(conn, 5, seed=1)
            assert len(c) == 5
        finally:
            conn.close()

    def test_returns_all_when_n_exceeds(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            allm = sample_memories(conn, 999, seed=0)
            assert len(allm) == 11   # live 记忆总数（不含已归档）
        finally:
            conn.close()


class TestMultiHopPairs:
    """multi-hop 边读取：scene 簇相关集 ≥2 + supersession 指向 live 后继。"""

    def test_scene_and_supersession_present(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            pairs = multi_hop_pairs(conn)
            scene = [p for p in pairs if p.kind == "scene"]
            super_pairs = [p for p in pairs if p.kind == "supersession"]
            assert len(scene) >= 1
            assert len(scene[0].related_ids) >= 2   # 图召回护栏：相关集 ≥2
            assert len(super_pairs) >= 1
            # supersession 相关集 = live 后继，可检索
            assert super_pairs[0].related_ids
            assert super_pairs[0].anchor_content   # 旧记忆内容（query 文本来源）
        finally:
            conn.close()


class TestBuildRealdbGt:
    """GT 形态 + to_ground_truth + 保存/加载往返。"""

    def test_shape_and_ground_truth(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            gt = build_realdb_gt(conn, sample_n=8, multi_hop_ratio=0.3, seed=0)
            assert len(gt.items) > 0
            multi = [it for it in gt.items if it.hop_type != "single"]
            assert len(multi) >= 1
            # 至少一条 multi 的 relevant 集合 ≥2（图召回护栏来源）
            assert any(len(it.relevant_ids) >= 2 for it in multi)

            gtd = gt.to_ground_truth()
            assert set(gtd.keys()) == {it.query for it in gt.items}
            for rel in gtd.values():
                assert all(isinstance(x, str) for x in rel)
                assert len(rel) >= 1   # 相关集非空（recall 有意义的前提）
        finally:
            conn.close()

    def test_save_load_roundtrip(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            gt = build_realdb_gt(conn, sample_n=8, multi_hop_ratio=0.3, seed=0)
            p = tmp_path / "gt.json"
            gt.save(p)
            loaded = RealDbGt.load(p)
            assert loaded.source == gt.source
            assert len(loaded.items) == len(gt.items)
            assert loaded.items[0].query == gt.items[0].query
            assert loaded.items[0].relevant_ids == gt.items[0].relevant_ids
            assert loaded.items[0].hop_type == gt.items[0].hop_type
        finally:
            conn.close()


class TestRunRealdb:
    """end-to-end + 可复现 + 零污染。"""

    def test_end_to_end_and_reproducible(self, tmp_path):
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            stats = replica_corpus_stats(conn)
            gt = build_realdb_gt(conn, sample_n=8, multi_hop_ratio=0.3, seed=0)
            runner = EvalRunner(cfg=None, rrf_skip_vector=True)
            m1 = runner.run_realdb(
                gt, conn, corpus_size=stats["size"], vector_available=False,
            )
            m2 = runner.run_realdb(
                gt, conn, corpus_size=stats["size"], vector_available=False,
            )
            assert isinstance(m1, RRFMetrics)
            assert m1.recall_at_k is not None
            assert m1.recall_at_k.query_count == len(gt.items)
            # 可复现：两次 recall@k 逐字段相等
            assert m1.recall_at_k.as_dict() == m2.recall_at_k.as_dict()
            assert m1.best_ndcg10 == m2.best_ndcg10
            # 0-token BM25 基线：诚实结论应为 bm25_only
            assert m1.conclusion == "inconclusive_bm25_only"
        finally:
            conn.close()

    def test_zero_data_pollution(self, tmp_path):
        data_dir = PROJECT_ROOT / "data"
        before = _snapshot_dir(data_dir)
        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            stats = replica_corpus_stats(conn)
            gt = build_realdb_gt(conn, sample_n=8, multi_hop_ratio=0.3, seed=0)
            runner = EvalRunner(cfg=None, rrf_skip_vector=True)
            runner.run_realdb(gt, conn, corpus_size=stats["size"], vector_available=False)
        finally:
            conn.close()
        after = _snapshot_dir(data_dir)
        assert after == before, f"realdb 基线污染了 data/：{after ^ before}"


class TestReportCarriesRealdbRecall:
    """realdb 报告也必须真实携带 recall_at_k（不靠 .get 兜底）。"""

    def test_report_md_renders_recall(self, tmp_path):
        from eval.reporter import generate_report_md

        db = make_mini_replica(tmp_path, n=12, seed=0)
        conn = open_replica(db, readonly=True)
        try:
            stats = replica_corpus_stats(conn)
            gt = build_realdb_gt(conn, sample_n=8, multi_hop_ratio=0.3, seed=0)
            runner = EvalRunner(cfg=None, rrf_skip_vector=True)
            metrics = runner.run_realdb(
                gt, conn, corpus_size=stats["size"], vector_available=False,
            )
            result = EvalResult(
                run_id="r1", rrf=metrics, summary=EvalSummary(),
            )
            md_path = generate_report_md(result, tmp_path)
            text = md_path.read_text(encoding="utf-8")
            assert "召回率 @k" in text
            assert "recall@1" in text
        finally:
            conn.close()
