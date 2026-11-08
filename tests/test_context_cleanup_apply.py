"""tests/test_context_cleanup_apply.py：存量清理脚本（默认 dry-run / apply 归档）。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from sgme.data import db as db_mod, memory_dao


def _load_script():
    p = Path(__file__).resolve().parents[1] / "scripts" / "context_cleanup_apply.py"
    spec = importlib.util.spec_from_file_location("context_cleanup_apply", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


def _insert(conn, content: str) -> str:
    return memory_dao.insert_memory(
        conn, content=content, memory_type="fact", priority=50, time_velocity="static",
        ttl_days=None, dimension_ids=[],
    )


def test_cleanup_dry_run_then_apply(mem_conn, tmp_path):
    mod = _load_script()
    keep = _insert(mem_conn, "保留条目")
    dup1 = _insert(mem_conn, "重复一")
    dup2 = _insert(mem_conn, "重复二")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"groups": [{"keep": keep, "archive": [dup1, dup2]}]}), encoding="utf-8")

    assert mod.main(["--db", str(tmp_path / "memory.db"), "--plan", str(plan)]) == 0
    assert memory_dao.get_memory(mem_conn, dup1) is not None  # dry-run 不写
    assert mod.main(["--db", str(tmp_path / "memory.db"), "--plan", str(plan), "--apply"]) == 0
    assert memory_dao.get_memory(mem_conn, dup1) is None
    assert memory_dao.get_memory(mem_conn, dup2) is None
    assert memory_dao.get_memory(mem_conn, keep) is not None
    archived = mem_conn.execute("SELECT COUNT(*) FROM memory_archive").fetchone()[0]
    assert archived == 2
