"""sgme/skills/gates.py：技能准入门禁（ST-36 M3；PR-7 pattern 枚举 + scripts 收紧）。

规则（复用 audit 引擎 R1-R5 思想 + 技能特化）：
1. frontmatter 必填：description/category 非空（B116 起 version/pattern 改可选——
   仅作语义增强字段，有值才校验）；pattern 为枚举字段（auto=热集自动加载 /
   manual=按需检索），枚举外拒绝
2. 触发词窗口：triggers 每项必须出现在 description 前 57 字符内（无 triggers 跳过）
3. 原子 ≤8K：body UTF-8 编码 ≤ 8192 字节（skip_limits 时由 store 层降级为警告）
4. 名称 kebab-case：``^[a-z0-9]+(-[a-z0-9]+)*$`` 且不在 existing_names（全库唯一）
5. scripts 声明（PR-7 收紧）：仅当技能目录下 scripts/ 子目录**实际存在**时才检查
   「正文引用 ⊆ frontmatter.scripts 声明」——纯文字提及（外部路径/示例/表格）
   不再拦截，防迁移历史文档误伤；skill_dir=None 等价于目录不存在
6. uses 合法性：每项过 indexer.validate_name 且不等于自身

返回空列表 = 通过；非空 = 拒绝（清单随 400 响应返回给调用方人工修正）。
"""
from __future__ import annotations

import re

from sgme.skills.indexer import validate_name

# kebab-case 白名单（设计 §四：名称 kebab-case 全库唯一）
KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 原子大小上限（字节）
MAX_BODY_BYTES = 8192

# 触发词窗口宽度（字符）：触发词必须落在 description 前 N 个字符内
TRIGGER_WINDOW = 57

# 必填字段（2026-08-28 B116 修正：version/pattern 改可选——已上线真实技能
# 仅含 name/description/tags/category，强制 version/pattern 会使其无法经写侧重写）
REQUIRED_FIELDS = ("description", "category")

# pattern 枚举（PR-7 定稿语义：调用模式——auto=高频热集常驻自动加载 / manual=按需检索）
VALID_PATTERNS = ("auto", "manual")

# 正文引用 scripts/ 文件的形态
_SCRIPTS_REF_RE = re.compile(r"scripts/([A-Za-z0-9._\-]+)")


def lint_skill(meta, body: str, name: str, existing_names,
               skill_dir=None) -> list[str]:
    """技能准入检查：返回违规清单（空列表=通过）。

    Args:
        meta: frontmatter dict（parse_skill_md 产出；None 兜底为 {}）。
        body: 正文（不含 frontmatter 围栏）。
        name: 本次登记的技能名（= 目录名）。
        existing_names: 库内已占用技能名集合（唯一性判据）。
        skill_dir: 技能目录路径（可选）。提供且其下存在 scripts/ 子目录时，
            启用「正文引用 ⊆ 声明」一致性检查；否则跳过（文档性提及放行）。

    Returns:
        违规字符串列表；每条人可读中文，可直接进 API details.violations。
    """
    meta = meta or {}
    violations: list[str] = []

    # 1) frontmatter 必填字段非空（strip 后非空白）
    for field in REQUIRED_FIELDS:
        v = meta.get(field)
        if not isinstance(v, str) or not v.strip():
            violations.append(f"frontmatter 缺少必填字段 {field} 或为空")

    # 1b) pattern 枚举校验（有值才查枚举；缺失已由必填覆盖）
    pv = str(meta.get("pattern") or "").strip().lower()
    if pv and pv not in VALID_PATTERNS:
        violations.append(
            f"pattern 非法值 {pv!r}（仅允许: {', '.join(VALID_PATTERNS)}——"
            "auto=热集自动加载 / manual=按需检索）"
        )

    # 2) 触发词窗口：无 triggers 字段则跳过此条
    triggers = meta.get("triggers")
    if isinstance(triggers, list):
        desc = str(meta.get("description") or "")
        window = desc[:TRIGGER_WINDOW]
        for t in triggers:
            t_str = str(t).strip()
            if t_str and t_str not in window:
                violations.append(
                    f"触发词窗口违规：「{t_str}」未出现在 description 前 {TRIGGER_WINDOW} 字符内"
                )

    # 3) 原子 ≤8K（UTF-8 字节数）
    body_bytes = (body or "").encode("utf-8")
    if len(body_bytes) > MAX_BODY_BYTES:
        violations.append(
            f"原子超限：正文 {len(body_bytes)} 字节 > 上限 {MAX_BODY_BYTES}（8K）"
        )

    # 4) 名称 kebab-case + 全库唯一
    if not isinstance(name, str) or not KEBAB_RE.match(name):
        violations.append(f"名称不符合 kebab-case（^[a-z0-9]+(-[a-z0-9]+)*$）: {name!r}")
    if name in set(existing_names or ()):
        violations.append(f"名称已被占用（全库唯一性冲突）: {name!r}")

    # 5) scripts 声明（收紧版）：仅当 skill_dir/scripts/ 实际存在时才检查一致性。
    #    背景（2026-08-26 迁移预检）：385 个 wiki 历史页正文的 scripts/xxx 全是
    #    外部路径/示例/表格等文档性提及，无一真资产——按目录实体判据避免误伤。
    if skill_dir is not None:
        scripts_dir = skill_dir / "scripts"
        if scripts_dir.is_dir():
            referenced = set(_SCRIPTS_REF_RE.findall(body or ""))
            raw_scripts = meta.get("scripts")
            declared = {str(x).strip() for x in raw_scripts} if isinstance(raw_scripts, list) else set()
            missing_declare = {
                f for f in referenced
                if f != "" and (scripts_dir / f).is_file()
            } - declared
            if missing_declare:
                violations.append(
                    "scripts/ 子目录实际存在，正文引用的资产文件未在 frontmatter 声明: "
                    + ", ".join(sorted(missing_declare))
                )
            # 声明了但文件缺失 → 断链拦截
            ghost = sorted(f for f in declared if not (scripts_dir / f).is_file())
            if ghost:
                violations.append(
                    "frontmatter scripts 声明的文件在 scripts/ 目录中不存在: "
                    + ", ".join(ghost)
                )

    # 6) uses 合法性：每项过 validate_name 且不等于自身
    uses = meta.get("uses")
    if isinstance(uses, list):
        for u in uses:
            u_str = str(u).strip()
            if u_str == name:
                violations.append(f"uses 引用自身（禁止自依赖）: {u_str}")
            elif u_str:
                try:
                    validate_name(u_str)
                except ValueError as e:
                    violations.append(f"uses 含非法技能名: {u_str}（{e}）")
    return violations
