#!/usr/bin/env python
"""scripts/find_atomic_candidates.py：原子化候选扫描（ST-36 M4b，纯规则零 LLM）。

判据（设计文档 M4b）：**一段内容出现在 ≥2 个技能里才抽原子技能**；
反向红线=无独立触发场景的内容不拆（防碎渣化）。本脚本只产候选清单供
用户拍板，不做任何重组——成本纪律。

算法：
1. 收集技能：--dirs 给一个或多个技能目录树（<root>/<name>/SKILL.md），
   可选 --db wiki.db 把 wiki skill 页也当技能参与扫描。
2. 段落切分：按空行分隔，过滤 <30 字符的短段（防碎渣化）。
3. 归一化：去所有空白差异 + 小写 → SHA1 精确指纹；SHA1 出现在 ≥2 个
   不同技能的段落是天然候选（同指纹 ratio=1.0 必然同组）。
4. 近似合并：difflib SequenceMatcher ratio ≥ 0.85 的段落聚为一组——
   把只有微小白噪音差异的变体也并进同一候选组，而不是漏报或拆碎。
5. 只保留组内涉及 ≥2 个技能的簇作为候选输出（单技能内的自重复不拆）。

输出：markdown 报告——每组列重复度/涉及技能名列表/段落首行预览/出现次数，
结尾统计（组数/涉及技能数/潜在可抽原子数）；--json 另出机器可读版。

用法示例：
  python scripts/find_atomic_candidates.py --dirs skills ~/.hermes/skills
  python scripts/find_atomic_candidates.py --dirs skills --db data/wiki.db --json report.json
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 段落最短长度（字符）：低于它的段落视为碎渣，直接过滤
MIN_PARA_LEN = 30
# 近似合并阈值：SequenceMatcher ratio ≥ 此值的两个候选段落视为同组
SIMILAR_RATIO = 0.85


@dataclass
class Para:
    """一个技能内的一个候选段落。"""

    skill: str      # 技能名
    raw: str        # 原文段落
    norm: str       # 归一化文本（小写、无空白）
    sha1: str       # 归一化后 SHA1


@dataclass
class CandidateGroup:
    """一组近似重复的跨技能段落。"""

    paras: list[Para] = field(default_factory=list)

    @property
    def skills(self) -> list[str]:
        """涉及技能名列表（保持首次出现顺序）。"""
        seen: list[str] = []
        for para in self.paras:
            if para.skill not in seen:
                seen.append(para.skill)
        return seen

    @property
    def occurrences(self) -> int:
        return len(self.paras)

    @property
    def similarity(self) -> float:
        """组内最低两两近似度（代表整组的重复度下界）。"""
        ratios = [
            difflib.SequenceMatcher(None, a.norm, b.norm).ratio()
            for i, a in enumerate(self.paras)
            for b in self.paras[i + 1 :]
        ]
        return round(min(ratios), 2) if ratios else 1.0

    @property
    def preview(self) -> str:
        """段落首行预览（截 60 字符）。"""
        first_line = self.paras[0].raw.strip().splitlines()[0] if self.paras else ""
        return first_line[:60]


# ---------------------------------------------------------------------------
# 输入收集
# ---------------------------------------------------------------------------


def collect_from_dirs(roots: list[Path]) -> dict[str, str]:
    """扫目录树：<root>/<name>/SKILL.md → {技能名: 全文}。同名后者覆盖并告警。"""
    skills: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            print(f"警告：目录不存在，跳过: {root}", file=sys.stderr)
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            name = skill_md.parent.name
            try:
                content = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"警告：读取失败跳过 {skill_md}: {exc}", file=sys.stderr)
                continue
            if name in skills and skills[name] != content:
                print(f"警告：技能 {name} 在多个根目录出现且内容不同，取后者", file=sys.stderr)
            skills[name] = content
    return skills


def collect_from_db(db_path: Path) -> dict[str, str]:
    """wiki.db 的 skill 页也当技能参与扫描。

    与 migrate_wiki_skills 同一套筛选约定：status='active' 且 tags 含 "skill"；
    tags 是 JSON 数组字符串；title 约定 skill:<name>——只认带前缀的页
    （无前缀的按 M4a 口径属「误挂标签知识页」，不参与扫描）。
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT page_id, title, content, tags FROM wiki_pages WHERE status='active'"
        ).fetchall()
    finally:
        conn.close()
    skills: dict[str, str] = {}
    for page_id, title, content, tags_json in rows:
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        if "skill" not in [str(t) for t in (tags or [])]:
            continue
        title = title or ""
        if not title.lower().startswith("skill:"):
            continue  # 无前缀=疑似误挂标签的知识页，不算技能
        name = title.split(":", 1)[1].strip() or f"wiki-{page_id}"
        skills[name] = content or ""
    return skills


# ---------------------------------------------------------------------------
# 扫描算法
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """归一化：去掉所有空白差异 + 小写（只比内容本身）。"""
    return "".join((text or "").split()).lower()


def split_paragraphs(content: str) -> list[str]:
    """空行分隔切段。frontmatter 围栏与围栏内的行不算正文段落。"""
    lines = content.splitlines()
    # 剥离页首 frontmatter（--- ... ---），防止元数据行被当正文比
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1 :]
                break
    text = "\n".join(lines)
    paras = re_split_paragraphs(text)
    return [p for p in paras if len(p.strip()) >= MIN_PARA_LEN]


def re_split_paragraphs(text: str) -> list[str]:
    r"""按空行切分段落（\n\s*\n 为界）。"""
    import re

    return [p.strip() for p in re.split(r"\n[ \t]*\n+", text) if p.strip()]


def scan(skills: dict[str, str]) -> list[CandidateGroup]:
    """主算法：全量段落贪心聚类（ratio≥0.85）→ 筛跨技能簇为候选。

    返回按出现次数降序的组列表。同 SHA1 的段落归一化文本相同、ratio=1.0，
    必然聚进同一组，所以精确命中是近似合并的特例，两条路径统一。
    """
    all_paras: list[Para] = []
    for skill_name, content in skills.items():
        for para in split_paragraphs(content):
            normed = normalize(para)
            if not normed:
                continue
            digest = hashlib.sha1(normed.encode("utf-8")).hexdigest()
            all_paras.append(Para(skill=skill_name, raw=para, norm=normed, sha1=digest))

    # 贪心单遍聚类：与已有组的代表比 ratio，≥阈值入组，否则自立新组
    groups: list[list[Para]] = []
    reps: list[str] = []
    for para in all_paras:
        placed = False
        for gi, rep in enumerate(reps):
            if difflib.SequenceMatcher(None, rep, para.norm).ratio() >= SIMILAR_RATIO:
                groups[gi].append(para)
                placed = True
                break
        if not placed:
            groups.append([para])
            reps.append(para.norm)

    # 只留跨技能簇：组内出现 ≥2 个不同技能才算原子化候选
    result = [
        CandidateGroup(paras=paras)
        for paras in groups
        if len({p.skill for p in paras}) >= 2
    ]
    result.sort(key=lambda g: (-g.occurrences, -g.similarity))
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_markdown(groups: list[CandidateGroup], total_skills: int) -> str:
    """扫描结果 → markdown 报告。"""
    lines: list[str] = ["# 原子化候选扫描报告（ST-36 M4b）", ""]
    if not groups:
        lines += [
            "未发现跨技能重复段落（判据：同一归一化段落出现在 ≥2 个技能且 ≥30 字符）。",
            "",
            f"## 统计",
            "",
            f"- 组数: 0",
            f"- 涉及技能数: 0",
            f"- 潜在可抽原子数: 0",
            "",
        ]
        return "\n".join(lines)

    involved = sorted({s for g in groups for s in g.skills})
    lines.append(f"共扫描 {total_skills} 个技能，发现 {len(groups)} 组疑似重复段落（仅候选，用户拍板后才重组）。")
    lines.append("")
    for idx, group in enumerate(groups, 1):
        skills_str = ", ".join(f"`{s}`" for s in group.skills)
        lines += [
            f"## 候选 {idx}",
            "",
            f"- 重复度（组内最低两两近似度）: {group.similarity:.2f}",
            f"- 出现次数: {group.occurrences}",
            f"- 涉及技能: {skills_str}",
            f"- 段落预览: {group.preview}",
            "",
        ]
    lines += [
        "## 统计",
        "",
        f"- 组数: {len(groups)}",
        f"- 涉及技能数: {len(involved)}",
        f"- 潜在可抽原子数: {len(groups)}（每组抽一个原子技能）",
        "",
        "> 反向红线提醒：无独立触发场景的内容不拆（防碎渣化伤检索）。",
        "",
    ]
    return "\n".join(lines)


def render_json(groups: list[CandidateGroup]) -> dict:
    """扫描结果 → 机器可读 dict。"""
    return {
        "groups": [
            {
                "similarity": group.similarity,
                "skills": group.skills,
                "preview": group.preview,
                "occurrences": group.occurrences,
                "paras": [{"skill": p.skill, "sha1": p.sha1} for p in group.paras],
            }
            for group in groups
        ],
        "stats": {
            "groups": len(groups),
            "skills_involved": len({s for g in groups for s in g.skills}),
            "potential_atoms": len(groups),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="原子化候选扫描（ST-36 M4b，纯规则零 LLM）")
    ap.add_argument("--dirs", nargs="+", required=True, help="一个或多个技能目录树（<root>/<name>/SKILL.md）")
    ap.add_argument("--db", default=None, help="可选 wiki.db 路径：wiki skill 页也当技能参与扫描")
    ap.add_argument("--json", dest="json_out", default=None, help="机器可读 JSON 报告输出路径")
    ap.add_argument("--report", default=None, help="markdown 报告输出路径（缺省打印 stdout）")
    args = ap.parse_args(argv)

    skills = collect_from_dirs([Path(p) for p in args.dirs])
    if args.db:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"错误：数据库不存在: {db_path}", file=sys.stderr)
            return 2
        skills.update(collect_from_db(db_path))
    if not skills:
        print("错误：未收集到任何技能（检查 --dirs/--db）", file=sys.stderr)
        return 2

    groups = scan(skills)
    md_text = render_markdown(groups, total_skills=len(skills))
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md_text, encoding="utf-8")
        print(f"markdown 报告已写入: {report_path}")
    else:
        print(md_text)
    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(render_json(groups), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON 报告已写入: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
