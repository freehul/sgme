# -*- coding: utf-8 -*-
"""WorkBuddy 侧 SGME 适配器「部署对齐 / 契约自检」。

边界（重要）
------------
* 绝不写入 ``<project-root>`` 仓库里的任何文件；仓库只作为**只读依赖来源**。
* 真源：``<project-root>/adapters/workbuddy/tools/align_deploy.py``；
  由同目录 ``install.py`` 部署到 ``~/.workbuddy/sgme-tools/``。
* 绝不调用 SGME 写侧端点（append / skill_put / config_update 等），只读
  ``GET /v1/health`` 与 ``GET /v1/capabilities``，因此不会写生产数据。
* 所有写入都限定在 WorkBuddy 侧：``~/.workbuddy/skills/sgme/``。

用法：
    python align_deploy.py --check    # 默认，纯只读，输出契约报告；有缺口退出 1
    python align_deploy.py --apply    # 幂等修复（同步依赖 / 补能力段 / 刷配置段）

用途：把服务端 ``/v1/capabilities`` 的能力面契约、项目真源的依赖闭包、
以及部署副本的本地增补段放在一起校验（--check）或幂等修复（--apply），
用于「服务端升级后，确认 WorkBuddy 部署副本仍对齐」。

历史：``install.py`` 曾漏复制 ``skill_cache.py`` 导致 CLI 兜底 ImportError
（2026-11-06 已修，``tests/test_install.py`` 有回归锁）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = HOME / ".workbuddy" / "sgme-deploy.json"

SKILL_BEGIN = "<!-- BEGIN WB-ALIGN-ADDENDUM -->"
SKILL_END = "<!-- END WB-ALIGN-ADDENDUM -->"
ENV_BEGIN = "# ---- BEGIN WB-ALIGN-CONTRACT ----"
ENV_END = "# ---- END WB-ALIGN-CONTRACT ----"

MATRIX_RE = re.compile(r"^\|\s*\d+\s*\|\s*([a-z_]+)\s*\|", re.M)
SELFCHECK_OLD_RE = re.compile(r'\("静态 ① 基准能力矩阵（\d+ 个）",')
SELFCHECK_NEW = '(f"静态 ① 基准能力矩阵（{len(BASELINE_TOOLS)} 个）",'


def expand(value: str) -> Path:
    return Path(value).expanduser()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def http_json(url: str, key: str, timeout: float = 8.0) -> dict:
    """只读 GET；显式禁用系统代理，避免内网请求被劫持。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def block_content(text: str, begin: str, end: str) -> str:
    if begin in text and end in text:
        return text.split(begin, 1)[1].split(end, 1)[0]
    return ""


def strip_block(text: str, begin: str, end: str) -> str:
    if begin in text and end in text:
        pre, rest = text.split(begin, 1)
        _, post = rest.split(end, 1)
        return pre.rstrip("\n") + "\n" + post.lstrip("\n")
    return text


def replace_block(text: str, begin: str, end: str, block: str) -> str:
    """幂等：先摘掉旧段，再以固定格式追加；重复调用结果逐字节一致。"""
    return strip_block(text, begin, end).rstrip("\n") + "\n\n" + block.strip() + "\n"


def load_baseline(deploy: Path) -> tuple[list[str], dict]:
    """从部署副本动态加载能力基线（不硬编码工具名）。"""
    scripts = str(deploy / "scripts")
    sys.path.insert(0, scripts)
    try:
        sys.modules.pop("sgme_client", None)
        sc = importlib.import_module("sgme_client")
        return list(sc.BASELINE_TOOLS), dict(getattr(sc, "BASELINE_SPEC", {}))
    finally:
        try:
            sys.path.remove(scripts)
        except ValueError:
            pass


def build_skill_block(missing: list[str], spec: dict, start_no: int, ctx: dict) -> str:
    head = [
        SKILL_BEGIN,
        "",
        "## 能力面对齐状态（WorkBuddy 部署侧）",
        "",
        "> 本段由 `~/.workbuddy/sgme-tools/align_deploy.py` 幂等维护，请勿手改。",
        f"> 服务端 SGME {ctx['version']} · api_compat={ctx['compat']} · "
        f"schema_version={ctx['schema']}；工具面 {ctx['server_tools']} = 会话面 "
        f"{len(ctx['baseline'])} + 管理面豁免 {len(ctx['exempt'])}。",
        "> 优先用本会话已挂载的 MCP 同名工具；无 MCP 或需脚本化时走 CLI。"
        "管理面 4 项按设计豁免，不列入本表。",
        "",
    ]
    if not missing:
        # 项目真源 SKILL.md 已覆盖全部会话面能力时，只留状态说明，不再画空表。
        head += [
            f"当前 SKILL.md 能力矩阵已覆盖全部 {len(ctx['baseline'])} 项会话面能力，无缺口。",
            "",
            SKILL_END,
        ]
        return "\n".join(head)
    head += ["| # | 能力 | 层 | CLI 命令 / MCP 工具 |", "|---|---|---|---|"]
    for i, tool in enumerate(missing, start_no):
        method, cli, layer, scope = spec.get(
            tool, (tool, tool.replace("_", "-"), "MCP", "")
        )
        head.append(f"| {i} | {tool} | {str(layer).upper()} | `{cli}` / `{tool}` |")
    head += ["", SKILL_END]
    return "\n".join(head)


def build_env_block(ctx: dict) -> str:
    return "\n".join([
        ENV_BEGIN,
        "# 本段由 align_deploy.py 维护；install.py 重跑后请执行 --apply 校验。",
        "# 部署契约配置: ~/.workbuddy/sgme-deploy.json",
        "# 对齐/自检: python ~/.workbuddy/sgme-tools/align_deploy.py --check | --apply",
        f"# 服务端: SGME {ctx['version']} / api_compat={ctx['compat']} / "
        f"schema_version={ctx['schema']}",
        f"# 能力面: {ctx['server_tools']} 工具 = 会话面 {len(ctx['baseline'])} "
        f"+ 管理面豁免 {len(ctx['exempt'])}",
        "# SKILL.md 矩阵: " + f"{ctx['base_matrix']} 项"
        + (f" + 补充段 {len(ctx['missing'])} 项" if ctx["missing"] else "（无缺口）"),
        "# 依赖脚本: " + ", ".join(sorted(ctx["src_names"])),
        "# 密钥仍按上方顺序解析，不落盘；本段不含任何密钥。",
        ENV_END,
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 侧 SGME 适配器部署对齐")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="纯只读契约自检（默认）")
    g.add_argument("--apply", action="store_true", help="幂等修复 WorkBuddy 侧部署副本")
    args = ap.parse_args()
    apply_mode = bool(args.apply)

    if not CONFIG_PATH.is_file():
        print(f"[FAIL] 契约配置不存在: {CONFIG_PATH}")
        return 2
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    paths = {k: expand(v) for k, v in cfg["paths"].items()}
    deploy = paths["deploy_dir"]
    repo_scripts = paths["repo_root"] / Path(cfg["paths"]["adapter_src"]) / "scripts"
    mcp_path = paths["mcp_config"]
    identity_path = paths["identity"]

    # ---- 服务端只读契约 ----
    svc = cfg["service"]
    mcp_cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
    server_cfg = mcp_cfg.get("mcpServers", {}).get("sgme", {})
    key = ""
    for name, val in (server_cfg.get("headers") or {}).items():
        if name.lower() == "x-api-key":
            key = str(val)
            break

    caps, health, svc_error = {}, {}, ""
    try:
        caps = http_json(svc["http"].rstrip("/") + svc["capabilities_endpoint"], key)
        health = http_json(svc["http"].rstrip("/") + svc["health_endpoint"], key)
    except Exception as exc:  # noqa: BLE001 - 离线时仍要能跑静态检查
        svc_error = f"{type(exc).__name__}: {exc}"[:160]

    version = str((caps.get("server") or {}).get("version") or health.get("version") or "?")
    compat = str(caps.get("api_compat") or "?")
    schema = str(caps.get("schema_version") or "?")

    # ---- 客户端能力基线 ----
    try:
        baseline, spec = load_baseline(deploy)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 无法加载部署副本的 sgme_client.py 能力基线: {exc}")
        print("       先跑 install.py，再跑本脚本 --apply 补齐 skill_cache.py 依赖")
        return 2

    skill_path = deploy / "SKILL.md"
    env_path = deploy / "scripts" / "client.env"
    selfcheck_path = deploy / "scripts" / "selfcheck.py"
    skill_text = skill_path.read_text(encoding="utf-8")
    env_text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    selfcheck_text = selfcheck_path.read_text(encoding="utf-8") if selfcheck_path.is_file() else ""

    base_matrix = set(MATRIX_RE.findall(strip_block(skill_text, SKILL_BEGIN, SKILL_END)))
    missing = [t for t in baseline if t not in base_matrix]
    cc = cfg["capability_contract"]
    exempt = list(cc["admin_exempt"])
    server_tools = len(baseline) + len(exempt)

    ctx = {
        "version": version, "compat": compat, "schema": schema,
        "server_tools": cc["server_tools"], "baseline": baseline, "exempt": exempt,
        "base_matrix": len(base_matrix), "missing": missing,
        "src_names": [p.name for p in repo_scripts.glob("*.py")] if repo_scripts.is_dir() else [],
    }

    # ---- 依赖闭包 ----
    required = list(cfg["required_scripts"])
    dep_missing = [n for n in required if not (deploy / "scripts" / n).is_file()]
    src_py = {p.name: p for p in repo_scripts.glob("*.py")} if repo_scripts.is_dir() else {}
    local_patched = set(cfg.get("local_patched_scripts", []))
    dep_stale = sorted(
        n for n, src in src_py.items()
        if n not in local_patched
        and (deploy / "scripts" / n).is_file()
        and sha256(deploy / "scripts" / n) != sha256(src)
    )

    # ---- 配置一致性 ----
    expect = cfg["mcp_server_expect"]
    cfg_issues = []
    if not key:
        cfg_issues.append("mcp.json 的 sgme server 缺 X-API-Key")
    if server_cfg.get("type") != expect["type"]:
        cfg_issues.append(f"type 应为 {expect['type']}")
    if str(server_cfg.get("url", "")).rstrip("/") != svc["mcp"].rstrip("/"):
        cfg_issues.append("url 与契约配置的 service.mcp 不一致")
    try:
        if int(server_cfg.get("timeout", 0)) < int(expect["timeout_min_seconds"]):
            cfg_issues.append("timeout 低于契约下限")
    except (TypeError, ValueError):
        cfg_issues.append("timeout 缺失或非法")
    identity = {}
    if identity_path.is_file():
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg_issues.append("身份文件不是合法 JSON")
    for fld, want in (("http", svc["http"]), ("mcp", svc["mcp"])):
        if str(identity.get(fld, "")).rstrip("/") != want.rstrip("/"):
            cfg_issues.append(f"身份文件 {fld} 与契约配置不一致")

    # ---- 本地增补状态 ----
    skill_block = block_content(skill_text, SKILL_BEGIN, SKILL_END)
    skill_block_ok = bool(skill_block) and all(t in skill_block for t in missing)
    env_block_ok = ENV_BEGIN in env_text and ENV_END in env_text
    selfcheck_dynamic = SELFCHECK_NEW in selfcheck_text
    selfcheck_pending = bool(SELFCHECK_OLD_RE.search(selfcheck_text))

    rows: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        rows.append((name, ok, detail))

    add("服务端只读可达", bool(caps) and health.get("status") == "ok",
        f"SGME {version} / api_compat={compat} / schema={schema}"
        + (f" / health={health.get('status')}" if health else f" / ERROR {svc_error}"))
    add("能力面总量契约", server_tools == cc["server_tools"],
        f"{server_tools} = 会话面 {len(baseline)} + 管理面豁免 {len(exempt)}；期望 {cc['server_tools']}")
    add("mcp.json + 身份文件", not cfg_issues,
        "一致（密钥来源 WorkBuddy MCP 配置，未落盘）" if not cfg_issues else "; ".join(cfg_issues))
    add("依赖脚本闭包", not dep_missing and not dep_stale,
        "齐全且与真源一致" if not dep_missing and not dep_stale
        else f"缺失={dep_missing} 落后={dep_stale}")
    add("SKILL.md 能力矩阵", not missing or skill_block_ok,
        f"基础 {len(base_matrix)} 项 + 补充段 {len(missing)} 项 → 覆盖 {len(baseline)} 项"
        + ("" if skill_block_ok else "（补充段缺失或不完整）"))
    add("client.env 契约段", env_block_ok,
        "已写入" if env_block_ok else "缺失（--apply 可补）")
    add("selfcheck 动态标题", selfcheck_dynamic,
        "已按 len(BASELINE_TOOLS) 动态显示" if selfcheck_dynamic else
        ("仍为硬编码，--apply 可修" if selfcheck_pending else "未检出可修模式"))

    # ---- --apply：只写 WorkBuddy 侧 ----
    if apply_mode:
        changes: list[str] = []
        for name, src in sorted(src_py.items()):
            if name.startswith("_"):
                continue
            dst = deploy / "scripts" / name
            if name in local_patched:
                # 这些文件带 WorkBuddy 侧本地增强，不能拿真源裸覆盖。
                # 用「真源 + 补丁」的期望内容比对，保证结果幂等且不反复写盘。
                if name == "selfcheck.py":
                    expected = SELFCHECK_OLD_RE.sub(
                        lambda _m: SELFCHECK_NEW, src.read_text(encoding="utf-8")
                    )
                    cur_text = dst.read_text(encoding="utf-8") if dst.is_file() else ""
                    if cur_text != expected:
                        dst.write_text(expected, encoding="utf-8")
                        changes.append(f"同步依赖+本地补丁 {name}")
                continue
            if not dst.is_file() or sha256(dst) != sha256(src):
                shutil.copy2(src, dst)
                changes.append(f"同步依赖 {name}")
        # 依赖同步可能覆盖 selfcheck.py，补丁必须在其后重打
        cur = selfcheck_path.read_text(encoding="utf-8")
        fixed = SELFCHECK_OLD_RE.sub(lambda _m: SELFCHECK_NEW, cur)
        if fixed != cur:
            selfcheck_path.write_text(fixed, encoding="utf-8")
            changes.append("selfcheck 标题改为动态")
        cur = skill_path.read_text(encoding="utf-8")
        fixed = replace_block(cur, SKILL_BEGIN, SKILL_END,
                              build_skill_block(missing, spec, len(base_matrix) + 1, ctx))
        if fixed != cur:
            skill_path.write_text(fixed, encoding="utf-8")
            changes.append(f"SKILL.md 补充段（{len(missing)} 项）")
        cur = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
        fixed = replace_block(cur, ENV_BEGIN, ENV_END, build_env_block(ctx))
        if fixed != cur:
            env_path.write_text(fixed, encoding="utf-8")
            changes.append("client.env 契约段")
        print(f"[APPLY] {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        for c in changes:
            print(f"  [OK] {c}")
        if not changes:
            print("  [--] 已是最新，无需改动")
        print("  提示：install.py 重跑后建议再跑一次 --apply，校验服务端能力面\n")
        # 用全新进程复检，确保报告反映 apply 之后的状态而非旧状态
        import subprocess
        recheck = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        sys.stdout.write(recheck.stdout)
        if recheck.stderr:
            sys.stderr.write(recheck.stderr)
        return recheck.returncode

    ok_all = all(r[1] for r in rows)
    mode = "APPLY+CHECK" if apply_mode else "CHECK"
    print(f"=== WorkBuddy x SGME 部署契约自检 [{mode}] ===")
    print(f"副本: {deploy}")
    print(f"服务端: SGME {version} | api_compat={compat} | schema_version={schema}")
    print("")
    for name, ok, detail in rows:
        print(f"  [{'OK' if ok else 'FAIL'}] {name:<22} {detail}")
    print("")
    print("契约配置: " + str(CONFIG_PATH))
    print("对齐脚本: " + str(Path(cfg["align"]["script"]).expanduser()))
    print("说明: 不写 SGME 仓库、不写生产数据；服务端仅 GET health/capabilities")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
