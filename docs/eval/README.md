# SGME LoCoMo Evaluation (Laptop 192.168.10.141)

Industry-standard LoCoMo benchmark results for SGME, produced on the LeoLaptop
(192.168.10.141) bare-metal install (non-Docker), as part of task ST-40 / T-141.

## Artifacts
- `SGME_LoCoMo_笔记本评测报告.md` - consolidated report (GT coverage, BM25 + hybrid
  recall@1/3/5/10, J-score breakdown). Contains Chinese narrative.
- `SGME_LoCoMo_笔记本评测报告.json` - machine-readable (bm25 arm + J-score; hybrid
  overall recall@10 = 0.6895 is recorded in the .md, from the parallel run log).

## Headline results (LoCoMo10, 10 conversations / 5882 turns / 1536 QA)
- GT coverage: 99.93% (1535/1536) -> recall trustworthy
- BM25 recall@10: 0.5451
- Hybrid (bge-m3 vector) recall@10: 0.6895
- J-score (bm25 retrieval + DeepSeek judge, sample 100): 0.7213

## Reproduction
On a machine with SGME installed + NAS Ollama bge-m3 (192.168.10.10:11434) reachable:

    python -m eval.locomo_eval --all --arms bm25,hybrid \
        --jscore --jscore-sample 100 --jscore-llm deepseek \
        --jscore-arm bm25 --output eval/results/laptop_full

Notes:
- The eval script reads DEEPSEEK_API_KEY_SGME from the environment; it does NOT load
  .env. Export it first:  set "DEEPSEEK_API_KEY_SGME=sk-..."  (quotes required to
  avoid a trailing-space corruption of the header).
- Use --reuse to skip the ~50-min re-embedding once eval/tmp/locomo/turn/memory.db
  already exists.
- "Zero-token direct ingest" path: no distillation -> no memory_edges -> graph recall
  is NOT evaluated. Numbers are comparable only to same-ingest baselines, not to
  SGME end-to-end production quality.
