"""profile/template.py：模板加载 + extends 继承展开 + 校验。

- load_template(mode): 加载 templates/{mode}.yaml + extends 展开 + 校验
- validate_template(t): section.dimensions ⊆ memory_types / limit 范围 / token 预算
- 校验失败抛 TemplateError（不静默降级，保留上一合法版本运行）
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from sgme import config

TEMPLATES_DIR = config.PROJECT_ROOT / "templates"

# 单条记忆 token 估算（§4 校验规则 5）
AVG_ITEM_TOKENS = 30

# 合法 match / sort 值
VALID_MATCH = {"all", "any"}
VALID_SORT = {"priority DESC", "priority ASC", "updated_at DESC", "updated_at ASC"}


class TemplateError(Exception):
    """模板加载/校验失败。"""


# ---------- 加载 ----------

def list_templates() -> list[str]:
    """列出可用模板名（TEMPLATES_DIR 下所有 *.yaml 的文件名 stem，sorted）。

    T-120 报错自解释：模板加载失败时把可用清单附进报错/提示文案，
    调用方无需猜测模板名，也无需查看服务器目录。
    """
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        # T-120：只暴露文件名不暴露绝对路径（防容器路径 /app/... 泄漏），并附可用模板清单
        raise TemplateError(
            f"模板文件不存在: {path.name}（可用: {', '.join(list_templates())}）"
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise TemplateError(f"模板格式错误（非字典）: {path}")
    return data


def _expand_extends(template: dict, mode: str) -> dict:
    """展开 extends 继承（单层）。

    - memory_types: 子覆盖（不并集）
    - sections: 按 title 同名覆盖，新 section 追加（在基础 sections 之后）
    - token_budget: 子覆盖
    - display_name: 子覆盖
    """
    extends = template.get("extends")
    if not extends:
        return template
    base_path = TEMPLATES_DIR / f"{extends}.yaml"
    base = _read_yaml(base_path)
    if base.get("extends"):
        raise TemplateError(f"继承链单层限制：{extends} 自身 extends {base.get('extends')}")

    # 合并：base 为底，子覆盖
    merged = {
        "name": template.get("name", base.get("name")),
        "display_name": template.get("display_name", base.get("display_name")),
        "memory_types": template.get("memory_types", base.get("memory_types")),
        "token_budget": template.get("token_budget", base.get("token_budget", 700)),
    }
    # sections 合并：base 在前，子按 title 覆盖或追加
    base_sections = base.get("sections", []) or []
    child_sections = template.get("sections", []) or []
    by_title: dict[str, dict] = {}
    order: list[str] = []
    for s in base_sections + child_sections:
        title = s.get("title", "")
        if title not in by_title:
            order.append(title)
        by_title[title] = s
    merged["sections"] = [by_title[t] for t in order]
    return merged


def expand_extends(template: dict, mode: str) -> dict:
    """公开包装：展开 extends 继承（单层）。

    v0.8 T-16 新增。``operations/template.py`` 在写盘前需要先展开 extends 再校验，
    而展开逻辑此前只有私有的 ``_expand_extends``。此处只做转发，
    不改变任何既有行为（``load_template`` 仍走原私有函数）。

    Args:
        template: 原始模板 dict（可能含 extends）。
        mode: 模板名（文件名，不含 .yaml）。

    Returns:
        展开后的模板 dict；无 extends 时原样返回。

    Raises:
        TemplateError: 基模板不存在或继承链超过单层。
    """
    return _expand_extends(template, mode)


def load_template(mode: str, dimensions: list[dict] | None = None) -> dict:
    """加载模板：读取 → extends 展开 → 校验。

    - mode: 模板名（文件名，不含 .yaml）
    - dimensions: 注册表维度列表（用于校验维度 id 已注册）
    - 返回展开后的完整模板 dict
    - 校验失败抛 TemplateError
    """
    path = TEMPLATES_DIR / f"{mode}.yaml"
    raw = _read_yaml(path)
    template = _expand_extends(raw, mode)
    # 确保 name 与文件名一致
    if template.get("name") != mode:
        raise TemplateError(f"模板 name={template.get('name')!r} 与文件名 {mode!r} 不一致")
    validate_template(template, dimensions)
    return template


# ---------- 校验 ----------

def validate_template(t: dict, dimensions: list[dict] | None = None) -> None:
    """校验模板（§4 规则）。

    1. name/memory_types 非空且全部为已注册维度 id
    2. section.dimensions ⊆ memory_types（越界拒绝）
    3. match ∈ {all,any}；sort 合法；limit ∈ [1,50]；priority_min ∈ [0,100]
    4. token 预算：Σ(limit) × AVG_ITEM_TOKENS ≤ token_budget
    """
    if "name" not in t:
        raise TemplateError("模板缺 name")
    memory_types = t.get("memory_types")
    if not isinstance(memory_types, list) or not memory_types:
        raise TemplateError(f"模板 {t.get('name')} memory_types 非空")

    # 已注册维度 id 集合
    registered_ids: set[str] = set()
    if dimensions:
        registered_ids = {d["id"] for d in dimensions}
    # memory_types 必须全部已注册
    for mt in memory_types:
        if registered_ids and mt not in registered_ids:
            raise TemplateError(f"memory_types 含未注册维度: {mt}")

    sections = t.get("sections")
    if not isinstance(sections, list) or not sections:
        raise TemplateError(f"模板 {t.get('name')} sections 至少 1 段")

    mt_set = set(memory_types)
    total_limit = 0
    for s in sections:
        title = s.get("title")
        if not title:
            raise TemplateError("section 缺 title")
        q = s.get("query", {})
        dims = q.get("dimensions")
        if not isinstance(dims, list) or not dims:
            raise TemplateError(f"section {title!r} dimensions 非空")
        # dimensions ⊆ memory_types
        for d in dims:
            if d not in mt_set:
                raise TemplateError(
                    f"section {title!r} 维度 {d!r} 越界（不在 memory_types 中）"
                )
            if registered_ids and d not in registered_ids:
                raise TemplateError(f"section {title!r} 维度 {d!r} 未注册")
        match = q.get("match", "all")
        if match not in VALID_MATCH:
            raise TemplateError(f"section {title!r} match 非法: {match}")
        sort = q.get("sort")
        if sort and sort not in VALID_SORT:
            raise TemplateError(f"section {title!r} sort 非法: {sort}")
        limit = q.get("limit")
        if not isinstance(limit, int) or limit < 1 or limit > 50:
            raise TemplateError(f"section {title!r} limit 必须 ∈ [1,50]，得到 {limit}")
        pm = q.get("priority_min", 0)
        if not isinstance(pm, int) or pm < 0 or pm > 100:
            raise TemplateError(f"section {title!r} priority_min 必须 ∈ [0,100]")
        tw = q.get("time_window")
        if tw:
            _parse_time_window(tw)  # 解析失败会抛
        total_limit += limit

    # token 预算
    budget = t.get("token_budget", 700)
    estimated = total_limit * AVG_ITEM_TOKENS
    if estimated > budget:
        raise TemplateError(
            f"token 预算超限：Σ(limit)×{AVG_ITEM_TOKENS}={estimated} > token_budget={budget}"
        )


# ---------- time_window 解析 ----------

_TIME_WINDOW_RE = re.compile(r"^updated_at\s*>\s*(\d+)([dhwm])$", re.IGNORECASE)


def _parse_time_window(tw: str) -> tuple[int, str]:
    """解析 time_window: 'updated_at > 30d' → (30, 'd')。

    单位：d=天, h=小时, w=周, m=月。
    """
    m = _TIME_WINDOW_RE.match(tw.strip())
    if not m:
        raise TemplateError(f"time_window 语法错误: {tw!r}（应为 'updated_at > N{{d|h|w|m}}'）")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n, unit


def time_window_to_threshold(tw: str) -> str:
    """time_window 转换为 ISO 时间戳阈值（updated_at > 阈值）。

    返回 UTC ISO 8601 时间戳。
    """
    from datetime import datetime, timedelta, timezone
    n, unit = _parse_time_window(tw)
    now = datetime.now(timezone.utc)
    if unit == "d":
        delta = timedelta(days=n)
    elif unit == "h":
        delta = timedelta(hours=n)
    elif unit == "w":
        delta = timedelta(weeks=n)
    elif unit == "m":
        # 月按 30 天近似
        delta = timedelta(days=30 * n)
    else:
        raise TemplateError(f"未知 time_window 单位: {unit}")
    threshold = now - delta
    return threshold.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 默认排序 ----------

def default_sort(dimensions: list[dict], section_dims: list[str]) -> str:
    """默认排序：section 维度全为动态 → updated_at DESC；其余 → priority DESC。"""
    dim_map = {d["id"]: d for d in dimensions}
    all_dynamic = all(
        dim_map.get(d, {}).get("time_velocity") == "dynamic"
        for d in section_dims
    ) if section_dims else False
    return "updated_at DESC" if all_dynamic else "priority DESC"
