"""operations/template.py：模板管理操作（v0.8 §5.8 / T-16）。

职责：`templates/*.yaml` 的读 / 写 / 校验编排，供 Admin 端点（require_admin_key）调用。
形状照 ``operations/health.py`` 样板：模块内私有工具 + ``xxx(...) -> OperationResult``
操作函数；**不认识任何协议**（不 import fastapi，不知道 HTTP 状态码），
错误码 → 状态码由入口层 ``server/app.py::ERROR_CODES`` 映射。

四个操作对应契约 §5.8：

- ``list_templates(dimensions, limit, offset)``  → GET    /v1/admin/templates
- ``update_template(name, payload, dimensions)`` → PUT    /v1/admin/templates/{name}
- ``create_template(name, payload, dimensions)`` → POST   /v1/admin/templates
- ``delete_template(name)``                      → DELETE /v1/admin/templates/{name}

设计决策（实现时定，契约未明示处均在此登记）
--------------------------------------------------
1. **restart_required 恒为 False**（``RESTART_REQUIRED`` 常量）。
   实测依据：``profile/template.py::load_template`` 每次调用都 ``_read_yaml`` 打开文件，
   模块内无 lru_cache / 无进程级模板字典；调用方 ``operations/inject.py::inject``
   也是每请求现调 ``load_template(mode, dimensions)``。故写盘即热加载生效。
   探针验证：写 A → load → 改 B → load，第二次读到 B。
2. **写入源优先级：``content`` > 结构化字段**。GET 的 items 同时返回结构化字段与
   ``content``（原始 YAML 全文），回填时两者都会带回来，必须定优先级。取 ``content``
   为准——它是编辑器的主编辑面（契约注「原始 YAML 全文，编辑回填用」），
   且原文写盘可保留用户注释与排版。``content`` 缺省/空串时才用结构化字段拼 YAML。
3. **PUT 语义为 upsert**（目标不存在则新建）。契约 §5.8.2 只规定了校验失败 400，
   未规定「不存在 → 404」；POST 才是带 409 冲突检测的「新建」。贸然加 404
   可能打断 SCSM 既有调用，故 PUT 不做存在性检查。
4. **sections 双形态兼容**。磁盘 YAML 用嵌套 ``query``；契约 §5.8.1 示例里 section 是
   扁平的（title/dimensions/limit/sort）。GET 两者都给（扁平键 + 保真的 ``query``
   子对象），写入时两种都认（有 ``query`` 用 ``query``，否则收集扁平键）。
5. **列表接口对坏模板容错**。单个 YAML 语法错/校验不过不得让整个列表 500——
   否则编辑器无法拉取并修复坏文件。坏条目照常返回，附加 ``valid``/``error`` 字段标注。
6. **行尾保留**。仓库 ``core.autocrlf=true``，工作区内 ``templates/*.yaml`` 是 CRLF。
   回写沿用目标文件既有行尾（新文件取同目录既有模板的行尾），避免整文件 git diff。

约束（契约 §5.8「零改动确认」）：写操作只落 ``templates/*.yaml``，不入 DB、不动 DDL、
不新增依赖包（yaml 已是既有依赖）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from sgme.operations.errors import (
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    InvalidArgs,
    OperationResult,
)
from sgme.profile import template as template_mod

# ---------- 常量 ----------

# 冲突错误码：operations/errors.py 未定义（该文件本次不在授权修改清单内），
# 故在本模块内声明。取值必须与 server/app.py::ERROR_CODES 的键一致 → HTTP 409。
ERR_CONFLICT = "ERR_CONFLICT"

# 内置模板（契约 §5.8.4：拒绝删除）。与 templates/ 目录下随仓库分发的四个文件一致。
BUILTIN_TEMPLATES: frozenset[str] = frozenset({"daily", "coding", "work", "full"})

# 见模块 docstring 决策 1：load_template 每请求读盘，写盘即生效，无需重启。
RESTART_REQUIRED: bool = False

# 分页默认值（契约 §5.8.1：limit 默认 50 / offset 默认 0，对齐 SCSM list_templates）
DEFAULT_LIMIT: int = 50
# limit 上限：超出按上限截断（不报错——SCSM 传大 limit 时不应 400）
MAX_LIMIT: int = 200

# 模板名白名单：同时承担**路径穿越防护**（不含 . / \ 等分隔符，天然无法越出 templates/）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 落盘 YAML 的规范键序（yaml.safe_dump 用 sort_keys=False 保序）
_TEMPLATE_KEYS: tuple[str, ...] = (
    "name",
    "display_name",
    "extends",
    "memory_types",
    "token_budget",
    "sections",
)

# section.query 的已知键（扁平形态 → 嵌套 query 的收集范围）
_QUERY_KEYS: tuple[str, ...] = (
    "dimensions",
    "match",
    "sort",
    "limit",
    "priority_min",
    "time_window",
    "ttl_filter",
)


# ---------- 私有工具 ----------

def _now_iso() -> str:
    """UTC ISO8601 时间戳（与 server/app.py::_now_iso 同格式，避免 operations 反向依赖入口层）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def templates_dir() -> Path:
    """模板目录：**每次调用现读** ``profile/template.py::TEMPLATES_DIR``。

    刻意不在模块导入时快照——测试通过 monkeypatch 该常量把目录指向 tmp_path 实现隔离，
    若此处缓存则隔离失效（会写到真实 templates/）。
    """
    return Path(template_mod.TEMPLATES_DIR)


def _template_path(name: str) -> Path:
    """模板文件路径 ``templates/{name}.yaml``（name 须已过 validate_name）。"""
    return templates_dir() / f"{name}.yaml"


def validate_name(name: str) -> str:
    """校验并规范化模板名。

    白名单式校验，同时是路径穿越防护：``..``、``/``、``\\``、绝对路径一律不匹配。

    Args:
        name: 待校验模板名（路径参数或 body 的 name）。

    Returns:
        去首尾空白后的模板名。

    Raises:
        InvalidArgs: 名称为空或含非法字符 → 入口层映射 400 ERR_INVALID_ARGS。
    """
    if not isinstance(name, str) or not name.strip():
        raise InvalidArgs("模板名不能为空")
    n = name.strip()
    if not _NAME_RE.match(n):
        raise InvalidArgs(
            f"模板名非法: {name!r}"
            "（只允许字母/数字/下划线/连字符，首字符为字母或数字，长度 ≤64，且不含路径分隔符）"
        )
    return n


def _read_raw_text(path: Path) -> str:
    """读取 YAML 原文（通用换行模式：磁盘 CRLF 在返回值里统一为 \\n）。"""
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def _parse_yaml_text(text: str, *, source: str) -> dict[str, Any]:
    """解析 YAML 文本为字典。

    Args:
        text: YAML 全文。
        source: 出错文案里的来源标识（如 "content"）。

    Returns:
        解析结果字典。

    Raises:
        InvalidArgs: 语法错误 / 内容为空 / 顶层非字典。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise InvalidArgs(f"YAML 解析失败（{source}）: {e}") from e
    if data is None:
        raise InvalidArgs(f"YAML 内容为空（{source}）")
    if not isinstance(data, dict):
        raise InvalidArgs(
            f"YAML 顶层必须是字典（{source}），得到 {type(data).__name__}"
        )
    return data


def _detect_newline(path: Path) -> str:
    """探测回写应使用的行尾。

    优先级：目标文件既有行尾 → 同目录其它模板的行尾 → ``"\\n"``。
    目的：仓库 core.autocrlf=true 时工作区模板是 CRLF，回写若换成 LF 会产生整文件 diff。
    """
    candidates: list[Path] = []
    if path.exists():
        candidates.append(path)
    else:
        parent = path.parent
        if parent.is_dir():
            candidates.extend(sorted(p for p in parent.glob("*.yaml") if p.is_file()))
    for c in candidates:
        try:
            data = c.read_bytes()
        except OSError:
            continue
        return "\r\n" if b"\r\n" in data else "\n"
    return "\n"


def _normalize_newlines(text: str) -> str:
    """把任意行尾统一成 \\n，并保证以换行结尾。

    必要性：写文件时用 ``newline=`` 做行尾翻译，若文本里已含 \\r\\n 会被翻成 \\r\\r\\n。
    HTTP body 里的 content 常带 CRLF，故落盘前必须先归一。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _atomic_write(path: Path, text: str) -> None:
    """原子写模板文件：临时文件写完再 ``os.replace``，杜绝读到半写内容。

    做法与 ``sgme/prompts/manager.py::publish`` 一致（项目既有原子写范式）。
    临时文件与目标同目录（保证 rename 是同卷原子操作），失败时清理残留。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    newline = _detect_newline(path)
    tmp = path.parent / f".{path.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline=newline) as f:
            f.write(_normalize_newlines(text))
        os.replace(tmp, path)
    except OSError:
        # 清理半写临时文件；清理失败不掩盖原始异常
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _dump_yaml(template: dict[str, Any]) -> str:
    """结构化模板 → YAML 文本（键序保留，中文不转义）。"""
    return yaml.safe_dump(
        template,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _section_query(section: dict[str, Any]) -> dict[str, Any]:
    """取 section 的查询条件：优先嵌套 ``query``，否则把扁平键收集起来。"""
    q = section.get("query")
    if isinstance(q, dict):
        return dict(q)
    return {k: section[k] for k in _QUERY_KEYS if k in section}


def _section_view(section: Any) -> dict[str, Any]:
    """GET items[].sections 的单段视图：契约扁平键 + 保真的 ``query`` 子对象。

    扁平键满足契约 §5.8.1 示例形态（title/dimensions/limit/sort）；``query`` 保留
    磁盘原样，保证「GET 取出 → 改 → PUT 存回」不丢字段（如 ttl_filter/time_window）。
    """
    if not isinstance(section, dict):
        return {
            "title": None,
            "dimensions": [],
            "match": "all",
            "sort": None,
            "limit": None,
            "priority_min": None,
            "time_window": None,
            "ttl_filter": None,
            "query": {},
        }
    q = _section_query(section)
    return {
        "title": section.get("title"),
        "dimensions": list(q.get("dimensions") or []),
        "match": q.get("match", "all"),
        "sort": q.get("sort"),
        "limit": q.get("limit"),
        "priority_min": q.get("priority_min"),
        "time_window": q.get("time_window"),
        "ttl_filter": q.get("ttl_filter"),
        "query": q,
    }


def _normalize_section(section: Any, idx: int) -> dict[str, Any]:
    """写入方向的 section 规范化：任意形态 → ``{"title": ..., "query": {...}}``。"""
    if not isinstance(section, dict):
        raise InvalidArgs(f"sections[{idx}] 必须是字典，得到 {type(section).__name__}")
    return {"title": section.get("title"), "query": _section_query(section)}


def _sections_are_nested(data: dict[str, Any]) -> bool:
    """判断 sections 是否已是磁盘规范形态（每段都有 dict 型 ``query``）。

    用于决定「content 原文写盘」是否安全：若用户提交的 YAML 用了扁平 section，
    原文写盘后 ``load_template`` 会读不到 query → 必须改走规范化重排版。
    """
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return False
    return all(isinstance(s, dict) and isinstance(s.get("query"), dict) for s in sections)


def _normalize_template(data: dict[str, Any], name: str) -> dict[str, Any]:
    """规范化模板结构（键序、section 形态），并补齐 name。

    未知顶层键原样保留（向前兼容将来扩展字段），排在规范键之后；``content``
    是传输层字段不落盘，此处剔除。
    """
    out: dict[str, Any] = {"name": data.get("name", name)}
    for k in _TEMPLATE_KEYS:
        if k == "name":
            continue
        if k in data and data[k] is not None:
            out[k] = data[k]
    for k, v in data.items():
        if k in out or k == "content":
            continue
        out[k] = v
    sections = out.get("sections")
    if isinstance(sections, list):
        out["sections"] = [_normalize_section(s, i) for i, s in enumerate(sections)]
    return out


def _expanded_for_validation(template: dict[str, Any], name: str) -> dict[str, Any]:
    """extends 展开（若有），供校验使用。展开失败按参数非法处理。"""
    if not template.get("extends"):
        return template
    try:
        return template_mod.expand_extends(template, name)
    except template_mod.TemplateError as e:
        raise InvalidArgs(f"模板校验失败: {e}") from e


def _validate_or_raise(template: dict[str, Any], name: str, dimensions: list[dict] | None) -> None:
    """复用 ``profile/template.py::validate_template`` 做业务校验。

    覆盖契约 §5.8.2 三条：维度已注册 / sections.dimensions ⊆ memory_types /
    Σ(limit)×AVG_ITEM_TOKENS ≤ token_budget（另含 match/sort/limit/priority_min 范围）。

    Raises:
        InvalidArgs: 校验不过，message 带 TemplateError 的详细原因。
    """
    expanded = _expanded_for_validation(template, name)
    try:
        template_mod.validate_template(expanded, dimensions)
    except template_mod.TemplateError as e:
        raise InvalidArgs(f"模板校验失败: {e}") from e


def _resolve_write_payload(
    payload: Any,
    name: str,
) -> tuple[dict[str, Any], str | None]:
    """解析写请求体 → (结构化数据, 原文 YAML 或 None)。

    见模块 docstring 决策 2：``content`` 优先。返回的第二项非 None 时表示
    「按用户原文写盘」（保留注释排版）；None 表示按结构化字段重新 dump。

    Raises:
        InvalidArgs: 请求体非对象 / 为空 / content YAML 非法 / name 与路径不一致。
    """
    if not isinstance(payload, dict):
        raise InvalidArgs(f"请求体必须是 JSON 对象，得到 {type(payload).__name__}")

    content = payload.get("content")
    raw_text: str | None = None
    if isinstance(content, str) and content.strip():
        data = _parse_yaml_text(content, source="content")
        raw_text = content
    else:
        data = {k: v for k, v in payload.items() if k != "content"}
        if not data:
            raise InvalidArgs("请求体为空：需提供 content（YAML 全文）或结构化模板字段")

    body_name = data.get("name")
    if body_name is not None and str(body_name) != name:
        raise InvalidArgs(f"模板 name={body_name!r} 与路径 {name!r} 不一致")

    # 扁平 section 的原文不能直接写盘（load_template 读不到 query）→ 退回规范化重排
    if raw_text is not None and not _sections_are_nested(data):
        raw_text = None

    return data, raw_text


def _write_template(
    name: str,
    payload: Any,
    dimensions: list[dict] | None,
) -> None:
    """校验 + 原子写盘（create/update 共用主体）。"""
    data, raw_text = _resolve_write_payload(payload, name)
    template = _normalize_template(data, name)
    _validate_or_raise(template, name, dimensions)
    text = raw_text if raw_text is not None else _dump_yaml(template)
    _atomic_write(_template_path(name), text)


# ---------- 操作：GET /v1/admin/templates ----------

def list_templates(
    dimensions: list[dict] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> OperationResult:
    """列出全部模板（契约 §5.8.1）。

    纯只读、幂等。按 name 字典序排列（确定性分页的前提）。
    单个模板损坏不影响整体：该条目 ``valid=false`` 并带 ``error``，其余照常返回。

    Args:
        dimensions: 已注册维度列表（用于给每条模板算 valid 标记）；None 时跳过维度注册校验。
        limit: 每页条数，默认 50，超过 MAX_LIMIT 按 MAX_LIMIT 截断。
        offset: 偏移量，默认 0。

    Returns:
        OperationResult(ok=True)，data 形如
        ``{"items": [...], "count": int, "total": int, "generated_at": iso}``。
        每个 item 含 name / display_name / memory_types / token_budget /
        sections / content（原始 YAML 全文）+ 附加的 builtin / valid / error。

    Raises:
        InvalidArgs: limit < 1 或 offset < 0 → 400 ERR_INVALID_ARGS。
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise InvalidArgs(f"limit 必须是 ≥1 的整数，得到 {limit!r}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidArgs(f"offset 必须是 ≥0 的整数，得到 {offset!r}")
    limit = min(limit, MAX_LIMIT)

    d = templates_dir()
    files: list[Path] = []
    if d.is_dir():
        files = sorted(
            (p for p in d.glob("*.yaml") if p.is_file() and not p.name.startswith(".")),
            key=lambda p: p.stem,
        )
    total = len(files)
    page = files[offset: offset + limit]

    items: list[dict[str, Any]] = []
    for path in page:
        stem = path.stem
        item: dict[str, Any] = {
            "name": stem,
            "display_name": None,
            "memory_types": [],
            "token_budget": None,
            "sections": [],
            "content": "",
            "builtin": stem in BUILTIN_TEMPLATES,
            "valid": False,
            "error": None,
        }
        try:
            item["content"] = _read_raw_text(path)
        except OSError as e:
            item["error"] = f"读取失败: {e}"
            items.append(item)
            continue
        try:
            data = yaml.safe_load(item["content"])
        except yaml.YAMLError as e:
            item["error"] = f"YAML 解析失败: {e}"
            items.append(item)
            continue
        if not isinstance(data, dict):
            item["error"] = "YAML 顶层必须是字典"
            items.append(item)
            continue

        view = data
        if data.get("extends"):
            try:
                view = template_mod.expand_extends(data, stem)
            except template_mod.TemplateError as e:
                item["error"] = f"extends 展开失败: {e}"
                items.append(item)
                continue

        item["display_name"] = view.get("display_name")
        item["memory_types"] = list(view.get("memory_types") or [])
        item["token_budget"] = view.get("token_budget")
        raw_sections = view.get("sections")
        item["sections"] = (
            [_section_view(s) for s in raw_sections] if isinstance(raw_sections, list) else []
        )
        try:
            template_mod.validate_template(view, dimensions)
            if view.get("name") != stem:
                raise template_mod.TemplateError(
                    f"模板 name={view.get('name')!r} 与文件名 {stem!r} 不一致"
                )
            item["valid"] = True
        except template_mod.TemplateError as e:
            item["error"] = str(e)
        items.append(item)

    return OperationResult.succeed({
        "items": items,
        "count": len(items),
        "total": total,
        "generated_at": _now_iso(),
    })


# ---------- 操作：PUT /v1/admin/templates/{name} ----------

def update_template(
    name: str,
    payload: Any,
    dimensions: list[dict] | None = None,
) -> OperationResult:
    """更新模板（契约 §5.8.2）。

    语义为 upsert——契约未规定「不存在 → 404」，见模块 docstring 决策 3。

    Args:
        name: 路径中的模板名；body 若带 name 必须与之一致。
        payload: 完整模板 JSON（``content`` 原文优先，否则用结构化字段）。
        dimensions: 已注册维度列表，透传给 validate_template。

    Returns:
        OperationResult(ok=True)，data = ``{"saved": True, "name": str,
        "restart_required": False}``（name 为附加字段，契约要求的两字段齐备）。

    Raises:
        InvalidArgs: 名称非法 / YAML 非法 / 模板校验不过 → 400 ERR_INVALID_ARGS。
    """
    n = validate_name(name)
    _write_template(n, payload, dimensions)
    return OperationResult.succeed({
        "saved": True,
        "name": n,
        "restart_required": RESTART_REQUIRED,
    })


# ---------- 操作：POST /v1/admin/templates ----------

def create_template(
    name: str | None,
    payload: Any,
    dimensions: list[dict] | None = None,
) -> OperationResult:
    """新建模板（契约 §5.8.3）。

    与 update 的唯一差异是**重名检测**：目标文件已存在 → 409 ERR_CONFLICT。

    Args:
        name: 模板名。POST 无路径参数，None 时从 body 的 name 字段取
            （body 用 content 时从 YAML 解析出的 name 取）。
        payload: 完整模板 JSON。
        dimensions: 已注册维度列表。

    Returns:
        OperationResult(ok=True)，data = ``{"created": True, "name": str,
        "restart_required": False}``。
        重名时 OperationResult(ok=False, error_code=ERR_CONFLICT) → 入口层 409。

    Raises:
        InvalidArgs: 名称缺失/非法、YAML 非法、模板校验不过 → 400。
    """
    resolved = name
    if resolved is None:
        resolved = _name_from_payload(payload)
    n = validate_name(resolved)

    if _template_path(n).exists():
        return OperationResult.fail(ERR_CONFLICT, f"模板已存在: {n}")

    _write_template(n, payload, dimensions)
    return OperationResult.succeed({
        "created": True,
        "name": n,
        "restart_required": RESTART_REQUIRED,
    })


def _name_from_payload(payload: Any) -> str:
    """POST 无路径参数时，从 body 推断模板名（结构化 name 或 content 里的 name）。"""
    if not isinstance(payload, dict):
        raise InvalidArgs(f"请求体必须是 JSON 对象，得到 {type(payload).__name__}")
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        return name
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        data = _parse_yaml_text(content, source="content")
        inner = data.get("name")
        if isinstance(inner, str) and inner.strip():
            return inner
    raise InvalidArgs("缺少模板名：请求体需含 name 字段（或 content YAML 内含 name）")


# ---------- 操作：DELETE /v1/admin/templates/{name} ----------

def delete_template(name: str) -> OperationResult:
    """删除模板（契约 §5.8.4）。

    判定顺序按契约行文：**先内置拦截，再存在性检查**——即 DELETE 一个内置模板
    始终得 400「内置模板不可删」，不会因文件恰好缺失而变成 404。

    Args:
        name: 模板名。

    Returns:
        OperationResult(ok=True)，data = ``{"deleted": True, "name": str,
        "restart_required": False}``。
        不存在时 ok=False + ERR_NOT_FOUND → 404。

    Raises:
        InvalidArgs: 名称非法 / 内置模板 → 400 ERR_INVALID_ARGS。
    """
    n = validate_name(name)
    if n in BUILTIN_TEMPLATES:
        raise InvalidArgs(f"内置模板不可删: {n}")

    path = _template_path(n)
    if not path.exists():
        return OperationResult.fail(ERR_NOT_FOUND, f"模板不存在: {n}")

    try:
        path.unlink()
    except OSError as e:
        # 文件系统层失败（占用/权限）属内部错误，非调用方参数问题 → 500
        return OperationResult.fail(ERR_INTERNAL, f"删除失败: {e}")

    return OperationResult.succeed({
        "deleted": True,
        "name": n,
        "restart_required": RESTART_REQUIRED,
    })
