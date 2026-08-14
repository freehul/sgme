"""#32 RRF 网格搜索接入——专项验收测试。

覆盖 T05 验收点：
- GT 派生正确性（message / content 双模式 + 跳过规则 + to_ground_truth 格式）
- 落库时序自愈（v0.3：init_fts 晚于 insert ⇒ _ensure_fts_ready 回填 content_seg
  并重建 FTS，召回非空——旧「晚 init ⇒ FTS 空」的最危险静默失败已由在线幂等迁移根治）
- FK 兜底（EvalRunner(cfg={}) 从 registry/*.yaml 兜底加载维度，避免 memory_tags FK 崩溃）
- memory_id 确定性（f"{case_id}#{idx}"，禁用 uuid）
- tie-break 确定性（同输入两次 best_params 一致，可复现）
- 诚实诊断字段正确性（五值 conclusion：no_effect 不伪造 best_k / conclusive 推荐 k /
  bm25_only / below_noise / no_queries，PRD §6.4.3）
- data/ 零生产污染（RRF 搜索全程不写 data/）

FFTS 时序反例与 build_corpus 集成均使用 eval/tmp 级别的临时 DB，与生产 data/ 物理隔离。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from eval.models import EvalCase, GtMemory, L1GroundTruth, RRFMetrics
from eval.retrieval_gt import (
    RetrievalQuery,
    build_corpus,
    derive_queries,
    memory_id_for,
    to_ground_truth,
)
from eval.rrf import RRFGridSearch

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _snapshot_dir(path: Path) -> frozenset:
    """快照目录内容（文件名 + 大小 + mtime），用于零生产污染断言。"""
    if not path.exists():
        return frozenset()
    return frozenset(
        (p.name, p.stat().st_size, p.stat().st_mtime_ns)
        for p in path.iterdir()
        if p.is_file()
    )


# ═══════════════════════════════════════════════════════════════════════
# fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def eval_db(tmp_path):
    """eval 级临时 DB：init_databases + import_registry（**不含** init_fts）。

    不含 init_fts 是为了让时序反例测试能自行控制 init_fts 的调用顺序。
    """
    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.data import memory_dao

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path)
    dims = sgme_config.load_dimensions()
    aliases = sgme_config.load_aliases()
    memory_dao.import_registry(mem_conn, dims, aliases)
    yield mem_conn
    mem_conn.close()
    wiki_conn.close()


# ═══════════════════════════════════════════════════════════════════════
# memory_id 确定性
# ═══════════════════════════════════════════════════════════════════════

class TestMemoryIdDeterminism:
    """memory_id 确定性：可复现评测的前提。"""

    def test_format(self):
        """memory_id_for(case_id, idx) == '{case_id}#{idx}'。"""
        assert memory_id_for("eval-024", 0) == "eval-024#0"
        assert memory_id_for("eval-024", 7) == "eval-024#7"

    def test_reproducible(self):
        """同输入恒同输出（禁用 uuid4）。"""
        assert memory_id_for("c1", 3) == memory_id_for("c1", 3)

    def test_no_uuid(self):
        """memory_id 不得含 uuid 特征（8-4-4-4-12 hex）。"""
        mid = memory_id_for("c1", 0)
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", mid), \
            f"memory_id 不得含 uuid：{mid}"


# ═══════════════════════════════════════════════════════════════════════
# GT 派生正确性
# ═══════════════════════════════════════════════════════════════════════

class TestGroundTruthDerivation:
    """GT 派生：message 模式 / content 模式 / 跳过规则 / to_ground_truth 格式。"""

    def _sample_case(self) -> EvalCase:
        return EvalCase(
            case_id="c1",
            conversation=(
                "[msg#1] 2026-01-01T10:00:00Z user:\n"
                "  我叫张明，在深圳做后端开发，用 Python 和 Go\n"
                "[msg#2] 2026-01-01T10:01:00Z assistant:\n"
                "  记下了\n"
            ),
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="张明在深圳做后端", dimensions=["identity"]),
                GtMemory(content="用 Python 和 Go", dimensions=["tech_stack"]),
            ]),
        )

    def test_message_mode_uses_conversation_body(self):
        """message 模式：query 用对话正文（剥头行），relevant_ids 为同用例全部 GT 记忆。"""
        queries = derive_queries([self._sample_case()], mode="message")
        assert len(queries) == 1
        q = queries[0]
        assert "张明" in q.query and "Python" in q.query
        # 头行被剥离
        assert "[msg#1]" not in q.query
        assert "[msg#2]" not in q.query
        # relevant_ids = 全部 GT 记忆（确定性 id）
        assert q.relevant_ids == ["c1#0", "c1#1"]

    def test_content_mode_uses_gt_memory_content(self):
        """content 模式：query 用 GT 记忆 content 拼接（对照用，非对话正文）。"""
        case = EvalCase(
            case_id="c1",
            conversation="[msg#1] 2026-01-01T10:00:00Z user:\n 无关对话正文\n",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="张明在深圳做后端开发", dimensions=["identity"]),
            ]),
        )
        queries = derive_queries([case], mode="content")
        assert len(queries) == 1
        # content 模式取 GT 记忆 content，而非对话正文
        assert "张明在深圳做后端开发" in queries[0].query
        assert "无关对话正文" not in queries[0].query
        assert queries[0].relevant_ids == ["c1#0"]

    def test_skips_cases_without_queries(self):
        """无 GT 记忆 / 无正文派生 query 的用例被跳过。"""
        no_mem = EvalCase(
            case_id="empty", conversation="x",
            expected_l1=L1GroundTruth(memories=[]),
        )
        no_body = EvalCase(
            case_id="nobody", conversation="",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="y", dimensions=["identity"]),
            ]),
        )
        assert derive_queries([no_mem, no_body], mode="message") == []

    def test_to_ground_truth_merges_duplicate_queries(self):
        """to_ground_truth 转成 {query: [relevant_ids]}，重复 query 合并去重保序。"""
        queries = [
            RetrievalQuery(query="qA", relevant_ids=["c1#0", "c1#1"]),
            RetrievalQuery(query="qA", relevant_ids=["c1#1", "c1#2"]),  # 同 query → 合并
            RetrievalQuery(query="qB", relevant_ids=["c2#0"]),
        ]
        gt = to_ground_truth(queries)
        assert set(gt.keys()) == {"qA", "qB"}
        assert gt["qA"] == ["c1#0", "c1#1", "c1#2"]   # 去重保序
        assert gt["qB"] == ["c2#0"]


# ═══════════════════════════════════════════════════════════════════════
# 落库时序铁律（最危险静默失败反例）
# ═══════════════════════════════════════════════════════════════════════

class TestFtsOrderingInvariant:
    """init_fts 晚于 insert 也能自愈：_ensure_fts_ready 回填 content_seg + 重建 FTS。

    v0.3 方案 §3.3：init_fts 入口做「列内容回填 + FTS 重建」（在线幂等），
    不再依赖「必须先 init_fts 再 insert」的触发器时序——晚 init 不再静默丢索引。
    """

    def test_recall_self_heals_when_init_after_insert(self, eval_db):
        """反例反转：init_fts 晚于 insert ⇒ 回填 content_seg + 重建，召回非空。"""
        from sgme.data.search import init_fts
        from sgme.data import memory_dao

        memory_dao.insert_memory(
            eval_db, content="Python FastAPI 底座", memory_type="persona",
            priority=50, time_velocity="static", ttl_days=None,
            dimension_ids=["tech_stack"],
        )
        init_fts(eval_db)  # ← 晚于 insert（旧实现：已有行不回填 FTS；新实现：自愈重建）

        rows = eval_db.execute(
            "SELECT m.memory_id FROM memories_fts f "
            "JOIN memories m ON m.rowid=f.rowid WHERE memories_fts MATCH ?",
            ('"Python"',),
        ).fetchall()
        assert len(rows) >= 1, \
            "init_fts 晚于 insert 时 _ensure_fts_ready 应回填 content_seg 并重建 FTS（自愈）"

    def test_recall_hit_when_init_before_insert(self, eval_db):
        """对照：init_fts 早于 insert ⇒ FTS5 MATCH 正常召回。"""
        from sgme.data.search import init_fts
        from sgme.data import memory_dao

        init_fts(eval_db)  # ← 早于 insert（正确时序）
        memory_dao.insert_memory(
            eval_db, content="Python FastAPI 底座", memory_type="persona",
            priority=50, time_velocity="static", ttl_days=None,
            dimension_ids=["tech_stack"],
        )
        rows = eval_db.execute(
            "SELECT m.memory_id FROM memories_fts f "
            "JOIN memories m ON m.rowid=f.rowid WHERE memories_fts MATCH ?",
            ('"Python"',),
        ).fetchall()
        assert len(rows) >= 1, "init_fts 早于 insert 时 FTS 应正常召回"


# ═══════════════════════════════════════════════════════════════════════
# FK 兜底（P0-0A）
# ═══════════════════════════════════════════════════════════════════════

class TestRegistryFallback:
    """EvalRunner(cfg={}) 时从 registry/*.yaml 兜底加载维度，避免 memory_tags FK 崩溃。"""

    def test_load_eval_registry_fallback(self, tmp_path):
        from eval.runner import EvalRunner
        from sgme.data import db as db_mod
        from sgme.data import memory_dao

        runner = EvalRunner(cfg={})
        dimensions, aliases = runner._load_eval_registry()
        assert dimensions, "cfg 无 dimensions 时兜底加载应返回非空维度列表"
        assert isinstance(aliases, dict)

        # 用兜底维度建库并落一条带维度标签的记忆，不应触发 FK 错误
        mem_conn, _, _ = db_mod.init_databases(tmp_path)
        memory_dao.import_registry(mem_conn, dimensions, aliases)
        reg_id = mem_conn.execute(
            "SELECT id FROM dimension_registry LIMIT 1"
        ).fetchone()[0]
        # insert_memory 未显式传 memory_id 时会自生成（uuid），
        # 必须捕获返回值才能定位 memory_tags
        mid = memory_dao.insert_memory(
            mem_conn, content="测试记忆", memory_type="persona",
            priority=50, time_velocity="static", ttl_days=None,
            dimension_ids=[reg_id],
        )
        tag = mem_conn.execute(
            "SELECT dimension_id FROM memory_tags WHERE memory_id=?", (mid,)
        ).fetchone()
        assert tag is not None and tag[0] == reg_id, "memory_tags 写入应成功（FK 兜底生效）"
        mem_conn.close()


# ═══════════════════════════════════════════════════════════════════════
# build_corpus 集成（确定性 memory_id + 查询派生 + data 零污染）
# ═══════════════════════════════════════════════════════════════════════

class TestBuildCorpus:
    """build_corpus 落库 memory_id 确定性 + 派生查询相关集 + 不写 data/。"""

    def test_memory_ids_and_queries(self, eval_db):
        case = EvalCase(
            case_id="c1",
            conversation="[msg#1] 2026-01-01T10:00:00Z user:\n 张明在深圳用 Python\n",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="张明在深圳", dimensions=["identity"]),
                GtMemory(content="用 Python", dimensions=["tech_stack"]),
            ]),
        )
        corpus = build_corpus(
            eval_db, [case], cfg=None, enable_vector=False, gt_mode="message",
        )
        # memory_id 确定性
        assert corpus.memory_ids == ["c1#0", "c1#1"]
        for i, mid in enumerate(corpus.memory_ids):
            assert mid == memory_id_for("c1", i)
        # 派生查询：relevant_ids 为同用例全部 GT 记忆
        assert len(corpus.queries) == 1
        assert corpus.queries[0].relevant_ids == ["c1#0", "c1#1"]
        # 显式跳过向量 ⇒ vector_available=False
        assert corpus.vector_available is False

    def test_no_data_pollution(self, eval_db):
        """build_corpus 落库只发生在 eval/tmp 级 DB，不写 data/。"""
        data_dir = PROJECT_ROOT / "data"
        before = _snapshot_dir(data_dir)

        case = EvalCase(
            case_id="c1",
            conversation="[msg#1] 2026-01-01T10:00:00Z user:\n 张明在深圳\n",
            expected_l1=L1GroundTruth(memories=[
                GtMemory(content="张明在深圳", dimensions=["identity"]),
            ]),
        )
        build_corpus(eval_db, [case], cfg=None, enable_vector=False, gt_mode="message")

        after = _snapshot_dir(data_dir)
        assert after == before, f"build_corpus 污染了 data/：{after ^ before}"


# ═══════════════════════════════════════════════════════════════════════
# ★ 诚实诊断：绝不伪造 best_k
# ═══════════════════════════════════════════════════════════════════════

class TestHonestDiagnosis:
    """ndcg_spread < EPS ⇒ inconclusive + recommended_k=None（不伪造 best_k）。

    架构师实证：当前评测集上 rrf_k 对 NDCG 零区分度（根因 FTS5 unicode61 对中文
    整段切 token，BM25 中位仅 1 条）。任何用 tie-break 选出的 best_k 都是噪声。
    """

    @staticmethod
    def _constant_query_fn() -> callable:
        """任意 k 返回相同结果 ⇒ NDCG 完全相同 ⇒ 极差 0（触发 inconclusive）。"""
        def query_fn(q: str, params: dict) -> list[str]:
            return ["x#0"]
        return query_fn

    @staticmethod
    def _spread_query_fn() -> callable:
        """rrf_k 越大命中越多 ⇒ NDCG 随 k 变化（触发 discriminative）。"""
        def query_fn(q: str, params: dict) -> list[str]:
            k = int(params.get("rrf_k", 60))
            n = (k // 30) % 4
            return [f"{q}#{i}" for i in range(n)]
        return query_fn

    def test_inconclusive_when_ndcg_spread_zero(self):
        """零区分度 + 向量起 + 低重叠 ⇒ inconclusive_no_effect + recommended_k=None。

        best_params 只是 tie-break 形式产物，不得被解读为推荐值（PRD §6.4.3）。
        """
        gt = {"q1": ["x#0"]}
        m = RRFGridSearch().search(
            self._constant_query_fn(), gt, k=10,
            meta={"vector_available": True, "route_overlap_jaccard": 0.07},
        )

        assert isinstance(m, RRFMetrics)
        assert m.ndcg_spread < 1e-6
        assert m.discriminative is False
        assert m.conclusion == "inconclusive_no_effect"
        assert m.recommended_k is None
        # best_params 仍存在（tie-break 确定性产物），但不得被解读为推荐
        assert set(m.best_params.keys()) == {"rrf_k"}

    def test_discriminative_recommends_k(self):
        """存在区分度且向量起 ⇒ conclusive + recommended_k=最佳 rrf_k（允许真实推荐）。"""
        gt = {"q1": ["q1#0", "q1#1", "q1#2"], "q2": ["q2#0"]}
        m = RRFGridSearch().search(
            self._spread_query_fn(), gt, k=10,
            meta={"vector_available": True, "route_overlap_jaccard": 0.5},
        )

        assert m.discriminative is True
        assert m.conclusion == "conclusive"
        assert m.recommended_k is not None
        assert m.recommended_k in {10, 30, 60, 90, 120}

    def test_tie_break_deterministic(self):
        """tie-break 确定性：同输入两次 best_params 完全一致（可复现性验收前提）。"""
        gt = {"q1": ["x#0"]}
        m1 = RRFGridSearch().search(self._constant_query_fn(), gt, k=10)
        m2 = RRFGridSearch().search(self._constant_query_fn(), gt, k=10)

        assert m1.best_params == m2.best_params
        assert m1.best_ndcg10 == m2.best_ndcg10
        # 零区分度下两者都不得伪造推荐值
        assert m1.recommended_k is None and m2.recommended_k is None

    def test_no_queries_conclusion(self):
        """ground_truth 为空 ⇒ conclusion=no_queries，不抛异常。"""
        m = RRFGridSearch().search(lambda q, p: [], {}, k=10)
        assert m.conclusion == "no_queries"
        assert m.query_count == 0
        assert m.recommended_k is None


# ═══════════════════════════════════════════════════════════════════════
# data/ 零生产污染（RRF 搜索本身）
# ═══════════════════════════════════════════════════════════════════════

class TestZeroProductionPollution:
    """RRF 网格搜索全程不写入 data/（零生产污染）。"""

    def test_rrf_search_no_data_pollution(self):
        data_dir = PROJECT_ROOT / "data"
        before = _snapshot_dir(data_dir)

        def query_fn(q: str, params: dict) -> list[str]:
            k = int(params.get("rrf_k", 60))
            return [f"{q}#{i}" for i in range((k // 30) % 4)]

        RRFGridSearch().search(query_fn, {"q1": ["q1#0"]}, k=10)

        after = _snapshot_dir(data_dir)
        assert after == before, f"RRF 搜索污染了 data/：{after ^ before}"


# ═══════════════════════════════════════════════════════════════════════
# P0-1 / P0-2：vector_available 语义 + 任意条失败即熔断
# ═══════════════════════════════════════════════════════════════════════

def _cases_with_memories(n: int, case_id: str = "cv") -> list[EvalCase]:
    """构造含 n 条 GT 记忆的单条用例（内容各不相同，便于逐条追踪）。"""
    return [EvalCase(
        case_id=case_id,
        conversation="[msg#1] 2026-01-01T10:00:00Z user:\n 张明在深圳用 Python 做后端\n",
        expected_l1=L1GroundTruth(memories=[
            GtMemory(content=f"记忆内容第 {i} 条", dimensions=["identity"])
            for i in range(n)
        ]),
    )]


_VEC_CFG = {"search": {"vector": {"enabled": True, "model": "fake-embed-model"}}}


class TestVectorAvailableSemantics:
    """★ P0-1：`vector_available` 仅在 100% 覆盖时为 True，部分覆盖一律 False。

    为什么不能用 `vector_count > 0`：
    部分覆盖时 `memory_vectors` 只有子集有行，向量路永远召不回另一部分，
    两路失去可比性，NDCG 随「这次嵌上了多少条」漂移（实测 0.9546 ↔ 0.5691）。
    """

    def test_full_coverage_is_available(self, eval_db, monkeypatch):
        """全部嵌入成功 ⇒ available=True，banner_reason 为空。"""
        from sgme.data.search import vector as vector_mod
        monkeypatch.setattr(
            vector_mod, "upsert_memory_vector",
            lambda conn, mid, text, cfg, client=None: True,
        )
        corpus = build_corpus(
            eval_db, _cases_with_memories(5), cfg=_VEC_CFG, enable_vector=True,
        )
        assert corpus.vector_count == 5
        assert corpus.size == 5
        assert corpus.vector_available is True
        assert corpus.banner_reason == ""
        assert corpus.vector_coverage == 1.0
        assert corpus.vector_failed_at is None

    def test_partial_coverage_is_not_available(self, eval_db, monkeypatch):
        """★ 部分覆盖 ⇒ available=False + banner_reason='vector_partial_2/5'。"""
        from sgme.data.search import vector as vector_mod
        calls: list[str] = []

        def fake_upsert(conn, mid, text, cfg, client=None):
            calls.append(mid)
            return len(calls) <= 2      # 前 2 条成功，第 3 条失败

        monkeypatch.setattr(vector_mod, "upsert_memory_vector", fake_upsert)
        corpus = build_corpus(
            eval_db, _cases_with_memories(5), cfg=_VEC_CFG, enable_vector=True,
        )
        assert corpus.vector_count == 2
        assert corpus.size == 5
        assert corpus.vector_available is False, \
            "部分覆盖必须判定为不可用（PRD 硬约定），否则 NDCG 会随覆盖率漂移"
        assert corpus.banner_reason == "vector_partial_2/5"
        assert corpus.vector_coverage == 0.4

    def test_zero_coverage_reason(self, eval_db, monkeypatch):
        """一条都没成功 ⇒ banner_reason='vector_unavailable_0/5'。"""
        from sgme.data.search import vector as vector_mod
        monkeypatch.setattr(
            vector_mod, "upsert_memory_vector",
            lambda conn, mid, text, cfg, client=None: False,
        )
        corpus = build_corpus(
            eval_db, _cases_with_memories(5), cfg=_VEC_CFG, enable_vector=True,
        )
        assert corpus.vector_count == 0
        assert corpus.vector_available is False
        assert corpus.banner_reason == "vector_unavailable_0/5"

    def test_skipped_by_flag_reason(self, eval_db):
        """--rrf-skip-vector ⇒ banner_reason='vector_skipped_by_flag'。"""
        corpus = build_corpus(
            eval_db, _cases_with_memories(3), cfg=_VEC_CFG, enable_vector=False,
        )
        assert corpus.vector_available is False
        assert corpus.banner_reason == "vector_skipped_by_flag"

    def test_no_cfg_reason(self, eval_db):
        """无 cfg（拿不到 embeddings 端点）⇒ banner_reason='vector_no_cfg'。"""
        corpus = build_corpus(
            eval_db, _cases_with_memories(3), cfg=None, enable_vector=True,
        )
        assert corpus.vector_available is False
        assert corpus.banner_reason == "vector_no_cfg"

    def test_empty_corpus_reason(self, eval_db):
        """空语料 ⇒ banner_reason='empty_corpus'，且不会误判为可用。"""
        corpus = build_corpus(eval_db, [], cfg=_VEC_CFG, enable_vector=True)
        assert corpus.size == 0
        assert corpus.vector_available is False
        assert corpus.banner_reason == "empty_corpus"

    def test_banner_reason_pure_function(self):
        """`vector_banner_reason` 判定表（优先级：空语料 > 跳过 > 无 cfg > 全失败 > 部分）。"""
        from eval.retrieval_gt import vector_banner_reason as brf

        assert brf(0, 0, True, True) == "empty_corpus"
        assert brf(5, 0, False, True) == "vector_skipped_by_flag"
        assert brf(5, 0, True, False) == "vector_no_cfg"
        assert brf(5, 0, True, True) == "vector_unavailable_0/5"
        assert brf(5, 3, True, True) == "vector_partial_3/5"
        assert brf(5, 5, True, True) == ""


class TestEmbedCircuitBreaker:
    """★ P0-2：任意一条嵌入失败即熔断（不是只看首条）。"""

    def test_breaks_on_any_failure_not_just_first(self, eval_db, monkeypatch):
        """第 3 条失败 ⇒ 只调用 3 次，剩余 3 条完全不再尝试。"""
        from sgme.data.search import vector as vector_mod
        calls: list[str] = []

        def fake_upsert(conn, mid, text, cfg, client=None):
            calls.append(mid)
            return len(calls) != 3      # 第 3 次失败

        monkeypatch.setattr(vector_mod, "upsert_memory_vector", fake_upsert)
        corpus = build_corpus(
            eval_db, _cases_with_memories(6), cfg=_VEC_CFG, enable_vector=True,
        )
        assert len(calls) == 3, \
            f"任意条失败即熔断，应只调用 3 次，实际 {len(calls)} 次（旧实现只在首条熔断）"
        assert corpus.vector_count == 2
        assert corpus.vector_failed_at == 2      # 0-based
        assert corpus.vector_available is False

    def test_first_failure_still_breaks(self, eval_db, monkeypatch):
        """首条失败仍然熔断（回归保护：修 P0-2 不能把原行为改坏）。"""
        from sgme.data.search import vector as vector_mod
        calls: list[str] = []

        def fake_upsert(conn, mid, text, cfg, client=None):
            calls.append(mid)
            return False

        monkeypatch.setattr(vector_mod, "upsert_memory_vector", fake_upsert)
        corpus = build_corpus(
            eval_db, _cases_with_memories(6), cfg=_VEC_CFG, enable_vector=True,
        )
        assert len(calls) == 1
        assert corpus.vector_failed_at == 0


# ═══════════════════════════════════════════════════════════════════════
# P0-3：embedding 磁盘缓存
# ═══════════════════════════════════════════════════════════════════════

class TestEmbedCache:
    """`sha256(content) + model` 磁盘缓存：命中 / 未命中 / dims 防御。"""

    def test_key_is_sha256_of_content(self):
        """key 就是 utf-8 内容的 sha256（可离线复算，便于排障）。"""
        import hashlib

        from eval.embed_cache import content_key

        text = "张明在深圳用 Python"
        assert content_key(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert content_key("") == content_key(None)  # type: ignore[arg-type]

    def test_roundtrip_hit(self, tmp_path):
        """写入后可命中，且向量按 float32 精度往返一致。"""
        from eval.embed_cache import EmbedCache

        vec = [0.125, -0.5, 0.75, 1.0]
        with EmbedCache(tmp_path / "c.sqlite") as cache:
            assert cache.put("hello", "m1", vec) is True
            got = cache.get("hello", "m1")
            assert got == vec            # 这些值 float32 可精确表示
            assert cache.stats.hits == 1
            assert cache.stats.writes == 1
            assert cache.row_count() == 1

    def test_miss_on_unknown_content(self, tmp_path):
        """未写入过的内容 ⇒ 未命中（返回 None，计 misses）。"""
        from eval.embed_cache import EmbedCache

        with EmbedCache(tmp_path / "c.sqlite") as cache:
            assert cache.get("nope", "m1") is None
            assert cache.stats.misses == 1
            assert cache.stats.hits == 0

    def test_model_is_part_of_key(self, tmp_path):
        """同内容不同模型 ⇒ 未命中（不同模型的向量不可混检）。"""
        from eval.embed_cache import EmbedCache

        with EmbedCache(tmp_path / "c.sqlite") as cache:
            cache.put("hello", "m1", [0.5, 0.5])
            assert cache.get("hello", "m1") is not None
            assert cache.get("hello", "m2") is None

    def test_dims_mismatch_is_miss(self, tmp_path):
        """★ dims 与期望不符 ⇒ 视为未命中（防御同名模型换代）。"""
        from eval.embed_cache import EmbedCache

        path = tmp_path / "c.sqlite"
        with EmbedCache(path) as cache:
            cache.put("hello", "m1", [0.1] * 768)

        # 换代后期望 1024 维：旧的 768 维行必须判为未命中，绝不能脏命中
        with EmbedCache(path, expected_dims=1024) as cache2:
            assert cache2.get("hello", "m1") is None
            assert cache2.stats.dims_mismatch == 1
            assert cache2.stats.misses == 1

    def test_dims_locked_within_process(self, tmp_path):
        """同一进程内首次见到的 dims 会被锁定，后续不同 dims 的写入被拒。"""
        from eval.embed_cache import EmbedCache

        with EmbedCache(tmp_path / "c.sqlite") as cache:
            assert cache.put("a", "m1", [0.1, 0.2, 0.3]) is True
            assert cache.put("b", "m1", [0.1, 0.2]) is False
            assert cache.stats.dims_mismatch == 1

    def test_readonly_does_not_write(self, tmp_path):
        """readonly=True ⇒ put 不写盘（用于验证归档缓存的完整性）。"""
        from eval.embed_cache import EmbedCache

        with EmbedCache(tmp_path / "c.sqlite", readonly=True) as cache:
            assert cache.put("hello", "m1", [0.1, 0.2]) is False
            assert cache.row_count() == 0

    def test_empty_vector_not_cached(self, tmp_path):
        """空向量不入库（否则会把一次失败固化成永久错误答案）。"""
        from eval.embed_cache import EmbedCache

        with EmbedCache(tmp_path / "c.sqlite") as cache:
            assert cache.put("hello", "m1", []) is False
            assert cache.row_count() == 0

    def test_seed_copy(self, tmp_path):
        """种子库存在而目标不存在 ⇒ 整库拷贝（归档只读 + 运行时写副本场景）。"""
        from eval.embed_cache import EmbedCache

        seed = tmp_path / "seed.sqlite"
        with EmbedCache(seed) as c:
            c.put("hello", "m1", [0.25, 0.5])

        work = tmp_path / "work.sqlite"
        with EmbedCache(work, seed_path=seed) as c2:
            assert work.exists()
            assert c2.get("hello", "m1") == [0.25, 0.5]

    def test_stats_dict_json_safe(self, tmp_path):
        """stats_dict 可直接 json.dumps（要写进 report.json）。"""
        import json

        from eval.embed_cache import EmbedCache

        with EmbedCache(tmp_path / "c.sqlite") as cache:
            cache.put("hello", "m1", [0.5])
            cache.get("hello", "m1")
            payload = json.dumps(cache.stats_dict())
        assert "hits" in payload and "rows" in payload


class TestEmbedCacheHook:
    """缓存钩子装到 `sgme.search.vector.embed()` 上：命中即零网络。"""

    def test_hit_returns_without_network(self, tmp_path):
        """缓存命中时不解析 base_url、不发 HTTP —— 离线 CI 的前提。"""
        from eval.embed_cache import EmbedCache
        from sgme.data.search import vector as vector_mod

        cache = EmbedCache(tmp_path / "c.sqlite")
        cache.put("离线文本", "fake-embed-model", [0.25, 0.5, 0.75])

        # cfg 故意不含 llm.chains ⇒ 一旦走真实分支必然返回 None
        cfg = {"search": {"vector": {"model": "fake-embed-model"}}}
        prev = vector_mod.set_embed_cache(cache)
        try:
            got = vector_mod.embed("离线文本", cfg)
        finally:
            vector_mod.set_embed_cache(prev)
            cache.close()

        assert got == [0.25, 0.5, 0.75], "缓存命中应直接返回向量，不触网"

    def test_no_cache_installed_is_unchanged(self):
        """未安装缓存时行为与改动前一致（生产路径零影响）。"""
        from sgme.data.search import vector as vector_mod

        assert vector_mod.get_embed_cache() is None
        # cfg 无 llm.chains ⇒ 解析 base_url 失败 ⇒ 返回 None（原有行为）
        assert vector_mod.embed("x", {"search": {"vector": {}}}) is None


# ═══════════════════════════════════════════════════════════════════════
# P0-4：route_overlap_jaccard
# ═══════════════════════════════════════════════════════════════════════

class TestRouteOverlapJaccard:
    """★ 两路 top-N 的 Jaccard：区分「两路同源」与「评测集真无分辨力」。"""

    @staticmethod
    def _hits(ids: list[str]) -> list[dict]:
        """把 memory_id 列表包成召回结果结构。"""
        return [{"memory_id": m, "content": m, "priority": 50,
                 "updated_at": "2026-01-01T00:00:00Z", "score": 1.0} for m in ids]

    def test_jaccard_math(self):
        """|∩|/|∪| 基本性质：全同 1.0 / 全异 0.0 / 空并集 0.0。"""
        from eval.runner import EvalRunner

        assert EvalRunner._jaccard({"a", "b"}, {"a", "b"}) == 1.0
        assert EvalRunner._jaccard({"a"}, {"b"}) == 0.0
        assert EvalRunner._jaccard(set(), set()) == 0.0
        assert EvalRunner._jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_field_exists_on_metrics(self):
        """RRFMetrics 必须真的有 route_overlap_jaccard 字段（不是 report 里的 .get 默认值）。"""
        assert "route_overlap_jaccard" in RRFMetrics.__dataclass_fields__
        assert RRFMetrics().route_overlap_jaccard == 0.0

    def test_homologous_routes_jaccard_one(self):
        """两路召回完全相同 ⇒ Jaccard=1.0（同源，融合无信息增益）。"""
        from eval.runner import EvalRunner

        ids = ["m1", "m2", "m3"]
        cache = {("q1", (), 20): (self._hits(ids), self._hits(ids))}
        diag = EvalRunner._recall_diagnostics(cache)

        assert diag["route_overlap_jaccard"] == 1.0
        assert diag["route_overlap_jaccard_dual"] == 1.0
        assert diag["dual_route_queries"] == 1

    def test_disjoint_routes_jaccard_zero(self):
        """两路完全不相交 ⇒ Jaccard=0.0（解耦，融合有信息增益）。"""
        from eval.runner import EvalRunner

        cache = {("q1", (), 20): (self._hits(["a", "b"]), self._hits(["c", "d"]))}
        diag = EvalRunner._recall_diagnostics(cache)

        assert diag["route_overlap_jaccard"] == 0.0
        assert diag["route_overlap_avg"] == 0.0
        assert diag["dual_route_queries"] == 1

    def test_partial_overlap_average(self):
        """多 query 取均值：(1/3 + 1.0) / 2。"""
        from eval.runner import EvalRunner

        cache = {
            ("q1", (), 20): (self._hits(["a", "b"]), self._hits(["b", "c"])),
            ("q2", (), 20): (self._hits(["x"]), self._hits(["x"])),
        }
        diag = EvalRunner._recall_diagnostics(cache)
        assert diag["route_overlap_jaccard"] == pytest.approx(
            round((1 / 3 + 1.0) / 2, 4), abs=1e-4
        )

    def test_single_route_counts_as_zero(self):
        """向量路为空 ⇒ Jaccard=0（不计入 dual，均值不被虚高）。"""
        from eval.runner import EvalRunner

        cache = {("q1", (), 20): (self._hits(["a"]), [])}
        diag = EvalRunner._recall_diagnostics(cache)

        assert diag["route_overlap_jaccard"] == 0.0
        assert diag["dual_route_queries"] == 0
        assert diag["jaccard_dual_query_count"] == 0
        assert diag["route_overlap_jaccard_dual"] == 0.0

    def test_empty_cache_keys_present(self):
        """空缓存也必须给出全部 Jaccard 键（reporter 不做 .get 兜底猜值）。"""
        from eval.runner import EvalRunner

        diag = EvalRunner._recall_diagnostics({})
        for key in (
            "route_overlap_jaccard",
            "route_overlap_jaccard_top10",
            "route_overlap_jaccard_dual",
            "route_overlap_top_n",
        ):
            assert key in diag

    def test_top_n_truncation(self):
        """top-10 口径按顺序截断（第 11 条之后不参与 top-10 Jaccard）。"""
        from eval.runner import EvalRunner

        bm25 = self._hits([f"m{i}" for i in range(20)])
        vec = self._hits([f"m{i}" for i in range(10, 30)])
        diag = EvalRunner._recall_diagnostics({("q", (), 20): (bm25, vec)})

        # top-20：交集 m10..m19 共 10 条，并集 30 条 ⇒ 1/3
        assert diag["route_overlap_jaccard"] == pytest.approx(round(10 / 30, 4), abs=1e-4)
        # top-10：bm25 取 m0..m9，vec 取 m10..m19 ⇒ 无交集
        assert diag["route_overlap_jaccard_top10"] == 0.0


class TestReportCarriesJaccard:
    """report.json 的 rrf 段必须真实携带 route_overlap_jaccard 与向量覆盖明细。"""

    def test_report_json_fields(self):
        from eval.models import EvalResult, EvalSummary
        from eval.reporter import _build_json_report

        rrf = RRFMetrics(
            best_ndcg10=0.5,
            route_overlap_jaccard=0.1234,
            vector_count=41,
            corpus_size=84,
            vector_coverage=0.4881,
            banner_reason="vector_partial_41/84",
            conclusion="inconclusive_bm25_only",
            recall_diagnostics={"route_overlap_jaccard": 0.1234},
            embed_cache={"hits": 84, "misses": 0, "writes": 0, "rows": 134},
        )
        result = EvalResult(run_id="r1", rrf=rrf, summary=EvalSummary())
        payload = _build_json_report(result)

        assert payload["rrf"]["route_overlap_jaccard"] == 0.1234
        assert payload["rrf"]["vector_count"] == 41
        assert payload["rrf"]["vector_coverage"] == 0.4881
        assert payload["rrf"]["banner_reason"] == "vector_partial_41/84"
        assert payload["rrf"]["embed_cache"]["hits"] == 84

    def test_report_md_renders_jaccard_and_banner(self, tmp_path):
        from eval.models import EvalResult, EvalSummary
        from eval.reporter import generate_report_md

        rrf = RRFMetrics(
            best_ndcg10=0.5,
            all_results=[{"rrf_k": 60, "ndcg10": 0.5, "ndcg5": 0.5,
                          "ndcg_k": 10, "query_count": 1}],
            best_params={"rrf_k": 60},
            route_overlap_jaccard=0.1234,
            vector_count=41,
            corpus_size=84,
            vector_coverage=0.4881,
            banner_reason="vector_partial_41/84",
            conclusion="inconclusive_bm25_only",
            recall_diagnostics={
                "cached_queries": 1, "route_overlap_jaccard": 0.1234,
                "dual_route_queries": 1,
            },
        )
        result = EvalResult(run_id="r1", rrf=rrf, summary=EvalSummary())
        md_path = generate_report_md(result, tmp_path)
        text = md_path.read_text(encoding="utf-8")

        assert "route_overlap_jaccard" in text
        assert "0.1234" in text
        assert "vector_partial_41/84" in text
        assert "41/84" in text
        # 诚实性：inconclusive 时不得出现「推荐 k」的可复制配置块
        assert "维持 `search.rrf.k: 60` 不变" in text


# ═══════════════════════════════════════════════════════════════════════
# PRD §6.4.3：五值 conclusion 枚举（生产侧分流）
# ═══════════════════════════════════════════════════════════════════════

class TestFiveValueConclusion:
    """五值 conclusion 生产侧分流（vector_available × ndcg_spread，PRD §6.4.3）。"""

    @staticmethod
    def _constant_query_fn():
        def query_fn(q: str, params: dict) -> list[str]:
            return ["x#0"]
        return query_fn

    def test_vector_off_maps_bm25_only(self):
        """vector=false 且有查询 ⇒ inconclusive_bm25_only（融合未跑，无数据）。"""
        m = RRFGridSearch().search(self._constant_query_fn(), {"q1": ["x#0"]}, k=10)
        assert m.conclusion == "inconclusive_bm25_only"
        assert m.recommended_k is None

    def test_vector_on_spread_zero_high_overlap_falls_back_no_effect(self):
        """vector=true + spread≈0 + jaccard≥J_LOW（高重叠同源，PRD 未显式覆盖）
        ⇒ 仍归 inconclusive_no_effect（k 无作用点；归因由 reporter 按 jaccard 分流）。"""
        m = RRFGridSearch().search(
            self._constant_query_fn(), {"q1": ["x#0"]}, k=10,
            meta={"vector_available": True, "route_overlap_jaccard": 0.8},
        )
        assert m.conclusion == "inconclusive_no_effect"
        assert m.recommended_k is None

    def test_below_noise_when_spread_between_tie_and_sig(self):
        """vector=true + 0 < spread < NDCG_SIG ⇒ inconclusive_below_noise。"""
        grid = RRFGridSearch()
        grid._results = [
            {"rrf_k": 10, "ndcg10": 0.5000},
            {"rrf_k": 30, "ndcg10": 0.5003},
        ]
        grid._best = {"rrf_k": 10, "ndcg10": 0.5000}
        diag = grid._diagnose(
            ["q1"],
            {"q1": [("a",), ("a",)]},
            meta={"vector_available": True, "route_overlap_jaccard": 0.3},
        )
        assert diag["ndcg_spread"] == pytest.approx(0.0003, abs=1e-6)
        assert diag["conclusion"] == "inconclusive_below_noise"
        assert diag["recommended_k"] is None

    def test_conclusive_only_sets_recommended_k(self):
        """vector=true + spread ≥ NDCG_SIG ⇒ conclusive，唯一允许 recommended_k 非 None。"""
        grid = RRFGridSearch()
        grid._results = [
            {"rrf_k": 10, "ndcg10": 0.5000},
            {"rrf_k": 30, "ndcg10": 0.6000},
        ]
        grid._best = {"rrf_k": 30, "ndcg10": 0.6000}
        diag = grid._diagnose(
            ["q1"],
            {"q1": [("a",), ("a",)]},
            meta={"vector_available": True, "route_overlap_jaccard": 0.3},
        )
        assert diag["conclusion"] == "conclusive"
        assert diag["recommended_k"] == 30


# ═══════════════════════════════════════════════════════════════════════
# PRD §6.4.3：低 Jaccard 归因修正（伪解耦 vs 评测集缺乏分辨力）
# ═══════════════════════════════════════════════════════════════════════

class TestJaccardVerdictAttribution:
    """低 Jaccard 归因须结合 bm25_avg_recall（修正归因写反 bug）。"""

    def test_low_jaccard_bm25_degenerate_reports_pseudo_decoupled(self):
        """bm25_avg_recall < 3 ⇒ 报「BM25 退化（伪解耦）」，不再归因评测集缺乏分辨力。"""
        from eval.reporter import _jaccard_verdict

        lines = _jaccard_verdict(0.0746, 34, {
            "bm25_avg_recall": 1.74,
            "bm25_median_recall": 1.0,
            "queries_with_empty_bm25": 16,
            "cached_queries": 50,
            "route_overlap_avg": 1.52,
        })
        assert any("伪解耦" in l for l in lines)
        assert any("BM25 退化" in l for l in lines)
        assert not any("缺乏分辨力" in l for l in lines)

    def test_low_jaccard_healthy_bm25_reports_lack_resolution(self):
        """bm25_avg_recall ≥ 3 ⇒ 才归因「评测集缺乏分辨力」。"""
        from eval.reporter import _jaccard_verdict

        lines = _jaccard_verdict(0.10, 34, {
            "bm25_avg_recall": 5.0,
            "bm25_median_recall": 5.0,
            "queries_with_empty_bm25": 0,
            "cached_queries": 50,
            "route_overlap_avg": 2.0,
        })
        assert any("缺乏分辨力" in l for l in lines)
        assert not any("伪解耦" in l for l in lines)

    def test_high_jaccard_reports_same_source(self):
        """Jaccard 高 ⇒ 两路同源假象（不受 bm25_avg_recall 影响）。"""
        from eval.reporter import _jaccard_verdict

        lines = _jaccard_verdict(0.7, 34, {"bm25_avg_recall": 1.74})
        assert any("同源" in l for l in lines)
        assert not any("伪解耦" in l for l in lines)
        assert not any("缺乏分辨力" in l for l in lines)
