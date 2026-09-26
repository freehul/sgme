# -*- coding: utf-8 -*-
"""SGME × ZCode 适配器部署脚本。

用法：
    python adapters/zcode/install.py
    python adapters/zcode/install.py --skills-root <ZCODE_SKILLS_DIR>
    python adapters/zcode/install.py --base-url http://<SGME_HOST>:9910
    python adapters/zcode/install.py --no-register     # 已有 key 时跳过 agent 注册
    python adapters/zcode/install.py --no-mcp          # 不写 ZCode MCP 配置
    python adapters/zcode/install.py --agents-md <AGENTS.md 路径>
    python adapters/zcode/install.py --no-selfcheck

行为：
- 复制 SKILL.md / README.md / scripts/{sgme_client,care_watch,selfcheck}.py
  → ~/.agents/skills/sgme-zcode/（幂等覆盖；不触碰其它技能）
- 注册 agent_id=zcode（POST /v1/admin/agents/register，需 SGME_ADMIN_KEY）；
  注册所得 key 写**部署副本** scripts/.env（仓库外，不入 git）——与 dsh/reasonix 同款
- 写部署配置 scripts/client.env（只含地址与密钥变量名，**不含密钥值**）
- 写本机身份文件 ~/.sgme/zcode-agent.json（agent_id/http/mcp，**不含密钥**）
- 默认把 `sgme` MCP server（type=http）合并写入 ~/.zcode/cli/config.json 的
  mcp.servers（写前备份；幂等；--no-mcp 跳过）——ZCode 新会话自动连接
- --agents-md 可选：向指定工作区 AGENTS.md 追加 SGME-ONBOARDING 段（已有段跳过）
- 部署后可选 selfcheck，结果追加 scripts/access-log.md（部署副本内）
- 提示新开 ZCode 会话生效（MCP 配置重启会话后自动连接）

地址解析顺序：`--base-url` → 环境变量 → 既有部署配置 → 身份文件 →
ZCode MCP 配置（~/.zcode/cli/config.json 的 sgme server）→ 回环默认。
密钥一律不落盘进仓库：注册 key 只写部署副本 .env（仓库外）；
SGME_ZCODE_KEY / SGME_AGENT_KEY 环境变量优先级见 sgme_client.resolve_agent_key。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = SRC_DIR / "scripts"
REPO_ROOT = SRC_DIR.parent.parent
SKILL_NAME = "sgme-zcode"
MCP_SERVER_NAME = "sgme"

sys.path.insert(0, str(SCRIPT_DIR))
from sgme_client import (  # noqa: E402
    ADMIN_KEY_ENV,
    AGENT_ID,
    DEFAULT_HTTP_URL,
    DEPLOY_CONFIG_NAME,
    KEY_ENV,
    ZCODE_CONFIG_PATH,
    ZCODE_IDENTITY_PATH,
    ZCODE_KEY_ENV,
    derive_http_from_mcp,
    derive_mcp_url,
    load_deploy_config,
    load_zcode_identity,
    load_zcode_mcp_config,
    resolve_agent_key,
)


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def default_skills_root() -> Path:
    """ZCode 技能根：环境变量 ZCODE_SKILLS_ROOT 优先，否则 ~/.agents/skills（user 级跨项目）。"""
    env = os.environ.get("ZCODE_SKILLS_ROOT")
    if env and env.strip():
        return Path(env.strip())
    return home_dir() / ".agents" / "skills"


def identity_path() -> Path:
    return home_dir() / ZCODE_IDENTITY_PATH


def zcode_config_path() -> Path:
    return home_dir() / ZCODE_CONFIG_PATH


def load_repo_env() -> None:
    """从仓库 config/.env 补加载 SGME_ADMIN_KEY 等（os.environ.setdefault，显式 env 优先）。

    安装脚本通常在仓库内执行；密钥铁律：只进进程环境，不写任何仓库文件。
    """
    env_file = REPO_ROOT / "config" / ".env"
    if not env_file.is_file():
        return
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


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

    ident = load_zcode_identity()
    ident_http = (ident.get("http") or "").strip()
    if ident_http:
        http = ident_http.rstrip("/")
        mcp = (mcp_url or (ident.get("mcp") or "").strip() or derive_mcp_url(http)).rstrip("/")
        return http, mcp, f"身份文件 {ZCODE_IDENTITY_PATH}"

    mcp_cfg = load_zcode_mcp_config()
    mcp_cfg_url = str(mcp_cfg.get("url") or "").strip()
    if mcp_cfg_url:
        http = derive_http_from_mcp(mcp_cfg_url)
        return http, (mcp_url or mcp_cfg_url).rstrip("/"), f"ZCode MCP 配置 ~/{ZCODE_CONFIG_PATH}"

    # ~/.sgme/install.json（SGME 服务端通用服务发现清单）兜底
    install_json = home_dir() / ".sgme" / "install.json"
    try:
        if install_json.is_file():
            data = json.loads(install_json.read_text(encoding="utf-8"))
            u = str(data.get("base_url") or "").strip()
            if u:
                return u.rstrip("/"), (mcp_url or derive_mcp_url(u)).rstrip("/"), "~/.sgme/install.json"
    except (OSError, json.JSONDecodeError):
        pass

    http = DEFAULT_HTTP_URL
    return http, (mcp_url or derive_mcp_url(http)).rstrip("/"), "回环默认"


def register_agent(http: str, admin_key: str) -> str | None:
    """注册 agent_id=zcode，返回明文 key（仅此一次落部署副本）。失败返回 None。"""
    if not admin_key:
        print(f"⚠️ 未设置 {ADMIN_KEY_ENV}，跳过 agent 注册（CLI 密钥链仍有兜底路径）")
        return None
    body = json.dumps({"agent_id": AGENT_ID, "scope": ["memory:rw"]}).encode("utf-8")
    req = urllib.request.Request(http + "/v1/admin/agents/register", data=body, method="POST")
    req.add_header("X-API-Key", admin_key)
    req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("api_key") or "") or None
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        detail = getattr(e, "read", lambda: b"")()
        print(f"⚠️ agent 注册失败：{e} {str(detail)[:120]}——跳过（可 --no-register 显式跳过）")
        return None


def write_deploy_config(dest: Path, http: str, mcp: str) -> Path:
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / DEPLOY_CONFIG_NAME
    lines = [
        "# SGME × ZCode 部署配置（由 install.py 生成；只含地址与密钥变量名，不含密钥值）",
        f"SGME_HTTP_URL={http}",
        f"SGME_MCP_URL={mcp}",
        f"# 密钥读取顺序：{ZCODE_KEY_ENV} → 部署副本 scripts/.env →",
        f"#   ~/{ZCODE_IDENTITY_PATH} → ~/{ZCODE_CONFIG_PATH} 的 sgme server → {KEY_ENV}（兜底）",
        f"# 写侧/管理能力另需 {ADMIN_KEY_ENV}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def save_deploy_key(dest: Path, key: str) -> Path | None:
    """注册所得 key 写部署副本 scripts/.env（仓库外部署产物，不入 git）。"""
    if not key:
        return None
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / ".env"
    header = "# SGME × ZCode 部署密钥（install.py 注册 agent_id=zcode 产物；仓库外，不入 git）\n"
    path.write_text(header + f"{ZCODE_KEY_ENV}={key}\n", encoding="utf-8")
    return path


def seed_identity(http: str, mcp: str) -> tuple[Path, str]:
    """写本机身份文件 ~/.sgme/zcode-agent.json（地址 + agent_id，不含密钥）。

    **密钥铁律**：本函数刻意不写 `api_key`——密钥由部署副本 .env 或环境变量提供，
    不再落一份盘（AGENTS.md 约束 10）。已有身份文件的自定义字段一律保留。
    """
    f = identity_path()
    existing = load_zcode_identity()
    data = dict(existing)
    data.update({"agent_id": AGENT_ID, "http": http, "mcp": mcp})
    data.pop("api_key", None)          # 密钥不落盘
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    had_key = bool(str(existing.get("api_key") or "").strip())
    note = "（已移除既有 api_key：密钥不落盘）" if had_key else "（不含密钥）"
    return f, note


def register_mcp_server(mcp_url: str, key: str) -> str:
    """把 sgme MCP server 合并写入 ~/.zcode/cli/config.json（幂等；写前备份）。

    格式（ZCode 约定）：type=http + url + headers；只动 `mcp.servers.sgme` 一项，
    其它 server 与配置原样保留。
    """
    if not key:
        return "跳过（无可用密钥，MCP 层需要 X-API-Key；CLI 通道不受影响）"
    cfg_path = zcode_config_path()
    cfg: dict = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return f"跳过（{cfg_path.name} 不是合法 JSON：{e}；不冒险改写）"
        bak = cfg_path.with_name(cfg_path.name + ".zcode-sgme-bak")
        try:
            shutil.copy2(cfg_path, bak)
        except OSError:
            pass
    servers = cfg.setdefault("mcp", {}).setdefault("servers", {})
    entry = {"type": "http", "url": mcp_url, "headers": {"X-API-Key": key}, "enabled": True}
    if servers.get(MCP_SERVER_NAME) == entry:
        return "sgme server 已是最新（无变更）"
    servers[MCP_SERVER_NAME] = entry
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"已写入（备份：{cfg_path.name + '.zcode-sgme-bak'}）"


AGENTS_MD_SECTION = """\
## SGME 接入纪律（SGME-ONBOARDING-v2 · ZCode）

你（ZCode）已接入长期记忆引擎 SGME（ShiGuang Memory Engine）。职责：把会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

> 技能 `sgme-zcode`（部署副本 {skills_dir}，由 install.py 同步，可随时重建）；本机 agent_id = `zcode`。
> 若本会话已挂载 `sgme` MCP server（mcp__sgme__* 工具）优先用 MCP；否则走 CLI。

- **服务发现**：环境变量 `SGME_HTTP_URL` → 部署副本 `scripts/client.env` → `~/.sgme/zcode-agent.json` → `~/.zcode/cli/config.json` 的 `sgme` server → 回环 `http://127.0.0.1:9910`
- **能力面**：`python {client} capabilities`（41 基准能力矩阵，离线）；`env-info` 看生效端点
- **五条铁律**：①每轮对话结束 append 当前轮次（session_key=zcode-<日期>-<项目>）②会话结束 refine-trigger（永远 async）③对话开始 inject/search ④关怀信号 pull→claim→ack ⑤role-list/role-assemble 换皮不换芯
- **强制查询**：涉及历史事实（之前/上次/还记得…）必须先 search 再回答，查不到如实说「记忆库中未找到」
- **三池**：待办 demand-create（project_id 一律大写）｜创意 idea-add｜立项 project-register
- **历史会话补导入**：`python <repo>/adapters/zcode/import_history.py --dry-run` 预览后去掉 --dry-run 执行
- 本段由 install.py 生成，含本机路径；克隆到其他机器后重跑 `adapters/zcode/install.py --agents-md` 刷新。
"""


def append_sgme_agents_md(agents_md: Path, skills_dir: Path, client: Path) -> bool:
    """向工作区 AGENTS.md 追加 SGME-ONBOARDING 段（已有段跳过；返回是否写入）。"""
    text = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else ""
    if "SGME-ONBOARDING" in text:
        print(f"AGENTS.md: 已有 SGME-ONBOARDING 段，跳过（{agents_md}）")
        return False
    section = AGENTS_MD_SECTION.format(skills_dir=skills_dir, client=client)
    if text and not text.endswith("\n"):
        text += "\n"
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text(text + "\n" + section, encoding="utf-8")
    print(f"AGENTS.md: 已追加 SGME-ONBOARDING 段（{agents_md}）")
    return True


def install(skills_root: Path) -> Path:
    dest = skills_root / SKILL_NAME
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "scripts").mkdir(exist_ok=True)

    for name in ("SKILL.md", "README.md"):
        src = SRC_DIR / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    for name in ("sgme_client.py", "care_watch.py", "selfcheck.py"):
        shutil.copy2(SCRIPT_DIR / name, dest / "scripts" / name)
    return dest


def append_access_log(dest: Path, note: str) -> None:
    log = dest / "scripts" / "access-log.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"- {ts} {note}\n"
    prev = log.read_text(encoding="utf-8") if log.is_file() else "# ZCode × SGME 接入日志\n\n"
    log.write_text(prev + line, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署 SGME × ZCode 适配器")
    ap.add_argument("--skills-root", default=None,
                    help="ZCode 技能根目录（默认 ~/.agents/skills）")
    ap.add_argument("--base-url", default=None, help="SGME HTTP 地址")
    ap.add_argument("--mcp-url", default=None, help="SGME MCP 地址")
    ap.add_argument("--no-register", action="store_true", help="跳过 agent 注册（已有 key 时）")
    ap.add_argument("--no-mcp", action="store_true", help="不写 ~/.zcode/cli/config.json 的 MCP 配置")
    ap.add_argument("--agents-md", default=None,
                    help="向该路径的 AGENTS.md 追加 SGME-ONBOARDING 段（可选）")
    ap.add_argument("--no-selfcheck", action="store_true", help="跳过自检")
    args = ap.parse_args(argv)

    load_repo_env()
    root = Path(args.skills_root) if args.skills_root else default_skills_root()
    dest = install(root)
    http, mcp, src = resolve_addresses(dest, args.base_url, args.mcp_url)
    cfg = write_deploy_config(dest, http, mcp)

    print(f"部署目录: {dest}")
    print(f"HTTP: {http}  MCP: {mcp}  （来源: {src}）")
    print(f"部署配置: {cfg}")

    # ---- agent 注册（拿专用 key，写部署副本 .env）----
    reg_key = None
    if args.no_register:
        print("agent 注册: 已跳过（--no-register）")
    else:
        reg_key = register_agent(http, os.environ.get(ADMIN_KEY_ENV, ""))
        if reg_key:
            key_path = save_deploy_key(dest, reg_key)
            print(f"agent 注册: agent_id={AGENT_ID} 已注册，key → {key_path}（仓库外）")
        else:
            print("agent 注册: 未完成——沿用既有密钥链（selfcheck 会验证可用性）")

    ident, note = seed_identity(http, mcp)
    print(f"身份文件: {ident} {note}")
    print(f"密钥: {ZCODE_KEY_ENV} → 部署副本 scripts/.env → ~/{ZCODE_IDENTITY_PATH} "
          f"→ ~/{ZCODE_CONFIG_PATH} → {KEY_ENV}（兜底）")

    # ---- ZCode MCP 配置（合并式，幂等）----
    if args.no_mcp:
        print("ZCode MCP 配置: 已跳过（--no-mcp）")
    else:
        mcp_key = reg_key or resolve_agent_key()[0]
        note = register_mcp_server(mcp, mcp_key)
        print(f"ZCode MCP 配置: {note}")

    if args.agents_md:
        append_sgme_agents_md(Path(args.agents_md), dest, dest / "scripts" / "sgme_client.py")

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

    print(f"完成。请新开 ZCode 会话以加载技能 {SKILL_NAME} 并自动连接 sgme MCP server。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
