# -*- coding: utf-8 -*-
"""SGME × WorkBuddy 适配器部署脚本。

用法：
    python adapters/workbuddy/install.py
    python adapters/workbuddy/install.py --skills-root <WORKBUDDY_SKILLS_DIR>
    python adapters/workbuddy/install.py --base-url http://<SGME_HOST>:9910
    python adapters/workbuddy/install.py --agent-id <本机注册id>
    python adapters/workbuddy/install.py --no-selfcheck

行为：
- 复制 SKILL.md / scripts/* / locales/* / README.md → ~/.workbuddy/skills/sgme/
- 幂等覆盖部署副本；不触碰其它技能
- 写部署配置 scripts/client.env（只含地址与密钥环境变量名，**不含密钥值**）
- 写本机身份文件 ~/.sgme/workbuddy-agent.json（agent_id/http/mcp，**不含密钥**）
- 部署后可选 selfcheck，结果追加 scripts/access-log.md（部署副本内）
- 提示新开 WorkBuddy 对话生效

地址解析顺序：`--base-url` → 环境变量 → 既有部署配置 → 身份文件 →
WorkBuddy MCP 配置（~/.workbuddy/mcp.json 的 sgme server）→ 回环默认。
agent_id 解析顺序（T-233）：`--agent-id` → 环境变量 `SGME_WORKBUDDY_AGENT_ID` →
既有身份文件值 → 默认 `workbuddy`（多设备接入必须覆盖为本机注册 id）。
密钥一律不落盘：由 `SGME_WORKBUDDY_KEY` / `SGME_AGENT_KEY` 环境变量或既有的
`~/.workbuddy/mcp.json` 提供。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = SRC_DIR / "scripts"
SKILL_NAME = "sgme"

sys.path.insert(0, str(SCRIPT_DIR))
from sgme_client import (  # noqa: E402
    ADMIN_KEY_ENV,
    DEFAULT_HTTP_URL,
    DEPLOY_CONFIG_NAME,
    KEY_ENV,
    WORKBUDDY_IDENTITY_PATH,
    WORKBUDDY_KEY_ENV,
    derive_http_from_mcp,
    derive_mcp_url,
    load_deploy_config,
    load_workbuddy_identity,
    load_workbuddy_mcp_json,
    resolve_agent_id,
)


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def default_skills_root() -> Path:
    """WorkBuddy 全局技能根：环境变量 WORKBUDDY_SKILLS_ROOT 优先，否则 ~/.workbuddy/skills。"""
    env = os.environ.get("WORKBUDDY_SKILLS_ROOT")
    if env and env.strip():
        return Path(env.strip())
    return home_dir() / ".workbuddy" / "skills"


def identity_path() -> Path:
    return home_dir() / WORKBUDDY_IDENTITY_PATH


def resolve_addresses(dest: Path, base_url: str | None = None,
                      mcp_url: str | None = None) -> tuple[str, str, str]:
    """解析生效地址，返回 (http, mcp, 来源说明)。"""
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

    ident = load_workbuddy_identity()
    ident_http = (ident.get("http") or "").strip()
    if ident_http:
        http = ident_http.rstrip("/")
        mcp = (mcp_url or (ident.get("mcp") or "").strip() or derive_mcp_url(http)).rstrip("/")
        return http, mcp, f"身份文件 {WORKBUDDY_IDENTITY_PATH}"

    mcp_cfg_url = str(load_workbuddy_mcp_json().get("url") or "").strip()
    if mcp_cfg_url:
        http = derive_http_from_mcp(mcp_cfg_url)
        return http, (mcp_url or mcp_cfg_url).rstrip("/"), "WorkBuddy MCP 配置 ~/.workbuddy/mcp.json"

    http = DEFAULT_HTTP_URL
    return http, (mcp_url or derive_mcp_url(http)).rstrip("/"), "回环默认"


def write_deploy_config(dest: Path, http: str, mcp: str) -> Path:
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / DEPLOY_CONFIG_NAME
    lines = [
        "# SGME × WorkBuddy 部署配置（由 install.py 生成；只含地址与密钥变量名，不含密钥值）",
        f"SGME_HTTP_URL={http}",
        f"SGME_MCP_URL={mcp}",
        f"# 密钥读取顺序：{WORKBUDDY_KEY_ENV} → ~/{WORKBUDDY_IDENTITY_PATH} →",
        f"#   ~/.workbuddy/mcp.json 的 sgme server → {KEY_ENV}（兜底）",
        f"# 写侧/管理能力另需 {ADMIN_KEY_ENV}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def seed_identity(http: str, mcp: str, agent_id: str | None = None) -> tuple[Path, str, str, str]:
    """写本机身份文件 ~/.sgme/workbuddy-agent.json（地址 + agent_id，不含密钥）。

    agent_id 解析优先级（T-233）：显式参数（`--agent-id`）→ 环境变量
    `SGME_WORKBUDDY_AGENT_ID` → 既有身份文件值 → 默认 `workbuddy`。
    ——不再无条件回写默认常量：既有身份文件里的本机注册 id（多设备场景）
    会被保留，不被打回默认。其余自定义字段一律保留。

    **密钥铁律**：本函数刻意不写 `api_key`——WorkBuddy 场景下密钥由
    `~/.workbuddy/mcp.json` 的 sgme server 自动继承，无需再落一份盘
    （AGENTS.md 约束 10：密钥不落盘）。

    返回 `(身份文件路径, 说明, 生效 agent_id, agent_id 来源)`。
    """
    f = identity_path()
    existing = load_workbuddy_identity()
    aid, aid_src = resolve_agent_id(agent_id)
    data = dict(existing)
    data.update({"agent_id": aid, "http": http, "mcp": mcp})
    data.pop("api_key", None)          # 密钥不落盘
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    had_key = bool(str(existing.get("api_key") or "").strip())
    note = "（已移除既有 api_key：密钥不落盘）" if had_key else "（不含密钥）"
    return f, note, aid, aid_src


def install(skills_root: Path) -> Path:
    dest = skills_root / SKILL_NAME
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
    prev = log.read_text(encoding="utf-8") if log.is_file() else "# WorkBuddy × SGME 接入日志\n\n"
    log.write_text(prev + line, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署 SGME × WorkBuddy 适配器")
    ap.add_argument("--skills-root", default=None,
                    help="WorkBuddy 技能根目录（默认 ~/.workbuddy/skills）")
    ap.add_argument("--base-url", default=None, help="SGME HTTP 地址")
    ap.add_argument("--mcp-url", default=None, help="SGME MCP 地址")
    ap.add_argument("--agent-id", default=None,
                    help="本机 agent_id（服务端为本机签发的注册 id；覆盖环境变量/身份文件/默认）")
    ap.add_argument("--no-selfcheck", action="store_true", help="跳过自检")
    ap.add_argument("--no-seed-identity", action="store_true",
                    help="不写本机身份文件 ~/.sgme/workbuddy-agent.json")
    args = ap.parse_args(argv)

    root = Path(args.skills_root) if args.skills_root else default_skills_root()
    dest = install(root)
    http, mcp, src = resolve_addresses(dest, args.base_url, args.mcp_url)
    cfg = write_deploy_config(dest, http, mcp)

    print(f"部署目录: {dest}")
    print(f"HTTP: {http}  MCP: {mcp}  （来源: {src}）")
    print(f"部署配置: {cfg}")

    if args.no_seed_identity:
        aid, aid_src = resolve_agent_id(args.agent_id)
        print("身份文件: 已跳过（--no-seed-identity）")
        print(f"agent_id: {aid}（来源: {aid_src}；本次未写入身份文件）")
    else:
        ident, note, aid, aid_src = seed_identity(http, mcp, args.agent_id)
        print(f"身份文件: {ident} {note}")
        print(f"agent_id: {aid}（来源: {aid_src}）")

    print(f"密钥: {WORKBUDDY_KEY_ENV} → 身份文件 → ~/.workbuddy/mcp.json → {KEY_ENV}（均不落盘）")

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

    print(f"完成。请新开 WorkBuddy 对话以加载技能 {SKILL_NAME}。")
    print("若同名旧技能仍存在，可在技能列表中停用，避免双触发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
