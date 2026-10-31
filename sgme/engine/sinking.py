"""沉淀清单抽取（零 LLM）：从会话原件里认出「这段会话已经写进技能库/知识库」的条目。

为什么不是让 LLM 再判一次：一个记忆过两次 LLM 等于成本翻倍（用户 2026-10-31 明确否定）。
会话原件里本来就留有调用痕迹——`wiki_page_add` 的返回、`skill_put` 的技能名、
`skills/<分类>/<名>/SKILL.md` 的路径——正则就能拿到，零 token。

产出交给 L1 提示词当上下文（让模型对已沉淀主题只写摘要不写细节），
并在落库时按标题重合度建「记忆 → 知识页/技能」关联。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: wiki 页写入调用（MCP/HTTP 两种命名都认）
_WIKI_CALL = re.compile(r"(?:sgme_)?wiki_page_(?:add|update)")
#: 技能写入调用
_SKILL_CALL = re.compile(r"(?:sgme_)?skill_put")
#: 技能文件路径（Hermes/SGME 本地技能库形态）
_SKILL_PATH = re.compile(r"skills[\\/]([A-Za-z0-9_-]+)[\\/]([A-Za-z0-9._-]+)[\\/]SKILL\.md", re.I)

_PAGE_ID = re.compile(r"page_id[\"']?\s*[:=]\s*[\"']?([\w\u4e00-\u9fff.:-]{3,90})")
#: 搜索结果里的 page_id（真 slug 一定含连字符且 ≥8 字符，能滤掉 "p1" 这类占位）
_PAGE_ID_SLUG = re.compile(r"page_id[\"']?\s*[:=]\s*[\"']([\w\u4e00-\u9fff.-]{8,120})[\"']")
#: slug 后紧跟的标题（搜索结果形态：{"page_id": ..., "title": ...}）
_TITLE_AFTER = re.compile(r"[^}]{0,120}?[\"']title[\"']\s*:\s*[\"']([^\"']{2,80})[\"']")
_TITLE = re.compile(r"(?:title|页面标题)[\"']?\s*[:=]\s*[\"']([^\"'\n]{2,80})")
_SKILL_NAME = re.compile(r"(?:name|技能名)[\"']?\s*[:=]\s*[\"']([a-z0-9][a-z0-9_-]{2,60})")

#: 调用点到返回值的窗口（返回 JSON 通常紧跟其后）
_WINDOW = 600
#: 清单上限（防止长会话里刷出几百条把提示词撑爆）
MAX_REFS = 20


@dataclass(frozen=True)
class SunkRef:
    """一条已沉淀条目。"""

    kind: str            # wiki | skill
    ref_id: str          # wiki: page_id；skill: 技能名
    title: str | None = None

    def line(self) -> str:
        if self.kind == "wiki":
            return f"- 知识页《{self.title or self.ref_id}》"
        return f"- 技能 `{self.ref_id}`"


def extract_sunk_refs(text: str, *, max_refs: int = MAX_REFS) -> list[SunkRef]:
    """从会话原文抽「已沉淀」条目。纯规则、幂等、零 LLM。"""
    if not text:
        return []

    found: dict[tuple[str, str], SunkRef] = {}

    for m in _WIKI_CALL.finditer(text):
        window = text[m.start(): m.start() + _WINDOW]
        pm = _PAGE_ID.search(window)
        if not pm:
            continue
        title_m = _TITLE.search(window)
        ref = SunkRef("wiki", pm.group(1), title_m.group(1) if title_m else None)
        found.setdefault((ref.kind, ref.ref_id), ref)

    for m in _SKILL_CALL.finditer(text):
        window = text[m.start(): m.start() + _WINDOW]
        nm = _SKILL_NAME.search(window)
        if not nm:
            continue
        ref = SunkRef("skill", nm.group(1))
        found.setdefault((ref.kind, ref.ref_id), ref)

    # 搜索结果/回执里的 page_id + 标题（真实原件里绝大多数 page_id 是这种形态）
    for m in _PAGE_ID_SLUG.finditer(text):
        pid = m.group(1)
        if "-" not in pid:
            continue
        tail = text[m.end(): m.end() + 200]
        tm = _TITLE_AFTER.search(tail)
        ref = SunkRef("wiki", pid, tm.group(1) if tm else None)
        found.setdefault((ref.kind, ref.ref_id), ref)

    for m in _SKILL_PATH.finditer(text):
        name = m.group(2)
        # SKILL.md 上一层目录名即技能名；分类目录只作上下文
        ref = SunkRef("skill", name)
        found.setdefault((ref.kind, ref.ref_id), ref)

    ordered = sorted(found.values(), key=lambda r: (r.kind != "wiki", r.ref_id))
    return ordered[:max_refs]


def render_sunk_refs(refs: list[SunkRef]) -> str:
    """渲染成提示词里的一段（空清单给明确说明，避免模型瞎猜）。"""
    if not refs:
        return "（本会话未发现已沉淀到知识库/技能库的条目）"
    return "\n".join(r.line() for r in refs)


def _tokens(text: str) -> set[str]:
    """取「显著词」：英文/数字词（≥3 字符）+ 中文二元组。

    单字符与 2 字词不参与——实测 3 字窗口会把「VPS」「流量」这种通用词命中，
    导致一条记忆挂上十几个不相干的页（宁可漏，不乱挂）。
    """
    if not text:
        return set()
    ascii_words = {w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", text)}
    zh = re.sub(r"[^\u4e00-\u9fff]", "", text)
    bigrams = {zh[i: i + 2] for i in range(len(zh) - 1)}
    return ascii_words | bigrams


def link_targets(content: str, refs: list[SunkRef], *, min_hits: int = 2) -> list[SunkRef]:
    """按「显著词重合数」挑出该挂的详情页（落库建关联用）。

    判据：标题（或技能名）与记忆正文共享 **≥ min_hits 个显著词**。
    宁可漏挂也不乱挂——乱挂会让「详情见 …」指错地方，比没有索引更坏。
    """
    if not content or not refs:
        return []
    mem_tokens = _tokens(content)
    if not mem_tokens:
        return []
    out: list[SunkRef] = []
    for ref in refs:
        key_tokens = _tokens(ref.title or ref.ref_id)
        if not key_tokens:
            continue
        if len(key_tokens & mem_tokens) >= min_hits:
            out.append(ref)
    return out
