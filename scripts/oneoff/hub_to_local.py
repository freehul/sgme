#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/oneoff/hub_to_local.py：hub 技能纳入本地库（T-109 执行）。

hub 扁平结构（<name>/SKILL.md）→ 本地库分类结构（<category>/<name>/SKILL.md）。
只纳入 lint 通过的 active 技能；复制 SKILL.md + references/（含外置文件）；
幂等（本地已有同名跳过）；每批 git 提交由调用方执行。

用法：
    python scripts/oneoff/hub_to_local.py [--dry-run]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

HUB = Path("D:/Projects/skills-hub-work")
LOCAL = Path("D:/HermesAgent/skills")

# category → 本地库分类目录（hub 的扁平 category 直接作为分类目录名）
def dest_dir(category: str) -> str:
    """category 映射到本地库分类目录（安全化：kebab-case）。"""
    import re
    cat = (category or "uncategorized").strip().lower()
    cat = re.sub(r"[^a-z0-9-]+", "-", cat).strip("-")
    return cat or "uncategorized"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hub 技能纳入本地库")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from sgme.skills.gates import lint_skill
    from sgme.skills.indexer import parse_skill_md

    local_names = {f.parent.name for f in LOCAL.rglob("SKILL.md")
                   if "references" not in f.parts and ".git" not in f.parts}
    hub_only = [d for d in HUB.iterdir() if d.is_dir() and d.name not in local_names
                and (d / "SKILL.md").exists()]

    copied = []
    skipped = []
    for d in sorted(hub_only):
        raw = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        parsed = parse_skill_md(raw)
        meta = parsed["meta"] or {}
        if meta.get("status") == "deprecated":
            skipped.append((d.name, "deprecated"))
            continue
        v = lint_skill(meta, parsed["body"], d.name, set())
        if v:
            skipped.append((d.name, f"lint({len(v)})"))
            continue
        cat = dest_dir(meta.get("category", "uncategorized"))
        target = LOCAL / cat / d.name
        if target.exists():
            skipped.append((d.name, "本地已存在"))
            continue
        if args.dry_run:
            copied.append((d.name, cat))
            continue
        # 复制 SKILL.md + references/（如有）
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(d / "SKILL.md", target / "SKILL.md")
        refs = d / "references"
        if refs.is_dir():
            shutil.copytree(refs, target / "references")
        copied.append((d.name, cat))

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"{mode}: 纳入 {len(copied)} | 跳过 {len(skipped)}")
    from collections import Counter
    cat_count = Counter(c for _, c in copied)
    print("按分类:", dict(cat_count.most_common()))
    if not args.dry_run:
        print("已复制到本地库，请 git add + commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
