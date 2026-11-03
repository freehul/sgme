"""compat.py：SGME 2.0 兼容期契约（D-58 / D-64，2026-11-04）。

两块能力：

1. **capabilities 广告**（D-58）：GET /v1/capabilities 返回
   schema_version / api_compat / catalog_version 三字段（设计要求的「增三字段」），
   外加兼容期窗口与端点处置清单——适配器启动时读它，不匹配只日志提示、不阻塞
   （单用户场景不做强制弹窗）。

2. **端点处置清单**（D-64）：
   - **豁免**：L1 捕获与语义未变的写（/v1/append、refine trigger、memory
     reject/unreject、credentials、三池、sessions 读、dream trigger、raw-fts
     rebuild、events consume、update/*）——不在清单里，照常接受；
   - **409 + upgrade_hint**：2.0 语义已变的写（agents 注册/吊销、persona、roles、
     skills 写与 sync/reindex、backup、config 写、prompts publish/activate/ab）；
   - **410 Gone**：场景层下线端点（scenes/{id}/status、scene-gc/trigger）——
     等 9.6 场景层下线批次实装，当前只有 enforce_gone 开关（默认关）。

**默认不强制**：compat.enforce / compat.enforce_gone 均为 false——现役八适配器
尚未全部声明 2.0 版本头；开启是部署期的显式决策（迁移阶段 4 切读前）。
判定口径：enforce=True 时，**未带版本头或带 X-SGME-API-Version: 1** 的请求打到
409 清单 → 拒绝；带 X-SGME-API-Version: 2 的 2.0 客户端放行。
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

from sgme import __version__
from sgme.data import db as db_mod

# 冲突端点（method, pattern, upgrade_hint）——D-64 ②
CONFLICT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/v1/admin/agents/register$"),
     "2.0 起 Agent 注册需登记角色授权字段；改用新适配器或按 /v1/capabilities 的清单升级"),
    ("DELETE", re.compile(r"^/v1/admin/agents/[^/]+$"),
     "2.0 起吊销语义接入角色授权登记；旧请求体不再受支持"),
    ("POST", re.compile(r"^/v1/admin/persona(/.*)?$"),
     "2.0 角色/人格卡结构升级为六槽位 + privacy_agents；旧请求体会写坏结构"),
    ("POST", re.compile(r"^/v1/admin/roles(?:/[^/]+)?$"),
     "2.0 角色卡结构升级为六槽位 + privacy_agents；旧请求体会写坏结构"),
    ("PUT", re.compile(r"^/v1/admin/skills/[^/]+$"),
     "2.0 技能真源从 git 换库（skill_files/skill_revisions），旧参数失效"),
    ("DELETE", re.compile(r"^/v1/admin/skills/[^/]+$"),
     "2.0 技能删除改为墓碑制并校验入向依赖，旧参数失效"),
    ("POST", re.compile(r"^/v1/admin/skills/(sync|reindex)$"),
     "2.0 技能同步改走库接口与包通道，旧参数失效"),
    ("POST", re.compile(r"^/v1/admin/backup.*$"),
     "2.0 备份新增增量保留期与 purge 语义，旧请求默认值不再等价"),
    ("PUT", re.compile(r"^/v1/admin/config(/.*)?$"),
     "2.0 配置分区与校验变化，旧请求可能被静默丢弃"),
    ("POST", re.compile(r"^/v1/admin/config(/.*)?$"),
     "2.0 配置分区与校验变化，旧请求可能被静默丢弃"),
    ("POST", re.compile(r"^/v1/admin/prompts/(publish|activate|ab)$"),
     "2.0 提示词版本化（版本号/manifest），旧参数失效"),
)

# 已下线端点（method, pattern, upgrade_hint）——D-64 ③
GONE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/v1/admin/scenes/[^/]+/status$"),
     "场景层已在 2.0 下线（D-53 scene_key 替代）；请改用记忆维度标签与 scene_key"),
    ("POST", re.compile(r"^/v1/admin/scene-gc/trigger$"),
     "场景层已在 2.0 下线（D-53 scene_key 替代）；场景 GC 不再提供"),
)


def _compat_cfg(cfg: dict | None) -> dict:
    """取兼容期配置（已由 load_config 合并默认值；这里只做缺省兜底）。"""
    base = {
        "api_family": "1.x",
        "stable_since": "",
        "window_days": 90,
        "enforce": False,
        "enforce_gone": False,
    }
    usr = (cfg or {}).get("compat")
    if isinstance(usr, dict):
        for k, v in usr.items():
            if k in base and v is not None:
                base[k] = v
    return base


def classify(method: str, path: str) -> dict | None:
    """判定请求是否命中处置清单：{"kind": "gone"|"conflict", "hint": ...} 或 None。"""
    m = (method or "").upper()
    p = path or ""
    for rm, pattern, hint in GONE_RULES:
        if rm == m and pattern.match(p):
            return {"kind": "gone", "hint": hint}
    for rm, pattern, hint in CONFLICT_RULES:
        if rm == m and pattern.match(p):
            return {"kind": "conflict", "hint": hint}
    return None


def _client_version(headers) -> str | None:
    try:
        raw = headers.get("x-sgme-api-version") or headers.get("X-SGME-API-Version")
    except Exception:  # noqa: BLE001 - Header 映射异常按未声明处理
        raw = None
    return str(raw).strip() if raw else None


def enforcement_decision(method: str, path: str, headers, cfg: dict | None) -> dict | None:
    """兼容期强制判定；返回 {"status": 409|410, "body": {...}, "hint": ...} 或 None。

    - 默认（enforce / enforce_gone 全关）→ 永远 None（零行为变更）；
    - gone：enforce_gone=True 时对全部客户端 410；
    - conflict：enforce=True 且客户端未声明 2.0（无头或 1）→ 409；
      声明 2（如 X-SGME-API-Version: 2）的客户端放行。
    """
    c = _compat_cfg(cfg)
    hit = classify(method, path)
    if hit is None:
        return None
    if hit["kind"] == "gone":
        if not bool(c.get("enforce_gone", False)):
            return None
        return {
            "status": 410,
            "hint": hit["hint"],
            "body": {
                "error": "endpoint_gone",
                "message": "该端点已在 SGME 2.0 下线",
                "upgrade_hint": hit["hint"],
                "capabilities": "/v1/capabilities",
            },
        }
    if not bool(c.get("enforce", False)):
        return None
    version = _client_version(headers)
    if version is not None and version.split(".")[0] == "2":
        return None
    return {
        "status": 409,
        "hint": hit["hint"],
        "body": {
            "error": "semantics_changed",
            "message": "该写端点的语义已在 SGME 2.0 变更，旧请求体不再受支持",
            "upgrade_hint": hit["hint"],
            "client_api_version": version or "undeclared",
            "capabilities": "/v1/capabilities",
        },
    }


def compat_phase(cfg: dict | None) -> dict:
    """兼容期窗口：stable_since + window_days → ends_at / days_left / phase。"""
    c = _compat_cfg(cfg)
    window = int(c.get("window_days") or 90)
    since_raw = str(c.get("stable_since") or "").strip()
    if not since_raw:
        return {
            "stable_since": None,
            "window_days": window,
            "ends_at": None,
            "days_left": None,
            "phase": "pre_stable",
        }
    try:
        since = date.fromisoformat(since_raw)
    except ValueError:
        return {
            "stable_since": since_raw,
            "window_days": window,
            "ends_at": None,
            "days_left": None,
            "phase": "unknown",
        }
    ends = date.fromordinal(since.toordinal() + window)
    today = datetime.now(UTC).date()
    days_left = (ends - today).days
    phase = "active" if days_left >= 0 else "expired"
    return {
        "stable_since": since.isoformat(),
        "window_days": window,
        "ends_at": ends.isoformat(),
        "days_left": days_left,
        "phase": phase,
    }


def capabilities(mem_conn, cfg: dict | None = None, skills_conn=None) -> dict:
    """GET /v1/capabilities 的响应体（只读）。"""
    catalog = "disabled"
    if skills_conn is not None:
        try:
            from sgme.operations.skill_cache import catalog_version

            catalog = catalog_version(skills_conn)
        except Exception:  # noqa: BLE001 - 广告字段失败不阻断，但如实标注
            catalog = "unavailable"
    c = _compat_cfg(cfg)
    return {
        "server": {"name": "sgme", "version": __version__},
        "schema_version": db_mod.SCHEMA_VERSION,
        "api_compat": c.get("api_family", "1.x"),
        "catalog_version": catalog,
        "compat": compat_phase(cfg),
        "enforcement": {
            "conflict": bool(c.get("enforce", False)),
            "gone": bool(c.get("enforce_gone", False)),
        },
        "endpoints": {
            "conflict": [
                {"method": m, "pattern": p.pattern, "upgrade_hint": h}
                for m, p, h in CONFLICT_RULES
            ],
            "gone": [
                {"method": m, "pattern": p.pattern, "upgrade_hint": h}
                for m, p, h in GONE_RULES
            ],
        },
        "upgrade_hint": "旧适配器请升级到声明 SGME 2.0 的版本；本次兼容期只增字段、不改语义",
    }
