# -*- coding: utf-8 -*-
"""scripts/upgrade_dsh_host.py：把仓库 dsh-sgme 升级到宿主 profile（可复跑）。

背景（B309 + 2026-11-06 两次教训）
---------------------------------
- 直接 ``pnpm install`` 会因 lockfile / 环境差异提示「全量删除 node_modules」，
  直接跑有破坏风险；
- 于是曾有「手工解包 tgz + junction」的装法——它**只搬插件本体、不装插件的运行时
  依赖**（schemastery / dev），也**不更新 pnpm-lock.yaml**。2026-11-06 实锤：DSH
  启动 import dsh-sgme 时报 ``ERR_MODULE_NOT_FOUND: schemastery``，整条 sgme
  插件被跳过、SGME 工具全部不可用，而 ``check_adapter_freshness --strict-deploy``
  全绿——手工重造 pnpm store 布局本身就是错误方向。
- 结论：走官方方法。宿主自带 CLI 已封装「用内置 pnpm 在指定 profile 里 add/install」：
  ``dsh plugin --profile <name> add <spec>``，它会同时对齐 package.json /
  pnpm-lock.yaml / node_modules 与依赖链接。本脚本只负责「打包 → 备份 → 调它 → 校验」。

流程
----
打包（tgz 缺失时）→ 备份 package.json / pnpm-lock.yaml → 官方 dsh plugin add →
校验（版本 + 入口 + 运行时依赖可解析）→ 可选 freshness 门禁。
默认 dry-run，只有 --apply 才动手；**绝不重启 DSH**。

用法::

    python scripts/upgrade_dsh_host.py                  # 打印计划（含将执行的命令）
    python scripts/upgrade_dsh_host.py --apply          # 执行（不重启 DSH）
    python scripts/upgrade_dsh_host.py --apply --verify # 执行后跑 freshness --strict-deploy

回滚：apply 前会把 profile 的 package.json / pnpm-lock.yaml 备份为
``*.bak-dsh-sgme-<version>-<ts>``；恢复备份后用官方 CLI 重装一次即可回到旧版。

⚠️ 装完必须重启 DSH 才加载新代码（本脚本不重启）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRIDGE = ROOT / "adapters" / "dsh" / "sgme-bridge"
DEFAULT_PROFILE = Path.home() / ".dsh" / "profiles" / "desktop"
DEFAULT_ARTIFACTS = Path.home() / ".dsh" / "plugin-artifacts"
ENTRY_REL = Path("lib") / "index.js"

# 官方 CLI 定位顺序：--dsh-cli > SGME_DSH_CLI > 桌面版内置 CLI > PATH 上的 dsh。
# 桌面版 profile（desktop）由 Electron 独占管理，只有内置 CLI 带 manageDesktopProfile
# 权限，所以内置路径必须排在 PATH 之前。
DSH_CLI_ENV = "SGME_DSH_CLI"
DEFAULT_DSH_CLI_TAIL = (
    Path("Programs") / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
)
INSTALL_TIMEOUT_S = 600


def read_version(bridge: Path) -> str:
    data = json.loads((bridge / "package.json").read_text(encoding="utf-8"))
    return str(data.get("version") or "").strip()


def locate_dsh_cli(explicit: str | None = None) -> Path | None:
    """按固定优先级找可管理 profile 的 dsh CLI；找不到返回 None（由 apply fail loud）。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(DSH_CLI_ENV)
    if env:
        candidates.append(Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / DEFAULT_DSH_CLI_TAIL)
    found = shutil.which("dsh")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def profile_identity(profile: Path) -> tuple[str, Path]:
    """从 profile 路径反推 (profile 名, dsh home) —— 官方 CLI 要的是 ``<home>/profiles/<name>``。"""
    resolved = profile.resolve()
    if resolved.parent.name != "profiles" or not resolved.name:
        raise SystemExit(
            "profile 路径必须形如 <dsh-home>/profiles/<name>（当前：%s）" % resolved
        )
    return resolved.name, resolved.parent.parent


def install_command(cli: Path, profile_name: str, tgz: Path) -> list[str]:
    """官方装法：dsh plugin --profile <name> add file:<tgz>（pnpm 在 profile 内执行）。"""
    return [
        str(cli),
        "plugin",
        "--profile",
        profile_name,
        "add",
        "file:" + tgz.resolve().as_posix(),
    ]


def format_command(cmd: list[str]) -> str:
    return " ".join('"%s"' % part if " " in part else part for part in cmd)


def _read_installed(profile: Path) -> str | None:
    entry = profile / "node_modules" / "dsh-sgme" / "package.json"
    if not entry.is_file():
        return None
    try:
        return json.loads(entry.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        return None


def dependency_gaps(plugin_dir: Path) -> list[str]:
    """校验部署副本声明的运行时依赖可从插件目录解析。

    与 ``check_adapter_freshness.py`` 的防线 4 同口径：pnpm 隔离布局下依赖链接
    在插件目录同级的 node_modules（``<store>/node_modules/<dep>``），npm 嵌套布局
    在插件目录自己的 node_modules。
    """
    pkg = plugin_dir / "package.json"
    if not pkg.is_file():
        return ["%s 缺 package.json" % plugin_dir]
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ["%s 的 package.json 不可读：%s" % (plugin_dir, exc)]
    deps = data.get("dependencies")
    if not isinstance(deps, dict) or not deps:
        return []
    real = plugin_dir.resolve()
    roots = [real.parent, real / "node_modules"]
    missing = [
        name
        for name in sorted(deps)
        if not any((root / name / "package.json").is_file() for root in roots)
    ]
    if not missing:
        return []
    return ["缺运行时依赖：%s（%s 与 %s 下均无法解析）" % ("、".join(missing), roots[0], roots[1])]


def _tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def plan(bridge: Path, profile: Path, artifacts: Path) -> dict:
    target = read_version(bridge)
    tgz = artifacts / ("dsh-sgme-%s.tgz" % target)
    installed = _read_installed(profile)
    profile_name, dsh_home = profile_identity(profile)
    cli = locate_dsh_cli()
    command = install_command(cli, profile_name, tgz) if cli is not None else None
    return {
        "bridge": str(bridge),
        "profile": str(profile),
        "profile_name": profile_name,
        "dsh_home": str(dsh_home),
        "artifacts": str(artifacts),
        "target_version": target,
        "installed_version": installed,
        "disabled": os.environ.get("SGME_DISABLE_HOST_UPGRADE") == "1",
        "needs_work": installed != target,
        "tgz": str(tgz),
        "dsh_cli": str(cli) if cli is not None else None,
        "command": command,
        "restart_required": True,
    }


def apply(
    bridge: Path,
    profile: Path,
    artifacts: Path,
    *,
    verify: bool = False,
    dsh_cli: str | None = None,
) -> dict:
    p = plan(bridge, profile, artifacts)
    if p["disabled"]:
        raise SystemExit("SGME_DISABLE_HOST_UPGRADE=1，拒绝执行")
    if not p["needs_work"]:
        p["result"] = "up-to-date"
        return p

    profile.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    tgz = Path(p["tgz"])
    if not tgz.is_file():
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("tgz 不存在且找不到 npm，无法打包（%s）" % tgz)
        subprocess.run(
            [npm, "pack", "--pack-destination", str(artifacts)],
            cwd=str(bridge),
            check=True,
            capture_output=True,
        )

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    version = p["target_version"]
    backups: list[str] = []
    for name in ("package.json", "pnpm-lock.yaml"):
        src = profile / name
        if src.is_file():
            dst = profile / ("%s.bak-dsh-sgme-%s-%s" % (name, version, stamp))
            shutil.copy2(src, dst)
            backups.append(str(dst))

    cli = locate_dsh_cli(dsh_cli)
    if cli is None:
        raise SystemExit(
            "找不到 dsh CLI。桌面版默认位置：%%LOCALAPPDATA%%\\%s；"
            "也可用 --dsh-cli 或环境变量 %s 指定" % (DEFAULT_DSH_CLI_TAIL, DSH_CLI_ENV)
        )
    command = install_command(cli, p["profile_name"], tgz)
    env = os.environ.copy()
    env["DSH_HOME"] = str(p["dsh_home"])

    try:
        res = subprocess.run(
            command,
            cwd=str(profile),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=INSTALL_TIMEOUT_S,
        )
        code, out, err = res.returncode, res.stdout or "", res.stderr or ""
    except subprocess.TimeoutExpired:
        code, out, err = -1, "", "dsh plugin add 超时（%ss）" % INSTALL_TIMEOUT_S

    p.update({
        "command": command,
        "backups": backups,
        "exit": code,
        "stdout_tail": _tail(out),
        "stderr_tail": _tail(err),
    })
    if code != 0:
        p["result"] = "install-failed"
        return p

    installed = _read_installed(profile)
    gaps = dependency_gaps(profile / "node_modules" / "dsh-sgme")
    p.update({
        "result": "installed",
        "installed_version_after": installed,
        "dependency_gaps": gaps,
        "verified": installed == version
        and (profile / "node_modules" / "dsh-sgme" / ENTRY_REL).is_file()
        and not gaps,
    })
    if verify:
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_adapter_freshness.py"), "--strict-deploy"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        p["freshness_exit"] = r.returncode
        p["freshness_tail"] = _tail((r.stdout or "") + (r.stderr or ""), 800).splitlines()[-2:]
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="升级宿主 dsh-sgme（走官方 dsh plugin，不重启 DSH）")
    ap.add_argument("--bridge", default=str(DEFAULT_BRIDGE))
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--artifacts", default=str(DEFAULT_ARTIFACTS))
    ap.add_argument("--dsh-cli", default=None, help="显式指定 dsh CLI（默认定位桌面版内置 CLI）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    bridge, profile, artifacts = Path(args.bridge), Path(args.profile), Path(args.artifacts)

    if args.apply:
        result = apply(bridge, profile, artifacts, verify=args.verify, dsh_cli=args.dsh_cli)
    else:
        result = plan(bridge, profile, artifacts)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("目标版本 %s；已装 %s" % (result["target_version"], result["installed_version"]))
        print("tgz     -> %s" % result["tgz"])
        print("profile -> %s（%s，DSH_HOME=%s）" % (
            result["profile"], result["profile_name"], result["dsh_home"]))
        if not args.apply:
            if result["command"]:
                print("将执行   -> %s" % format_command(result["command"]))
            else:
                print("⚠️ 未找到 dsh CLI；--apply 时会失败并提示（可用 --dsh-cli 指定）")
            print("dry-run：未做任何修改（加 --apply 执行）")
            return 0

        if result.get("result") == "up-to-date":
            print("结果：已是最新，无需动作")
        elif result.get("result") == "installed":
            print("结果：installed；链接校验 %s" % ("通过" if result.get("verified") else "未通过"))
            if result.get("dependency_gaps"):
                for gap in result["dependency_gaps"]:
                    print("  ❌ %s" % gap)
            if "freshness_exit" in result:
                print("freshness --strict-deploy exit=%s" % result["freshness_exit"])
        else:
            print("结果：install-failed（exit=%s）" % result.get("exit"))
            if result.get("stderr_tail"):
                print("--- dsh plugin add stderr（尾部） ---")
                print(result["stderr_tail"])
        print("⚠️ 需要重启 DSH 才加载新代码（本脚本不重启）")
        if result.get("result") != "installed" or not result.get("verified"):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
