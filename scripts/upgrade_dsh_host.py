# -*- coding: utf-8 -*-
"""scripts/upgrade_dsh_host.py：把仓库 dsh-sgme 升级到宿主 profile（可复跑）。

背景（B309 手工升级的教训）：pnpm install 会因 lockfile/环境差异提示「全量删除
node_modules」，直接执行有破坏风险；手工解包 + junction 又不可复现。本脚本把
「打包 → 备份 → 改写 profile 依赖 → 解包到 pnpm store → 重建链接 → 校验」固化，
默认 dry-run，只有 --apply 才动手，且**绝不重启 DSH**。

用法：
  python scripts/upgrade_dsh_host.py                 # 打印计划
  python scripts/upgrade_dsh_host.py --apply         # 执行（不重启 DSH）
  python scripts/upgrade_dsh_host.py --apply --verify # 执行后跑 freshness 检查

回滚：脚本会把 profile 的 package.json / pnpm-lock.yaml 备份为
*.bak-dsh-sgme-<version>-<ts>；旧 store 目录保留，恢复备份并重建链接即可。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRIDGE = ROOT / "adapters" / "dsh" / "sgme-bridge"
DEFAULT_PROFILE = Path.home() / ".dsh" / "profiles" / "desktop"
DEFAULT_ARTIFACTS = Path.home() / ".dsh" / "plugin-artifacts"
ENTRY_REL = Path("lib") / "index.js"


def read_version(bridge: Path) -> str:
    data = json.loads((bridge / "package.json").read_text(encoding="utf-8"))
    return str(data.get("version") or "").strip()


def store_entry_name(artifacts: Path, profile: Path, tgz: Path) -> str:
    """还原 pnpm store 目录名（与 pnpm 实际布局一致）。"""
    rel = os.path.relpath(artifacts, profile)
    enc = rel.replace("\\", "+").replace("/", "+")
    pkg = tgz.name[:-4] if tgz.name.endswith(".tgz") else tgz.name
    return f"{pkg}@file+{enc}+{tgz.name}"


def storage_path(profile: Path, tgz: Path, artifacts: Path) -> Path:
    return profile / ".pnpm" / store_entry_name(artifacts, profile, tgz) / "node_modules" / "dsh-sgme"


def update_dependency(package_json: Path, tgz: Path) -> None:
    data = json.loads(package_json.read_text(encoding="utf-8"))
    deps = data.setdefault("dependencies", {})
    deps["dsh-sgme"] = "file:" + str(tgz).replace("\\", "/")
    package_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def plan(bridge: Path, profile: Path, artifacts: Path) -> dict:
    target = read_version(bridge)
    tgz = artifacts / f"dsh-sgme-{target}.tgz"
    installed = None
    entry = profile / "node_modules" / "dsh-sgme" / "package.json"
    if entry.is_file():
        try:
            installed = json.loads(entry.read_text(encoding="utf-8")).get("version")
        except json.JSONDecodeError:
            installed = None
    return {
        "bridge": str(bridge),
        "profile": str(profile),
        "artifacts": str(artifacts),
        "target_version": target,
        "installed_version": installed,
        "disabled": os.environ.get("SGME_DISABLE_HOST_UPGRADE") == "1",
        "needs_work": installed != target,
        "tgz": str(tgz),
        "store": str(storage_path(profile, tgz, artifacts)),
        "restart_required": True,
    }


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        try:
            # Windows junction：rmtree 会报 "Cannot call rmtree on a symbolic link"，
            # os.rmdir 可删除 junction；普通目录 rmdir 失败再落 rmtree。
            os.rmdir(path)
            return
        except OSError:
            shutil.rmtree(path)


def _make_dir_link(link: Path, target: Path) -> str:
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True,
        )
        return "junction"


def apply(bridge: Path, profile: Path, artifacts: Path, *, verify: bool = False) -> dict:
    p = plan(bridge, profile, artifacts)
    if p["disabled"]:
        raise SystemExit("SGME_DISABLE_HOST_UPGRADE=1，拒绝执行")
    if not p["needs_work"]:
        p["result"] = "up-to-date"
        return p
    profile.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    tgz = Path(p["tgz"])
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("找不到 npm，无法打包")
    if not tgz.is_file():
        subprocess.run(
            [npm, "pack", "--pack-destination", str(artifacts)],
            cwd=str(bridge), check=True, capture_output=True,
        )
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    version = p["target_version"]
    for name in ("package.json", "pnpm-lock.yaml"):
        src = profile / name
        if src.is_file():
            shutil.copy2(src, profile / f"{name}.bak-dsh-sgme-{version}-{stamp}")
    update_dependency(profile / "package.json", tgz)

    store = Path(p["store"])
    if store.exists():
        _remove_path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tgz, "r:gz") as tf:
            tf.extractall(tmp)
        src = Path(tmp) / "package"
        if not src.is_dir():
            raise SystemExit("tgz 内缺少 package/ 目录")
        shutil.move(str(src), str(store))

    link = profile / "node_modules" / "dsh-sgme"
    _remove_path(link)
    link_type = _make_dir_link(link, store)
    installed = json.loads((link / "package.json").read_text(encoding="utf-8")).get("version")
    p.update({
        "result": "installed",
        "link_type": link_type,
        "installed_version_after": installed,
        "verified": installed == version and (link / ENTRY_REL).is_file(),
    })
    if verify:
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_adapter_freshness.py"), "--strict-deploy"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        p["freshness_exit"] = r.returncode
        p["freshness_tail"] = (r.stdout or r.stderr or "").strip().splitlines()[-2:]
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="升级宿主 dsh-sgme（不重启 DSH）")
    ap.add_argument("--bridge", default=str(DEFAULT_BRIDGE))
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--artifacts", default=str(DEFAULT_ARTIFACTS))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    bridge, profile, artifacts = Path(args.bridge), Path(args.profile), Path(args.artifacts)
    result = apply(bridge, profile, artifacts, verify=args.verify) if args.apply else plan(bridge, profile, artifacts)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"目标版本 {result['target_version']}；已装 {result['installed_version']}")
        print(f"tgz -> {result['tgz']}")
        print(f"store -> {result['store']}")
        if not args.apply:
            print("dry-run：未做任何修改（加 --apply 执行）")
        else:
            print(f"结果：{result.get('result')}；链接 {result.get('link_type')}；校验 {result.get('verified')}")
            if "freshness_exit" in result:
                print(f"freshness --strict-deploy exit={result['freshness_exit']}")
            print("⚠️ 需要重启 DSH 才加载新代码（本脚本不重启）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
