# -*- coding: utf-8 -*-
"""tests/test_migration_switches.py：D-55 阶段 2 双写灰度 / 阶段 4 切读回归。

口径（设计 §9.2）：
- migration.dual_write：enabled=false 全量关闭；agents 非空按 agent 灰度；
  关闭时新写入不落 2.0 新字段（scene_key / derivation / ...），旧列照写。
- migration.read_mode：legacy=1.x 读口径（休眠记忆仍可见、无锚点块）；
  shadow=服务 legacy 但 stats.read_mode 标 shadow；new=2.0 读口径。
- D-56：supersession 收口走 lifecycle='archived' + superseded_by（不写 status）。
"""
from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme import migration as migration_mod
from sgme.data import db
from sgme.data import memory_dao
from sgme.operations import inject as inject_ops


def test_migration_defaults_and_grayscale():
    assert migration_mod.read_mode({}) == "new"
    assert migration_mod.dual_write_enabled({}) is True
    assert migration_mod.dual_write_enabled({"migration": {"dual_write": {"enabled": False}}}) is False
    cfg = {"migration": {"dual_write": {"enabled": True, "agents": ["hermes"]}}}
    assert migration_mod.dual_write_enabled(cfg, "hermes") is True
    assert migration_mod.dual_write_enabled(cfg, "dsh") is False
    # 非法 read_mode 回落默认 new（不静默接受拼写错误）
    assert migration_mod.read_mode({"migration": {"read_mode": "wat"}}) == "new"


def test_load_config_includes_migration_section():
    cfg = sgme_config.load_config()
    assert "migration" in cfg
    assert cfg["migration"]["read_mode"] == "new"
    assert cfg["migration"]["dual_write"]["enabled"] is True


def test_dual_write_off_strips_2_0_fields(tmp_path):
    conn = db.connect_memory(tmp_path)
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry "
        "(id, display_name, category, time_velocity, created_at, priority) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z',10)"
    )
    conn.commit()
    off = memory_dao.insert_memory(
        conn, "关闭双写时写入的记忆", memory_type="fact", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-off",
        extra={"derivation": "fact", "modality": "done", "scene_key": "x:y"},
        write_2_0=False,
    )
    row = memory_dao.get_memory(conn, off)
    assert row["scene_key"] is None, "关闭双写不应落 scene_key"
    assert row["derivation"] is None, "关闭双写不应落 derivation"
    on = memory_dao.insert_memory(
        conn, "开启双写时写入的记忆", memory_type="fact", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-on",
        extra={"derivation": "fact", "modality": "done"},
    )
    row_on = memory_dao.get_memory(conn, on)
    assert row_on["derivation"] == "fact"
    assert row_on["scene_key"], "开启双写应计算 scene_key"
    conn.close()


def _seed_inject(conn):
    cfg = sgme_config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    memory_dao.insert_memory(
        conn, "活跃记忆：主人偏好短句。", memory_type="preference", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-active",
    )
    memory_dao.insert_memory(
        conn, "休眠记忆：很久以前的一条旧事。", memory_type="episodic", priority=30,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-sleeping",
    )
    conn.execute("UPDATE memories SET lifecycle='sleeping' WHERE memory_id='m-sleeping'")
    conn.commit()
    return cfg


def test_read_mode_legacy_includes_sleeping_and_new_excludes(tmp_path):
    conn = db.connect_memory(tmp_path)
    cfg = _seed_inject(conn)
    cfg["migration"] = {"read_mode": "new"}
    res_new = inject_ops.inject(conn, cfg, custom_filter={"dimensions": ["identity"]})
    assert res_new.ok and res_new.data["stats"]["read_mode"] == "new"
    cfg["migration"] = {"read_mode": "legacy"}
    res_legacy = inject_ops.inject(conn, cfg, custom_filter={"dimensions": ["identity"]})
    assert res_legacy.ok and res_legacy.data["stats"]["read_mode"] == "legacy"
    # 2.0 谓词排除休眠 → legacy 的结果条目应更多（至少 1 条）
    def total(resp):
        return sum(len(b.get("items") or []) for b in resp.data["blocks"])
    assert total(res_legacy) >= total(res_new)
    cfg["migration"] = {"read_mode": "shadow"}
    res_shadow = inject_ops.inject(conn, cfg, custom_filter={"dimensions": ["identity"]})
    assert res_shadow.ok and res_shadow.data["stats"]["read_mode"] == "shadow"
    conn.close()


def test_close_supersession_rejected_dry_run_and_apply(tmp_path):
    conn = db.connect_memory(tmp_path)
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry "
        "(id, display_name, category, time_velocity, created_at, priority) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z',10)"
    )
    conn.commit()
    for mid in ("legacy-1", "legacy-2"):
        memory_dao.insert_memory(
            conn, f"{mid} 的旧内容", memory_type="fact", priority=50,
            time_velocity="static", ttl_days=None, dimension_ids=["identity"],
            agent_tag="dsh", memory_id=mid,
        )
    memory_dao.reject_memory(
        conn, "legacy-1",
        "已被「新方案」替代（supersession 联动，替代记忆 m-new）",
    )
    memory_dao.reject_memory(conn, "legacy-2", "用户纠错：这条不对")
    dry = memory_dao.close_supersession_rejected(conn)
    assert dry["total"] == 1 and dry["with_pointer"] == 1 and dry["applied"] == 0
    assert memory_dao.get_memory(conn, "legacy-1")["status"] == "rejected"
    done = memory_dao.close_supersession_rejected(conn, apply=True)
    assert done["applied"] == 1 and not done["missing_pointer"]
    row = memory_dao.get_memory(conn, "legacy-1")
    assert row["status"] == "active"
    assert row["lifecycle"] == "archived"
    assert row["superseded_by"] == "m-new"
    # 真用户拒绝不动
    assert memory_dao.get_memory(conn, "legacy-2")["status"] == "rejected"
    conn.close()


def test_supersession_sets_archive_axis(tmp_path):
    conn = db.connect_memory(tmp_path)
    cfg = _seed_inject(conn)
    memory_dao.insert_memory(
        conn, "旧主体 AIXM 使用 Vue 3。", memory_type="fact", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-old",
    )
    old = memory_dao.get_memory(conn, "m-old")
    assert old["status"] == "active"
    assert memory_dao.mark_superseded(conn, "m-old", "m-new") is True
    row = memory_dao.get_memory(conn, "m-old")
    assert row["lifecycle"] == "archived"
    assert row["status"] == "active"
    assert row["superseded_by"] == "m-new"
    conn.close()
