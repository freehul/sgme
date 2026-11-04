"""tests/test_scene_readiness.py：场景层下线就绪清单脚本测试（临时 memory.db，全离线）。

覆盖：
- 输出字段：GONE/CONFLICT 规则数、compat phase/enforce_gone、场景表行数、前置清单；
- 只读性：memory.db 字节不变、不创建配置/数据目录、不写配置文件；
- --data-dir 指向缺失库 → available=False 且不创建目录；
- CLI --json：stdout 只有一段 JSON（config 加载摘要被重定向到 stderr）。
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
SCRIPT_PATH = SCRIPTS_DIR / "scene_decommission_readiness.py"


def _load_script(module_name: str, file_name: str):
    """按路径加载 scripts/ 下脚本为模块（脚本非包成员，直接 import 不可靠）。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


readiness = _load_script("scene_readiness_under_test", "scene_decommission_readiness.py")


def make_memory_db(db_path: Path) -> Path:
    """造最小 memory.db：场景三表 + 边/页上下文表。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE scenes (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE scene_vectors (id INTEGER PRIMARY KEY, scene_id INTEGER);
        CREATE TABLE scenes_fts (rowid INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE memory_edges (source TEXT, target TEXT);
        CREATE TABLE memory_pages (kind TEXT, id INTEGER);
        """
    )
    conn.executemany(
        "INSERT INTO scenes (id, name) VALUES (?, ?)",
        [(1, "s1"), (2, "s2"), (3, "s3")],
    )
    conn.executemany(
        "INSERT INTO scene_vectors (id, scene_id) VALUES (?, ?)", [(1, 1), (2, 2)]
    )
    conn.execute("INSERT INTO scenes_fts (rowid, content) VALUES (1, 'fts')")
    conn.executemany(
        "INSERT INTO memory_edges (source, target) VALUES (?, ?)",
        [("l2", "x"), ("l2", "y"), ("scene", "z")],
    )
    conn.executemany(
        "INSERT INTO memory_pages (kind, id) VALUES (?, ?)", [("scene", 1), ("wiki", 2)]
    )
    conn.commit()
    conn.close()
    return db_path


def fake_cfg(data_dir: Path, *, stable_since: str = "2000-01-01",
             enforce_gone: bool = False) -> dict:
    """最小配置字典（与 load_config() 的 compat / paths 形状一致）。"""
    return {
        "compat": {
            "api_family": "1.x",
            "stable_since": stable_since,
            "window_days": 30,
            "enforce": False,
            "enforce_gone": enforce_gone,
        },
        "paths": {"data_dir": str(data_dir)},
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 报告字段
# ---------------------------------------------------------------------------


def test_report_fields_and_scene_rows(tmp_path: Path):
    """字段齐全：规则数、phase、场景行数、清单、建议配置。"""
    from sgme import compat

    db_path = make_memory_db(tmp_path / "memory.db")
    digest_before = _sha256(db_path)

    report = readiness.build_report(cfg=fake_cfg(tmp_path), data_dir=tmp_path)

    assert report["read_only"] is True
    assert report["config_write"] == "refused"
    assert Path(report["data_dir"]) == tmp_path

    assert report["gone_rules"]["count"] == len(compat.GONE_RULES) == 2
    assert report["conflict_rules"]["count"] == len(compat.CONFLICT_RULES)
    patterns = [rule["pattern"] for rule in report["gone_rules"]["rules"]]
    assert any("scenes" in p and "status" in p for p in patterns)
    assert any("scene-gc" in p for p in patterns)

    assert report["compat"]["enforce"] is False
    assert report["compat"]["enforce_gone"] is False
    assert report["compat"]["phase_name"] == "expired"
    assert report["compat"]["phase"]["days_left"] < 0

    scene = report["scene_data"]
    assert scene["available"] is True
    assert scene["scene_rows"] == {"scenes": 3, "scene_vectors": 2, "scenes_fts": 1}
    assert scene["total_rows"] == 6
    assert scene["clear"] is False
    assert scene["edge_sources"] == {"l2": 2, "scene": 1}
    assert scene["page_kinds"] == {"scene": 1, "wiki": 1}

    items = {item["item"]: item for item in report["prerequisites"]}
    assert items["GONE 规则已登记"]["status"] == "done"
    assert items["场景四表已冻结 / 已备份"]["status"] == "action"
    assert "6 行" in items["场景四表已冻结 / 已备份"]["detail"]
    summary = report["checklist_summary"]
    assert summary["action"] >= 1
    assert sum(summary.values()) == len(report["prerequisites"])

    assert "enforce_gone: true" in report["suggested_config"]["yaml"]
    assert report["suggested_config"]["apply"] == "manual"
    assert report["verify_steps"]

    # 只读：库字节不变，目录里没有新增文件
    assert _sha256(db_path) == digest_before
    assert {p.name for p in tmp_path.iterdir()} == {"memory.db"}


def test_phase_pre_stable_and_unknown(tmp_path: Path):
    """stable_since 空 → pre_stable；非法日期 → unknown（并进待人工动作）。"""
    make_memory_db(tmp_path / "memory.db")

    pre = readiness.build_report(
        cfg=fake_cfg(tmp_path, stable_since=""), data_dir=tmp_path
    )
    assert pre["compat"]["phase_name"] == "pre_stable"
    assert pre["compat"]["phase"]["stable_since"] is None

    unknown = readiness.build_report(
        cfg=fake_cfg(tmp_path, stable_since="not-a-date"), data_dir=tmp_path
    )
    assert unknown["compat"]["phase_name"] == "unknown"
    phase_item = [
        item for item in unknown["prerequisites"] if item["item"] == "兼容期阶段可判定"
    ][0]
    assert phase_item["status"] == "action"
    assert "YYYY-MM-DD" in phase_item["action"]


def test_enforce_gone_already_true_is_reported(tmp_path: Path):
    """enforce_gone 已为 true：原样报告，验证项转为现状说明（info）。"""
    make_memory_db(tmp_path / "memory.db")

    report = readiness.build_report(
        cfg=fake_cfg(tmp_path, enforce_gone=True), data_dir=tmp_path
    )
    assert report["compat"]["enforce_gone"] is True
    items = {item["item"]: item["status"] for item in report["prerequisites"]}
    assert items["开启后 410 验证"] == "info"


def test_missing_db_reports_unavailable_without_creating_dir(tmp_path: Path):
    """库缺失：available=False、total_rows=None，且脚本不创建目录/库。"""
    data_dir = tmp_path / "no-such-data-dir"

    report = readiness.build_report(cfg=fake_cfg(data_dir), data_dir=data_dir)

    assert report["scene_data"]["available"] is False
    assert report["scene_data"]["total_rows"] is None
    assert report["scene_data"]["clear"] is None
    assert report["scene_data"]["tables"] == {}
    assert not data_dir.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_text_output_contains_checklist(tmp_path: Path, monkeypatch, capsys):
    """文本模式：打印前置清单与拒绝代改声明（配置读取用替身，避免碰真实配置）。"""
    make_memory_db(tmp_path / "memory.db")
    monkeypatch.setattr(readiness, "_load_config", lambda: fake_cfg(tmp_path))

    assert readiness.main(["--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "【开启 enforce_gone 前置清单】" in out
    assert "【建议配置片段（只打印，不代改）】" in out
    assert "拒绝代改" in out
    assert "enforce_gone: true" in out


def test_cli_json_output_and_readonly(tmp_path: Path):
    """CLI --json：stdout 只有一段可解析 JSON；不写配置、不改库。"""
    db_path = make_memory_db(tmp_path / "memory.db")
    digest_before = _sha256(db_path)

    home = tmp_path / "sgme_home"
    cfg_path = tmp_path / "cfg" / "sgme.yaml"  # 不存在 → load_config 用默认值
    env = os.environ.copy()
    env["SGME_HOME"] = str(home)
    env["SGME_CONFIG_PATH"] = str(cfg_path)
    env["SGME_LOG_OUTPUT"] = str(tmp_path / "sgme.log")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json", "--data-dir", str(tmp_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)  # stdout 必须是纯 JSON（加载摘要在 stderr）

    assert data["read_only"] is True
    assert data["config_write"] == "refused"
    assert Path(data["data_dir"]) == tmp_path
    assert data["gone_rules"]["count"] == 2
    assert data["scene_data"]["scene_rows"]["scenes"] == 3
    assert data["scene_data"]["total_rows"] == 6
    assert "enforce_gone: true" in data["suggested_config"]["yaml"]

    # 只读：库字节不变；配置文件目录未被创建
    assert _sha256(db_path) == digest_before
    assert not cfg_path.exists()
