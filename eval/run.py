"""eval/run.py：CLI 入口。

用法：
  python -m eval.run --baseline                          # 跑基线评测（dry-run）
  python -m eval.run --baseline --cases <path>            # 指定评测集
  python -m eval.run --baseline --dry-run                 # dry-run 自检模式
  python -m eval.run --compare <report_a> <report_b>      # A/B 差分对比

设计依据：docs/design/SGME-评测框架设计-v0.1.md §1.1、PRD §8.2。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("eval.run")

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    """CLI 主入口。"""
    parser = argparse.ArgumentParser(
        description="SGME 提炼质量评测 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python -m eval.run --baseline --dry-run
  python -m eval.run --baseline --cases eval/cases/v001_sample.yaml
  python -m eval.run --compare results/run_a/report.json results/run_b/report.json
        """,
    )

    parser.add_argument(
        "--baseline",
        action="store_true",
        help="运行基线评测（L1 维度标注）",
    )
    parser.add_argument(
        "--cases",
        type=str,
        default=None,
        help="评测集 YAML 文件路径（默认 eval/cases/v001_sample.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="dry-run 模式：mock LLM 输出，验证全链路可运行",
    )
    parser.add_argument(
        "--stages",
        type=str,
        default="l1",
        help="评测阶段（逗号分隔：l1,l15,l2,rrf；默认 l1）",
    )
    parser.add_argument(
        "--rrf-gt-mode",
        type=str,
        choices=["message", "content"],
        default="message",
        help="RRF 检索 GT 派生模式：message=用例对话正文（默认）/ content=GT 记忆内容（退化对照）",
    )
    parser.add_argument(
        "--rrf-skip-vector",
        action="store_true",
        help=(
            "RRF 阶段跳过向量嵌入（退化单路 BM25，无需 embeddings 端点，跑得快）；"
            "此时 vector_available=False、banner_reason=vector_skipped_by_flag"
        ),
    )
    parser.add_argument(
        "--rrf-no-embed-cache",
        action="store_true",
        help=(
            "禁用 embedding 磁盘缓存（默认启用 eval/fixtures/embed_cache_v001.sqlite）；"
            "禁用后每条 embed 都真打端点，跨 run 可复现性不再受保证"
        ),
    )
    parser.add_argument(
        "--prompt-version",
        type=str,
        default=None,
        help="提示词版本（如 v001），dry-run 模式忽略",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出目录（默认 eval/results/<run_id>/）",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("REPORT_A", "REPORT_B"),
        help="A/B 对比两份 report.json（纯离线 diff）",
    )

    args = parser.parse_args()

    # A/B 对比模式
    if args.compare:
        _run_compare(args.compare[0], args.compare[1])
        return

    # 基线评测模式
    if args.baseline:
        _run_baseline(args)
        return

    # 无模式指定
    parser.print_help()
    print("\n请指定 --baseline 或 --compare", file=sys.stderr)
    sys.exit(1)


def _run_baseline(args: argparse.Namespace) -> None:
    """执行基线评测。"""
    from eval import loader as eval_loader
    from eval.reporter import generate_report_json, generate_report_md
    from eval.runner import EvalRunner

    # 确定评测集路径
    if args.cases:
        cases_path = Path(args.cases)
        if not cases_path.is_absolute():
            cases_path = PROJECT_ROOT / cases_path
    else:
        cases_path = PROJECT_ROOT / "eval" / "cases" / "v001_sample.yaml"

    if not cases_path.exists():
        logger.error("评测集文件不存在: %s", cases_path)
        logger.info("可用选项: --cases eval/cases/v001_sample.yaml")
        sys.exit(1)

    dry_run = args.dry_run  # --dry-run 显式控制（False = 真实 LLM）
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]

    logger.info("=" * 60)
    logger.info("SGME 基线评测开始")
    logger.info("  评测集: %s", cases_path)
    logger.info("  阶段: %s", stages)
    logger.info("  模式: %s", "dry-run (mock LLM)" if dry_run else "真实 LLM")
    logger.info("=" * 60)

    # 加载用例
    cases = eval_loader.load_cases(cases_path)
    logger.info("加载 %d 条评测用例", len(cases))

    # 加载真实配置：eval DB 的 import_registry 需要 dimensions/aliases，
    # 且 rrf 阶段的向量通路需要 llm/search 段。加载失败降级空 dict
    # （runner._load_eval_registry 会再从 registry/*.yaml 兜底）。
    cfg: dict = {}
    try:
        from sgme import config as sgme_config
        cfg = sgme_config.load_config()
    except Exception as e:
        logger.warning("sgme 配置加载失败，降级 cfg={}（registry 将由 runner 兜底读取）: %s", e)
        cfg = {}

    # 初始化 runner
    from eval.embed_cache import DEFAULT_CACHE_PATH
    runner = EvalRunner(
        cfg=cfg,
        prompt_version=args.prompt_version,
        rrf_gt_mode=args.rrf_gt_mode,
        rrf_skip_vector=args.rrf_skip_vector,
        rrf_embed_cache=None if args.rrf_no_embed_cache else DEFAULT_CACHE_PATH,
    )

    # 执行评测
    result = runner.run_all(cases, stages=stages, dry_run=dry_run)

    # 输出目录
    if args.output:
        output_dir = Path(args.output)
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir
    else:
        output_dir = PROJECT_ROOT / "eval" / "results" / result.run_id

    # 生成报告
    json_path = generate_report_json(result, output_dir)
    md_path = generate_report_md(result, output_dir)

    print("\n" + "=" * 60)
    print("评测完成")
    print(f"  Run ID: {result.run_id}")
    print(f"  L1 F1: {result.l1.dimension_micro_f1:.4f}" if result.l1 else "  L1: N/A")
    print(f"  Strict Match: {result.l1.strict_match_rate:.4f}" if result.l1 else "")

    # RRF 摘要行（诚实：无区分度时明说不给推荐值）
    rrf = result.rrf
    if rrf is not None:
        if rrf.vector_available:
            vec_flag = f"双路(向量 {rrf.vector_count}/{rrf.corpus_size})"
        else:
            vec_flag = (
                f"单路(向量不可用 {rrf.vector_count}/{rrf.corpus_size}"
                f"{', ' + rrf.banner_reason if rrf.banner_reason else ''})"
            )
        if rrf.discriminative and rrf.recommended_k is not None:
            verdict = f"推荐 search.rrf.k: {rrf.recommended_k}"
        else:
            verdict = "无区分度 → 维持 search.rrf.k: 60 不变"
        print(
            f"  RRF: NDCG@{rrf.ndcg_k}={rrf.best_ndcg10:.4f} "
            f"spread={rrf.ndcg_spread:.6f} conclusion={rrf.conclusion} "
            f"jaccard={rrf.route_overlap_jaccard:.4f} "
            f"[{vec_flag}, queries={rrf.query_count}, corpus={rrf.corpus_size}]"
        )
        print(f"       → {verdict}")
        cache = rrf.embed_cache or {}
        if cache:
            print(
                f"       embedding 缓存: 命中={cache.get('hits', 0)} "
                f"未命中={cache.get('misses', 0)} 新写入={cache.get('writes', 0)} "
                f"库内={cache.get('rows', 0)} 条"
            )

    print(f"  P0 全通过: {'是' if result.summary.passed_p0 else '否'}")
    print(f"  报告: {json_path}")
    print(f"  报告: {md_path}")
    print("=" * 60)

    # 退出码：P0 门槛只对提炼阶段（l1/l15/l2）生效。
    # 纯 rrf 运行不产出 L1/L2 指标，用 P0 判退出码会恒定失败，语义错误。
    if not any(s in stages for s in ("l1", "l15", "l2")):
        logger.info("stages=%s 不含提炼阶段，P0 门槛不适用，退出码 0", stages)
        sys.exit(0)

    if result.summary.passed_p0:
        sys.exit(0)
    else:
        logger.warning("P0 指标不达标，退出码 1")
        sys.exit(1)


def _run_compare(report_a: str, report_b: str) -> None:
    """执行 A/B 对比（纯离线 diff）。"""
    from eval.ab import compare_reports, format_diff_markdown

    logger.info("A/B 对比开始")
    logger.info("  A: %s", report_a)
    logger.info("  B: %s", report_b)

    diff = compare_reports(report_a, report_b)
    md = format_diff_markdown(diff)

    print("\n" + md)

    # 也保存到文件
    output_dir = PROJECT_ROOT / "eval" / "results" / "ab_diff"
    output_dir.mkdir(parents=True, exist_ok=True)
    diff_path = output_dir / "ab_diff.md"
    diff_path.write_text(md, encoding="utf-8")
    logger.info("A/B 差分报告已保存: %s", diff_path)


if __name__ == "__main__":
    main()
