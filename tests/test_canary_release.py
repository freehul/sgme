"""tests/test_canary_release.py：canary 发布 dry-run 计划测试（全离线）。

覆盖：
- 无 tag / 无 canary 历史 → v2.0.0-canary.1；
- 有 canary 历史 → 序号递增，changelog 从上一个 tag 起算；
- --base-version 显式基线；非法基线 → argparse 退出码 2；
- 门禁失败 → 退出码 1，且绝不打印「可发布」；全绿 → 可发布；
- dry-run 只执行 git tag --list / git log，不 tag、不 push；
- git 不可用 → 退出码 1 + JSON 错误。

subprocess 全部 monkeypatch，不触碰真实 git 与三道门禁脚本。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

GATE_SCRIPTS = (
    "adapter_parity.py",
    "check_adapter_freshness.py",
    "verify_decisions.py",
)


def _load_script(module_name: str, file_name: str):
    """按路径加载 scripts/ 下脚本为模块（脚本非包成员，直接 import 不可靠）。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / file_name)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


canary = _load_script("canary_release_under_test", "canary_release.py")


def _fake_run(tags_text: str, log_text: str = "- abc1234 feat: 示例提交\n",
              gate_results: dict | None = None, calls: list | None = None):
    """构造 subprocess.run 替身：git 只读命令直接回放，门禁按脚本名给返回码。"""
    gate_results = gate_results or {}

    def fake_run(cmd, **kwargs):
        cmd = [str(part) for part in cmd]
        if calls is not None:
            calls.append(cmd)
        if cmd[:2] == ["git", "tag"]:
            assert cmd[2:] == ["--list"], f"git tag 只允许 --list：{cmd}"
            return SimpleNamespace(returncode=0, stdout=tags_text, stderr="")
        if cmd[:2] == ["git", "log"]:
            return SimpleNamespace(returncode=0, stdout=log_text, stderr="")
        for script in GATE_SCRIPTS:
            if any(script in part for part in cmd):
                rc = gate_results.get(script, 0)
                return SimpleNamespace(
                    returncode=rc,
                    stdout=f"[{script}] 门禁输出\n",
                    stderr="",
                )
        raise AssertionError(f"未预期的子进程命令：{cmd}")

    return fake_run


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# tag 计算
# ---------------------------------------------------------------------------


def test_no_history_defaults_to_canary_1(monkeypatch, capsys):
    """全无 tag：默认基线取 pyproject version → v<version>-canary.1。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("", calls=calls))

    assert canary.main(["--json"]) == 0
    data = _json_out(capsys)

    assert data["mode"] == "dry-run"
    assert data["read_only"] is True
    release = data["release"]
    base = canary._project_version()
    assert release["next_tag"] == f"v{base}-canary.1"
    assert release["base_version"] == base
    assert release["sequence"] == 1
    assert release["source"] == "default"
    assert release["previous_canary"] is None

    assert data["git"]["tag_count"] == 0
    assert data["git"]["latest_tag"] is None
    assert data["changelog"]["last_tag"] is None
    assert data["changelog"]["commits"] == ["- abc1234 feat: 示例提交"]
    assert data["gates"] is None
    assert data["release_ready"] is None

    # 只执行了两条只读 git 命令，未 tag / 未 push
    assert calls == [
        ["git", "tag", "--list"],
        ["git", "log", "--oneline", "-n", "50"],
    ]


def test_existing_canary_increments_and_uses_previous_tag(monkeypatch, capsys):
    """有 canary 历史：沿用基准版本、序号 +1；changelog 从最高 canary tag 起算。"""
    tags = "v1.10.6\nv2.0.0-canary.1\nv2.0.0-canary.2\n"
    calls: list[list[str]] = []
    monkeypatch.setattr(canary.subprocess, "run", _fake_run(tags, calls=calls))

    assert canary.main(["--json"]) == 0
    data = _json_out(capsys)

    assert data["release"]["next_tag"] == "v2.0.0-canary.3"
    assert data["release"]["source"] == "history"
    assert data["release"]["previous_canary"] == "v2.0.0-canary.2"
    assert data["git"]["latest_tag"] == "v2.0.0-canary.2"
    assert data["git"]["canary_tags"] == ["v2.0.0-canary.1", "v2.0.0-canary.2"]
    assert data["changelog"]["range"] == "v2.0.0-canary.2..HEAD"
    assert calls[1][:4] == ["git", "log", "--oneline", "v2.0.0-canary.2..HEAD"]


def test_stable_tag_only_still_uses_default_canary_base(monkeypatch, capsys):
    """只有正式 tag、没有 canary：按设计基线 2.0.0 出 canary.1，changelog 用正式 tag。"""
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("v1.10.6\nv1.9.0\n"))

    assert canary.main(["--json"]) == 0
    data = _json_out(capsys)

    base = canary._project_version()
    assert data["release"]["next_tag"] == f"v{base}-canary.1"
    assert data["release"]["source"] == "default"
    assert data["git"]["latest_tag"] == "v1.10.6"
    assert data["changelog"]["range"] == "v1.10.6..HEAD"


def test_base_version_override(monkeypatch, capsys):
    """--base-version 显式基线：该基准下取最大 N + 1。"""
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("v2.0.0-canary.1\nv2.0.0-canary.2\n"))

    assert canary.main(["--json", "--base-version", "2.1.0"]) == 0
    data = _json_out(capsys)
    assert data["release"]["next_tag"] == "v2.1.0-canary.1"
    assert data["release"]["source"] == "explicit"


def test_invalid_base_version_exits_2(monkeypatch):
    """非法基线交给 argparse：退出码 2，不进入 git 调用。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("", calls=calls))

    with pytest.raises(SystemExit) as exc:
        canary.main(["--base-version", "2.0"])
    assert exc.value.code == 2
    assert calls == []


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_gate_all_pass_marks_releasable(monkeypatch, capsys):
    """三道门禁全 0：退出码 0，打印「可发布」，JSON release_ready=True。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("", calls=calls))

    assert canary.main(["--check-gates"]) == 0
    out = capsys.readouterr().out
    assert "✅ 门禁全绿，可发布" in out
    assert "❌ 门禁未通过" not in out

    gate_calls = [c for c in calls if not c[0] == "git"]
    assert [Path(c[1]).name for c in gate_calls] == list(GATE_SCRIPTS)
    assert all(c[0] == sys.executable for c in gate_calls)
    assert gate_calls[0][2:] == ["--strict"]
    assert gate_calls[1][2:] == ["--strict-deploy"]
    assert gate_calls[2][2:] == ["--strict"]


def test_gate_failure_returns_1_without_release_banner(monkeypatch, capsys):
    """任一 gate 失败：退出码 1，且输出里绝不出现「可发布」。"""
    gate_results = {"check_adapter_freshness.py": 1}
    monkeypatch.setattr(
        canary.subprocess, "run", _fake_run("", gate_results=gate_results)
    )

    assert canary.main(["--check-gates"]) == 1
    out = capsys.readouterr().out
    assert "❌ 门禁未通过（1/3 失败）" in out
    assert "可发布" not in out
    assert "❌ adapter_freshness --strict-deploy（exit 1）" in out


def test_gate_failure_json_fields(monkeypatch, capsys):
    """JSON 模式同样退出 1；release_ready=False，失败 gate 带 returncode 与 tail。"""
    gate_results = {"verify_decisions.py": 2}
    monkeypatch.setattr(
        canary.subprocess, "run", _fake_run("", gate_results=gate_results)
    )

    assert canary.main(["--check-gates", "--json"]) == 1
    data = _json_out(capsys)

    assert data["gates_ok"] is False
    assert data["release_ready"] is False
    gates = data["gates"]
    assert gates["failed"] == 1 and gates["passed"] == 2
    failed = [g for g in gates["gates"] if not g["ok"]]
    assert len(failed) == 1
    assert failed[0]["name"] == "verify_decisions"
    assert failed[0]["returncode"] == 2
    assert failed[0]["output_tail"] == ["[verify_decisions.py] 门禁输出"]


def test_plan_only_json_does_not_run_gates(monkeypatch, capsys):
    """不带 --check-gates：计划里 gates=None、release_ready=None，不调用门禁。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("v1.10.6\n", calls=calls))

    assert canary.main(["--json"]) == 0
    data = _json_out(capsys)
    assert data["gates"] is None
    assert data["release_ready"] is None
    assert all(c[0] == "git" for c in calls)


# ---------------------------------------------------------------------------
# 只读边界与失败路径
# ---------------------------------------------------------------------------


def test_plan_never_executes_tag_or_push(monkeypatch):
    """计划里的 git tag -a / git push 只是文本，不进入 subprocess。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(canary.subprocess, "run", _fake_run("v1.10.6\n", calls=calls))

    report = canary.build_plan(REPO_ROOT)
    next_tag = report["release"]["next_tag"]
    assert report["planned_actions"][0]["command"].startswith(f"git tag -a {next_tag}")
    assert f"git push origin {next_tag}" in report["planned_actions"][1]["command"]

    git_calls = [c for c in calls if c[0] == "git"]
    assert git_calls and all(c[1] in ("tag", "log") for c in git_calls)
    assert not any("push" in c for c in calls)
    assert not any(c[0] == "git" and "-a" in c for c in calls)


def test_git_failure_returns_1(monkeypatch, capsys):
    """git 不可用（非仓库/无 git）：退出码 1，JSON 给可读错误。"""
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: not a git repository"
        )

    monkeypatch.setattr(canary.subprocess, "run", fake_run)

    assert canary.main(["--json"]) == 1
    data = _json_out(capsys)
    assert "not a git repository" in data["error"]
    assert data["read_only"] is True
