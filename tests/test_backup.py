"""T14 测试：备份恢复模块。

覆盖 checklist T14：
- create_snapshot（full / incremental）
- rotate_snapshots
- push_remote（配置 / 跳过）
- archive_raw_cold（>90 天文件压缩）
- restore（pre_backup / recovers）
- verify_integrity（ok / broken）
- 备份端点（admin ok / agent forbidden）

所有测试使用 tmp_path 隔离，不污染真实 data/ 与 raw/。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.backup import manager
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（monkeypatch sgme_config.RAW_DIR）。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def data_dir(tmp_path):
    dd = tmp_path / "data"
    dd.mkdir()
    return dd


@pytest.fixture
def backup_dir(tmp_path):
    bd = tmp_path / "backups"
    bd.mkdir()
    return bd


@pytest.fixture
def conns(data_dir, cfg):
    """初始化三库并导入 registry（v0.7：memory / session / wiki）。

    注意：restore 测试中 conn 会被 restore 关闭，cleanup 用 try/except 兜底。
    """
    mem, session, wiki = db_mod.init_databases(data_dir)
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield mem, session, wiki
    for conn in (mem, session, wiki):
        try:
            conn.close()
        except Exception:
            pass


def _set_old_mtime(path: Path, days_ago: int) -> None:
    """将文件 mtime 设为 N 天前。"""
    old_time = time.time() - days_ago * 86400
    os.utime(path, (old_time, old_time))


# ---------- create_snapshot ----------

def test_create_snapshot_full(data_dir, backup_dir, raw_dir, conns):
    """full 快照后 dest_dir 含 memory.db + session.db + wiki.db + raw/ 副本。"""
    # 准备 raw 文件
    (raw_dir / "session1.md").write_text("# test content", encoding="utf-8")

    result = manager.create_snapshot(
        data_dir=data_dir, dest_dir=backup_dir, level="full", conn_pair=conns,
    )

    assert "snapshot_id" in result
    assert result["level"] == "full"
    assert result["snapshot_id"].startswith("full_")
    assert result["path"].endswith(result["snapshot_id"])

    snap_dir = Path(result["path"])
    assert (snap_dir / "memory.db").exists()
    assert (snap_dir / "session.db").exists()
    assert (snap_dir / "wiki.db").exists()
    assert (snap_dir / "raw" / "session1.md").exists()

    # files 列表应包含 memory.db / session.db / wiki.db / raw/session1.md
    assert "memory.db" in result["files"]
    assert "session.db" in result["files"]
    assert "wiki.db" in result["files"]
    assert any("session1.md" in f for f in result["files"])

    # 快照的 memory.db 应为有效 SQLite 库（可读 schema_versions）
    import sqlite3
    chk = sqlite3.connect(str(snap_dir / "memory.db"))
    tables = [
        r[0] for r in chk.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    chk.close()
    assert "memories" in tables


def test_create_snapshot_incremental(data_dir, backup_dir, raw_dir, conns):
    """incremental 快照三库全量 + 原始层仅当日文件。"""
    # 当日文件
    (raw_dir / "today.md").write_text("today content", encoding="utf-8")
    # 旧文件（mtime 设为 30 天前）
    old_file = raw_dir / "old.md"
    old_file.write_text("old content", encoding="utf-8")
    _set_old_mtime(old_file, days_ago=30)

    result = manager.create_snapshot(
        data_dir=data_dir, dest_dir=backup_dir, level="incremental", conn_pair=conns,
    )

    snap_dir = Path(result["path"])
    # 三库全量（SQLite 无真增量）
    assert (snap_dir / "memory.db").exists()
    assert (snap_dir / "session.db").exists()
    assert (snap_dir / "wiki.db").exists()
    # 当日文件应被复制
    assert (snap_dir / "raw" / "today.md").exists()
    # 旧文件不应被复制
    assert not (snap_dir / "raw" / "old.md").exists()


# ---------- rotate_snapshots ----------

def test_rotate_snapshots(data_dir, backup_dir, raw_dir, conns):
    """创建 10 份 full 后 rotate(keep_full=7) 保留 7 份。"""
    for _ in range(10):
        manager.create_snapshot(
            data_dir=data_dir, dest_dir=backup_dir, level="full", conn_pair=conns,
        )

    result = manager.rotate_snapshots(backup_dir, keep_full=7)

    # 保留 7 份 full
    full_kept = [k for k in result["kept"] if k.startswith("full_")]
    assert len(full_kept) == 7
    # 删除 3 份
    assert len(result["removed"]) == 3
    # 删除的目录确实不存在
    for name in result["removed"]:
        assert not (backup_dir / name).exists()


# ---------- push_remote ----------

def test_push_remote_configured(tmp_path):
    """配置 remote_dir 后推送成功。"""
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "memory.db").write_text("dummy")

    remote = tmp_path / "remote"
    result = manager.push_remote(snap, remote)

    assert result["ok"] is True
    assert "remote_path" in result
    assert (remote / "snapshot" / "memory.db").exists()


def test_push_remote_skipped(tmp_path):
    """remote_dir=None 跳过不报错。"""
    snap = tmp_path / "snapshot"
    snap.mkdir()

    result = manager.push_remote(snap, None)

    assert result["ok"] is True
    assert result["skipped"] is True


# ---------- archive_raw_cold ----------

def test_archive_raw_cold(tmp_path):
    """>90 天文件被压缩。"""
    raw = tmp_path / "raw"
    raw.mkdir()
    archive = tmp_path / "archive"

    # 新文件（当日）
    (raw / "new.md").write_text("new", encoding="utf-8")
    # 旧文件（>90 天）
    old = raw / "old.md"
    old.write_text("old content", encoding="utf-8")
    _set_old_mtime(old, days_ago=100)

    result = manager.archive_raw_cold(raw, archive, days=90)

    assert result["archived_count"] == 1
    assert len(result["files"]) == 1
    # 归档文件存在
    archived_path = Path(result["files"][0])
    assert archived_path.exists()
    assert archived_path.suffix in (".zst", ".gz")
    # 原文件仍保留（仍参与溯源）
    assert old.exists()
    assert (raw / "new.md").exists()


# ---------- restore ----------

def test_restore_pre_backup(data_dir, backup_dir, raw_dir, conns):
    """restore 前出现 pre_restore_ 前缀快照。"""
    # 创建快照
    snap = manager.create_snapshot(
        data_dir=data_dir, dest_dir=backup_dir, level="full", conn_pair=conns,
    )

    # 恢复
    result = manager.restore(
        snapshot_path=snap["path"],
        data_dir=data_dir,
        raw_dir=raw_dir,
        conn_pair=conns,
    )

    assert result["pre_restore_snapshot"].startswith("pre_restore_")
    # pre_restore 快照目录存在且含三库
    pre_dir = backup_dir / result["pre_restore_snapshot"]
    assert pre_dir.exists()
    assert (pre_dir / "memory.db").exists()
    assert (pre_dir / "session.db").exists()
    assert (pre_dir / "wiki.db").exists()

    # 清理新开的 conn
    new_conns = result.get("_new_conns")
    if new_conns:
        for c in new_conns:
            try:
                c.close()
            except Exception:
                pass


def test_restore_recovers(data_dir, backup_dir, raw_dir, conns, cfg):
    """restore 后三库可读，数据回滚到快照时点。"""
    mem, session, wiki = conns

    # 插入原始数据
    memory_dao.insert_memory(
        mem,
        content="original memory",
        memory_type="persona",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["identity"],
        sources=[("file-001:1", "session")],
    )

    # 创建快照
    snap = manager.create_snapshot(
        data_dir=data_dir, dest_dir=backup_dir, level="full", conn_pair=conns,
    )

    # 快照后插入新数据
    memory_dao.insert_memory(
        mem,
        content="after snapshot",
        memory_type="persona",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["identity"],
        sources=[("file-002:1", "session")],
    )

    # 恢复
    result = manager.restore(
        snapshot_path=snap["path"],
        data_dir=data_dir,
        raw_dir=raw_dir,
        conn_pair=conns,
    )

    # 用新 conn 验证：只应有 "original memory"，不应有 "after snapshot"
    new_mem, new_session, new_wiki = result["_new_conns"]
    try:
        rows = new_mem.execute("SELECT content FROM memories").fetchall()
        contents = [r["content"] for r in rows]
        assert "original memory" in contents
        assert "after snapshot" not in contents
        # raw_files 已迁入 session.db，恢复后同样可读
        new_session.execute("SELECT COUNT(*) FROM raw_files").fetchone()
    finally:
        new_mem.close()
        new_session.close()
        new_wiki.close()


# ---------- verify_integrity ----------

def test_verify_integrity_ok(tmp_path, cfg):
    """正常溯源链校验通过。"""
    mem, session, wiki = db_mod.init_databases(tmp_path)
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])

    try:
        # 插入 raw_file
        session_dao.insert_raw_file(
            session,
            file_id="file-001",
            path="raw/session.md",
            session_key="session-1",
        )

        # 插入 memory 指向该 raw_file
        memory_dao.insert_memory(
            mem,
            content="test memory",
            memory_type="persona",
            priority=50,
            time_velocity="static",
            ttl_days=None,
            dimension_ids=["identity"],
            sources=[("file-001:1", "session")],
        )

        result = manager.verify_integrity(mem, session)

        assert result["ok"] is True
        assert result["broken_count"] == 0
        assert result["broken_samples"] == []
    finally:
        mem.close()
        session.close()
        wiki.close()


def test_verify_integrity_broken(tmp_path, cfg):
    """人为破坏后检出 broken。"""
    mem, session, wiki = db_mod.init_databases(tmp_path)
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])

    try:
        # 插入 memory 指向不存在的 raw_file（破坏溯源链）
        memory_dao.insert_memory(
            mem,
            content="orphan memory",
            memory_type="persona",
            priority=50,
            time_velocity="static",
            ttl_days=None,
            dimension_ids=["identity"],
            sources=[("nonexistent-file:1", "session")],
        )

        result = manager.verify_integrity(mem, session)

        assert result["ok"] is False
        assert result["broken_count"] >= 1
        assert len(result["broken_samples"]) >= 1
        # 样本包含 memory_id 与 source_ref
        sample = result["broken_samples"][0]
        assert "memory_id" in sample
        assert "source_ref" in sample
        assert sample["source_ref"] == "nonexistent-file:1"
    finally:
        mem.close()
        session.close()
        wiki.close()


# ---------- 备份端点 ----------

@pytest.fixture
def app(tmp_path, cfg, raw_dir):
    """创建隔离的 FastAPI 应用（tmp_path data/ + raw/ + backup 配置）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(data_dir)
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # 覆盖 backup 配置到 tmp_path（避免写入真实 data/backups）
    cfg["backup"] = {
        "dir": str(tmp_path / "backups"),
        "schedule": "0 2 * * *",
        "raw_cold_days": 90,
        "remote_dir": None,
    }
    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        data_dir=data_dir,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    for conn in (mem_conn, session_conn, wiki_conn):
        try:
            db_mod.close(conn)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _stop_backup_scheduler_after():
    """每个测试后停止 backup_scheduler 常驻线程（防跨文件连接泄漏）。"""
    yield
    from sgme.engine import backup_scheduler
    backup_scheduler.stop_scheduler(timeout=2.0)


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


def test_backup_endpoint_admin_ok(client):
    """admin key 调 /v1/admin/backup/create 返回 snapshot_id。"""
    resp = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "snapshot_id" in body
    assert body["level"] == "full"
    assert body["snapshot_id"].startswith("full_")


def test_backup_endpoint_agent_forbidden(client):
    """agent key 调 backup 端点返回 403。"""
    resp = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=AGENT_HEADERS,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"
