# -*- coding: utf-8 -*-
"""sgme/migration.py：2.0 迁移阶段开关（D-55 / D-56 收口）。

设计 §9.2 四阶段：
- 阶段 2 双写：migration.dual_write 按 agent 灰度——关闭时新写入**不落 2.0
  新字段**（scene_key / derivation / modality / lifecycle / needs_review /
  inject_tier / meaning_anchor / ttl_renew_*），旧列照写；任何时刻可关。
- 阶段 4 切读：migration.read_mode = legacy | shadow | new
  - legacy：注入走 1.x 读口径（不做 2.0 生命周期谓词、不挂锚点块）；
  - shadow：仍服务 legacy，同时计算 2.0 结果并记录差异（不服务）；
  - new：2.0 读口径（默认）。

本模块只做**开关判定**，不碰数据；写路径与读路径各自调用。
"""
from __future__ import annotations

DEFAULT_MIGRATION_CONFIG = {
    "dual_write": {"enabled": True, "agents": []},
    "read_mode": "new",
    "shadow": {"enabled": True},
}

READ_MODES = ("legacy", "shadow", "new")
# 阶段 2 门控的 2.0 新字段（关闭双写时不写这些列）
STAGE2_FIELDS = frozenset({
    "scene_key", "derivation", "modality", "lifecycle", "needs_review",
    "inject_tier", "meaning_anchor", "ttl_renew_count", "ttl_renewed_at",
})


def migration_cfg(cfg: dict | None) -> dict:
    """取合并后的 migration 段（缺省即默认值）。"""
    base = {
        "dual_write": dict(DEFAULT_MIGRATION_CONFIG["dual_write"]),
        "read_mode": DEFAULT_MIGRATION_CONFIG["read_mode"],
        "shadow": dict(DEFAULT_MIGRATION_CONFIG["shadow"]),
    }
    usr = (cfg or {}).get("migration")
    if isinstance(usr, dict):
        dw = usr.get("dual_write")
        if isinstance(dw, dict):
            if isinstance(dw.get("enabled"), bool):
                base["dual_write"]["enabled"] = dw["enabled"]
            agents = dw.get("agents")
            if isinstance(agents, list):
                base["dual_write"]["agents"] = [str(a) for a in agents if str(a).strip()]
            elif isinstance(agents, str):
                base["dual_write"]["agents"] = [
                    a.strip() for a in agents.split(",") if a.strip()
                ]
        rm = usr.get("read_mode")
        if isinstance(rm, str) and rm.strip() in READ_MODES:
            base["read_mode"] = rm.strip()
        sh = usr.get("shadow")
        if isinstance(sh, dict) and isinstance(sh.get("enabled"), bool):
            base["shadow"]["enabled"] = sh["enabled"]
    return base


def dual_write_enabled(cfg: dict | None, agent_tag: str | None = None) -> bool:
    """阶段 2：该 agent 的新写入是否落 2.0 新字段。

    - enabled=false → False（全量关闭，回滚点 R2）；
    - agents 为空 → 全量开启；
    - agents 非空 → 仅名单内 agent 开启（灰度）。
    """
    dw = migration_cfg(cfg)["dual_write"]
    if not dw.get("enabled", True):
        return False
    agents = dw.get("agents") or []
    if not agents:
        return True
    return str(agent_tag or "") in agents


def read_mode(cfg: dict | None) -> str:
    return migration_cfg(cfg)["read_mode"]


def new_read_enabled(cfg: dict | None) -> bool:
    """阶段 4：是否以 2.0 读口径**服务**（shadow 不服务新口径）。"""
    return read_mode(cfg) == "new"


def shadow_enabled(cfg: dict | None) -> bool:
    return read_mode(cfg) == "shadow" and bool(migration_cfg(cfg)["shadow"].get("enabled", True))
