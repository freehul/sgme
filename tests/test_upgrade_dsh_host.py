# -*- coding: utf-8 -*-
"""tests/test_upgrade_dsh_host.py：宿主升级脚本的纯函数 / dry-run 回归。

2026-11-06 教训：旧实现手工解包 tgz 到 profile/.pnpm 并建 junction，不装运行时
依赖也不更新 lockfile → DSH 启动 import 失败。现在脚本改走官方
``dsh plugin --profile <name> add file:<tgz>``，用例覆盖「命令构造 / profile
路径推导 / 依赖可解析校验 / dry-run 零副作用」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import upgrade_dsh_host as up


def _bridge(tmp: Path, version: str = "9.9.9") -> Path:
    b = tmp / "bridge"
    b.mkdir()
    (b / "package.json").write_text(
        json.dumps({"name": "dsh-sgme", "version": version}), encoding="utf-8"
    )
    return b


def _profile(tmp: Path, installed: str | None = None) -> Path:
    """造一个 <home>/profiles/desktop 形态的 profile（官方 CLI 的路径契约）。"""
    profile = tmp / "home" / "profiles" / "desktop"
    if installed is not None:
        pkg = profile / "node_modules" / "dsh-sgme" / "package.json"
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.write_text(json.dumps({"version": installed}), encoding="utf-8")
    return profile


def test_plan_detects_installed_version(tmp_path):
    bridge = _bridge(tmp_path, "9.9.9")
    profile = _profile(tmp_path, "1.0.0")
    p = up.plan(bridge, profile, tmp_path / "artifacts")
    assert p["target_version"] == "9.9.9"
    assert p["installed_version"] == "1.0.0"
    assert p["needs_work"] is True
    assert p["profile_name"] == "desktop"
    assert p["dsh_home"] == str((tmp_path / "home").resolve())
    assert p["restart_required"] is True


def test_plan_up_to_date(tmp_path):
    bridge = _bridge(tmp_path, "2.0.0")
    profile = _profile(tmp_path, "2.0.0")
    p = up.plan(bridge, profile, tmp_path / "artifacts")
    assert p["needs_work"] is False


def test_profile_identity_requires_profiles_layout(tmp_path):
    good = tmp_path / "home" / "profiles" / "desktop-probe"
    assert up.profile_identity(good) == ("desktop-probe", (tmp_path / "home").resolve())
    with pytest.raises(SystemExit):
        up.profile_identity(tmp_path / "elsewhere" / "desktop")


def test_install_command_uses_official_plugin_add(tmp_path):
    cli = tmp_path / "dsh.cmd"
    tgz = tmp_path / "artifacts" / "dsh-sgme-9.9.9.tgz"
    cmd = up.install_command(cli, "desktop", tgz)
    assert cmd[:5] == [str(cli), "plugin", "--profile", "desktop", "add"]
    assert cmd[5] == "file:" + tgz.resolve().as_posix()


def test_dependency_gaps_detect_missing_and_resolved(tmp_path):
    plugin = tmp_path / "profile" / "node_modules" / "dsh-sgme"
    plugin.mkdir(parents=True)
    (plugin / "package.json").write_text(
        json.dumps({"name": "dsh-sgme", "version": "9.9.9",
                    "dependencies": {"schemastery": "^3.18.0"}}),
        encoding="utf-8",
    )
    # pnpm 隔离布局：依赖链接在插件目录同级的 node_modules
    gaps = up.dependency_gaps(plugin)
    assert gaps and "schemastery" in gaps[0]

    dep = tmp_path / "profile" / "node_modules" / "schemastery"
    dep.mkdir(parents=True)
    (dep / "package.json").write_text('{"name": "schemastery"}', encoding="utf-8")
    assert up.dependency_gaps(plugin) == []


def test_apply_refuses_when_disabled(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, "9.9.9")
    profile = _profile(tmp_path, "1.0.0")
    monkeypatch.setenv("SGME_DISABLE_HOST_UPGRADE", "1")
    with pytest.raises(SystemExit):
        up.apply(bridge, profile, tmp_path / "artifacts")


def test_apply_up_to_date_needs_no_cli(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, "2.0.0")
    profile = _profile(tmp_path, "2.0.0")
    monkeypatch.setattr(up, "locate_dsh_cli", lambda explicit=None: None)
    result = up.apply(bridge, profile, tmp_path / "artifacts")
    assert result["result"] == "up-to-date"


def test_dry_run_main_does_not_touch_files(tmp_path, capsys, monkeypatch):
    bridge = _bridge(tmp_path, "9.9.9")
    profile = tmp_path / "home" / "profiles" / "desktop"
    rc = up.main([
        "--bridge", str(bridge), "--profile", str(profile),
        "--artifacts", str(tmp_path / "artifacts"), "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["needs_work"] is True
    assert not profile.exists()
