# -*- coding: utf-8 -*-
"""SGME × Hermes 适配插件部署脚本。

用法：
    python adapters/hermes/install.py            # 部署到默认 HERMES_HOME
    python adapters/hermes/install.py --home C:\\Users\\xxx\\AppData\\Local\\hermes   # 指定目录

行为：
- 复制 adapters/hermes/{__init__.py,plugin.yaml} → $HERMES_HOME/plugins/sgme/
- 幂等：覆盖旧副本，保留已启用状态（config.yaml 的 memory.provider: sgme 不动）
- 部署后提示重启 Hermes 生效
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 本项目 adapters/hermes/ 目录（脚本所在位置）
SRC_DIR = Path(__file__).resolve().parent
FILES = ("__init__.py", "plugin.yaml")


def default_hermes_home() -> Path:
    """默认 HERMES_HOME（与 Hermes 运行时一致）。"""
    import os
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    # Windows 默认：%LOCALAPPDATA%/hermes；macOS/Linux：~/.hermes
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
    return Path.home() / ".hermes"


def install(home: Path) -> Path:
    """部署插件到 $HERMES_HOME/plugins/sgme/。返回目标目录。"""
    dest = home / "plugins" / "sgme"
    dest.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        src = SRC_DIR / f
        if not src.exists():
            raise FileNotFoundError(f"源文件缺失: {src}")
        shutil.copy2(src, dest / f)
        print(f"  ✅ {f} → {dest / f}")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="SGME × Hermes 适配插件部署")
    parser.add_argument("--home", type=Path, default=None, help="HERMES_HOME（默认自动探测）")
    args = parser.parse_args()

    home = args.home or default_hermes_home()
    print(f"HERMES_HOME: {home}")
    if not (home / "config.yaml").exists():
        print(f"  ⚠️ 未找到 {home}/config.yaml，确认目录正确？继续部署…")
    dest = install(home)
    print(f"\n部署完成: {dest}")
    print("下一步：")
    print("  1. 确认 config.yaml 中 memory.provider: sgme（hermes memory setup 或手动）")
    print("  2. 重启 Hermes 生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
