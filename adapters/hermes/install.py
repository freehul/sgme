# -*- coding: utf-8 -*-
"""SGME × Hermes 适配插件部署脚本。

用法：
    python adapters/hermes/install.py                          # 部署到默认 HERMES_HOME
    python adapters/hermes/install.py --home <HERMES_HOME>      # 指定目录
    python adapters/hermes/install.py --install-json <path>     # 指定服务发现清单落点

行为：
- 复制 adapters/hermes/{__init__.py,plugin.yaml} → $HERMES_HOME/plugins/sgme/
- 生成**服务发现清单**（默认 ~/.sgme/install.json）：HTTP 地址端口 + Key 的**环境变量名引用**，
  不落任何明文密钥（字段形态与 sgme/config.py::write_client_install_json、adapters/dsh/install.py 对齐）
- 幂等：覆盖旧副本，保留已启用状态（config.yaml 的 memory.provider: sgme 不动）
- 部署后提示重启 Hermes 生效
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

# 本项目 adapters/hermes/ 目录（脚本所在位置）
SRC_DIR = Path(__file__).resolve().parent
FILES = ("__init__.py", "plugin.yaml")

# 默认 SGME 地址（回环）；生产部署用 SGME_BASE_URL 指向远端 SGME（如 http://<NAS_IP>:9910）
DEFAULT_BASE_URL = "http://127.0.0.1:9910"
# 服务发现清单落点覆盖（测试/非标准环境用）；不设则固定 ~/.sgme/install.json
INSTALL_JSON_ENV = "SGME_INSTALL_JSON"


def default_hermes_home() -> Path:
    """默认 HERMES_HOME（与 Hermes 运行时一致）。"""
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


# ---------- 服务发现清单 install.json（agent 发现 SGME 安装位置用） ----------


def resolve_base_url(base_url: str | None = None) -> str:
    """解析 SGME 地址：显式参数 > SGME_BASE_URL 环境变量 > 回环默认。"""
    return (base_url or os.environ.get("SGME_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def resolve_agent_id() -> str:
    """本适配器在 SGME 侧的溯源标识（append 自报 agent_id）。"""
    return os.environ.get("SGME_HERMES_AGENT_ID") or "hermes"


def adapter_version() -> str:
    """读本插件 plugin.yaml 的 version（版本口径跟随 SGME 版本，见 README）。"""
    try:
        for line in (SRC_DIR / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].split("#", 1)[0].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def install_json_path() -> Path:
    """服务发现清单路径：SGME_INSTALL_JSON 覆盖，否则固定 ~/.sgme/install.json。"""
    env = os.environ.get(INSTALL_JSON_ENV)
    return Path(env) if env else Path.home() / ".sgme" / "install.json"


def write_install_json(
    install_path: Path | None = None,
    base_url: str | None = None,
) -> Path:
    """生成服务发现清单（agent 服务发现第 2 步）。

    内容（对齐服务端 config.write_client_install_json 的字段形态）：
    - schema_version：清单结构版本（固定 1）
    - base_url / http.host / http.port：从 SGME_BASE_URL 解析
    - mcp.port：SGME_MCP_PORT env 或默认 9913
    - keys：**只写环境变量名引用**（SGME_ADMIN_KEY / SGME_AGENT_KEY / SGME_BEARER_TOKEN），
      绝不落明文密钥（项目铁律：密钥不落盘）
    - adapter / adapter_version / agent_id：本适配器标识与版本口径

    幂等：重复调用覆盖为相同内容。返回写入路径。
    """
    url = resolve_base_url(base_url)
    parsed = urlparse(url)
    data = {
        "schema_version": 1,
        "adapter": "hermes",
        "adapter_version": adapter_version(),
        "base_url": url,
        "http": {
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or 9910,
        },
        "mcp": {"port": int(os.environ.get("SGME_MCP_PORT", "9913"))},
        "keys": {
            "admin": "SGME_ADMIN_KEY",
            "agent": "SGME_AGENT_KEY",
            "bearer": "SGME_BEARER_TOKEN",
        },
        "agent_id": resolve_agent_id(),
    }
    target = install_path or install_json_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="SGME × Hermes 适配插件部署")
    parser.add_argument("--home", type=Path, default=None, help="HERMES_HOME（默认自动探测）")
    parser.add_argument("--install-json", type=Path, default=None,
                        help="服务发现清单落点（默认 ~/.sgme/install.json）")
    parser.add_argument("--no-install-json", action="store_true",
                        help="跳过服务发现清单生成")
    args = parser.parse_args()

    home = args.home or default_hermes_home()
    print(f"HERMES_HOME: {home}")
    if not (home / "config.yaml").exists():
        print(f"  ⚠️ 未找到 {home}/config.yaml，确认目录正确？继续部署…")
    dest = install(home)
    print(f"[1/2] 插件已部署: {dest}")

    if args.no_install_json:
        print("[2/2] 已跳过服务发现清单生成（--no-install-json）")
    else:
        ij_path = write_install_json(args.install_json)
        print(f"[2/2] 服务发现清单: {ij_path}")
        print("      （base_url / http 地址端口 + Key 环境变量名引用，不落明文密钥）")

    print("\n下一步：")
    print("  1. 确认 config.yaml 中 memory.provider: sgme（hermes memory setup 或手动）")
    print("  2. 环境变量提供密钥：SGME_AGENT_KEY / SGME_ADMIN_KEY（只引用变量名，不写进配置）")
    print("  3. 重启 Hermes 生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
