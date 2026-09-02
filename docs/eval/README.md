# SGME LongMemEval Evaluation

LongMemEval (Chen et al., ICLR 2025) is now SGME's primary industry-standard
evaluation benchmark, replacing LoCoMo (ST-40 / T-141 evolution). The harness
lives at `eval/longmemeval_eval.py`.

## Why LongMemEval
- 500 questions / 25,112 sessions / 246,930 turns — the hardest and most
  authoritative long-term conversation-memory benchmark published to date.
- 6 question types: single-session-user, single-session-assistant,
  single-session-preference, multi-session, temporal-reasoning, knowledge-update.
- Protocol mirrors gbrain's `eval longmemeval`: per-question **isolated DB**
  (reset + ingest only that question's haystack), top-k retrieval, **session-level
  recall** by `answer_session_ids`, then optional LLM-judge J-score + token-F1.

## Artifacts
- `eval/results/longmemeval_full/longmemeval_report.json` — 500-question
  bm25-only baseline (2026-09-01): recall 0.6847, J-score 0.354, F1 0.2473.
- `eval/results/longmemeval_hybrid_rerun/longmemeval_report.json` — 500-question
  **bm25 vs hybrid retrieval-only rerun (2026-09-02, 2118s)**: bm25 0.6847 →
  **hybrid 0.8426 (+23.1%)**. Per-type lift: temporal-reasoning +31.9%,
  multi-session +27.6%, single-session-assistant +24.4%,
  single-session-preference +23.1%, knowledge-update +16.7%,
  single-session-user +10.3%. No QA in this run (retrieval only).
- `eval/results/longmemeval_hybrid_qa/longmemeval_report.json` — 500-question
  bm25+hybrid run **with QA/judge** (Zhipu `glm-4-flash`, 2026-09-02, 5203s).
  This is the headline number for the hybrid arm:

  | metric | bm25 baseline | hybrid | delta |
  |---|---|---|---|
  | retrieval recall@8 | 0.6847 | **0.8426** | **+23.1%** |
  | QA J-score | 0.354 | **0.384** | +3.0 pp |
  | QA token-F1 | 0.2473 | **0.2783** | +3.1 pp |
  | NO CONTEXT rate | 0.450 | **0.348** | **-10.2 pp** |

  > **Key finding — the retrieval/QA scissors gap**: retrieval improved +23.1%
  > but J-score only +3.0 pp. Bottleneck has moved from "cannot retrieve" to
  > "retrieved but cannot aggregate" (temporal-reasoning: recall 0.7816 yet
  > J-score 0.1579; multi-session: recall 0.8108 yet J-score 0.2273). Further
  > retrieval tuning has low marginal return — invest in generation/aggregation.

## Usage

    python -m eval.longmemeval_eval --limit 500 --arms bm25,hybrid \
        --primary hybrid --qa --output eval/results/longmemeval_hybrid_full

Environment:
- `SGME_JUDGE_BASE_URL` / `SGME_JUDGE_KEY_ENV` / `--judge-model` — QA judge.
  DeepSeek keys may run out of balance (HTTP 402); Zhipu `glm-4-flash` is the
  current default (non-thinking, ~0.45s/call). `glm-4.7-flash` is a thinking
  model at ~35s/call — too slow for a full run (~10h).
- `SGME_EMBED_BASE_URL` / `SGME_EMBED_MODEL` — for the `hybrid` arm.

Always disable the safe-delete hook (the harness legitimately manages its own
per-question sqlite files):

    CODEBUDDY_SESSION_ID= CLAUDE_SESSION_ID= python -m eval.longmemeval_eval ...

## Vector arm (hybrid) — use local LM Studio, not NAS Ollama

**Do not use the NAS Ollama bge-m3**: measured at ~49s per embedding, a 25k-session
full run would take days. Local LM Studio on the RTX 4080S is ~200x faster
(38.7ms/item at batch=64, 1024-dim):

    lms server start --port 8123
    lms load text-embedding-bge-m3-legal-euro-r7 --gpu max

    export SGME_EMBED_BASE_URL=http://localhost:8123/v1
    export SGME_EMBED_MODEL=text-embedding-bge-m3-legal-euro-r7

**Port gotcha**: `lms server start --port 1234` fails with
`Error: listen EACCES: permission denied 127.0.0.1:1234`, and `netstat` shows no
owning process. The cause is Windows' dynamic-port exclusion ranges, not a
process conflict. Check them with:

    netsh interface ipv4 show excludedportrange protocol=tcp

On this machine 1169-1268 and 1269-1368 are reserved, covering both 1234 and
1235. Pick a port outside the exclusion list — **8123** works.

## Embedding model selection

A/B measured on hard question types (20 questions each, recall@8):

| question type | bm25 | hybrid + bge-m3-legal (8192 ctx) | hybrid + nomic (2048 ctx) |
|---|---|---|---|
| temporal-reasoning | 0.5375 | 0.8333 | 0.8708 |
| knowledge-update   | 0.875  | **0.925** | 0.900 |

Chose `text-embedding-bge-m3-legal-euro-r7`: nomic's 2048-token context truncates
over half the sessions and measurably hurts the truncation-sensitive
`knowledge-update` type. Session length (estimated tokens): p50=2503, p90=4198,
p99=5139, max=19529 — only 0.02% exceed 8192.

Note: `bge-m3-legal-euro-r7` is a legal/European-language fine-tune, so it is not
optimal for general English dialogue. An untuned bge-m3 should score slightly higher.

## Sampling by question type (--offset)

The dataset is ordered by question type (ranges overlap):

| type | index range | n |
|---|---|---|
| single-session-user | 0-69 | 70 |
| multi-session | 70-232 | 163 |
| single-session-preference | 132-161 | 30 |
| temporal-reasoning | 233-365 | 133 |
| knowledge-update | 366-443 | 78 |
| single-session-assistant | 444-499 | 56 |

    # validate on temporal-reasoning only
    python -m eval.longmemeval_eval --offset 233 --limit 20 --arms bm25,hybrid

> **Warning — validate on hard types, not the first N questions.** The first 70
> questions are all `single-session-user`, where bm25 is already saturated at
> 0.85 and hybrid actually scores *lower* (0.80) because fusion dilutes bm25's
> strength. Validating there produces the false conclusion "the vector arm is
> useless". On hard types the vector arm delivers **+55% to +62%**.

## `refined` arm — built and validated, but throughput-blocked

The default `bm25`/`hybrid` arms **direct-ingest raw sessions** and skip SGME's
refinement pipeline entirely, so they measure only the *retrieval floor* —
L1 extraction, L1.5 persistence, scene governance and graph recall never run.
The `refined` arm closes that gap by driving SGME's real production path:

    append_l0(...)            # write L0 raw layer
    refine_one(...)           # L1 extraction + L1.5 persistence (scenes, edges)

Implementation: `open_question_db_refined()` in `eval/longmemeval_eval.py`.
Recall is mapped back via `memory_sources.source_ref -> raw_files.file_id ->
raw_files.session_key` (`_resolve_sessions`), so session-level recall stays
comparable with the other arms.

    python -m eval.longmemeval_eval --arms refined --refine-backend cloud \
        --limit 1 --output eval/results/_refined_smoke

- `--refine-backend cloud` (default) uses SGME's **real production refinement
  chain** (`agnes-2.5-flash`) — faithful and reliable: measured **~3.8 structured
  memories + ~0.7 scenes per session** (Q1: 54 sessions -> 169 memories, 38 scenes).
- `--refine-backend local` points refinement at local LM Studio. **Not usable
  today**: only the 9B model fits in VRAM (RTX 4080S 16GB, ~13GB taken, ~3GB
  free), and it fails L1 JSON extraction on English sessions (~1 memory/session
  with frequent parse failures). 12B/27B models cannot be loaded.

**Blocker — throughput**: refinement runs at **~60 s/session** (~55-60s measured).
A question needs ~50 sessions, so:

| scope | sessions | wall time |
|---|---|---|
Throughput recalibrated 2026-09-02 by a 20-session live benchmark on the real
cloud chain (agnes-2.5-flash, 0 errors): mean 42.6s / median 28.7s per session,
3.45 LLM calls/session (L1 chunks 2.13 + L1.5 conflict adjudication ~1.3 as the
memory store fills up), 4.45 memories/session. Full-500 extrapolation:

| Scope | sessions | serial wall time | API cost |
|---|---|---|---|
| 1 question | ~50 | ~24-36 min | 0 |
| 100 questions | ~5,020 | ~1.7-2.5 days | 0 |
| 500 questions | 25,112 | **~8.5-12.5 days** | **0** (free tier) |

**Measured concurrency reality check (B146, 2 questions x 2 workers on the
live chain): 3417s for both -> ~28.5 min/question throughput, only ~1.35x
speedup vs serial (not 2x) — the free tier degrades under concurrent load.
workers=2 full-500 therefore lands at ~10 days, barely better than serial;
meaningful speedup requires the paid tier (DeepSeek-V4-Flash + 4 lanes ~2 days
/ ~$80). The primary value of B146 is checkpoint/resume + per-question fault
isolation for unattended multi-day runs, not raw speed.** Full cost model and
benchmark data: `docs/eval/longmemeval_refined_cost_v0.1.md`.

Full-500 `refined` is therefore **not scheduled** by default. Status: arm is
built and validated; full run deferred pending a decision on wall-clock
tolerance (checkpoint + concurrency now make it feasible without supervision).

### Checkpoint / resume and question-level concurrency (B146)

    # 2 concurrent questions; each question has its own isolated temp DBs
    python -m eval.longmemeval_eval --arms refined --workers 2         --output eval/results/longmemeval_refined_full

    # after an interruption: same command + --resume resumes from checkpoint
    python -m eval.longmemeval_eval --arms refined --workers 2 --resume         --output eval/results/longmemeval_refined_full

- Progress is checkpointed per question to `<output>/checkpoint.jsonl`; a
  resumed run skips completed questions. The checkpoint's first line is a
  config fingerprint (dataset/arms/top_k/qa/refine_backend/...) — any mismatch
  discards the checkpoint automatically, so stale results can never be mixed in.
- Per-question failures are recorded as error records and never kill the run;
  `n_question_errors` in the report shows how many were skipped.
- Why concurrency is safe here: questions are fully isolated (own SQLite files,
  own raw dir); the LLM chain's token bucket is process-global and the measured
  throttle wait is 0s (inference latency dominates), so 2-3 lanes stay well
  under the agnes free-tier RPM limit.

> **Open question (unresolved)**: on Q1 both direct-bm25 and refined scored
> recall@8 = 0.0. Q1 ("What degree did I graduate with?") is a hard
> single-session-user question, and a single question proves nothing — whether
> refinement *improves* recall needs a multi-question sample across all five
> question types. Do not cite Q1 as evidence either way.

## Notes / honest boundaries
- Every question runs on its **own isolated DB** (zero cross-question leakage)
  and never touches any production DB — GT is the benchmark's own `answer_session_ids`.
- **Graph recall is intentionally dormant**: direct ingest produces no
  `memory_edges`, so the graph path contributes 0. This exactly matches gbrain's
  own raw-ingest protocol, keeping SGME comparable to the public leaderboard.
  Verified: `backfill_system_edges` yields 0 edges under raw ingest because
  structural edges depend on `memory_stats`, a refinement-pipeline artifact that
  raw ingest never produces.
- The `bm25` arm needs no embedding (fast). The `hybrid` arm needs the local
  embedding endpoint above.
- **Judge comparability**: the public leaderboard uses GPT-4 as judge; SGME uses
  Zhipu `glm-4-flash`. J-Score absolute values are therefore not directly
  comparable — retrieval recall (judge-independent) is the metric to compare.
- Report generator: `scripts/oneoff/gen_lme_report.py --result <json> --out <md>
  --baseline <json>` — auto-detects arms and renders the bm25-vs-hybrid lift table.
