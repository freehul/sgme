"""tests/test_purged_copies_report.py：已删记忆剩余副本报告测试（临时库，全离线）。

覆盖：
- 数据源：deletion_audit(mode='purge') 取 id，排除 __backup_purge__ 哨兵、排除 logical；
- 逐 id 活库残留（memories / memory_archive / memory_vectors）与命中快照；
- --memory-id 显式过滤（不依赖审计表）；
- 快照聚合命中复用 purge_backups.scan_snapshot_db 的口径；
- 只读性：活库/快照库字节不变、不新增文件、缺失目录不创建；
- CLI --json 输出纯 JSON、文本模式关键段落齐全。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "purged_copies_report.py"


def _load_script(module_name: str, file_name: str):
    """按路径加载 scripts/ 下脚本为模块（脚本非包成员，直接 import 不可靠）。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


report_mod = _load_script("purged_copies_report_under_test", "purged_copies_report.py")


def make_live_db(db_path: Path) -> Path:
    """造活库：删除审计 + 三张待查表（含残留行）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE deletion_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          memory_id TEXT NOT NULL,
          mode TEXT NOT NULL,
          created_at TEXT
        );
        CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT);
        CREATE TABLE memory_archive (memory_id TEXT PRIMARY KEY, content TEXT);
        CREATE TABLE memory_vectors (memory_id TEXT, vec BLOB);
        """
    )
    conn.executemany(
        "INSERT INTO deletion_audit (memory_id, mode, created_at) VALUES (?, ?, ?)",
        [
            ("mem-purged-1", "purge", "2026-11-01T00:00:00Z"),
            ("mem-purged-2", "purge", "2026-11-02T00:00:00Z"),
            ("mem-logical", "logical", "2026-11-02T00:00:00Z"),
            ("__backup_purge__", "purge", "2026-11-03T00:00:00Z"),  # 哨兵，必须排除
        ],
    )
    # 模拟 D-44 清理不彻底：归档表/向量表仍有残留行
    conn.execute(
        "INSERT INTO memory_archive (memory_id, content) VALUES (?, ?)",
        ("mem-purged-1", "残留归档正文"),
    )
    conn.execute(
        "INSERT INTO memory_vectors (memory_id, vec) VALUES (?, ?)",
        ("mem-purged-2", b"\x00"),
    )
    conn.commit()
    conn.close()
    return db_path


def make_snapshot(backup_dir: Path, name: str, *,
                  memory_ids: tuple[str, ...] = (),
                  archive_ids: tuple[str, ...] = (),
                  raw_files: int = 0) -> Path:
    """造一个快照目录：<backup_dir>/<name>/memory.db（+ 可选 raw 文件）。"""
    snap_dir = backup_dir / name
    snap_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(snap_dir / "memory.db"))
    conn.executescript(
        """
        CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT);
        CREATE TABLE memory_archive (memory_id TEXT PRIMARY KEY, content TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO memories (memory_id, content) VALUES (?, ?)",
        [(mid, f"快照正文 {mid}") for mid in memory_ids],
    )
    conn.executemany(
        "INSERT INTO memory_archive (memory_id, content) VALUES (?, ?)",
        [(mid, f"快照归档 {mid}") for mid in archive_ids],
    )
    conn.commit()
    conn.close()
    if raw_files:
        raw_dir = snap_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for index in range(raw_files):
            (raw_dir / f"session-{index}.md").write_text(f"# raw {index}\n", encoding="utf-8")
    return snap_dir


def make_raw_dir(root: Path, file_count: int) -> Path:
    """造 L0 原始层：<root>/raw/sessions/*.md。"""
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for index in range(file_count):
        (sessions / f"sess-{index}.md").write_text(f"# 会话 {index}\n", encoding="utf-8")
    return root / "raw"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# 报告内容
# ---------------------------------------------------------------------------


def test_report_from_deletion_audit(tmp_path: Path):
    """默认数据源：只取 purge、排除哨兵/logical；报告活库残留与快照命中。"""
    root = tmp_path
    data_dir = root / "data"
    backup_dir = data_dir / "backups"
    make_live_db(data_dir / "memory.db")
    make_snapshot(
        backup_dir, "snap-2026-0101",
        memory_ids=("mem-purged-1", "mem-other"),
        archive_ids=("mem-purged-2",),
        raw_files=2,
    )
    make_snapshot(backup_dir, "snap-2026-0102", memory_ids=("mem-other",))
    make_raw_dir(root, 3)

    report = report_mod.build_report(data_dir, backup_dir)

    assert report["read_only"] is True
    assert report["source"] == "deletion_audit"
    assert report["memory_ids"] == ["mem-purged-1", "mem-purged-2"]

    records = {record["memory_id"]: record for record in report["records"]}
    assert set(records) == {"mem-purged-1", "mem-purged-2"}
    first = records["mem-purged-1"]
    assert first["active"] == {"memories": 0, "memory_archive": 1, "memory_vectors": 0}
    assert first["active_any"] is True
    assert first["snapshots_with_id"] == ["snap-2026-0101"]
    second = records["mem-purged-2"]
    assert second["active"]["memory_vectors"] == 1
    assert second["snapshots_with_id"] == ["snap-2026-0101"]
    # 哨兵 / logical 不得出现
    assert "__backup_purge__" not in records
    assert "mem-logical" not in records

    snapshots = {snap["name"]: snap for snap in report["snapshots"]}
    assert snapshots["snap-2026-0101"]["matched_rows"] == 2
    assert snapshots["snap-2026-0101"]["tables"] == {"memories": 1, "memory_archive": 1}
    assert snapshots["snap-2026-0101"]["ids_in_snapshot"] == [
        "mem-purged-1", "mem-purged-2",
    ]
    assert snapshots["snap-2026-0101"]["raw_files"] == 2
    assert snapshots["snap-2026-0102"]["matched_rows"] == 0
    assert snapshots["snap-2026-0102"]["ids_in_snapshot"] == []

    assert report["raw_layer"]["files"] == 3
    assert "无法按段落定位" in report["raw_layer"]["note"]

    assert report["summary"] == {
        "ids": 2,
        "ids_still_active": 2,
        "ids_in_snapshots": 2,
        "snapshots_scanned": 2,
        "snapshots_with_hits": 1,
    }
    assert report["warnings"] == []


def test_memory_id_filter_overrides_audit(tmp_path: Path):
    """--memory-id 显式指定：只报告该 id，不依赖 deletion_audit。"""
    data_dir = tmp_path / "data"
    backup_dir = data_dir / "backups"
    make_live_db(data_dir / "memory.db")
    make_snapshot(backup_dir, "snap-a", memory_ids=("mem-purged-2", "mem-manual"))

    report = report_mod.build_report(
        data_dir, backup_dir, memory_ids=["mem-manual", "mem-purged-2", "mem-manual"]
    )

    assert report["source"] == "explicit"
    assert report["memory_ids"] == ["mem-manual", "mem-purged-2"]
    assert [record["memory_id"] for record in report["records"]] == [
        "mem-manual", "mem-purged-2",
    ]
    manual = report["records"][0]
    assert manual["active"] == {"memories": 0, "memory_archive": 0, "memory_vectors": 0}
    assert manual["active_any"] is False
    assert manual["snapshots_with_id"] == ["snap-a"]


def test_missing_dirs_report_zeros_without_creating(tmp_path: Path):
    """活库/备份/raw 都不存在：给告警与 0，不创建任何目录。"""
    data_dir = tmp_path / "data-missing"
    backup_dir = tmp_path / "backups-missing"

    report = report_mod.build_report(data_dir, backup_dir, memory_ids=["mem-x"])

    assert report["records"][0]["active"] == {
        "memories": 0, "memory_archive": 0, "memory_vectors": 0,
    }
    assert report["snapshots"] == []
    assert report["raw_layer"]["files"] == 0
    assert report["summary"]["snapshots_scanned"] == 0
    assert len(report["warnings"]) >= 3
    assert not data_dir.exists()
    assert not backup_dir.exists()


# ---------------------------------------------------------------------------
# 只读性
# ---------------------------------------------------------------------------


def test_read_only_bytes_unchanged(tmp_path: Path):
    """报告前后：活库/快照库字节一致，目录内文件集合不变。"""
    data_dir = tmp_path / "data"
    backup_dir = data_dir / "backups"
    live = make_live_db(data_dir / "memory.db")
    snap_a = make_snapshot(backup_dir, "snap-a", memory_ids=("mem-purged-1",))
    snap_b = make_snapshot(backup_dir, "snap-b", archive_ids=("mem-purged-2",))
    make_raw_dir(tmp_path, 2)

    before = {
        "live": _sha256(live),
        "snap_a": _sha256(snap_a / "memory.db"),
        "snap_b": _sha256(snap_b / "memory.db"),
    }
    files_before = _tree_files(tmp_path)

    report_mod.build_report(data_dir, backup_dir)

    assert _sha256(live) == before["live"]
    assert _sha256(snap_a / "memory.db") == before["snap_a"]
    assert _sha256(snap_b / "memory.db") == before["snap_b"]
    assert _tree_files(tmp_path) == files_before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    env["SGME_HOME"] = str(tmp_path / "sgme_home")
    env["SGME_CONFIG_PATH"] = str(tmp_path / "cfg" / "sgme.yaml")
    env["SGME_LOG_OUTPUT"] = str(tmp_path / "sgme.log")
    return env


def test_cli_json_output_is_pure_json(tmp_path: Path):
    """CLI --json：stdout 只有一段 JSON；--memory-id 过滤生效；只读。"""
    data_dir = tmp_path / "data"
    backup_dir = data_dir / "backups"
    live = make_live_db(data_dir / "memory.db")
    make_snapshot(backup_dir, "snap-a", memory_ids=("mem-purged-1",))
    make_raw_dir(tmp_path, 1)
    files_before = _tree_files(tmp_path)
    live_digest = _sha256(live)

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--json",
            "--data-dir",
            str(data_dir),
            "--backup-dir",
            str(backup_dir),
            "--memory-id",
            "mem-purged-1,mem-purged-2",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_cli_env(tmp_path),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)

    assert data["read_only"] is True
    assert data["source"] == "explicit"
    assert data["memory_ids"] == ["mem-purged-1", "mem-purged-2"]
    assert data["summary"]["ids"] == 2
    assert data["records"][0]["snapshots_with_id"] == ["snap-a"]
    assert data["raw_layer"]["files"] == 1

    assert _sha256(live) == live_digest
    assert _tree_files(tmp_path) == files_before


def test_text_output_sections(tmp_path: Path, capsys):
    """文本模式：逐条副本、快照明细、raw 人工 redact 提醒齐全。"""
    data_dir = tmp_path / "data"
    backup_dir = data_dir / "backups"
    make_live_db(data_dir / "memory.db")
    make_snapshot(backup_dir, "snap-a", memory_ids=("mem-purged-1",))
    make_raw_dir(tmp_path, 2)

    assert report_mod.main(
        ["--data-dir", str(data_dir), "--backup-dir", str(backup_dir)]
    ) == 0
    out = capsys.readouterr().out
    assert "【逐条剩余副本】" in out
    assert "【快照扫描明细】" in out
    assert "【原始层提醒】" in out
    assert "无法按段落定位" in out
    assert "mem-purged-1" in out
    assert "活库仍有残留" in out
