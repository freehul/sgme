# -*- coding: utf-8 -*-
"""SGME × MiMo Desktop 适配器部署脚本。

用法：
    python adapters/mimo/install.py
    python adapters/mimo/install.py --skills-root <MIMO_SKILLS_DIR>
    python adapters/mimo/install.py --base-url http://<SGME_HOST>:9910
    python adapters/mimo/install.py --no-selfcheck

行为：
- 复制 SKILL.md / scripts/* / locales/* / README.md → ~/.config/mimocode/skills/mimo/
- 幂等覆盖部署副本；不触碰其它技能
- 写部署配置 scripts/client.env（只含地址与密钥环境变量名，不含密钥值）
- 部署后可选 selfcheck，结果追加 scripts/access-log.md（部署副本内）
- 提示新开 MiMo Desktop 对话生效
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = SRC_DIR / "scripts"

sys.path.insert(0, str(SCRIPT_DIR))
from sgme_client import (  # noqa: E402
    ADMIN_KEY_ENV,
    DEFAULT_HTTP_URL,
    DEPLOY_CONFIG_NAME,
    KEY_ENV,
    derive_mcp_url,
    load_deploy_config,
    load_mimo_identity,
)


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def default_skills_root() -> Path:
    """MiMoCode 全局技能根：环境变量 MIMO_SKILLS_ROOT 优先，否则 ~/.config/mimocode/skills。"""
    env = os.environ.get("MIMO_SKILLS_ROOT")
    if env and env.strip():
        return Path(env.strip())
    return home_dir() / ".config" / "mimocode" / "skills"


def resolve_addresses(dest: Path, base_url: str | None = None,
                      mcp_url: str | None = None) -> tuple[str, str, str]:
    if base_url:
        http = base_url.strip().rstrip("/")
        return http, (mcp_url or derive_mcp_url(http)).rstrip("/"), "命令行参数 --base-url"

    env_http = (os.environ.get("SGME_HTTP_URL") or os.environ.get("SGME_BASE_URL") or "").strip()
    if env_http:
        http = env_http.rstrip("/")
        return http, (mcp_url or derive_mcp_url(http)).rstrip("/"), "环境变量"

    existing = load_deploy_config(dest / "scripts" / DEPLOY_CONFIG_NAME)
    cfg_http = existing.get("SGME_HTTP_URL") or existing.get("SGME_BASE_URL")
    if cfg_http:
        http = cfg_http.rstrip("/")
        mcp = (mcp_url or existing.get("SGME_MCP_URL") or derive_mcp_url(http)).rstrip("/")
        return http, mcp, "既有部署配置"

    ident = load_mimo_identity()
    ident_http = (ident.get("http") or "").strip()
    if ident_http:
        http = ident_http.rstrip("/")
        mcp = (mcp_url or (ident.get("mcp") or "").strip() or derive_mcp_url(http)).rstrip("/")
        return http, mcp, "身份文件 mimo-agent.json"

    http = DEFAULT_HTTP_URL
    return http, (mcp_url or derive_mcp_url(http)).rstrip("/"), "回环默认"


def write_deploy_config(dest: Path, http: str, mcp: str) -> Path:
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / DEPLOY_CONFIG_NAME
    lines = [
        "# SGME × MiMo 部署配置（由 install.py 生成；只含地址与密钥变量名，不含密钥值）",
        f"SGME_HTTP_URL={http}",
        f"SGME_MCP_URL={mcp}",
        f"# 密钥从环境变量 {KEY_ENV} / {ADMIN_KEY_ENV} 或 ~/.sgme/mimo-agent.json 读取",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def install(skills_root: Path) -> Path:
    dest = skills_root / "mimo"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "scripts").mkdir(exist_ok=True)
    (dest / "locales").mkdir(exist_ok=True)

    for name in ("SKILL.md", "README.md"):
        src = SRC_DIR / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    for name in ("sgme_client.py", "care_watch.py", "selfcheck.py"):
        shutil.copy2(SCRIPT_DIR / name, dest / "scripts" / name)
    for loc in (SRC_DIR / "locales").glob("*.json"):
        shutil.copy2(loc, dest / "locales" / loc.name)
    return dest


def append_access_log(dest: Path, note: str) -> None:
    log = dest / "scripts" / "access-log.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"- {ts} {note}\n"
    prev = log.read_text(encoding="utf-8") if log.is_file() else "# MiMo × SGME 接入日志\n\n"
    log.write_text(prev + line, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署 SGME × MiMo Desktop 适配器")
    ap.add_argument("--skills-root", default=None, help="MiMo 技能根目录（默认 ~/.config/mimocode/skills）")
    ap.add_argument("--base-url", default=None, help="SGME HTTP 地址")
    ap.add_argument("--mcp-url", default=None, help="SGME MCP 地址")
    ap.add_argument("--no-selfcheck", action="store_true", help="跳过自检")
    args = ap.parse_args(argv)

    root = Path(args.skills_root) if args.skills_root else default_skills_root()
    dest = install(root)
    http, mcp, src = resolve_addresses(dest, args.base_url, args.mcp_url)
    cfg = write_deploy_config(dest, http, mcp)

    print(f"部署目录: {dest}")
    print(f"HTTP: {http}  MCP: {mcp}  （来源: {src}）")
    print(f"部署配置: {cfg}")
    print(f"密钥: 读环境变量 {KEY_ENV} 或 ~/.sgme/mimo-agent.json（不落盘）")

    note = f"install 部署 HTTP={http} MCP={mcp} source={src}"
    if not args.no_selfcheck:
        r = subprocess.run(
            [sys.executable, str(dest / "scripts" / "selfcheck.py")],
            cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(r.stdout)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        note += f" selfcheck_exit={r.returncode}"
    append_access_log(dest, note)

    print("完成。请新开 MiMo Desktop 对话以加载技能 mimo。")
    print("若旧技能 sgme 仍在 ~/.config/mimocode/skills/sgme/，可在 Plugins 页停用以免抢触发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
