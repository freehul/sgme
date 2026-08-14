"""eval：SGME 提炼质量评测包。

评测套件 = 提炼链路的消费者，不做改造者。
通过现有 public API（extract_l1/resolve_conflicts/aggregate/normalize_batch）对接，
不动 sgme/engine/* 核心逻辑。

模块：
- eval.models：数据结构（EvalCase/EvalResult/MetricsResult 等）
- eval.loader：评测集加载（YAML → EvalCase 列表）
- eval.metrics：度量计算（L1 F1/L2 Section 命中率/画像质量/NDCG）
- eval.runner：评测流水线（串联 L1/L1.5/L2/模板查询 + 调用 metrics）
- eval.reporter：报告生成（report.json + report.md）
- eval.retrieval_gt：检索评测语料落库 + ground truth 派生（RRF 网格搜索输入）
- eval.rrf：RRF 网格搜索 + NDCG 计算 + 区分度诊断
- eval.ab：A/B 差分报告（纯离线对比两份 report.json）
- eval.run：CLI 入口（python -m eval.run --baseline / --compare）
"""
