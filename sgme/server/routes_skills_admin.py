"""server/routes_skills_admin.py：技能写侧管理端点（ST-36 M3，设计 §四 写侧治理）。

业务实现全部在 ``sgme.skills.store``（写侧编排层），本文件只做协议翻译：
- 业务拒绝（store 返回 ok=False + code）→ 按 code 映射 HTTP 状态码，
  lint_failed/duplicate/referenced/conflict → 400/409，violations/referenced_by
  清单进 ``error.details``
- 环境故障（StoreError：git 不可用/超时）→ 500 ERR_INTERNAL

端点（读侧 GET 归 routes_skills.py，另一代理并行开发；本路由在 app.py 中
先于 routes_admin 注册——同路径写端点由治理版优先接管）：
    PUT    /v1/admin/skills/{name}               写入（{meta,body} 或 {content} 自动解析）
    DELETE /v1/admin/skills/{name}?hard=&force=  删除（默认软删；入向引用未 force → 409）
    POST   /v1/admin/skills/{name}/rename        {new_name} 改名（墓碑制，永不原地改名）

兼容策略（迁移期）：``skills.source_dirs`` 未配置时回退旧 skills_hub 直写路径
（无门禁覆盖写/物理删，行为与 routes_admin 旧端点一致），既有 hub 部署零影响；
M4 wiki 迁移收敛后移除回退分支。

鉴权：全部 Depends(require_admin_key)（403 缺失/非管理员）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sgme.server.app import api_error, require_admin_key

router = APIRouter()


def _source_dirs(request: Request) -> list[str]:
    """取配置的技能源目录列表（cfg.skills.source_dirs）；未配置返回空列表。"""
    cfg = request.app.state.cfg or {}
    sec = cfg.get("skills") or {}
    return list(sec.get("source_dirs") or [])


def _registry_path(request: Request) -> str | None:
    """墓碑登记文件路径（cfg.skills.tombstone_registry 可覆盖默认）。"""
    cfg = request.app.state.cfg or {}
    return ((cfg.get("skills") or {}).get("tombstone_registry")) or None


def _parse_body(body: dict | None) -> tuple[dict, str]:
    """请求体归一：``{meta, body}`` 直用；``{content}`` 自动解析 frontmatter。

    Returns:
        (meta, body)；两种形态都不满足或正文为空 → 400。
    """
    from sgme.skills.indexer import parse_skill_md

    b = body or {}
    if isinstance(b.get("content"), str) and b["content"].strip():
        parsed = parse_skill_md(b["content"])
        meta = dict(parsed["meta"])
        # 显式传 meta 时与 content frontmatter 合并（显式字段优先）
        for k, v in (b.get("meta") or {}).items():
            meta[k] = v
        return meta, parsed["body"]
    if "meta" in b and "body" in b and str(b.get("body") or "").strip():
        return dict(b.get("meta") or {}), str(b.get("body") or "")
    raise api_error(
        "ERR_INVALID_ARGS",
        "请求体需要 {meta, body} 或 {content}（SKILL.md 全文，自动解析 frontmatter）",
    )


# ---------- 兼容回退：skills_hub 直写路径（source_dirs 未配置时） ----------


def _legacy_hub(request: Request):
    """按配置初始化 skills_hub；未启用或配置非法 → 400（对齐 routes_admin 旧语义）。"""
    from sgme.skills_hub import init as init_skills_hub

    try:
        hub = init_skills_hub(request.app.state.cfg)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", f"skills_hub 配置非法: {e}") from e
    if hub is None:
        raise api_error("ERR_INVALID_ARGS", "skills_hub 未启用（enabled=false）")
    return hub


@router.put("/v1/admin/skills/{name}")
def put_skill(
    name: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """写入技能：lint 门禁 → 三层查重 → 落盘 <source_dir>/<name>/SKILL.md → git commit。

    lint 违规 → 400 且 ``error.details.violations`` 带完整清单；
    同名同内容重复提交 / 同内容异名 → 409。
    """
    from sgme.skills.indexer import validate_name
    from sgme.skills import store as skills_store

    dirs = _source_dirs(request)

    # 兼容回退：未配置 skills.source_dirs → 旧 hub 直写（无门禁，行为同 routes_admin 旧端点）
    if not dirs:
        content = (body or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise api_error("ERR_INVALID_ARGS", "技能内容不能为空")
        hub = _legacy_hub(request)
        try:
            path = hub.put_skill(name, content)
        except ValueError as e:
            raise api_error("ERR_INVALID_ARGS", str(e)) from e
        return {"name": name, "path": str(path)}

    meta, body_text = _parse_body(body)
    # skip_limits（PR-7）：历史存量整体入库——超 8K 从拒绝降为警告；语义违规仍拒
    skip_limits = bool((body or {}).get("skip_limits"))

    # 名称白名单前置校验（路径穿越等直接 400，不进编排层）
    try:
        validate_name(name)
    except ValueError as e:
        raise api_error("ERR_INVALID_ARGS", f"非法技能名: {e}") from e

    try:
        result = skills_store.write_skill(name, meta, body_text, dirs,
                                          skip_limits=skip_limits)
    except skills_store.StoreError as e:
        raise api_error("ERR_INTERNAL", f"技能写入失败: {e.message}") from e
    if not result.get("ok"):
        if result.get("code") == "lint_failed":
            raise api_error("ERR_LINT_FAILED", "准入门禁拦截，请修正后重试",
                            {"violations": result.get("violations", [])})
        raise api_error("ERR_DUPLICATE_SKILL", "查重拒绝（同名冲突或同内容异名）",
                        {"violations": result.get("violations", [])})
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@router.delete("/v1/admin/skills/{name}")
def delete_skill(
    name: str,
    request: Request,
    hard: bool = False,
    force: bool = False,
    _: str = Depends(require_admin_key),
):
    """删除技能：默认软删（deprecated 标记+commit）；hard=true 物理删目录。

    入向引用一级信号（frontmatter uses）：有引用且未 force → 409 +
    ``error.details.referenced_by`` 清单；force=true 强制清理后删。
    二级信号（正文提及）只进 warnings 不拦。
    """
    from sgme.skills import store as skills_store

    dirs = _source_dirs(request)

    # 兼容回退：旧 hub 物理删除（幂等，返回 {"deleted": bool}，同 routes_admin 旧端点）
    if not dirs:
        hub = _legacy_hub(request)
        try:
            deleted = hub.remove_skill(name)
        except ValueError as e:
            raise api_error("ERR_INVALID_ARGS", str(e)) from e
        return {"deleted": deleted}

    try:
        result = skills_store.remove_skill(name, hard=hard, force=force, source_dirs=dirs)
    except skills_store.StoreError as e:
        raise api_error("ERR_INTERNAL", f"技能删除失败: {e.message}") from e
    if not result.get("ok"):
        if result.get("code") == "referenced":
            raise api_error("ERR_REFERENCED_BY_USES",
                            "删除被拒：存在入向 uses 引用（可加 force=true 强制删除）",
                            {"referenced_by": result.get("referenced_by", [])})
        if result.get("code") == "not_found":
            raise api_error("ERR_NOT_FOUND", f"技能不存在: {name}")
        raise api_error("ERR_INVALID_ARGS", "删除被拒",
                        {"violations": result.get("violations", [])})
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@router.post("/v1/admin/skills/{name}/rename")
def rename_skill(
    name: str,
    request: Request,
    body: dict | None = None,
    _: str = Depends(require_admin_key),
):
    """改名（墓碑制）：写新名副本 + 旧位置 superseded_by 墓碑 + tombstones.json 登记。

    Body：``new_name``（必填）。旧名不存在 → 404；新名已占用 → 409；
    新名过不了门禁 → 400 带违规清单。需 skills.source_dirs 指向 git 技能仓。
    """
    from sgme.skills import store as skills_store

    dirs = _source_dirs(request)
    if not dirs:
        raise api_error("ERR_INVALID_ARGS",
                        "改名需要 skills.source_dirs 指向 git 技能仓（墓碑制依赖 git 历史）")
    new_name = str((body or {}).get("new_name") or "").strip()
    if not new_name:
        raise api_error("ERR_INVALID_ARGS", "缺少 new_name")
    try:
        result = skills_store.rename_skill(name, new_name, dirs,
                                           registry_path=_registry_path(request))
    except skills_store.StoreError as e:
        raise api_error("ERR_INTERNAL", f"技能改名失败: {e.message}") from e
    if not result.get("ok"):
        details = {"violations": result.get("violations", [])}
        if result.get("code") == "not_found":
            raise api_error("ERR_NOT_FOUND", f"旧名不存在: {name}", details)
        if result.get("code") == "conflict":
            raise api_error("ERR_NAME_CONFLICT", f"新名已被占用或非法: {new_name}", details)
        raise api_error("ERR_LINT_FAILED", "新名未通过准入门禁",
                        {"violations": result.get("violations", [])})
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}
