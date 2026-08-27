"""sgme/skills/indexer.py：技能双源收集 + 记录模型（ST-36 M1，可重建派生索引）。

双源（迁移期并存，按名去重，git 目录优先）：
1. 本地 git 工作区目录（<root>/<name>/SKILL.md，frontmatter 为准）
2. wiki_pages 中 tags 含 "skill" 且 status='active' 的页面（title 约定 ``skill:<name>``）

索引器只做「扫描→结构化」，检索排序在 bm25.py / vectors.py；
全部纯函数无全局状态，测试与调用方自由组合。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# 技能清单文件（约定同 Hermes / progressive-skill）
SKILL_FILE = "SKILL.md"
# 名称白名单：字母/数字/下划线/中划线/点（防路径穿越，镜像 skills_hub._validate_name）
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# frontmatter 围栏
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
# wiki skill 页 title 前缀
WIKI_TITLE_PREFIX = "skill:"


@dataclass
class SkillRecord:
    """单条技能索引记录（四级披露的数据底座）。

    Attributes:
        name: 技能名（kebab-case 白名单内）。
        description: 简介（frontmatter description，触发词窗口校验留给 M3 门禁）。
        tags: 标签列表（含固定首标 "skill"，供统一搜索过滤与展示）。
        category: 分类（git 版来自 frontmatter；wiki 版取 category 段去掉 skill/ 前缀）。
        version/pattern/uses: frontmatter 直传字段（uses = 显式依赖声明，入向引用一级信号源）。
        content: 全文（L2 直接下发；L3 物化以字节流另行读取）。
        sha256: 内容指纹（缓存失效判据——commit SHA 的文件级等价物）。
        source: 来源标记（git | wiki）。
        origin_path: 来源路径（git 文件路径或 wiki page_id，溯源用）。
    """

    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = ""
    version: str = ""
    pattern: str = ""
    uses: list[str] = field(default_factory=list)
    content: str = ""
    sha256: str = ""
    source: str = ""
    origin_path: str = ""


def validate_name(name: str) -> str:
    """技能名安全校验（非空/无路径分隔符/无穿越/白名单字符），非法抛 ValueError。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("技能名不能为空")
    name = name.strip()
    if name in (".", "..") or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法技能名（路径分隔符/穿越）: {name!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"技能名含非法字符（仅允许字母/数字/下划线/中划线/点）: {name!r}")
    return name


def parse_skill_md(text: str) -> dict:
    """解析 SKILL.md：返回 {meta: dict, body: str}；无 frontmatter 时 meta 为空 dict。"""
    m = _FM_RE.match(text or "")
    if not m:
        return {"meta": {}, "body": text or ""}
    meta: dict = {}
    try:
        import yaml  # 项目已有依赖（config 层在用）

        loaded = yaml.safe_load(m.group(1))
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        meta = {}
    return {"meta": meta, "body": text[m.end():]}


def _to_list(v) -> list[str]:
    """YAML 标量/列表归一为字符串列表。"""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _record_from_meta(name: str, meta: dict, body: str, source: str, origin: str) -> SkillRecord:
    full = body.strip()
    return SkillRecord(
        name=name,
        description=str(meta.get("description", "") or "").strip(),
        tags=_to_list(meta.get("tags")) or ["skill"],
        category=str(meta.get("category", "") or "").strip(),
        version=str(meta.get("version", "") or "").strip(),
        pattern=str(meta.get("pattern", "") or "").strip(),
        uses=[validate_name(n) for n in _to_list(meta.get("uses"))],
        content=full,
        sha256=hashlib.sha256(full.encode("utf-8")).hexdigest(),
        source=source,
        origin_path=origin,
    )


def collect_from_dir(root: str | Path) -> list[SkillRecord]:
    """扫描目录树：<root>/<name>/SKILL.md（两级分类目录也支持，取末段为名）。"""
    rootp = Path(root)
    out: list[SkillRecord] = []
    if not rootp.is_dir():
        return out
    for f in sorted(rootp.rglob(SKILL_FILE)):
        rel = f.relative_to(rootp)
        name = validate_name(rel.parent.name)
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_skill_md(text)
        rec = _record_from_meta(name, parsed["meta"], parsed["body"], "git", str(f))
        if "skill" not in rec.tags:
            rec.tags.insert(0, "skill")
        out.append(rec)
    return out


def _is_clean_name(s: str) -> bool:
    """是否全由技能名白名单字符组成（[A-Za-z0-9_.-]）。"""
    return bool(s) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", s or ""))


def _ascii_slug(s: str) -> str:
    """仅保留白名单字符，折叠连续分隔符，去首尾分隔符。

    用于把中文 title / page_id 规整为 ASCII 片段（如 '技能库-ff769a90' → 'ff769a90'）。
    """
    kept = [c for c in (s or "") if _NAME_RE.match(c)]
    out = re.sub(r"[-_.]+", "-", "".join(kept))
    return out.strip("-._")


def _wiki_skill_name(title: str, category: str, page_id: str) -> str:
    """为 wiki skill 页推导合法 ASCII 技能名（白名单 [A-Za-z0-9_.-]）。

    优先级：title 的 'skill:' 前缀 > category 子段(skill/X) > 净化 title > page_id ASCII 残段；
    全部失败返回 ''（调用方跳过并记告警）。
    """
    title = (title or "").strip()
    category = (category or "").strip()
    low_cat = category.lower()

    if title.lower().startswith(WIKI_TITLE_PREFIX):
        cand = title[len(WIKI_TITLE_PREFIX):].strip()
    elif low_cat.startswith("skill/") and len(category) > len("skill/"):
        cand = category[len("skill/"):].strip()
    elif low_cat == "skill":
        cand = ""
    else:
        cand = ""

    # 非全 ASCII 名（如中文 title）→ 回退净化 title，再回退 page_id 残段
    if not _is_clean_name(cand):
        cand = _ascii_slug(title) or _ascii_slug(page_id)
    if not _is_clean_name(cand):
        cand = _ascii_slug(cand)
    return cand


def collect_from_wiki(conn: sqlite3.Connection | None) -> list[SkillRecord]:
    """从 wiki_pages 收集 skill 标记活跃页（双条件任一命中即视为技能页）。

    判定条件（2026-08-27 修复：生产数据 skill 页多为 category='skill/xxx' 标记，
    tags 不含 'skill'，旧过滤 ``tags LIKE '%"skill"%'`` 仅命中 3/7；且中文 title
    过不了 validate_name 白名单被静默跳过 → 技能仓库长期为空）：
    - tags 含 'skill'（设计原约定，兼容 JSON 双/单引号存储），或
    - category 以 'skill' 开头（'skill' / 'skill/vps' 等）

    命名：经 ``_wiki_skill_name`` 推导合法 ASCII 名（中文 title 自动规整为 ASCII
    片段或 page_id 哈希残段），同名追加 page_id 哈希前缀消歧。
    连接为 None 或查询异常时返回空列表（容错隔离，不影响 git 源）。
    """
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT page_id, title, content, category, tags FROM wiki_pages "
            "WHERE status='active' AND (tags LIKE '%skill%' OR lower(category) LIKE 'skill%') "
            "ORDER BY title"
        ).fetchall()
    except Exception:
        return []
    out: list[SkillRecord] = []
    seen: set[str] = set()
    for r in rows:
        # ⚠️ 不依赖外部 row_factory（裸 sqlite3.connect 调用时 r 是元组；
        #    生产 FastAPI wiki_conn 设了 row_factory。此处按位置取列 + Row 兼容）
        if isinstance(r, sqlite3.Row):
            page_id, title, content, category, tags = (
                r["page_id"], r["title"], r["content"], r["category"], r["tags"])
        else:
            page_id, title, content, category, tags = (r[0], r[1], r[2], r[3], r[4])
        title = title or ""
        name = _wiki_skill_name(title, category or "", str(page_id))
        if not name:
            continue
        # 同名消歧：追加 page_id 稳定哈希末 4 位（hashlib 非进程随机，可复现）
        if name in seen:
            suffix = hashlib.sha256(str(page_id).encode("utf-8")).hexdigest()[:4]
            name = f"{name}-{suffix}"
        seen.add(name)
        cat = (category or "").strip().removeprefix("skill/")
        out.append(SkillRecord(
            name=name,
            description="",
            tags=["skill"],
            category=cat,
            content=(content or "").strip(),
            sha256=hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
            source="wiki",
            origin_path=title,
        ))
    return out


def merge_records(*groups: list[SkillRecord]) -> list[SkillRecord]:
    """多源合并去重：同名时先出现的组优先（调用方把 git 组放前面）。"""
    seen: dict[str, SkillRecord] = {}
    for g in groups:
        for rec in g:
            if rec.name not in seen:
                seen[rec.name] = rec
    return [seen[k] for k in sorted(seen)]


def index_all(source_dirs: list[str], wiki_conn=None) -> list[SkillRecord]:
    """双源索引入口：git 工作区组在前（同名优先），wiki 组在后；按名排序返回。"""
    groups = [collect_from_dir(d) for d in (source_dirs or [])]
    groups.append(collect_from_wiki(wiki_conn))
    return merge_records(*groups)
