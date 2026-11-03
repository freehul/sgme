# -*- coding: utf-8 -*-
"""tests/test_memory_delete.py：D-44 / D-54 删除两态回归。

口径：
- logical（默认）：status='rejected' + reject_reason='user_delete'，数据保留，
  deletion_audit 只留事件；
- purge：必须 admin + 确认词「彻底删除」；物理清主表/归档/子表/边，审计留
  删除前快照与 content_sha256；结果带「剩余副本」清单；
- agent key 不得 purge（HTTP 403）；
- 备份边界（D-44 ③）：增量快照按天保留 30 天，full 仍按份数轮转，
  pre_restore 永不轮转，带 daily_ 前缀的增量也能被识别。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.backup import manager as backup_manager
from sgme.data import db
from sgme.data import memory_dao
from sgme.operations import memory as memory_ops

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


@pytest.fixture()
def mem_conn(tmp_path):
    conn = db.connect_memory(tmp_path)
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry "
        "(id, display_name, category, time_velocity, created_at) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    memory_dao.insert_memory(
        conn, "主人偏好短句，先给结论再说理由。",
        memory_type="preference", priority=60, time_velocity="static",
        ttl_days=None, dimension_ids=["identity"], agent_tag="dsh",
        memory_id="m-del-1",
    )
    # 归档副本 + 边（purge 应一并清掉）
    conn.execute(
        "INSERT INTO memory_archive "
        "(memory_id, content, memory_type, priority, time_velocity, created_at, "
        " updated_at, archived_at, superseded_by) "
        "VALUES ('m-del-1','旧内容','fact',50,'static','2026-10-01T00:00:00Z',"
        "'2026-10-02T00:00:00Z','2026-10-02T00:00:00Z','m-new')"
    )
    conn.execute(
        "INSERT INTO memory_edges (edge_id, from_id, to_id, relation, weight, created_at, source) "
        "VALUES ('e1','m-del-1','m-other','relates',1.0,'2026-10-02T00:00:00Z','test')"
    )
    conn.commit()
    yield conn
    conn.close()


def test_logical_delete_marks_rejected_and_audits(mem_conn):
    res = memory_ops.delete_memory(mem_conn, "m-del-1")
    assert res.ok, res
    assert res.data["mode"] == "logical" and res.data["recoverable"] is True
    row = mem_conn.execute(
        "SELECT status, reject_reason FROM memories WHERE memory_id='m-del-1'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "user_delete"
    audit = mem_conn.execute(
        "SELECT mode, triggered_by, reason, snapshot_json FROM deletion_audit "
        "WHERE memory_id='m-del-1'"
    ).fetchall()
    assert len(audit) == 1 and audit[0]["mode"] == "logical"
    assert audit[0]["snapshot_json"] is None


def test_logical_delete_is_idempotent_no_duplicate_audit(mem_conn):
    memory_ops.delete_memory(mem_conn, "m-del-1")
    res = memory_ops.delete_memory(mem_conn, "m-del-1")
    assert res.ok
    n = mem_conn.execute(
        "SELECT COUNT(*) c FROM deletion_audit WHERE memory_id='m-del-1'"
    ).fetchone()["c"]
    assert n == 1


def test_purge_requires_admin_and_confirm(mem_conn):
    res = memory_ops.delete_memory(mem_conn, "m-del-1", mode="purge")
    assert not res.ok and res.error_code == "ERR_FORBIDDEN"
    res = memory_ops.delete_memory(
        mem_conn, "m-del-1", mode="purge", admin=True, confirm_phrase="删除"
    )
    assert not res.ok and res.error_code == "ERR_INVALID_ARGS"
    # 两条都没通过 → 数据仍在
    assert mem_conn.execute(
        "SELECT 1 FROM memories WHERE memory_id='m-del-1'"
    ).fetchone() is not None


def test_purge_removes_everything_and_keeps_audit_snapshot(mem_conn):
    res = memory_ops.delete_memory(
        mem_conn, "m-del-1", mode="purge", admin=True,
        confirm_phrase="彻底删除", reason="用户明确要求彻底删除",
        agent_id="dsh",
    )
    assert res.ok, res
    assert res.data["status"] == "purged" and res.data["recoverable"] is False
    assert res.data["remaining_copies"], "必须诚实列出剩余副本"
    for table in ("memories", "memory_archive", "memory_edges"):
        n = mem_conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE "
            + ("from_id='m-del-1' OR to_id='m-del-1'" if table == "memory_edges"
               else "memory_id='m-del-1'")
        ).fetchone()["c"]
        assert n == 0, f"{table} 未清理"
    audit = mem_conn.execute(
        "SELECT mode, snapshot_json, content_sha256 FROM deletion_audit "
        "WHERE memory_id='m-del-1'"
    ).fetchall()
    assert len(audit) == 1 and audit[0]["mode"] == "purge"
    snap = json.loads(audit[0]["snapshot_json"])
    assert "短句" in snap["memory"]["content"]
    assert snap["tags"] == ["identity"]
    assert len(audit[0]["content_sha256"]) == 64


def test_invalid_mode_rejected(mem_conn):
    res = memory_ops.delete_memory(mem_conn, "m-del-1", mode="forever")
    assert not res.ok and res.error_code == "ERR_INVALID_ARGS"


# ---------- HTTP 入口：agent 只能 logical，admin 才能 purge ----------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sgme.data import memory_dao as dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw)
    cfg = sgme_config.load_config()
    cfg["skills"] = {"enabled": False, "source_dirs": []}
    mem, session, wiki = db.init_databases(tmp_path / "data")
    dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    dao.insert_memory(
        mem, "主人偏好短句，先给结论。", memory_type="preference", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        agent_tag="dsh", memory_id="m-http-1",
    )
    app = create_app(
        cfg=cfg, mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin-key", agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json", start_background_tasks=False,
    )
    yield TestClient(app)
    app.state.mem_conn.close()
    session.close()
    if wiki is not None:
        wiki.close()


def test_agent_can_logical(client):
    r = client.post("/v1/memory/m-http-1/delete", headers=AGENT_HEADERS, json={})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "logical"


def test_agent_cannot_purge(client):
    r = client.post(
        "/v1/memory/m-http-1/delete", headers=AGENT_HEADERS,
        json={"mode": "purge", "confirm": "彻底删除"},
    )
    assert r.status_code == 403, r.text
    assert "ERR_FORBIDDEN" in r.text


def test_admin_purge_end_to_end(client):
    r = client.post(
        "/v1/memory/m-http-1/delete", headers=ADMIN_HEADERS,
        json={"mode": "purge", "confirm": "彻底删除", "reason": "用户要求"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "purged"
    r2 = client.get("/v1/memory/m-http-1", headers=AGENT_HEADERS)
    assert r2.status_code == 404


# ---------- 备份边界（D-44 ③） ----------


def _mk_snap(root, name):
    d = root / name
    d.mkdir(parents=True)
    (d / "memory.db").write_bytes(b"x")
    return d


def test_rotate_incremental_by_days(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    old = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d_%H%M%S")
    recent = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d_%H%M%S")
    _mk_snap(root, f"incremental_{old}_aaaaaaaa")
    _mk_snap(root, f"daily_incremental_{old}_bbbbbbbb")
    _mk_snap(root, f"incremental_{recent}_cccccccc")
    _mk_snap(root, "pre_restore_full_20200101_000000_dddddddd")
    for i in range(9):
        _mk_snap(root, f"full_2026010{i}_030000_eeeeeeee")
    out = backup_manager.rotate_snapshots(
        root, keep_full=7, keep_incremental_days=30
    )
    names = {p.name for p in root.iterdir() if p.is_dir()}
    assert f"incremental_{old}_aaaaaaaa" not in names
    assert f"daily_incremental_{old}_bbbbbbbb" not in names
    assert f"incremental_{recent}_cccccccc" in names
    assert "pre_restore_full_20200101_000000_dddddddd" in names
    assert len([n for n in names if n.startswith("full_")]) == 7
    assert len(out["removed"]) == 4  # 2 个旧增量 + 2 个旧 full
