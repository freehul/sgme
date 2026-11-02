"""sgme/operations/skill_lint.py：技能格式 lint（T-403，设计 §5.2 Anthropic 基准）。

与 ``sgme/skills/gates.py`` 的准入语义门禁互补：这里只做**格式**基准校验，
不查重、不看 pattern/uses/scripts 等 SGME 业务字段。

规则（返回逐条结构化结果，供门禁与预检端点共用）
--------------------------------------------------
1. name：仅小写字母/数字/连字符/下划线；长度 1–64；不得以连字符开头/结尾
2. description：非空且 ≤1024 字符；无「当…时 / Use when / 触发」类触发式写法
   仅记 **warn**（建议而非阻断）
3. content：总行数 <500（≥500 记 error）；必须含 YAML frontmatter（``---`` 开头）；
   正文（frontmatter 之后）非空
4. frontmatter：``name`` 与 ``description`` 必需，且与入参一致（不一致记 error）

返回项统一形态 ``{"level": "error"|"warn", "rule": ..., "message": ...}``；
``message`` 为中文，可直接回传给调用方。空列表 = 通过。
"""
from __future__ import annotations

import re

import yaml

# 名称长度上限（字符）
MAX_NAME_LEN = 64
# description 长度上限（字符）
MAX_DESCRIPTION_LEN = 1024
# 正文总行数上限（不含）：行数 >= 500 即超限
MAX_CONTENT_LINES = 500

# 名称白名单：仅小写字母、数字、连字符、下划线
_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# YAML frontmatter：文件必须以 --- 开头，闭合围栏后可跟换行或文件结束
_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

# 触发式写法（英文/中文）；同时接受「触发」「适用于」关键词
_TRIGGER_PATTERNS = (
    re.compile(r"当[^\n。！？]{0,80}?时"),
    re.compile(r"(?i)\buse\s+(?:this\s+skill\s+|it\s+)?when\b"),
    re.compile(r"(?i)\bwhen\s+to\s+use\b"),
)
_TRIGGER_KEYWORDS = ("触发", "适用于")


def _item(level: str, rule: str, message: str) -> dict:
    """构造单条 lint 结果。"""
    return {"level": level, "rule": rule, "message": message}


def _norm_ws(value) -> str:
    """空白归一（比较 frontmatter 与入参描述时避免换行/折叠差异误报）。"""
    return " ".join(str(value or "").split())


def _has_trigger_style(description: str) -> bool:
    """description 是否含触发式写法（当…时 / Use when / 触发 / 适用于）。"""
    if any(p.search(description) for p in _TRIGGER_PATTERNS):
        return True
    return any(k in description for k in _TRIGGER_KEYWORDS)


def lint_skill(
    name: str,
    description: str,
    content_md: str,
    require_frontmatter: bool = True,
) -> list[dict]:
    """按 Anthropic 基准校验一个技能的格式。

    Args:
        name: 技能名（= 目录名 / URL 中的名字）。
        description: 入参描述（通常取自 frontmatter，用于与正文交叉校验）。
        content_md: SKILL.md 全文（含 frontmatter 围栏）。
        require_frontmatter: 是否要求正文带 ``---`` 围栏。
            **写入口（用户提交 SKILL.md 原文）传 True**；
            **全库扫描传 False**——库里 ``skills.content`` 存的是入库时已剥离
            frontmatter 的正文（name/description 等拆进结构化列），
            对库内数据要求围栏必然全量误报（2026-11-02 实测：534 条全红）。

    Returns:
        违规清单；每项 ``{level, rule, message}``。空列表 = 通过。
    """
    name = name if isinstance(name, str) else ""
    description = description if isinstance(description, str) else ""
    content_md = content_md if isinstance(content_md, str) else ""

    results: list[dict] = []

    # ---------- 1. name ----------
    if not 1 <= len(name) <= MAX_NAME_LEN:
        results.append(_item(
            "error", "name_length",
            f"技能名长度必须为 1–{MAX_NAME_LEN} 字符（当前 {len(name)}）",
        ))
    if name and not _NAME_RE.match(name):
        results.append(_item(
            "error", "name_charset",
            "技能名仅允许小写字母、数字、连字符（-）与下划线（_），"
            "不能含大写字母、点、空格或其它字符",
        ))
    if name.startswith("-") or name.endswith("-"):
        results.append(_item(
            "error", "name_hyphen_edge", "技能名不得以连字符开头或结尾",
        ))

    # ---------- 2. description ----------
    if not description.strip():
        results.append(_item("error", "description_empty", "技能描述（description）不能为空"))
    else:
        if len(description) > MAX_DESCRIPTION_LEN:
            results.append(_item(
                "error", "description_length",
                f"技能描述超过 {MAX_DESCRIPTION_LEN} 字符上限（当前 {len(description)}）",
            ))
        if not _has_trigger_style(description):
            results.append(_item(
                "warn", "description_trigger",
                "描述建议使用触发式写法（如「当…时使用」/「Use when …」/「触发…」），"
                "便于 agent 判断何时调用",
            ))

    # ---------- 3. content ----------
    line_count = len(content_md.splitlines())
    if line_count >= MAX_CONTENT_LINES:
        results.append(_item(
            "error", "content_line_limit",
            f"技能正文共 {line_count} 行，超过上限（必须 <{MAX_CONTENT_LINES} 行）",
        ))

    match = _FM_RE.match(content_md)
    if match is None:
        if require_frontmatter:
            results.append(_item(
                "error", "content_frontmatter_missing",
                "缺少 YAML frontmatter：SKILL.md 必须以 --- 开头并用 --- 闭合",
            ))
            if not content_md.strip():
                results.append(_item("error", "content_empty", "技能正文不能为空"))
            return results
        # 库内数据：frontmatter 已拆成结构化列，正文就是 content 全量
        if not content_md.strip():
            results.append(_item("error", "content_empty", "技能正文不能为空"))
        return results

    body = content_md[match.end():]
    if not body.strip():
        results.append(_item("error", "content_empty", "技能正文不能为空（frontmatter 之后无内容）"))

    # ---------- 4. frontmatter 字段与入参一致 ----------
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:  # YAML 语法错误
        results.append(_item(
            "error", "content_frontmatter_invalid", f"frontmatter 不是合法 YAML: {e}"
        ))
        return results
    if not isinstance(loaded, dict):
        results.append(_item(
            "error", "content_frontmatter_invalid",
            "frontmatter 必须是 YAML 映射（key: value 形式）",
        ))
        return results

    fm_name = loaded.get("name")
    if not isinstance(fm_name, str) or not fm_name.strip():
        results.append(_item(
            "error", "frontmatter_name_missing", "frontmatter 缺少必填字段 name 或为空",
        ))
    elif name and fm_name.strip() != name.strip():
        results.append(_item(
            "error", "frontmatter_name_mismatch",
            f"frontmatter name（{fm_name.strip()}）与技能名（{name.strip()}）不一致",
        ))

    fm_desc = loaded.get("description")
    if not isinstance(fm_desc, str) or not fm_desc.strip():
        results.append(_item(
            "error", "frontmatter_description_missing",
            "frontmatter 缺少必填字段 description 或为空",
        ))
    elif description.strip() and _norm_ws(fm_desc) != _norm_ws(description):
        results.append(_item(
            "error", "frontmatter_description_mismatch",
            "frontmatter description 与入参描述不一致",
        ))

    return results


def split_results(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 level 拆分 (errors, warnings)，供写入口直接使用。"""
    errors = [r for r in results if r.get("level") == "error"]
    warnings = [r for r in results if r.get("level") == "warn"]
    return errors, warnings
