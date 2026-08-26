"""sgme/skills/gates.py：准入门禁（ST-36 M3，设计 §四 准入规格）。

纯函数 lint_skill：输入 meta/body/name/existing_names，输出违规清单 list[str]。
规则（复用 audit 引擎 R1-R5 思想 + 技能特化）：
1. frontmatter 必填：description/version/pattern/category 非空
2. 触发词窗口：triggers 每项必须出现在 description 前 57 字符内（无 triggers 跳过）
3. 原子 ≤8K：body UTF-8 编码 ≤ 8192 字节
4. 名称 kebab-case：``^[a-z0-9]+(-[a-z0-9]+)*$`` 且不在 existing_names（全库唯一）
5. scripts 声明：正文引用 scripts/<文件名> 时 meta.scripts 必须非空且覆盖被引用文件名
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

# 必填字段
REQUIRED_FIELDS = ("description", "version", "pattern", "category")


def lint_skill(meta, body: str, name: str, existing_names) -> list[str]:
    """技能准入检查：返回违规清单（空列表=通过）。

    Args:
        meta: frontmatter dict（parse_skill_md 产出；None 兜底为 {}）。
        body: 正文（不含 frontmatter 围栏）。
        name: 本次登记的技能名（= 目录名）。
        existing_names: 库内已占用技能名集合（唯一性判据）。

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

    # 5) scripts 声明：正文引用 scripts/<file> 时 meta.scripts 必须非空且覆盖
    referenced = set(re.findall(r"scripts/([A-Za-z0-9._\-]+)", body or ""))
    scripts_meta = meta.get("scripts")
    scripts_list = [str(x).strip() for x in scripts_meta] if isinstance(scripts_meta, list) else []
    missing = sorted(referenced - set(scripts_list))
    if referenced and not scripts_list:
        violations.append(
            "正文引用了 scripts/ 资产但 frontmatter 未声明 scripts 列表: "
            + ", ".join(sorted(referenced))
        )
    elif missing:
        violations.append("frontmatter scripts 未覆盖正文引用的资产文件: " + ", ".join(missing))

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
                    violations.append(f"uses 含非法依赖名: {e}")

    return violations
