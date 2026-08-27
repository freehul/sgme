#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/oneoff/skills_recall_eval.py：M1 及格线①召回率评测（ST-36）。

设计 §二 M1 三条及格线之①「统一搜索命中技能率可统计提升」——本脚本用
真实技能库（git 源）+ 代表性查询集，统计 top-k 命中率（技能级召回）。

判据（与记忆/wiki 检索对照）：
- 每个查询有一个「期望命中的技能名」（人工标注，查询来自该技能的核心触发场景）
- top-5 / top-10 命中期望技能 = 召回；全查询集召回率 = 及格线①数据

用法：
    python scripts/oneoff/skills_recall_eval.py [--root D:/HermesAgent/skills] [--top 5,10]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 加入项目根（脚本从仓库内跑时）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 代表性查询集：(查询, 期望技能名)——查询取自该技能真实触发场景
QUERIES: list[tuple[str, str]] = [
    ("SGME 挂了我怎么排查服务", "sgme-operations"),
    ("抖音视频分析下载转写", "video-analysis-pipeline"),
    ("编码纪律 写代码前必须加载", "coding-discipline"),
    ("git PR 拆分合并流程", "git-pr-workflow"),
    ("推送到公开仓库前隐私扫描", "publish-review"),
    ("代理断了 clash 订阅失效", "proxy-stack-ops"),
    ("NAS 技能库同步对账", "skills-hub-sync"),
    ("Windows 脚本高频坑", "windows-shell-pitfalls"),
    ("extract obligations deadlines from document", "document-to-action-items"),
    ("论文写作 NeurIPS 投稿", "research-paper-writing"),
    ("Word 文档读写", "docx"),
    ("Excel 报表生成", "xlsx"),
    ("视频转 ASCII 动图", "ascii-video"),
    ("数字人视频生产管线", "digital-human-video"),
    ("思维导图信息图", "baoyu-infographic"),
    ("interlinked markdown knowledge base", "llm-wiki"),
    ("测试驱动开发 RED GREEN", "test-driven-development"),
    ("systematic debugging root cause", "systematic-debugging"),
    ("code review security scan", "requesting-code-review"),
    ("throwaway experiment validate idea", "spike"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="M1 技能召回率评测")
    ap.add_argument("--root", default="D:/HermesAgent/skills", help="技能库根目录")
    ap.add_argument("--top", default="5,10", help="top-k 列表，逗号分隔")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"技能库不存在: {root}", file=sys.stderr)
        return 2

    from sgme.operations.skills import search_skills

    cfg = {
        "skills": {
            "enabled": True,
            "source_dirs": [str(root)],
            "budget": 40,
            "vector_cache_policy": "lazy",
        }
    }
    tops = [int(x) for x in args.top.split(",") if x.strip()]

    print(f"技能库: {root}")
    print(f"查询集: {len(QUERIES)} 条")
    print()

    # 统计
    hit_count = {k: 0 for k in tops}
    detail_rows = []
    for q, expect in QUERIES:
        hits = search_skills(q, cfg, None)  # wiki_conn=None：纯 git 源评测
        names = [h["name"] for h in hits]
        row = {"q": q, "expect": expect, "hit_in": None}
        for k in tops:
            if expect in names[:k]:
                hit_count[k] += 1
                row["hit_in"] = k
        row["top5"] = names[:5]
        detail_rows.append(row)

    # 输出
    for row in detail_rows:
        mark = "✅" if row["hit_in"] else "❌"
        print(f"{mark} [{row['q']}] 期望={row['expect']} "
              f"{'命中@top' + str(row['hit_in']) if row['hit_in'] else '未命中'}")
        print(f"      top5={row['top5']}")

    print()
    n = len(QUERIES)
    for k in tops:
        rate = hit_count[k] / n * 100
        print(f"top-{k} 命中率: {hit_count[k]}/{n} = {rate:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
