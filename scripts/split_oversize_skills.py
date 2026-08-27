#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/split_oversize_skills.py：超 8K 技能批量拆分外置化（ST-36 T-107）。

把 >8192 字节正文的技能 SKILL.md 拆为「入口 + references/*.md」标准形态：
- 入口保留：frontmatter + 首个 ## 前的引言 + 未超限小节 + 外置块的一行引用
- 按 ## 二级标题切块，块字节级拷贝进 references/<NN>-<slug>.md（内容零改写）
- frontmatter 追加 references: [...] 数组；幂等（已有 references/ 跳过）

用法：
    python scripts/split_oversize_skills.py --root D:/Projects/skills-hub-work --dry-run
    python scripts/split_oversize_skills.py --root ... --names aixm,sgme --apply

默认 dry-run 只出报告；--apply 才写文件。报告 markdown 分节：成功/跳过/needs-manual。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

MAX_BODY_BYTES = 8192  # 与 sgme/skills/gates.py 保持一致

FM_RE = re.compile(r"\A(---\r?\n.*?\r?\n---\r?\n?)(.*)", re.S)
H2_RE = re.compile(r"^##\s+", re.M)


def _eol_of(text: str) -> str:
    """取正文主行尾（CRLF 优先——hub 实际数据以 CRLF 为准）。"""
    return "\r\n" if "\r\n" in text else "\n"


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 ## 切块。返回 [(标题行, 块全文含标题)]；首个 ## 前的引言不在结果中。

    块边界=下一个 ## 行首；块尾多余空行在写外置文件时去除，入口/外置内容字节保真。
    """
    matches = list(H2_RE.finditer(body))
    if not matches:
        return []
    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end]
        title = chunk.split("\n", 1)[0].lstrip("#").strip()
        sections.append((title, chunk.rstrip() + "\n"))
    return sections


def slugify(title: str, idx: int, used: set[str]) -> str:
    """英文标题转 kebab-case；非 ASCII（中文等）用 section-序号。"""
    s = re.sub(r"[^A-Za-z0-9]+", "-", title.lower()).strip("-")
    if not s or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", s):
        s = f"section-{idx:02d}"
    base = s[:48]
    name, k = base, 2
    while name in used:
        name = f"{base}-{k}"
        k += 1
    return name


def split_one(skill_dir: Path) -> dict:
    """拆单个技能目录。返回结果 dict（status/skill/before/after/refs/message）。"""
    skill = skill_dir.name
    f = skill_dir / "SKILL.md"
    raw = f.read_bytes().decode("utf-8")
    before = len(raw.encode("utf-8"))

    fm_m = FM_RE.match(raw)
    fm, body = (fm_m.group(1), fm_m.group(2)) if fm_m else ("", raw)

    eol = _eol_of(body)
    intro_end = H2_RE.search(body)
    intro = body[: intro_end.start()] if intro_end else ""
    sections = split_sections(body)
    if not sections:
        return {"status": "needs-manual", "skill": skill, "before": before,
                "message": "正文无二级标题可切"}

    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)

    # 先全量切块按字节降序排，贪心外置直到入口 ≤8K；短块(<512B)不值得外置留原文
    order = sorted(range(len(sections)), key=lambda i: -len(sections[i][1].encode("utf-8")))
    externalized: set[int] = set()
    used_names: set[str] = set()
    ref_files: list[str] = []  # 保序，最后写 frontmatter

    def entrance_size() -> tuple[int, str]:
        kept = "".join(sections[i][1] for i in range(len(sections)) if i not in externalized)
        link_lines = eol.join(
            f"- [{sections[i][0]}](references/{ref_map[i]})" for i in sorted(externalized)
        )
        head = intro + kept + (eol + eol + link_lines + eol if externalized else "")
        return len((fm + head).encode("utf-8")), head

    ref_map: dict[int, str] = {}
    # 第一遍：>2048 字节的块全部候选外置（入口引用行远小于块本体）
    for i in order:
        if len(sections[i][1].encode("utf-8")) <= 2048:
            continue
        slug = slugify(sections[i][0], i + 1, used_names)
        fname = f"{i + 1:02d}-{slug}.md"
        used_names.add(slug)
        ref_map[i] = fname
        externalized.add(i)
        ref_files.append(fname)
        size, _ = entrance_size()
        if size <= MAX_BODY_BYTES:
            break

    size, head = entrance_size()

    if size > MAX_BODY_BYTES:
        # 还超 → 继续外置剩余长块（含 <2048 的），直到只剩骨架
        for i in order:
            if i in externalized:
                continue
            slug = slugify(sections[i][0], i + 1, used_names)
            fname = f"{i + 1:02d}-{slug}.md"
            used_names.add(slug)
            ref_map[i] = fname
            externalized.add(i)
            ref_files.append(fname)
            size, head = entrance_size()
            if size <= MAX_BODY_BYTES:
                break

    after = size
    if after > MAX_BODY_BYTES:
        # 全部外置仍超限（frontmatter 或引言本身巨大）→ needs-manual 不写入
        return {"status": "needs-manual", "skill": skill, "before": before,
                "after": after, "message": f"全量外置后入口仍 {after}B"}

    # ---- dry-run 到此为止，以下才真正写盘 ----
    result_refs: list[str] = []
    for i in sorted(ref_map):
        fname = ref_map[i]
        (refs_dir / fname).write_text(sections[i][1], encoding="utf-8", newline="")
        result_refs.append(fname)

    # frontmatter 追加 references 数组（幂等键）
    new_fm = fm
    if new_fm and "references:" not in new_fm:
        arr = ", ".join(result_refs)
        ins = f"references: [{arr}]" + eol
        parts = new_fm.split(eol)
        parts.insert(-1, ins.rstrip(eol))  # 插到结尾 --- 之前
        new_fm = eol.join(parts) + eol

    kept = "".join(sections[i][1] for i in range(len(sections)) if i not in externalized)
    links = eol.join(
        f"- [{sections[i][0]}](references/{ref_map[i]})" for i in sorted(externalized)
    )
    new_head = kept + (eol + eol + "## 外置章节" + eol + eol + links + eol if externalized else "")
    f.write_text(new_fm + new_head, encoding="utf-8", newline="")

    return {"status": "ok", "skill": skill, "before": before, "after": after,
            "refs": result_refs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="超 8K 技能批量拆分外置化（T-107）")
    ap.add_argument("--root", required=True, help="技能库根目录（<root>/<name>/SKILL.md）")
    ap.add_argument("--names", help="逗号分隔只处理指定技能；缺省=全部超限件")
    ap.add_argument("--apply", action="store_true", help="真实写盘（缺省 dry-run）")
    ap.add_argument("--report", help="报告落盘路径；缺省 stdout")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"根目录不存在: {root}", file=sys.stderr)
        return 2

    only = set(args.names.split(",")) if args.names else None
    targets = []
    total_over = 0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        raw = f.read_bytes().decode("utf-8", errors="replace")
        fm_m = FM_RE.match(raw)
        body = fm_m.group(2) if fm_m else raw
        n = len(body.encode("utf-8"))
        if n > MAX_BODY_BYTES:
            total_over += 1
            if only is None or d.name in only:
                targets.append(d)

    rows = {"ok": [], "skip": [], "manual": []}
    for d in targets:
        if (d / "references").is_dir():
            rows["skip"].append({"skill": d.name, "message": "已有 references/ 目录（幂等跳过）"})
            continue
        r = split_one(d) if args.apply else {
            "status": "dry-run-ok", "skill": d.name,
            "message": "dry-run 预演通过（--apply 才写盘）",
        }
        key = {"ok": "ok", "dry-run-ok": "ok", "needs-manual": "manual"}.get(r["status"], "manual")
        rows[key].append(r)

    lines = [
        "# 超 8K 技能拆分报告（T-107）",
        "",
        f"- 扫描根: `{args.root}`",
        f"- 超 8K 总数: {total_over}",
        f"- 本次处理: {len(targets)}（{'APPLY' if args.apply else 'DRY-RUN'}）",
        f"- 成功: {len(rows['ok'])} / 幂等跳过: {len(rows['skip'])} / needs-manual: {len(rows['manual'])}",
        "",
        "## 明细",
        "",
    ]
    for r in rows["ok"]:
        if "before" in r:
            lines.append(
                f"- ✅ {r['skill']}: {r['before']}B → {r.get('after', '?')}B，外置 {len(r.get('refs', []))} 文件"
            )
        else:
            lines.append(f"- ✅ {r['skill']}: dry-run 预演通过（--apply 才写盘）")
    for r in rows["skip"]:
        lines.append(f"- ⏭️ SKIP {r['skill']}: {r['message']}")
    for r in rows["manual"]:
        lines.append(f"- ⚠️ NEEDS-MANUAL {r['skill']}: {r.get('message', '')}")
    out = "\n".join(lines) + "\n"

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(out, encoding="utf-8", newline="")
        print(f"report -> {args.report}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
