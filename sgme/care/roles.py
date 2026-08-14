# -*- coding: utf-8 -*-
"""sgme/care/roles.py：角色层数据结构（T-35，ST-25 地基）。

角色 = 沟通外皮（皮）；记忆池 = 芯（用户画像保持模板查询零物化，架构铁律不变）。
角色文件采用 **Character Card V2 兼容子集**（spec/data 双层结构，2026-08-13
调研定案）：可移植、生态兼容；``extensions.sgme_care`` 命名空间挂关怀策略
（问候模板/触发规则/频率档位），供 T-38 消费方读取。

角色 persona 是**唯一物化例外**（2026-08-13 用户拍板）：LLM 生成
（Persona Architect 四层深度扫描方法论），存 ``data/personas/<role_id>.md``。

铁律：
- 原件永不删：delete_role 只移入 ``.archive/``，不物理删除
- 角色卡 = 项目内文件（roles/ 目录随 git 管理，可移植交付）
- persona = 运行数据（data/personas/，不入 git）
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sgme.care.roles")

# ---------- CC V2 兼容子集字段定义 ----------

# 必填字段（CC V2 最低要求：name + description）
REQUIRED_FIELDS = ("name", "description")
# 可选标准字段（CC V2 spec 子集；character_book 为 V2 新增，extensions 任意命名空间）
OPTIONAL_FIELDS = (
    "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions", "character_book", "extensions",
)
# 合法顶层键（spec/spec_version/data）
TOP_LEVEL_KEYS = ("spec", "spec_version", "data")
# 关怀策略扩展键（extensions.sgme_care，供消费方读取）
CARE_EXT_KEYS = ("greeting_templates", "trigger_rules", "frequency")

# 角色 id 白名单：小写字母/数字/连字符/下划线（防路径穿越）
_ROLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# CC V2 固定标识
CC_SPEC = "chara_card_v2"
CC_SPEC_VERSION = "2.0"

# 存档子目录（原件永不删）
_ARCHIVE_DIR = ".archive"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _role_file(roles_dir: Path, role_id: str) -> Path:
    """角色卡文件路径（防路径穿越：id 白名单校验）。"""
    if not _ROLE_ID_RE.match(role_id):
        raise ValueError(f"非法角色 id: {role_id!r}（须匹配 {_ROLE_ID_RE.pattern}）")
    return roles_dir / f"{role_id}.json"


def validate_role_card(data: dict[str, Any]) -> list[str]:
    """校验角色卡（CC V2 兼容子集），返回错误列表；空列表 = 合法。

    规则：
    - data 必须是对象，必填 name/description 且非空字符串
    - 可选字段只允许白名单键；extensions.sgme_care 只允许关怀策略键
    - 顶层只允许 spec/spec_version/data（单文件即角色）
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["角色卡必须是 JSON 对象"]
    unknown = set(data.keys()) - set(TOP_LEVEL_KEYS)
    if unknown:
        errors.append(f"顶层多余键: {sorted(unknown)}（只允许 {TOP_LEVEL_KEYS}）")
    d = data.get("data")
    if not isinstance(d, dict):
        errors.append("data 必须是对象")
        return errors
    for f in REQUIRED_FIELDS:
        v = d.get(f)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"必填字段缺失或为空: {f}")
    for k, v in d.items():
        if k in REQUIRED_FIELDS or k in OPTIONAL_FIELDS:
            if not isinstance(v, (str, dict, list)) and v is not None:
                errors.append(f"字段类型非法: {k}")
        else:
            errors.append(f"data 多余键: {k}（可选键: {OPTIONAL_FIELDS}）")
    ext = d.get("extensions")
    if ext is not None:
        if not isinstance(ext, dict):
            errors.append("extensions 必须是对象")
        else:
            care = ext.get("sgme_care")
            if care is not None:
                if not isinstance(care, dict):
                    errors.append("extensions.sgme_care 必须是对象")
                else:
                    for k in care:
                        if k not in CARE_EXT_KEYS:
                            errors.append(
                                f"extensions.sgme_care 多余键: {k}"
                                f"（允许: {CARE_EXT_KEYS}）"
                            )
    return errors


def normalize_role_card(data: dict[str, Any]) -> dict[str, Any]:
    """规范化角色卡：补 CC V2 标识与默认空字段，返回可落盘结构。"""
    d = dict(data.get("data") or {})
    for f in OPTIONAL_FIELDS:
        d.setdefault(f, None)
    return {
        "spec": CC_SPEC,
        "spec_version": CC_SPEC_VERSION,
        "data": d,
    }


# ---------- 文件 CRUD（roles/ 目录） ----------

def ensure_roles_dir(roles_dir: Path) -> None:
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / _ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)


def list_roles(roles_dir: Path) -> list[dict[str, Any]]:
    """角色卡列表（轻量：role_id/name/description/updated_at）。"""
    ensure_roles_dir(roles_dir)
    out: list[dict[str, Any]] = []
    for f in sorted(roles_dir.glob("*.json")):
        try:
            card = json.loads(f.read_text(encoding="utf-8"))
            d = card.get("data") or {}
            out.append({
                "role_id": f.stem,
                "name": d.get("name", f.stem),
                "description": d.get("description", ""),
                "updated_at": d.get("updated_at"),
            })
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("角色卡读取失败 %s: %s", f.name, e)
            continue
    return out


def get_role(roles_dir: Path, role_id: str) -> dict[str, Any] | None:
    """单张角色卡全文；不存在返回 None。"""
    fp = _role_file(roles_dir, role_id)
    if not fp.exists():
        return None
    try:
        card = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("角色卡解析失败 %s: %s", role_id, e)
        raise ValueError(f"角色卡解析失败: {role_id}") from e
    card["role_id"] = role_id
    return card


def save_role(roles_dir: Path, role_id: str, data: dict[str, Any]) -> Path:
    """保存角色卡（幂等 upsert：已存在则更新并刷新 updated_at）。

    校验失败抛 ValueError（不落盘）。
    """
    errors = validate_role_card(data)
    if errors:
        raise ValueError("角色卡校验失败: " + "; ".join(errors))
    ensure_roles_dir(roles_dir)
    card = normalize_role_card(data)
    card["data"]["updated_at"] = _now_iso()
    if "created_at" not in card["data"]:
        card["data"]["created_at"] = card["data"]["updated_at"]
    fp = _role_file(roles_dir, role_id)
    fp.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("角色卡已保存: %s（%s）", role_id, card["data"]["name"])
    return fp


def archive_role(roles_dir: Path, role_id: str) -> bool:
    """归档角色卡：移入 .archive/（原件永不删铁律，不物理删除）。

    Returns:
        True=已归档；False=角色不存在。
    """
    ensure_roles_dir(roles_dir)
    fp = _role_file(roles_dir, role_id)
    if not fp.exists():
        return False
    dst = roles_dir / _ARCHIVE_DIR / fp.name
    fp.rename(dst)
    logger.info("角色卡已归档: %s → %s", role_id, dst)
    return True


# ---------- persona 物化（唯一物化例外） ----------

PERSONA_PROMPT = """你是 Persona Architect（人格架构师）。请基于给定的用户画像素材，
为用户生成一份「{role_name}」角色视角的沟通画像（persona.md）。角色本身的人设
（性格/语气/场景）已由角色卡定义，你要做的是让角色**知道怎么和这位用户沟通**。

四层深度扫描（TencentDB Persona Architect 方法论）：
- L1 基础锚点：用户是谁（身份/家庭/工作等事实）、可用的破冰话题
- L2 兴趣图谱：用户关心什么（项目/偏好/兴趣），按活跃度分级
- L3 交互协议：沟通习惯、雷区（用户明确不喜欢的）、称谓偏好
- L4 认知内核：决策逻辑、驱动力、价值观（判断何时该提醒/关怀）

约束（硬性）：
- 输出 ≤ {max_chars} 字符
- 禁止推测：内容只准来自给定素材，没有的不写
- 禁止编造用户没有表达过的事实
- 只输出 persona 正文（Markdown），不要解释

用户画像素材：
{profile}
"""


def render_persona_prompt(role_name: str, profile: str, max_chars: int = 2000) -> str:
    """渲染 persona 生成提示词（四层扫描）。"""
    return PERSONA_PROMPT.format(role_name=role_name, profile=profile, max_chars=max_chars)


def save_persona(role_id: str, text: str, persona_dir: Path | None = None) -> Path:
    """persona 物化文件落盘（data/personas/<role_id>.md）。"""
    pd = persona_dir or Path("data") / "personas"
    pd.mkdir(parents=True, exist_ok=True)
    # 备份旧版（保留 3 份，借鉴 TencentDB）
    fp = pd / f"{role_id}.md"
    if fp.exists():
        for i in range(3, 0, -1):
            old = pd / f"{role_id}.md.bak{i}"
            if old.exists():
                old.rename(pd / f"{role_id}.md.bak{i + 1}")
        fp.rename(pd / f"{role_id}.md.bak1")
    fp.write_text(text.strip() + "\n", encoding="utf-8")
    logger.info("persona 已物化: %s（%d 字符）", role_id, len(text))
    return fp


def load_persona(role_id: str, persona_dir: Path | None = None) -> str | None:
    """读取 persona 物化文件；不存在返回 None。"""
    pd = persona_dir or Path("data") / "personas"
    fp = pd / f"{role_id}.md"
    if not fp.exists():
        return None
    return fp.read_text(encoding="utf-8")


# ---------- 当前角色（T-40：用户选择的沟通角色，运行数据） ----------

def _active_role_file(data_dir: Path | None = None) -> Path:
    """当前角色状态文件（运行数据，不入 git）。"""
    d = data_dir or Path("data")
    return d / "care" / "active_role.json"


def get_active_role(data_dir: Path | None = None) -> str | None:
    """读取当前角色 id；未设置返回 None。"""
    fp = _active_role_file(data_dir)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8")).get("role_id")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("active_role 读取失败（按未设置处理）: %s", e)
        return None


def set_active_role(role_id: str, data_dir: Path | None = None) -> Path:
    """设置当前角色（幂等写入；角色 id 白名单校验）。"""
    if not _ROLE_ID_RE.match(role_id):
        raise ValueError(f"非法角色 id: {role_id!r}")
    fp = _active_role_file(data_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
        json.dumps({"role_id": role_id, "set_at": _now_iso()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("当前角色已设置: %s", role_id)
    return fp
