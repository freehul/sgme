#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/oneoff/skills_recall_eval.py：M1 及格线①召回率评测（ST-36，T-108 扩展版）。

设计 §二 M1 三条及格线之①「统一搜索命中技能率可统计提升」——真实技能库
（git 源）+ 代表性查询集（按技能分类均匀覆盖触发场景），统计 top-k 命中率。

T-108 扩展（2026-08-27）：
- 查询集 20 → 52 条（覆盖 18 分类 ≈ 100 技能，中英按内容语言匹配）
- wiki 双源对照：--with-wiki 时接入 wiki_conn（wiki skill 标记页也进索引），
  与纯 git 源对比召回差异——三层统一检索基线的一部分

判据：每个查询有一个「期望命中的技能名」，top-5 / top-10 命中期望 = 召回。

用法：
    python scripts/oneoff/skills_recall_eval.py [--root D:/HermesAgent/skills] [--top 5,10] [--with-wiki]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 52 条查询：(查询, 期望技能名)——覆盖 18 分类；英文技能用英文查询（内容语言匹配）
QUERIES: list[tuple[str, str]] = [
    # apple（4）
    ("Apple Notes 笔记读写", "apple-notes"),
    ("remindctl reminders add list complete", "apple-reminders"),
    ("track Apple devices AirTags FindMy", "findmy"),
    ("iMessage 发送消息", "imessage"),
    # autonomous-ai-agents（6）
    ("SGME 挂了怎么排查服务", "sgme-operations"),
    ("SGME 功能开发实战 worktree", "sgme-development"),
    ("delegate coding to Claude Code CLI", "claude-code"),
    ("delegate coding to OpenAI Codex CLI", "codex"),
    ("drive desktop background first", "computer-use"),
    ("orchestrate Hermes Agent features", "hermes-agent"),
    # creative（12）
    ("dark themed SVG architecture diagram", "architecture-diagram"),
    ("ASCII art 大字横幅", "ascii-art"),
    ("视频转 ASCII 彩色动图", "ascii-video"),
    ("信息图 21 布局 21 风格", "baoyu-infographic"),
    ("HTML 落地页设计稿", "claude-design"),
    ("ComfyUI 文生图工作流", "comfyui"),
    ("Google DESIGN.md token 规范", "design-md"),
    ("本地数字人视频口播", "digital-human-video"),
    ("手绘风 Excalidraw 图", "excalidraw"),
    ("文本去 AI 味人味化", "humanizer"),
    ("Manim 数学动画", "manim-video"),
    ("p5.js 生成艺术", "p5js"),
    # devops/email（3）
    ("SDLC 评审流程", "sdlc-review"),
    ("triage inbox prioritize threads", "email-inbox-triage"),
    ("IMAP SMTP 命令行邮件", "himalaya"),
    # github（6）
    ("pygount 代码量统计", "codebase-inspection"),
    ("GitHub 认证 token 配置", "github-auth"),
    ("review PR diffs inline comments", "github-code-review"),
    ("issue 转 PR 流程", "github-issue-to-pr"),
    ("PR 生命周期合并", "github-pr-workflow"),
    ("仓库克隆管理 fork", "github-repo-management"),
    # mlops（5）
    ("LLM 评测框架 harness", "evaluating-llms-harness"),
    ("W&B 实验跟踪", "weights-and-biases"),
    ("HuggingFace 下载模型", "huggingface-hub"),
    ("llama.cpp GGUF 推理", "llama-cpp"),
    ("vLLM 模型服务部署", "serving-llms-vllm"),
    # note/productivity（12）
    ("Obsidian 笔记库操作", "obsidian"),
    ("Airtable 表格 API", "airtable"),
    ("Box 网盘文件管理", "box"),
    ("Google 全家桶 Gmail 日历", "google-workspace"),
    ("地图 POI 路线", "maps"),
    ("meeting notes cited decisions owners", "meeting-action-items"),
    ("PDF 文本编辑 nano", "nano-pdf"),
    ("Notion 页面数据库", "notion"),
    ("OCR 提取 PDF 扫描件", "ocr-and-documents"),
    ("PDF 合并拆分", "pdf"),
    ("PowerPoint 演示文稿", "powerpoint"),
    ("watch product flight prices alert", "product-price-monitor"),
    # research（5）
    ("arXiv 论文检索", "arxiv"),
    ("recover paywalled blocked pages", "blocked-page-recovery"),
    ("RSS 博客监控", "blogwatcher"),
    ("watch companies material news", "competitor-news-monitor"),
    ("ground answers cited verifiable sources", "grounded-citations"),
    # smart-home/social（3）
    ("Philips Hue 灯控", "openhue"),
    ("代理栈 VPS 节点排障", "proxy-stack-ops"),
    ("抖音视频分析管线", "video-analysis-pipeline"),
    # software-development（12）
    ("编码纪律 写代码前加载", "coding-discipline"),
    ("Web 应用探索性 QA", "dogfood"),
    ("git 历史隐私清洗", "git-history-scrub"),
    ("PR 拆分本地合并", "git-pr-workflow"),
    ("author in-repo SKILL.md frontmatter", "hermes-agent-skill-authoring"),
    ("桌面 DOM 检查 CDP", "inspecting-hermes-desktop-dom"),
    ("logged-in browser session reuse", "logged-in-browser"),
    ("Node inspect 调试", "node-inspect-debugger"),
    ("计划落盘 markdown", "plan"),
    ("发布前隐私审查", "release-audit"),
    ("发布部署缓存排障", "release-deploy-cache"),
    ("simplify code 并行清理", "simplify-code"),
]

# wiki 双源对照：这些查询的期望技能在 wiki 侧也有 skill 标记页（可对照双源召回）
WIKI_CONTRAST_EXPECTS = {
    "sgme-operations", "sgme-development", "coding-discipline", "git-pr-workflow",
    "proxy-stack-ops", "video-analysis-pipeline", "docx", "xlsx",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="M1 技能召回率评测（T-108 扩展版）")
    ap.add_argument("--root", default="D:/HermesAgent/skills", help="技能库根目录")
    ap.add_argument("--top", default="5,10", help="top-k 列表，逗号分隔")
    ap.add_argument("--with-wiki", action="store_true", help="接入 wiki 双源对照")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"技能库不存在: {root}", file=sys.stderr)
        return 2

    from sgme.operations.skills import search_skills

    # wiki_conn：--with-wiki 时接入生产 wiki.db 副本（拉取：scp nas:/vol1/1000/Docker/sgme/data/data/wiki.db data/wiki_prod.db）
    wiki_conn = None
    if args.with_wiki:
        wiki_db = Path("D:/Projects/SGME/data/wiki_prod.db")
        if wiki_db.exists():
            import sqlite3
            wiki_conn = sqlite3.connect(str(wiki_db))
            cur = wiki_conn.cursor()
            cur.execute("SELECT COUNT(*) FROM wiki_pages WHERE status='active'")
            print(f"wiki 双源对照: {wiki_db}（active {cur.fetchone()[0]} 页，含 superseded 归档）")
        else:
            print(f"⚠️ wiki 生产副本不存在（{wiki_db}），跳过双源对照", file=sys.stderr)

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
    print(f"查询集: {len(QUERIES)} 条 | 模式: {'git+wiki 双源' if wiki_conn else '纯 git 源'}")
    print()

    hit_count = {k: 0 for k in tops}
    misses = []
    for q, expect in QUERIES:
        hits = search_skills(q, cfg, wiki_conn)
        names = [h["name"] for h in hits]
        # 每个 k 独立统计（top-10 命中包含 top-5 命中的，不互斥）
        for k in tops:
            if expect in names[:k]:
                hit_count[k] += 1
        if expect not in names[: max(tops)]:
            misses.append((q, expect, names[:5]))

    n = len(QUERIES)
    print(f"{'top-k':>6} {'命中':>4} {'/':>1} {'总数':>4} {'命中率':>8}")
    for k in tops:
        print(f"{k:>6} {hit_count[k]:>4} {n:>4} {hit_count[k]/n*100:>7.1f}%")

    if misses:
        print(f"\n未命中 {len(misses)} 条：")
        for q, expect, top5 in misses:
            print(f"  ❌ [{q}] 期望={expect} | top5={top5}")

    # wiki 双源对照：对比 WIKI_CONTRAST_EXPECTS 集合在双源 vs 单源的召回
    if wiki_conn:
        print("\n--- wiki 双源对照（期望技能在 wiki 也有标记页）---")
        for q, expect in QUERIES:
            if expect not in WIKI_CONTRAST_EXPECTS:
                continue
            hits_git = search_skills(q, cfg, None)
            hits_dual = search_skills(q, cfg, wiki_conn)
            g = expect in [h["name"] for h in hits_git]
            d = expect in [h["name"] for h in hits_dual]
            mark = "✅" if d else "❌"
            print(f"  {mark} [{q}] 期望={expect} git={'✓' if g else '✗'} 双源={'✓' if d else '✗'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
