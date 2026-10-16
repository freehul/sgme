# -*- coding: utf-8 -*-
"""check：适配器分发新鲜度门禁（T-226 / T-227 防复发）。

为什么需要本脚本
----------------
两起已确认事故的共同机理是「真源改了、派生产物没跟上」，且发生时没有任何
机器可见的信号（vitest 全 mock 永不加载 lib/"本地过、产物旧"）：

- P1-9（T-226）：``adapters/dsh/sgme-bridge/src/tools.ts`` 加了 conversation_search，
  但 ``lib/index.js`` 产物没重建 —— DSH 运行时能力缺失，静默 5 天。
- P1-10（T-227）：``adapters/hermes/plugin.yaml`` 升到 1.7.0，
  ``skills/adapter-hermes/`` 分发副本停在 1.5.0 —— A2 通道安装拿到半新半旧包。

本脚本把「派生产物是否新鲜」变成机器可见，两条独立防线：

1. 分发副本新鲜度（八个 ``skills/adapter-<host>/``）
   复用 ``scripts/publish_adapter_skills.py`` 的生成逻辑，把真源重新打包到临时目录，
   与已提交的分发副本逐文件比对（文件集合 + 内容哈希，CRLF→LF 归一）。任何差异——
   缺文件 / 多文件 / 内容不同——都是漂移。等价于「现在重跑一遍 publish 会不会产生 diff」。

2. DSH 桥接产物品新鲜度（``adapters/dsh/sgme-bridge/lib/``）
   - ``src/*.ts`` 里出现的全部 ``name: '…'`` 字面量（工具/命令注册名）集合，
     必须与 ``lib/index.js`` 中同一提取口径的结果**完全相等**（缺 = 忘了 build，
     多 = 产物比 src 新，都会让 parity/运行时与源码对不上）；
   - ``src/*.ts`` 每个非空模块必须在 ``lib/index.js`` 里有 ``//#region src/<file>``
     标记（tsdown 逐模块打包标记）—— 防「新增源文件忘了 build」。

用法::

    python scripts/check_adapter_freshness.py            # 人类可读
    python scripts/check_adapter_freshness.py --json     # 机器可读

退出码：0 = 全部新鲜（部署缺口只警告）；1 = 存在漂移或部署缺口（--strict-deploy）。

3. 部署态契约（防线 3，2026-10-17 加，T-292）
   前两道防线只管「仓库内的派生产物」——部署到宿主 profile 里的那份**没有人看**。
   2026-10-17 实锤：T-277 重建了 lib 与分发副本，但没有 bump package.json 版本，
   于是 npm latest 与 profile 部署副本都停在旧内容——本机 DSH 运行版少
   `credential_list` / `credential_get` 两个工具，两道防线全绿，无人察觉。
   防线 3 把「部署副本缺基准工具」变成机器可见：
   - 部署目录解析顺序：`--deploy <dir>` > `SGME_DSH_DEPLOY_DIR` > `~/.dsh/profiles/desktop/node_modules/dsh-sgme`
     （零配置解析到本机 profile；目录不存在 = 跳过并提示，不影响 CI 与无部署的机器）；
   - 判据 = **契约级**：MCP 基准工具集（`scripts/adapter_parity.py` 的 baseline）
     去掉 dsh 的架构豁免（`append` / `agent_onboarding`）后，必须全部出现在部署产物里；
     再多一层「部署 vs 仓库产物逐文件哈希」——不一致即提示重刷（副本漂移）。
   部署落后于仓库是**正常开发时序**（先合仓库、再发布、再刷新宿主），因此默认只警告、
   不算漂移；发布门禁用 `--strict-deploy` 让缺口同样失败。

修复方式：① 分发副本漂移 → ``python scripts/publish_adapter_skills.py``；
② dsh 产物漂移 → ``pnpm -C adapters/dsh/sgme-bridge run build`` 后提交 lib；
③ 部署缺口 → bump 版本 + 发布（npm）/ 刷宿主 profile 后重启宿主。
注意顺序：先 build 再 publish（publish 从真源打包，产物新了副本才会跟着新）。
"""
from __future__ import annotations
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BRIDGE_REL = Path("adapters/dsh/sgme-bridge")

# name 字面量口径：src 用单引号、bundle 用双引号，统一同时接受；
# 两端必须用同一个正则，保证「集合相等」判定对称。
NAME_RE = re.compile(r"name:\s*['\"]([a-z][a-z0-9_]*)['\"]")
REGION_PREFIX = "//#region "

# 防线 3：部署态契约（见模块 docstring）。三条口径写死在这里，避免各处漂移：
# - 部署目录默认候选（~ 展开在运行时做，便于测试注入）；
# - dsh 的架构豁免工具（`adapter_parity.py` exemptions.dsh 同口径）；
# - 部署产物相对部署目录的路径（npm 包内固定布局）。
DEPLOY_DIR_ENV = "SGME_DSH_DEPLOY_DIR"
DEFAULT_DEPLOY_REL = Path(".dsh") / "profiles" / "desktop" / "node_modules" / "dsh-sgme"
DEPLOY_ENTRY_REL = Path("lib") / "index.js"
DSH_EXEMPT_TOOLS = ("append", "agent_onboarding")

# 名称不同但语义等价的映射：唯一真源 = scripts/adapter_parity_map.yaml 的 aliases 段
# （`search` ↔ dsh 的 `memory_search`）——在这里再抄一份就会漂移，故运行时读。
PARITY_MAP_REL = Path("scripts") / "adapter_parity_map.yaml"


def _load_publish_module(repo_root: Path):
    """按路径加载 publish_adapter_skills.py（隔离实例，避免污染其它导入方）。"""
    script = repo_root / "scripts" / "publish_adapter_skills.py"
    spec = importlib.util.spec_from_file_location(
        "sgme_publish_adapter_skills_freshness", script
    )
    assert spec is not None and spec.loader is not None, f"无法加载 {script}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _normalized_digest(data: bytes) -> str:
    """内容哈希（CRLF→LF 归一）。

    本仓 Windows 工作副本受 core.autocrlf 影响，同一提交的文本文件在不同时刻
    可能是 CRLF 或 LF 落盘（git checkouts 会改写行尾，而 publish/构建产物是
    工具直写）。行尾差异经 git 归一后是无意义差异，门禁必须跨行尾口径稳定——
    否则会出现「什么都没改却红」的假警报。语义差异照旧检出。
    """
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _tree_hashes(root: Path, is_junk) -> dict[str, str]:
    """目录树 → {相对路径: sha256}；junk（__pycache__ 等）与发布脚本同口径过滤。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_junk(rel):
            continue
        out[rel.as_posix()] = _normalized_digest(p.read_bytes())
    return out


def check_skill_copies(repo_root: Path = ROOT, hosts=None) -> list[str]:
    """防线 1：八个分发副本 vs 真源重新打包的逐文件比对。返回漂移清单。"""
    pub = _load_publish_module(repo_root)
    findings: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="sgme-freshness-"))
    try:
        pub.ADAPTERS = repo_root / "adapters"  # type: ignore[attr-defined]
        pub.SKILLS = tmp  # type: ignore[attr-defined]
        for host in (hosts or pub.HOSTS):
            r = pub.publish_host(host)
            if not r.get("ok"):
                findings.append(
                    f"[skills/adapter-{host}] 真源无法打包：{r.get('error')}"
                )
                continue
            expected = _tree_hashes(tmp / f"adapter-{host}", pub._is_junk)
            actual = _tree_hashes(repo_root / "skills" / f"adapter-{host}", pub._is_junk)
            if not actual:
                findings.append(
                    f"[skills/adapter-{host}] 分发副本缺失 → 重跑 "
                    f"python scripts/publish_adapter_skills.py {host}"
                )
                continue
            for rel in sorted(set(expected) - set(actual)):
                findings.append(
                    f"[skills/adapter-{host}] 缺文件 {rel}（真源有、副本无）→ 重跑 publish"
                )
            for rel in sorted(set(actual) - set(expected)):
                findings.append(
                    f"[skills/adapter-{host}] 多文件 {rel}（副本有、真源无）→ 重跑 publish"
                )
            for rel in sorted(set(expected) & set(actual)):
                if expected[rel] != actual[rel]:
                    findings.append(
                        f"[skills/adapter-{host}] 内容过期 {rel}（与真源重新打包结果不一致）"
                        f" → 重跑 publish"
                    )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return findings


def tool_names_in_file(path: Path) -> set[str]:
    """从一个产物文件里提取 name 字面量（工具/命令注册名）。解析不到返回空集。"""
    if not path.is_file():
        return set()
    return set(NAME_RE.findall(path.read_text(encoding="utf-8")))


def check_dsh_bundle(repo_root: Path = ROOT) -> list[str]:
    """防线 2：dsh 桥接 lib 产物与 src 的同步性。返回漂移清单。"""
    bridge = repo_root / BRIDGE_REL
    src_dir = bridge / "src"
    lib_js = bridge / "lib" / "index.js"
    lib_dts = bridge / "lib" / "index.d.ts"
    findings: list[str] = []

    if not src_dir.is_dir():
        return [f"[dsh-bridge] 缺 src 目录：{src_dir}"]
    if not lib_js.is_file():
        return [f"[dsh-bridge] 缺 lib/index.js 产物 → pnpm -C {BRIDGE_REL.as_posix()} run build"]
    if not lib_dts.is_file():
        findings.append(
            f"[dsh-bridge] 缺 lib/index.d.ts 类型产物 → pnpm -C {BRIDGE_REL.as_posix()} run build"
        )

    src_files = sorted(src_dir.rglob("*.ts"))
    src_text = "\n".join(p.read_text(encoding="utf-8") for p in src_files)
    lib_text = lib_js.read_text(encoding="utf-8")
    src_names = set(NAME_RE.findall(src_text))
    lib_names = tool_names_in_file(lib_js)

    # fail-closed：解析到 0 个说明提取口径失效，必须失败而不是静默通过
    if not src_names:
        findings.append("[dsh-bridge] src 解析到 0 个 name 字面量（提取规则失效？）")
    if not lib_names:
        findings.append("[dsh-bridge] lib/index.js 解析到 0 个 name 字面量（提取规则失效？）")

    for n in sorted(src_names - lib_names):
        findings.append(
            f"[dsh-bridge] lib 缺 {n!r}（src 有、产物无）→ 改了 src 忘了 pnpm build"
        )
    for n in sorted(lib_names - src_names):
        findings.append(
            f"[dsh-bridge] lib 多 {n!r}（产物有、src 无）→ 产物与 src 不同步，需重建"
        )

    for p in src_files:
        if not p.read_text(encoding="utf-8").strip():
            continue  # 空模块无产物可期待
        rel = p.relative_to(bridge).as_posix()
        if (REGION_PREFIX + rel) not in lib_text:
            findings.append(
                f"[dsh-bridge] lib 无 {REGION_PREFIX}{rel} 标记 → 该源文件未进产物"
                f"（改了 src 忘了 build）"
            )
    return findings


# ---------------- 防线 3：部署态契约（T-292） ----------------

def mcp_baseline_tools(repo_root: Path = ROOT) -> set[str]:
    """从 `sgme/mcp_server.py` 解析 MCP 工具面（与 adapter_parity.py 同口径）。

    解析不到 = 提取口径失效，返回空集由调用方 fail-closed 处理。
    """
    server = repo_root / "sgme" / "mcp_server.py"
    if not server.is_file():
        return set()
    return set(re.findall(r'\{"name":\s*"([a-z_]+)"', server.read_text(encoding="utf-8")))


def dsh_alias_map(repo_root: Path = ROOT) -> dict[str, str]:
    """解析 adapter_parity_map.yaml 的 aliases 段里 dsh 的等价名映射。

    手工微解析（只取 `  <基准名>:` 下一行里的 `    dsh: <名>`），避免为一个映射
    引入 YAML 依赖；解析不到返回空映射（退化为「按名字严格比对」）。
    """
    path = repo_root / PARITY_MAP_REL
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 2 and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
        elif indent == 4 and current and line.strip().startswith("dsh:"):
            out[current] = line.split(":", 1)[1].strip()
    return out


def resolve_deploy_dir(explicit=None) -> Path | None:
    """解析部署目录：参数 > 环境变量 > 本机默认 profile。都不存在返回 None（跳过）。

    None ≠ 失败：CI、以及没装 dsh 插件的机器上本来就没有部署副本，跳过即可；
    只有「目录存在但契约不满足」才产生缺口。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(DEPLOY_DIR_ENV)
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / DEFAULT_DEPLOY_REL)
    for c in candidates:
        if (c / DEPLOY_ENTRY_REL).is_file():
            return c
    return None


def check_deploy_contract(repo_root: Path = ROOT, deploy_dir=None) -> list[str]:
    """防线 3：部署副本是否满足能力面契约。返回缺口清单（空 = 无缺口）。

    两类判据：
    - 契约级：MCP 基准工具（去 dsh 架构豁免）必须全部在部署产物里——缺 = 部署落后；
    - 哈希级：部署产物与仓库产物逐文件一致——不一致 = 部署副本内容漂移。
    """
    if deploy_dir is None:
        return []
    entry = deploy_dir / DEPLOY_ENTRY_REL
    if not entry.is_file():
        return [f"[deploy] 部署目录无产物 {entry} → 检查部署路径"]

    baseline = mcp_baseline_tools(repo_root)
    if not baseline:
        return ["[deploy] MCP 基线解析到 0 个工具（提取规则失效？）→ fail-closed"]
    aliases = dsh_alias_map(repo_root)
    contract = sorted(baseline - set(DSH_EXEMPT_TOOLS))

    deployed = tool_names_in_file(entry)
    if not deployed:
        return ["[deploy] %s 解析到 0 个 name 字面量（提取规则失效？）→ fail-closed" % entry]

    gaps = []
    for name in contract:
        expect = aliases.get(name, name)
        if expect in deployed:
            continue
        gaps.append(
            "[deploy] 部署副本缺工具 %r（MCP 基准 %r 有、%s 无）"
            " → bump 版本 + 发布后刷新宿主 profile 并重启"
            % (expect, name, deploy_dir.name)
        )

    repo_entry = repo_root / BRIDGE_REL / DEPLOY_ENTRY_REL
    if repo_entry.is_file():
        a = _normalized_digest(entry.read_bytes())
        b = _normalized_digest(repo_entry.read_bytes())
        if a != b:
            gaps.append(
                f"[deploy] 部署产物与仓库产物内容不一致（{deploy_dir.name} 与 "
                f"{BRIDGE_REL.as_posix()}/lib/index.js 哈希不同）→ 重新安装宿主插件"
            )
    return gaps


def check(repo_root: Path = ROOT, deploy_dir=None) -> dict:
    """执行三道防线，返回 {"ok": bool, "findings": [...], "gaps": [...], "deploy": str|None}。

    - findings：仓库内派生产物漂移（分发副本 / dsh lib）——必须修，ok=False；
    - gaps：部署态缺口（部署落后于仓库）——正常开发时序里允许存在，默认只警告；
      发布门禁用 `--strict-deploy` 把它也算失败（与 adapter_parity 的 pending 口径一致）。
    """
    findings = check_skill_copies(repo_root) + check_dsh_bundle(repo_root)
    deploy = resolve_deploy_dir() if deploy_dir is None else deploy_dir
    gaps = check_deploy_contract(repo_root, deploy)
    return {
        "ok": not findings,
        "findings": findings,
        "gaps": gaps,
        "deploy": str(deploy) if deploy else None,
    }


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，emoji/中文输出会 UnicodeEncodeError 把门禁自己弄红
    # （2026-10-17 实锤：数据面 ok=True 却 exit 1）——输出统一走 UTF-8，降级不报错。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="SGME 适配器分发新鲜度门禁")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--root", default=str(ROOT), help="仓库根（默认本文件所在仓库）")
    ap.add_argument(
        "--deploy",
        default=None,
        help="宿主部署目录（默认取 SGME_DSH_DEPLOY_DIR 环境变量，再回落 ~/.dsh/profiles/desktop/node_modules/dsh-sgme）",
    )
    ap.add_argument(
        "--strict-deploy",
        action="store_true",
        help="部署态缺口也算失败（发布门禁用；默认只警告）",
    )
    args = ap.parse_args(argv)

    res = check(Path(args.root), resolve_deploy_dir(args.deploy))
    failed = bool(res["findings"]) or (args.strict_deploy and bool(res["gaps"]))
    if args.json:
        res["failed"] = failed
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res["findings"]:
            print("❌ 适配器分发漂移 %d 项：" % len(res["findings"]))
            for f in res["findings"]:
                print("  · %s" % f)
        else:
            print("✅ 适配器分发新鲜：八副本与真源一致；dsh lib 与 src 同步")
        if res["gaps"]:
            mark = "❌" if args.strict_deploy else "⚠️"
            print("%s 部署态缺口 %d 项（部署 %s）：" % (mark, len(res["gaps"]), res["deploy"]))
            for g in res["gaps"]:
                print("  · %s" % g)
        elif res["deploy"]:
            print("✅ 部署态契约满足：%s" % res["deploy"])
        else:
            print("⏭  部署态检查跳过：未找到宿主部署目录（CI / 未装插件）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
