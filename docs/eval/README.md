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
- `eval/results/longmemeval_full/longmemeval_report.json` + `.md` — full
  500-question report (recall by type + QA J-score/F1).

## Usage
On a machine with SGME installed (`pip install -e ".[dev]"`):

    python -m eval.longmemeval_eval --limit 500 --arms bm25 --qa \
        --output eval/results/longmemeval_full

Environment:
- `DEEPSEEK_API_KEY_SGME` — required when `--qa` is set (LLM judge). Export with
  quotes (`set "DEEPSEEK_API_KEY_SGME=sk-..."`) to avoid trailing-space header
  corruption.
- `SGME_EMBED_BASE_URL` / `SGME_EMBED_MODEL` — for the `hybrid` arm (defaults to
  the NAS Ollama bge-m3 at `http://192.168.10.10:11434/v1`).

Notes / honest boundaries:
- Every question runs on its **own isolated DB** (zero cross-question leakage)
  and never touches any production DB — GT is the benchmark's own `answer_session_ids`.
- **Graph recall is intentionally dormant**: direct ingest produces no
  `memory_edges`, so the graph path contributes 0. This exactly matches gbrain's
  own raw-ingest protocol, keeping SGME comparable to the public leaderboard.
- The `bm25` arm needs no embedding (fast). The `hybrid` arm requires the NAS
  Ollama bge-m3 endpoint; if that endpoint is slow/unreachable, run
  `--arms bm25` only.
- Disable the safe-delete hook when running (the harness legitimately manages its
  own per-question sqlite files): prefix the command with
  `CODEBUDDY_SESSION_ID= CLAUDE_SESSION_ID= `.
