# -*- coding: utf-8 -*-
"""tests/test_upgrade_dsh_host.py：宿主升级脚本的纯函数/dry-run 回归。"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import upgrade_dsh_host as up


def _bridge(tmp: Path, version: str = "9.9.9") -> Path:
    b = tmp / "bridge"
    b.mkdir()
    (b / "package.json").write_text(
        json.dumps({"name": "dsh-sgme", "version": version}), encoding="utf-8"
    )
    return b


def test_store_entry_name_matches_pnpm_layout(tmp_path):
    profile = tmp_path / "profiles" / "desktop"
    artifacts = tmp_path / "plugin-artifacts"
    tgz = artifacts / "dsh-sgme-0.6.5.tgz"
    name = up.store_entry_name(artifacts, profile, tgz)
    assert name == "dsh-sgme-0.6.5@file+..+..+plugin-artifacts+dsh-sgme-0.6.5.tgz"
    store = up.storage_path(profile, tgz, artifacts)
    assert store.parts[-4:] == (name, "node_modules", "dsh-sgme")[-3:] or "dsh-sgme" in store.parts


def test_update_dependency_writes_forward_slash(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"dependencies": {"dsh-sgme": "file:old.tgz"}}), encoding="utf-8")
    tgz = tmp_path / "a" / "dsh-sgme-1.0.0.tgz"
    up.update_dependency(pkg, tgz)
    data = json.loads(pkg.read_text(encoding="utf-8"))
    assert data["dependencies"]["dsh-sgme"] == "file:" + str(tgz).replace("\\", "/")


def test_plan_detects_installed_version(tmp_path):
    bridge = _bridge(tmp_path, "9.9.9")
    profile = tmp_path / "profile"
    (profile / "node_modules" / "dsh-sgme").mkdir(parents=True)
    (profile / "node_modules" / "dsh-sgme" / "package.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )
    p = up.plan(bridge, profile, tmp_path / "artifacts")
    assert p["target_version"] == "9.9.9"
    assert p["installed_version"] == "1.0.0"
    assert p["needs_work"] is True
    assert p["restart_required"] is True


def test_plan_up_to_date(tmp_path):
    bridge = _bridge(tmp_path, "2.0.0")
    profile = tmp_path / "profile"
    (profile / "node_modules" / "dsh-sgme").mkdir(parents=True)
    (profile / "node_modules" / "dsh-sgme" / "package.json").write_text(
        json.dumps({"version": "2.0.0"}), encoding="utf-8"
    )
    p = up.plan(bridge, profile, tmp_path / "artifacts")
    assert p["needs_work"] is False


def test_remove_path_handles_directory_junction(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "x").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    try:
        import os
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("当前环境不允许创建目录链接")
    up._remove_path(link)
    assert not link.exists()
    assert target.exists()


def test_dry_run_main_does_not_touch_files(tmp_path, capsys):
    bridge = _bridge(tmp_path, "9.9.9")
    profile = tmp_path / "profile"
    rc = up.main([
        "--bridge", str(bridge), "--profile", str(profile),
        "--artifacts", str(tmp_path / "artifacts"), "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["needs_work"] is True
    assert not profile.exists()
