#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/oneoff/hub_skills_normalize.py：hub 独有技能技能化改造（T-109 前置）。

背景（2026-08-27 实锤）：hub 314 个独有技能中 313 个 lint 不过——缺 frontmatter
必填字段（version 152/pattern 313/category 288）+ 114 个超 8K。这是 M4a wiki 迁移
遗留：wiki skill:* 页转技能时只补了 description，其余字段缺失、未拆分。

本脚本做两件事（幂等可重跑）：
1. 补 frontmatter：version=1.0.0（缺时）、pattern=manual（缺时）、
   category 按 description 关键词推断（缺时，默认 uncategorized）
2. 超 8K 技能拆分（复用 split_oversize_skills.split_one——含引言保留/错位根治）

用法：
    python scripts/oneoff/hub_skills_normalize.py [--root D:/Projects/skills-hub-work] [--apply]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FM_RE = re.compile(r"\A(---\r?\n.*?\r?\n---\r?\n?)(.*)", re.S)

# description 关键词 → category 推断表（按命中优先级）
CATEGORY_RULES: list[tuple[str, str]] = [
    (r"comfyui|工作流|节点|模型部署", "creative"),
    (r"hermes|gateway|memory|插件|desktop", "hermes"),
    (r"nas|飞牛|群晖|docker", "devops"),
    (r"vps|代理|clash|xray|sing-box|proxy", "network"),
    (r"douyin|抖音|视频", "social-media"),
    (r"邮件|mail|email", "email"),
    (r"git|github|repo|版本", "github"),
    (r"数据|数据库|sqlite|存储", "data"),
    (r"开发|编码|代码|debug|测试|重构|模块|依赖|前端|浏览器", "software-development"),
    (r"windows|win", "windows"),
    (r"linux|服务器|shell|bash|python环境|venv", "linux"),
    (r"ai|llm|模型|agent|prompt|深度调研|调研|评测|benchmark", "ai"),
    (r"设计|ui|视觉|figma|仪表盘|dashboard", "design"),
    (r"文档|写作|文案|research|学习|教程|书", "research"),
    (r"方法论|决策|矛盾|辩证|群众路线|战略|战术|组织|思想|工作法|学习|平衡", "methodology"),
    (r"音频|music|sound|音乐", "media"),
    (r"安全|批准|备份|终端|命令", "security"),
    (r"逆向|ida|ollydbg|反编译", "security"),
    (r"摄像头|rtsp|onvif|home assistant", "smart-home"),
]
DEFAULT_CATEGORY = "uncategorized"


def infer_category(description: str) -> str:
    """按 description 关键词推断 category（首条命中）。"""
    desc = description or ""
    for pattern, cat in CATEGORY_RULES:
        if re.search(pattern, desc, re.IGNORECASE):
            return cat
    return DEFAULT_CATEGORY


def normalize_one(skill_dir: Path, apply: bool) -> dict:
    """补 frontmatter 字段 + 拆分。返回结果 dict。"""
    f = skill_dir / "SKILL.md"
    raw = f.read_bytes().decode("utf-8")
    m = FM_RE.match(raw)
    if not m:
        return {"skill": skill_dir.name, "status": "no-frontmatter"}

    fm, body = m.group(1), m.group(2)
    fm_eol = "\r\n" if "\r\n" in fm else "\n"

    # 解析现有字段（简化：按行找 key: value）
    fields: dict[str, str] = {}
    for line in fm.split(fm_eol):
        line = line.strip()
        if line.startswith("---") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip().strip('"').strip("'")

    additions: list[str] = []
    # version
    if not fields.get("version"):
        additions.append(f"version: 1.0.0")
    # pattern
    if not fields.get("pattern"):
        additions.append(f"pattern: manual")
    # category
    if not fields.get("category"):
        cat = infer_category(fields.get("description", ""))
        additions.append(f"category: {cat}")

    if not additions:
        return {"skill": skill_dir.name, "status": "no-change"}

    if not apply:
        return {"skill": skill_dir.name, "status": "would-add",
                "message": "+".join(a.split(":")[0] for a in additions)}

    # 插入 frontmatter 结尾 --- 之前
    head, sep, tail = fm.rpartition("---")
    new_fm = head + fm_eol.join(additions) + fm_eol + sep + tail
    f.write_text(new_fm + body, encoding="utf-8", newline="")

    # 拆分（超 8K 时）
    split_result = None
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > 8192:
        try:
            import importlib.util
            # 拆分脚本在 pr-9-splitter 分支（worktree），main 尚未合并——两个位置都找
            split_candidates = [
                Path(__file__).resolve().parents[1] / "split_oversize_skills.py",  # 同仓库 scripts/
                Path(__file__).resolve().parents[2] / "SGME-wt-pr9" / "scripts" / "split_oversize_skills.py",
            ]
            split_path = next((p for p in split_candidates if p.exists()), None)
            if split_path is None:
                raise FileNotFoundError("split_oversize_skills.py 未找到（main/pr-9 均无）")
            spec = importlib.util.spec_from_file_location("split_oversize_skills", split_path)
            assert spec is not None and spec.loader is not None
            split_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(split_mod)
            split_result = split_mod.split_one(skill_dir)
        except Exception as e:
            split_result = {"status": "split-error", "message": str(e)}

    return {"skill": skill_dir.name, "status": "ok",
            "added": [a.split(":")[0] for a in additions],
            "size_before": body_bytes,
            "split": split_result}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hub 独有技能技能化改造")
    ap.add_argument("--root", default="D:/Projects/skills-hub-work")
    ap.add_argument("--apply", action="store_true", help="真实写盘（缺省 dry-run）")
    args = ap.parse_args(argv)

    root = Path(args.root)
    local = {f.parent.name for f in Path("D:/HermesAgent/skills").rglob("SKILL.md")
             if "references" not in f.parts and ".git" not in f.parts}
    hub_only = [d for d in root.iterdir() if d.is_dir() and d.name not in local
                and (d / "SKILL.md").exists()]

    results = [normalize_one(d, args.apply) for d in sorted(hub_only)]
    added = [r for r in results if r["status"] in ("ok", "would-add")]
    nochange = [r for r in results if r["status"] == "no-change"]
    errs = [r for r in results if r["status"] not in ("ok", "would-add", "no-change")]
    splits = [r for r in added if r.get("split") and r["split"].get("status") == "ok"]

    print(f"hub 独有技能: {len(hub_only)} | {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"需补字段: {len(added)} | 无需改: {len(nochange)} | 异常: {len(errs)}")
    print(f"其中超 8K 已拆: {len(splits)}")
    for r in errs[:5]:
        print(f"  ⚠️ {r}")
    # category 推断分布
    from collections import Counter
    cats = Counter()
    for r in added:
        if "category" in r.get("added", []):
            m = re.search(r"category: (\S+)", str(r.get("message", "")))
            # 重新推断
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
