"""T-422 · D-17 冷归档前置门控测试（2026-11-18）。

D-17 决策：归档前置条件 = 会话已完成 L2 提炼（归档库=只读回溯层）。
archive_raw_cold 此前只按 mtime 天数判定，未提炼的会话可能被压走——
本组测试锁死门控行为：

- require_refined=True（默认）：raw_files.status != 'refined' 一律跳过；
  状态查不到（无行 / 无连接）时保守跳过（宁可不归档）；
- require_refined=False：旧行为（只按 mtime），供手工兜底场景显式豁免。

所有测试 tmp_path 隔离，不碰生产 raw/ 与数据库。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sgme import config as sgme_config
from sgme.backup import manager
from sgme.data import db as db_mod
from sgme.data import session_dao


# ---------- fixtures ----------

@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（同 test_backup.py 口径，sessions 子目录）。"""
    rd = tmp_path / "raw" / "sessions"
    rd.mkdir(parents=True)
    monkeypatch.setattr(sgme_config, "RAW_DIR", tmp_path / "raw")
    return rd


@pytest.fixture
def session_conn(tmp_path):
    _, conn, _ = db_mod.init_databases(tmp_path / "data")  # (mem, session, wiki) 取 session
    yield conn
    try:
        conn.close()
    except Exception:
        pass


def _aged_session(raw_dir, name: str, days: int = 120) -> Path:
    """造一个 >days 天未修改的 L0 会话文件，返回其路径。"""
    f = raw_dir / f"{name}.md"
    f.write_text(f"---\nfile_id: {name}\n---\n\n# 1 old\n", encoding="utf-8")
    old = time.time() - days * 86400
    os.utime(f, (old, old))
    return f


def _register(conn, file_id: str, status: str) -> None:
    session_dao.insert_raw_file(
        conn,
        file_id=file_id,
        path=f"sessions/{file_id}.md",
        session_key=f"sess-{file_id}",
        status=status,
    )


# ---------- 门控（require_refined=True 默认） ----------

def test_unrefined_skipped(raw_dir, session_conn):
    """status='new'（未完成 L2）即使超龄也不许压走。"""
    f = _aged_session(raw_dir, "aaaaaaaa-1111")
    _register(session_conn, f.stem, "new")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=session_conn)
    assert out["archived_count"] == 0
    assert out["skipped_unrefined"] == 1
    assert f.exists()  # 原件保留


def test_refined_archived(raw_dir, session_conn):
    """status='refined' 超龄文件正常归档（压缩搬家，原件保留）。"""
    f = _aged_session(raw_dir, "bbbbbbbb-2222")
    _register(session_conn, f.stem, "refined")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=session_conn)
    assert out["archived_count"] == 1
    assert out["skipped_unrefined"] == 0
    assert f.exists()  # 原件保留（D-17 归档=压缩搬家）
    assert Path(out["files"][0]).suffix in (".zst", ".gz")


def test_missing_status_conservative_skip(raw_dir, session_conn):
    """raw_files 无该行（状态查不到）→ 保守跳过，宁可不归档。"""
    f = _aged_session(raw_dir, "cccccccc-3333")  # 不注册任何行
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=session_conn)
    assert out["archived_count"] == 0
    assert out["skipped_no_status"] == 1
    assert f.exists()


def test_no_conn_conservative_skip(raw_dir):
    """require_refined=True 但没给 session_conn → 全部保守跳过并告警。"""
    f = _aged_session(raw_dir, "dddddddd-4444")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=None)
    assert out["archived_count"] == 0
    assert out["skipped_no_status"] == 1
    assert f.exists()


def test_archived_status_skipped(raw_dir, session_conn):
    """status='archived'（已走过冷归档）不再二次压缩。"""
    f = _aged_session(raw_dir, "eeeeeeee-5555")
    _register(session_conn, f.stem, "archived")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=session_conn)
    assert out["archived_count"] == 0
    assert out["skipped_unrefined"] == 1


def test_error_status_skipped(raw_dir, session_conn):
    """status='error'（E3 熔断态）不属于已提炼，不许压走。"""
    f = _aged_session(raw_dir, "ffffffff-6666")
    _register(session_conn, f.stem, "error")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=session_conn)
    assert out["archived_count"] == 0
    assert out["skipped_unrefined"] == 1


# ---------- 旧行为豁免 ----------

def test_require_refined_false_old_behavior(raw_dir, session_conn):
    """require_refined=False 显式豁免 → 只按 mtime，未提炼也归档（手工兜底用）。"""
    f = _aged_session(raw_dir, "77777777-7777")
    _register(session_conn, f.stem, "new")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   require_refined=False, session_conn=session_conn)
    assert out["archived_count"] == 1
    assert out["skipped_unrefined"] == 0 and out["skipped_no_status"] == 0


def test_new_file_never_archived(raw_dir, session_conn):
    """未超龄文件无论什么状态都不归档（mtime 判定不回归）。"""
    f = raw_dir / "88888888-8888.md"
    f.write_text("---\nfile_id: 88888888-8888\n---\n\n# 1 new\n", encoding="utf-8")  # 新 mtime
    _register(session_conn, f.stem, "refined")
    out = manager.archive_raw_cold(raw_dir.parent, raw_dir.parent / "archive", days=90,
                                   session_conn=session_conn)
    assert out["archived_count"] == 0
    assert not (raw_dir.parent / "archive" / f.name).exists()
