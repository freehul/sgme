"""eval/locomo_eval.py：ST-40 / T-141 —— LoCoMo 业界标准评测。

双口径（方案 v0.2 D6 要求，缺一不可）：
1. **检索口径**：recall@1/3/5/10（GT = QA.evidence 映射到的 memory_id 集合）
2. **端到端口径**：J-score（检索 top-k → LLM 生成答案 → LLM judge 与 gold answer 比对）

为什么必须两个口径都跑（Mem0 论文 66.9% 是 J-score 口径，不是纯检索 recall）：
- 纯 recall 只测「证据 chunk 有没有被捞出来」，与最终能不能答对问题不等价
  （捞出来了但 LLM 用错 / 捞的顺序不对都可能答错）；
- 纯 J-score 不测检索质量（gold answer 可能靠 LLM 先验知识蒙对），
  容易掩盖检索退化。
两个口径一起看才发现「检索掉了但答案蒙对」和「检索到了但答错」这两类问题。

用法：
    # 冒烟（1 conversation，先验 GT 覆盖率）
    python -m eval.locomo_eval --conv 1 --output eval/results/locomo_smoke

    # 全量基线（10 conversation，Mem0 可比口径）
    python -m eval.locomo_eval --all --output eval/results/locomo_full

    # 双口径（检索 + J-score 抽样 100）
    python -m eval.locomo_eval --all --jscore --jscore-sample 100

    # 粒度对照（英文语料适配结论的证据）
    python -m eval.locomo_eval --all --granularity session --output eval/results/locomo_session

产出：`<output>/locomo_report.json` + `<output>/locomo_report.md`
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import sqlite3

from sgme import config as sgme_config
from sgme.data import search as search_mod

from eval import metrics as eval_metrics
from eval.locomo import (
    CATEGORY_NAMES,
    load_locomo,
    locomo_stats,
    iter_qa,
)
from eval.locomo_ingest import (
    IngestConfig,
    LocomoIndex,
    build_locomo_replica,
    resolve_evidence,
)

logger = logging.getLogger("eval.locomo_eval")

DEFAULT_LIMIT = 10


# ── GT ──

@dataclass
class LocomoGtItem:
    """一条 LoCoMo 评测项（QA → 相关记忆集合）。"""
    conv_id: str = ""
    qa_index: int = 0
    question: str = ""
    answer: str | None = None
    category: int = 0
    category_name: str = ""
    evidence: list[str] = field(default_factory=list)
    relevant_ids: list[str] = field(default_factory=list)
    unresolved_dia: list[str] = field(default_factory=list)


@dataclass
class LocomoGt:
    items: list[LocomoGtItem] = field(default_factory=list)
    qa_total: int = 0             # 参与过滤前的 QA 总数
    qa_covered: int = 0           # 至少映射到 1 条 memory 的 QA 数
    unresolved_dia_count: int = 0

    @property
    def coverage(self) -> float:
        """GT 覆盖率 = 能映射到 ≥1 条 memory 的 QA 占比。

        ★ 审查意见 R2：这个数**必须先测**。覆盖率 <70% 说明灌库/映射链路有洞，
        此时跑 recall 毫无意义（分母里混了一大批永远不可能命中的 QA，
        指标被系统性拉低，却会被误读成「检索能力差」）。
        """
        return round(self.qa_covered / self.qa_total, 4) if self.qa_total else 0.0

    def counts_by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.category_name] = out.get(it.category_name, 0) + 1
        return dict(sorted(out.items()))


def build_gt(
    convs: list,
    index: LocomoIndex,
    *,
    include_adversarial: bool = False,
) -> LocomoGt:
    """QA.evidence（dia_id）→ memory_id 相关集。

    统计口径：qa_total 取「主评测口径下带 evidence 的 QA 数」（默认剔除 adversarial）。
    覆盖不到的证据 dia_id 逐条留在 unresolved_dia 里，便于定位是解析 bug 还是数据脏。
    """
    gt = LocomoGt()
    qa_all = list(iter_qa(convs, include_adversarial=include_adversarial, require_evidence=True))
    gt.qa_total = len(qa_all)
    for q in qa_all:
        # conv_id 必须传：dia_id 只在单 conversation 内唯一（见 locomo_ingest.LocomoIndex）
        mids, miss = resolve_evidence(index, q.dia_ids, q.conv_id)
        if not mids:
            continue
        gt.items.append(LocomoGtItem(
            conv_id=q.conv_id,
            qa_index=q.qa_index,
            question=q.question,
            answer=q.answer,
            category=q.category,
            category_name=q.category_name,
            evidence=list(q.dia_ids),
            relevant_ids=mids,
            unresolved_dia=miss,
        ))
        gt.unresolved_dia_count += len(miss)
    gt.qa_covered = len(gt.items)
    return gt


# ── 检索臂 ──

def arm_cfg(name: str, base: dict | None = None, *, scoped: bool = True) -> dict:
    """按臂名构造 search 配置。

    - `bm25`：纯 BM25（向量关、图关）——0 token，最稳的地板基线
    - `hybrid`：BM25 + 向量 RRF 融合（生产默认口径）
    - `bm25_nostop`：BM25 + 关闭停用词过滤 —— 英文语料适配的对照臂
      （T-130 停用词表是中英双语，需实测它在英文语料上是否误杀内容词）

    `scoped`（默认 True）：开 T-140 agent_scope，把检索限制在 QA 所属 conversation
    内。这是 LoCoMo 的标准口径——10 个 conversation 是 10 组互不相识的人，
    Mem0 的评测同样是每个 conversation 一个 user_id。不开的话跨 conv 的同号
    dia_id 内容会互相挤占 top-k，recall 被稀释（实测全量 recall@10 从 0.56 → 0.08）。

    图召回臂**不提供**：直灌不产出 memory_edges，图路无边可走（见 locomo_ingest 模块说明）。
    """
    cfg = json.loads(json.dumps(base or sgme_config.load_config()))  # 深拷贝，避免污染
    s = cfg.setdefault("search", {})
    v = s.setdefault("vector", {})
    g = s.setdefault("graph", {})
    if name in ("bm25", "bm25_punct"):
        v["enabled"] = False
        g["enabled"] = False
    elif name == "hybrid":
        v["enabled"] = True
        g["enabled"] = False
    elif name == "bm25_nostop":
        v["enabled"] = False
        g["enabled"] = False
        s.setdefault("stoplist", {})["enabled"] = False
    else:
        raise ValueError(f"未知臂名 {name!r}")
    if scoped:
        cfg.setdefault("agent_scope", {})["enabled"] = True
    return cfg


@contextlib.contextmanager
def patched_fts_query_nopunct():
    """临时给 `_build_fts_query` 打补丁：剔除标点/单字符 token（仅评测进程内生效）。

    ★ 为什么需要这个对照臂（英文语料适配的关键实验）：
    查询侧 `segment()` + `_build_fts_query` 会把英文问句切成
    ``['When','did','Caroline','go','to','the','LGBTQ','support','group','?']``，
    停用词过滤后仍残留 **标点与碎片 token**（`'`、`-`、`?`、`s`）。
    这些 token 在英文语料里几乎命中所有文档，会以 OR 形式拖垮 BM25 排序。

    本补丁**不改动生产代码**——只在评测进程内替换模块级函数，退出即还原，
    目的是量化「修掉标点 token 值多少 recall」，为是否立项提供依据。
    """
    orig = search_mod._build_fts_query

    def _patched(query: str, *, use_stoplist: bool = True) -> str:
        built = orig(query, use_stoplist=use_stoplist)
        kept = [
            p for p in built.split(" OR ")
            if any(ch.isalnum() for ch in p) and len(p.strip('"')) > 1
        ]
        return " OR ".join(kept) if kept else built

    search_mod._build_fts_query = _patched
    try:
        yield
    finally:
        search_mod._build_fts_query = orig


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(int(len(s) * 0.95), len(s) - 1)]


def run_arm(
    mem_conn: sqlite3.Connection,
    gt: LocomoGt,
    cfg: dict,
    *,
    limit: int = DEFAULT_LIMIT,
    max_items: int | None = None,
    scoped: bool = True,
) -> dict:
    """单臂检索评测：逐条 QA 跑 search_memories，累计 recall@k / P95 延迟。

    按 category 分桶聚合——LoCoMo 的 5 类难度差异极大（single_hop 好做、
    multi_hop/open_domain 难），只报总量会把「某一类崩了」掩盖掉。

    `scoped`：检索作用域限制到 QA 所属 conversation（agent_id=conv_id，
    需 cfg 同步开 agent_scope，见 `arm_cfg`）。
    """
    items = gt.items if max_items is None else gt.items[:max_items]
    per_query: list[dict[int, float]] = []
    cats: list[str] = []
    latencies: list[float] = []
    empty_hits = 0

    for it in items:
        t0 = time.perf_counter()
        res = search_mod.search_memories(
            mem_conn, None, query=it.question, limit=limit,
            include_sources=False, cfg=cfg,
            agent_id=it.conv_id if scoped else None,
        )
        latencies.append((time.perf_counter() - t0) * 1000)
        predicted = [r["memory_id"] for r in res]
        if not predicted:
            empty_hits += 1
        per_query.append(eval_metrics.compute_recall_at_k(predicted, it.relevant_ids, ks=(1, 3, 5, 10)))
        cats.append(it.category_name)

    def _agg(idxs: list[int] | None = None) -> dict:
        sel = per_query if idxs is None else [per_query[i] for i in idxs]
        if not sel:
            return {}
        return eval_metrics.aggregate_recall_at_k(sel, ks=(1, 3, 5, 10)).as_dict()

    by_cat: dict[str, dict] = {}
    for c in sorted(set(cats)):
        idxs = [i for i, x in enumerate(cats) if x == c]
        by_cat[c] = _agg(idxs)

    return {
        "query_count": len(items),
        "recall_at_k": _agg(),
        "by_category": by_cat,
        "p95_latency_ms": round(_p95(latencies), 2),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "empty_result_count": empty_hits,
    }


# ── 语料向量化（hybrid 臂前置）──

def embed_corpus(
    mem_conn: sqlite3.Connection,
    cfg: dict,
    *,
    limit: int | None = None,
    workers: int = 6,
    batch_size: int = 32,
) -> dict:
    """Batch-embed the replica corpus via Ollama /v1/embeddings (input array).

    Why batching instead of per-item embedding:
    - Ollama computes the whole batch in ONE forward pass (~3.5s for 16 texts
      vs ~480ms/text serial) => ~10-40x faster on pure compute.
    - Each batch is ONE HTTP request, which keeps us well under the gateway's
      burst rate limit (the per-item path fired 8 concurrent requests and
      tripped 429s).
    - EmbedCache (sha256(text)+model) dedups across runs: vectors already
      embedded by an earlier run are cache hits, so a resumed run only embeds
      the remainder.

    sqlite is not thread-safe, so batches are embedded in worker threads but
    ALL DB writes (upsert_vector + cache.put) happen on the main thread after
    the pool returns. Any batch that exhausts retries aborts the run (partial
    coverage = vector path unavailable, per retrieval_gt hard contract).
    """
    import random

    import httpx
    from sgme.data import memory_dao
    from sgme.data.search import vector as vector_mod
    from eval.embed_cache import EmbedCache

    t0 = time.perf_counter()
    rows = mem_conn.execute(
        "SELECT memory_id, content FROM memories WHERE status != 'rejected' ORDER BY memory_id"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    total = len(rows)
    vec_cfg = (cfg.get("search") or {}).get("vector") or {}
    model = vec_cfg.get("model", "")
    base_url = (vec_cfg.get("base_url") or "").rstrip("/")
    cache = EmbedCache(EmbedCache.default_path())
    prev = vector_mod.set_embed_cache(cache)

    # Phase 1: serve cache hits directly (no network).
    ok = 0
    misses: list[tuple[int, str, str]] = []  # (row_idx, memory_id, text)
    for i, r in enumerate(rows):
        vec = cache.get(r["content"], model)
        if vec is not None:
            memory_dao.upsert_vector(
                mem_conn, r["memory_id"],
                vector_mod._serialize_vector(vec), model, dims=len(vec),
            )
            ok += 1
        else:
            misses.append((i, r["memory_id"], r["content"]))
    mem_conn.commit()
    logger.info("向量化: 缓存命中 %d / 待嵌入 %d（共 %d）", ok, len(misses), total)

    # Phase 2: batch-embed misses (parallel batches, serial writes).
    failed_at: int | None = None

    def embed_batch(batch: list) -> list:
        texts = [b[2] for b in batch]
        for attempt in range(1, 7):
            try:
                r = httpx.post(
                    f"{base_url}/embeddings",
                    json={"model": model, "input": texts},
                    timeout=120,
                )
                if r.status_code == 429:
                    time.sleep(min(2.0 ** attempt, 16.0) + random.random())
                    continue
                r.raise_for_status()
                data = {d["index"]: d["embedding"] for d in r.json()["data"]}
                return [(batch[k][1], batch[k][2], data[k]) for k in range(len(batch))]
            except Exception as e:  # noqa: BLE001
                logger.warning("batch embed 失败(尝试%d): %s", attempt, str(e)[:120])
                time.sleep(min(2.0 ** attempt, 16.0) + random.random())
        raise RuntimeError(f"batch embed 耗尽重试: {texts[0][:40]!r}")

    if misses:
        batches = [misses[s:s + batch_size] for s in range(0, len(misses), batch_size)]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for res in ex.map(embed_batch, batches):
                for memory_id, text, vec in res:
                    cache.put(text, model, vec)
                    memory_dao.upsert_vector(
                        mem_conn, memory_id,
                        vector_mod._serialize_vector(vec), model, dims=len(vec),
                    )
                    ok += 1
                mem_conn.commit()
                logger.info("向量化进度 %d/%d（%.1f%%）", ok, total, ok * 100.0 / max(total, 1))

    vector_mod.set_embed_cache(prev)
    cache_stats = cache.stats_dict()

    return {
        "corpus_size": total,
        "vector_count": ok,
        "coverage": round(ok / total, 4) if total else 0.0,
        "available": bool(total) and ok == total,
        "failed_at": failed_at,
        "workers": max(1, workers),
        "batch_size": batch_size,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "embed_cache": cache_stats,
    }


# ── J-score（端到端 LLM judge）──

_ANSWER_PROMPT = """You are answering a question using ONLY the retrieved memory snippets from a long-term conversation memory system.

Retrieved memories (may be empty or partially irrelevant):
{context}

Question: {question}

Rules:
- Answer using ONLY the retrieved memories. If the memories do not contain the answer, reply exactly: NO CONTEXT
- Be concise: a short phrase or one sentence. Do NOT explain, do NOT add reasoning.
- Preserve the original wording of dates, names, and numbers as they appear in the memories.

Answer:"""

_JUDGE_PROMPT = """You are an impartial judge evaluating a question-answering system.

Question: {question}
Gold answer: {gold}
System answer: {pred}

Decide whether the system answer is CORRECT with respect to the gold answer.
- CORRECT: the system answer conveys the same key fact(s) as the gold answer (wording/tense/detail-level differences are fine).
- WRONG: it contradicts the gold answer, states a different fact, or says the information is unavailable when the gold answer does exist.

Reply with exactly one word, CORRECT or WRONG, on the first line. Optionally add a one-line reason on the second line."""


def _llm_call(prompt: str, llm_cfg: dict, client=None) -> str:
    from sgme.llm import chain as llm_chain

    try:
        text, _prov, _usage = llm_chain.call_with_fallback(
            llm_cfg, prompt, chain_name="refinement", client=client
        )
        return (text or "").strip()
    except Exception as e:                      # LLMUnavailable / 限流 / 网络
        logger.warning("LLM 调用失败: %s", e)
        return ""


def make_deepseek_llm_fn(
    model: str = "deepseek-v4-flash",
    api_key_env: str = "DEEPSEEK_API_KEY_SGME",
    base_url: str = "https://api.deepseek.com/v1",
    throttle_s: float = 0.25,
    max_retry: int = 5,
) -> Callable[[str], str]:
    """Build an llm_fn(prompt)->str that calls DeepSeek directly.

    Bypasses the agnes rate limit (the default refinement chain head) and
    matches SGME production LLM (deepseek-v4-flash). Retries on 429 with
    exponential backoff. Returns '' on persistent failure (judge_score treats
    '' as an infra error, not a wrong answer).
    """
    import os
    import random
    import time

    import httpx

    key = os.environ.get(api_key_env) or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError(f"missing API key env {api_key_env}")

    def fn(prompt: str) -> str:
        last_err = ""
        if throttle_s > 0:
            time.sleep(throttle_s)
        for attempt in range(1, max_retry + 1):
            try:
                r = httpx.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                    timeout=60,
                )
                if r.status_code == 429:
                    last_err = "429"
                    time.sleep(min(2.0 ** attempt, 16.0) + random.random())
                    continue
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:120]
                time.sleep(min(2.0 ** attempt, 16.0) + random.random())
        logger.warning("deepseek llm_fn failed after %d retries: %s", max_retry, last_err)
        return ""

    return fn


def judge_score(
    mem_conn: sqlite3.Connection,
    gt: LocomoGt,
    *,
    cfg: dict,
    llm_cfg: dict,
    sample_n: int = 100,
    top_k: int = DEFAULT_LIMIT,
    seed: int = 0,
    scoped: bool = True,
    llm_fn: Callable[[str], str] | None = None,
) -> dict:
    """端到端 J-score：检索 → 生成答案 → LLM judge 与 gold answer 比对。

    - 抽样（默认 100）而不是全量：1,540 条 × 2 次 LLM 调用 ≈ 3,080 次请求，
      按 0.5 rps 节流约 1.7 小时，且吃满免费额度；抽样 100 条约 7 分钟，
      置信区间宽度可接受（n=100, p≈0.6 → ±10pp）。
    - `llm_fn` 可注入（测试用桩），生产跑走真实链路。
    - 生成失败（LLM 异常）记入 errors，不计为 WRONG——避免把基础设施故障
      算成模型能力问题，这是评测诚实性的底线。
    """
    items = list(gt.items)
    if sample_n and sample_n < len(items):
        items = random.Random(seed).sample(items, sample_n)

    call = llm_fn or (lambda p: _llm_call(p, llm_cfg))
    n_correct = 0
    n_wrong = 0
    n_no_context = 0
    n_error = 0
    per_cat: dict[str, list[int]] = {}
    details: list[dict] = []

    for it in items:
        res = search_mod.search_memories(
            mem_conn, None, query=it.question, limit=top_k,
            include_sources=False, cfg=cfg,
            agent_id=it.conv_id if scoped else None,
        )
        context = "\n".join(
            f"[{i + 1}] {r['content']}" for i, r in enumerate(res)
        ) or "(no memories retrieved)"

        pred = call(_ANSWER_PROMPT.format(context=context, question=it.question)).strip()
        if not pred:
            n_error += 1
            per_cat.setdefault(it.category_name, []).append(-1)
            details.append({"q": it.question, "gold": it.answer, "pred": None, "verdict": "error"})
            continue
        pred_head = pred.splitlines()[0].strip() if pred else ""

        if pred_head.upper().startswith("NO CONTEXT"):
            n_no_context += 1
            per_cat.setdefault(it.category_name, []).append(0)
            details.append({"q": it.question, "gold": it.answer, "pred": pred_head, "verdict": "no_context"})
            continue

        verdict_raw = call(_JUDGE_PROMPT.format(
            question=it.question, gold=str(it.answer or ""), pred=pred_head,
        )).strip()
        head = verdict_raw.splitlines()[0].strip().upper() if verdict_raw else ""
        if head.startswith("CORRECT"):
            n_correct += 1
            per_cat.setdefault(it.category_name, []).append(1)
            verdict = "correct"
        elif head.startswith("WRONG"):
            n_wrong += 1
            per_cat.setdefault(it.category_name, []).append(0)
            verdict = "wrong"
        else:
            n_error += 1
            per_cat.setdefault(it.category_name, []).append(-1)
            verdict = f"unparsable:{head[:40]}"
        details.append({"q": it.question, "gold": it.answer, "pred": pred_head, "verdict": verdict})

    judged = n_correct + n_wrong
    by_cat: dict[str, dict] = {}
    for c, marks in sorted(per_cat.items()):
        valid = [m for m in marks if m >= 0]
        by_cat[c] = {
            "judged": len(valid),
            "correct": sum(valid),
            "j_score": round(sum(valid) / len(valid), 4) if valid else 0.0,
            "errors": sum(1 for m in marks if m < 0),
        }

    return {
        "sample_n": len(items),
        "judged": judged,
        "correct": n_correct,
        "wrong": n_wrong,
        "no_context": n_no_context,
        "errors": n_error,
        "j_score": round(n_correct / judged, 4) if judged else 0.0,
        "j_score_denominator": "judged (correct+wrong) —— no_context 与 error 不计入分母",
        "no_context_rate": round(n_no_context / len(items), 4) if items else 0.0,
        "by_category": by_cat,
        "top_k": top_k,
        "seed": seed,
        "details": details,
    }


# ── 报告 ──

def _fmt(rec: dict) -> str:
    if not rec:
        return "-"
    return " / ".join(f"{rec.get(f'recall@{k}', 0.0):.4f}" for k in (1, 3, 5, 10))


def report_md(result: dict) -> str:
    gt = result["gt"]
    arms = result["arms"]
    L: list[str] = [
        "# ST-40 / T-141 —— LoCoMo 业界标准评测报告",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 数据：{result['data_path']}（{result['corpus_stats']['conversations']} conversation / "
        f"{result['corpus_stats']['sessions']} session / {result['corpus_stats']['turns']} turn）",
        f"- 灌库：粒度 **{result['ingest_config']['granularity']}**（{result['index']['memory_count']} 条记忆，"
        f"with_date={result['ingest_config']['with_date']}，零 token 直灌）",
        f"- 参与 conversation：{', '.join(result['conv_ids'])}",
        f"- 检索作用域：**{result['scope']['mechanism']}**（scoped={result['scope']['scoped']}）",
        "",
        "## 一、GT 覆盖率（先决指标）",
        "",
        f"- 主评测口径 QA（剔除 adversarial + 必须有 evidence）：**{gt['qa_total']}**",
        f"- 成功映射到 ≥1 条记忆：**{gt['qa_covered']}** → 覆盖率 **{gt['coverage']:.2%}**",
        f"- 未解析的 evidence dia_id：{gt['unresolved_dia_count']}",
        f"- 分类分布：{gt['by_category']}",
        "",
        "> 覆盖率 <70% 时下游 recall 不可采信（分母混入了不可能命中的 QA）。",
        "",
        "## 二、检索口径：recall@1/3/5/10（k=1 / 3 / 5 / 10）",
        "",
        "| 臂 | 全量 | " + " | ".join(sorted(gt["by_category"])) + " |",
        "|---|---|" + "---|" * len(gt["by_category"]),
    ]
    for name, arm in arms.items():
        cells = [f"**{name}**", _fmt(arm["recall_at_k"])]
        for c in sorted(gt["by_category"]):
            cells.append(_fmt(arm["by_category"].get(c)))
        L.append("| " + " | ".join(cells) + " |")
    L += [
        "",
        "## 三、延迟与空结果",
        "",
    ]
    for name, arm in arms.items():
        L.append(
            f"- **{name}**：查询 {arm['query_count']} 条，P95 {arm['p95_latency_ms']}ms / "
            f"均值 {arm['mean_latency_ms']}ms，空结果 {arm['empty_result_count']} 条"
        )
    js = result.get("jscore")
    if js:
        L += [
            "",
            "## 四、端到端口径：J-score（LLM-as-judge）",
            "",
            f"- 抽样 {js['sample_n']} 条（seed={js['seed']}，top_k={js['top_k']}）",
            f"- 判定 {js['judged']} 条：correct {js['correct']} / wrong {js['wrong']} "
            f"/ no_context {js['no_context']} / error {js['errors']}",
            f"- **J-score = {js['j_score']:.4f}**（分母 = judged，no_context 与 error 不计入）",
            f"- NO CONTEXT 率 {js['no_context_rate']:.2%}（检索没捞到任何可用证据的比例）",
            "",
            "| 分类 | 判定数 | 正确 | J-score | 错误 |",
            "|---|---|---|---|---|",
        ]
        for c, d in js["by_category"].items():
            L.append(f"| {c} | {d['judged']} | {d['correct']} | {d['j_score']} | {d['errors']} |")
    L += [
        "",
        "## 五、边界（不可越过解读）",
        "",
        "- 本通路**零 token 直灌**，不跑提炼 → 不产出 memory_edges，**图召回未参与评测**；",
        "- 故本数字只能与「同样直灌口径」的基线横向比，**不等于 SGME 端到端生产效果**；",
        "- J-score 为抽样值（非全量），存在抽样误差；误差量级见上 sample_n。",
        "",
    ]
    return "\n".join(L)


# ── 主流程 ──

def main() -> None:
    ap = argparse.ArgumentParser(description="ST-40 LoCoMo 业界标准评测")
    ap.add_argument("--data", default=None, help="locomo10.json 路径")
    ap.add_argument("--all", action="store_true", help="全量 10 conversation（Mem0 可比口径）")
    ap.add_argument("--conv", type=int, default=1, help="参与评测的 conversation 数（默认 1=冒烟）")
    ap.add_argument("--granularity", choices=["turn", "window", "session"], default="turn")
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--no-date", action="store_true", help="灌库时不带 session 日期（temporal 类将不可答）")
    ap.add_argument("--arms", default="bm25", help="检索臂，逗号分隔：bm25 / hybrid / bm25_nostop")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="检索返回条数（recall@10 需 ≥10）")
    ap.add_argument("--jscore", action="store_true", help="跑端到端 J-score（需 LLM）")
    ap.add_argument("--jscore-sample", type=int, default=100)
    ap.add_argument("--jscore-seed", type=int, default=0)
    ap.add_argument("--jscore-arm", default=None, help="J-score 用哪个臂的检索结果（默认 arms 首个）")
    ap.add_argument("--jscore-llm", choices=["chain", "deepseek"], default="deepseek",
                    help="J-score LLM backend: chain=agnes fallback (may be rate-limited) / deepseek=direct production LLM (bypass limit)")
    ap.add_argument("--embed-workers", type=int, default=6,
                    help="批量嵌入并发批数（每批 1 请求，规避突发限流）")
    ap.add_argument("--embed-batch", type=int, default=32,
                    help="每批文本数（Ollama 单次前向算整批）")
    ap.add_argument("--workdir", default="eval/tmp/locomo", help="灌库副本工作目录")
    ap.add_argument("--reuse", action="store_true", help="复用已存在的副本与索引（不重建）")
    ap.add_argument("--max-items", type=int, default=None, help="只跑前 N 条 QA（调试用）")
    ap.add_argument("--no-scope", action="store_true",
                    help="不按 conversation 隔离检索（诊断用；主口径必须隔离）")
    ap.add_argument("--output", default="eval/results/locomo", help="报告输出目录")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    data_path = args.data or str(
        __import__("eval.locomo", fromlist=["x"]).DEFAULT_LOCOMO_PATH
    )
    convs_all = load_locomo(data_path)
    convs = convs_all if args.all else convs_all[: max(1, args.conv)]
    stats = locomo_stats(convs)

    # 1) 灌库
    workdir = Path(args.workdir) / f"{args.granularity}{'_nodate' if args.no_date else ''}"
    idx_path = workdir / "locomo_index.json"
    if args.reuse and idx_path.exists() and (workdir / "memory.db").exists():
        index = LocomoIndex.load(idx_path)
        db_path = Path(index.db_path)
        logger.info("复用已有副本: %s（%d 条记忆）", db_path, index.memory_count)
    else:
        db_path, index = build_locomo_replica(
            workdir,
            convs,
            IngestConfig(
                granularity=args.granularity,
                window=args.window,
                stride=args.stride,
                with_date=not args.no_date,
            ),
        )
        index.save(idx_path)

    # 2) GT
    if not args.no_scope and not index.config.get("per_conv_agent_tag", False):
        raise SystemExit(
            "隔离检索要求灌库时 per_conv_agent_tag=True（agent_tag=conv_id）。"
            "当前副本不是该口径 —— 请删掉副本重灌，或加 --no-scope 明确放弃隔离。"
        )
    gt = build_gt(convs, index)
    logger.info(
        "GT 覆盖率 %.2f%%（%d/%d），未解析 dia %d",
        gt.coverage * 100, gt.qa_covered, gt.qa_total, gt.unresolved_dia_count,
    )
    if gt.coverage < 0.7:
        logger.warning("GT 覆盖率 <70%，下游 recall 不可采信，请先修映射链路")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        base_cfg = sgme_config.load_config()
        scoped = not args.no_scope
        arms: dict[str, dict] = {}
        vec_info: dict | None = None
        for name in [a.strip() for a in args.arms.split(",") if a.strip()]:
            cfg = arm_cfg(name, base_cfg, scoped=scoped)
            if name == "hybrid":
                vec_info = embed_corpus(conn, cfg, workers=max(1, args.embed_workers),
                                        batch_size=max(1, args.embed_batch))
                logger.info("向量化: %s", vec_info)
                if not vec_info.get("available"):
                    logger.warning("向量覆盖不全，hybrid 臂退化为单路 BM25")
            if name == "bm25_punct":
                with patched_fts_query_nopunct():
                    arms[name] = run_arm(conn, gt, cfg, limit=args.limit,
                                         max_items=args.max_items, scoped=scoped)
            else:
                arms[name] = run_arm(conn, gt, cfg, limit=args.limit,
                                     max_items=args.max_items, scoped=scoped)
            logger.info("臂 %s 完成: %s", name, arms[name]["recall_at_k"])

        jscore = None
        if args.jscore:
            from sgme.llm import chain as llm_chain

            llm_cfg = llm_chain.load_config()
            arm_name = args.jscore_arm or next(iter(arms))
            llm_fn = None
            if args.jscore_llm == "deepseek":
                llm_fn = make_deepseek_llm_fn()
            jscore = judge_score(
                conn, gt, cfg=arm_cfg(arm_name, base_cfg, scoped=scoped), llm_cfg=llm_cfg,
                sample_n=args.jscore_sample, top_k=args.limit, seed=args.jscore_seed,
                scoped=scoped, llm_fn=llm_fn,
            )
            logger.info("J-score = %s（%d 条）", jscore["j_score"], jscore["judged"])

        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "task": "ST-40 / T-141 LoCoMo",
            "data_path": data_path,
            "conv_ids": [c.sample_id for c in convs],
            "corpus_stats": stats,
            "ingest_config": index.config,
            "index": {"memory_count": index.memory_count, "granularity": index.granularity},
            "gt": {
                "qa_total": gt.qa_total,
                "qa_covered": gt.qa_covered,
                "coverage": gt.coverage,
                "unresolved_dia_count": gt.unresolved_dia_count,
                "by_category": gt.counts_by_category(),
            },
            "vector": vec_info,
            "scope": {
                "scoped": scoped,
                "mechanism": "T-140 agent_scope（agent_id=conv_id，灌库 agent_tag=conv_id）",
            },
            "arms": arms,
            "jscore": jscore,
        }
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "locomo_report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md = report_md(result)
        (out / "locomo_report.md").write_text(md, encoding="utf-8")
        print(md)
        logger.info("报告已落盘: %s", out)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
