#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/oneoff/fix_misplaced_references.py：修复 T-107 拆分脚本 references 错位（一次性）。

背景（2026-08-27）：split_oversize_skills.py v1 用 body 行尾切分 frontmatter，
CRLF frontmatter + LF body 时 `---\r` ≠ `---`，insert(-1) 把 references 数组插到
frontmatter 结束符之后（正文开头）。本脚本把错位的 `references: [...]` 行移回
frontmatter 内（结尾 --- 之前），幂等可重跑。

用法：
    python scripts/oneoff/fix_misplaced_references.py --root <projects-root>/skills-hub-work --dry-run
    python scripts/oneoff/fix_misplaced_references.py --root <projects-root>/skills-hub-work --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FM_RE = re.compile(r"\A(---\r?\n.*?\r?\n---\r?\n?)(.*)", re.S)
REF_LINE_RE = re.compile(r"\Areferences:\s*\[[^\]]*\]\s*(?:\r?\n|$)")


def fix_one(skill_dir: Path, apply: bool) -> dict:
    """把 SKILL.md 中错位的 references 行移回 frontmatter。返回结果 dict。"""
    f = skill_dir / "SKILL.md"
    raw = f.read_bytes().decode("utf-8")
    m = FM_RE.match(raw)
    if not m:
        return {"skill": skill_dir.name, "status": "no-frontmatter", "message": "无 frontmatter"}
    fm, body = m.group(1), m.group(2)

    # 已正确（references 在 frontmatter 内）→ 跳过
    if "references:" in fm:
        return {"skill": skill_dir.name, "status": "ok-already", "message": "references 已在 frontmatter 内"}

    # body 开头是错位的 references 行？
    ref_m = REF_LINE_RE.match(body)
    if not ref_m:
        return {"skill": skill_dir.name, "status": "no-misplaced", "message": "body 开头无 references 行"}

    ref_line = ref_m.group(0).rstrip("\r\n")
    rest = body[ref_m.end():]
    # 移掉 references 行后 body 开头可能的空行
    rest = re.sub(r"\A(?:\r?\n)+", "", rest)

    # 插回 frontmatter（结尾 --- 之前），用 fm 自身行尾
    # ⚠️ 不用 parts.insert(-1)——结尾有换行时空串在最后，insert(-1) 会插到 --- 之后
    #    用 rpartition 锚定最后的 --- 结束符，从根上保证位置
    fm_eol = "\r\n" if "\r\n" in fm else "\n"
    head, sep, tail = fm.rpartition("---")
    if not sep:
        return {"skill": skill_dir.name, "status": "no-close-marker", "message": "frontmatter 无 --- 结束符"}
    new_fm = head + ref_line + fm_eol + sep + tail
    new_raw = new_fm + rest

    if not apply:
        return {"skill": skill_dir.name, "status": "would-fix",
                "message": f"references 移回 frontmatter（{len(ref_line.encode('utf-8'))}B）"}

    f.write_text(new_raw, encoding="utf-8", newline="")
    # 写后自检
    after = f.read_bytes().decode("utf-8")
    m2 = FM_RE.match(after)
    if m2 and "references:" in m2.group(1) and not REF_LINE_RE.match(m2.group(2)):
        return {"skill": skill_dir.name, "status": "fixed", "message": "references 已移回 frontmatter"}
    return {"skill": skill_dir.name, "status": "verify-failed", "message": "写后自检未通过"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="修复 references 错位（T-107 一次性修复）")
    ap.add_argument("--root", required=True, help="技能库根目录（<root>/<name>/SKILL.md）")
    ap.add_argument("--apply", action="store_true", help="真实写盘（缺省 dry-run）")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"根目录不存在: {root}", file=sys.stderr)
        return 2

    rows = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        raw = f.read_bytes().decode("utf-8", errors="replace")
        if "references:" not in raw:
            continue  # 无关技能
        rows.append(fix_one(d, args.apply))

    fixed = sum(1 for r in rows if r["status"] in ("fixed", "would-fix"))
    already = sum(1 for r in rows if r["status"] == "ok-already")
    other = [r for r in rows if r["status"] not in ("fixed", "would-fix", "ok-already")]
    print(f"处理 {len(rows)} 技能（{'APPLY' if args.apply else 'DRY-RUN'}）：修复 {fixed} / 已正确 {already} / 异常 {len(other)}")
    for r in other:
        print(f"  ⚠️ {r['skill']}: [{r['status']}] {r['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
