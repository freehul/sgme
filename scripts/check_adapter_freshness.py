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

本脚本把「派生产物是否新鲜」变成机器可见，四条独立防线：

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
   ``credential_list`` / ``credential_get`` 两个工具，两道防线全绿，无人察觉。
   防线 3 把「部署副本缺基准工具」变成机器可见：
   - 部署目录解析顺序：``--deploy <dir>`` > ``SGME_DSH_DEPLOY_DIR`` > ``~/.dsh/profiles/desktop/node_modules/dsh-sgme``
     （零配置解析到本机 profile；目录不存在 = 跳过并提示，不影响 CI 与无部署的机器）；
   - 判据 = **契约级**：MCP 基准工具集（``scripts/adapter_parity.py`` 的 baseline）
     去掉 dsh 的**永久豁免**后，必须全部出现在部署产物里；豁免**唯一真源 =
     ``scripts/adapter_parity_map.yaml`` 的 ``exemptions.<tool>.dsh``**（与能力面
     对账同源，本文件不再抄第二份）；再多一层「部署 vs 仓库产物逐文件哈希」——
     不一致即提示重刷（副本漂移）。
   部署落后于仓库是**正常开发时序**（先合仓库、再发布、再刷新宿主），因此默认只警告、
   不算漂移；发布门禁用 ``--strict-deploy`` 让缺口同样失败。

   ⚠️ 2026-11-03 修正两处假阳性（T-446，14 项缺口里 4 项是假的）：
   ① 豁免曾是写死两项 ``(append, agent_onboarding)``，漏了 parity map 里 dsh 的
      4 个管理面豁免（skill_candidate_pending / list / adopt / review）→ 4 个假缺口；
   ② 旧别名解析「按缩进扫全 YAML」，把 exemptions 段里的 ``dsh: 同上（…）`` 中文
      理由当成工具名参与契约比对 → 输出里出现中文"工具"、缺口语义也错。
   现在豁免与别名都来自 parity map（``adapter_parity.load_map``），单一真源。

   修复方式：① 分发副本漂移 → ``python scripts/publish_adapter_skills.py``；
   ② dsh 产物漂移 → ``pnpm -C adapters/dsh/sgme-bridge run build`` 后提交 lib；
   ③ 部署缺口 → bump 版本 + 发布（npm）/ 刷宿主 profile 后重启宿主。
   注意顺序：先 build 再 publish（publish 从真源打包，产物新了副本才会跟着新）。

4. 部署副本运行时依赖可解析（防线 4，2026-11-06 加）
   2026-11-06 实锤：宿主升级脚本手工解包 tgz 到 profile/.pnpm 并建 junction，
   只搬插件本体、不装插件的运行时依赖（schemastery / dev）→ DSH 启动 load 插件时
   ``ERR_MODULE_NOT_FOUND: schemastery``，整条 sgme 插件被跳过、SGME 工具全部不可用，
   而防线 3 全绿（产物哈希与工具面都对，只是跑不起来）。防线 4 把「部署副本声明的
   运行时依赖能否从插件目录解析」变成机器可见；缺口默认只警告，``--strict-deploy``
   让它失败。修复方式：用官方 ``dsh plugin --profile <name> add file:<tgz>`` 重装，
   不要手工解包 tgz——pnpm 会同时对齐 lockfile 与依赖链接。
"""
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
# - dsh 的永久豁免唯一真源 = parity map 的 exemptions 段（见 dsh_exempt_tools）；
# - 部署产物相对部署目录的路径（npm 包内固定布局）。
DEPLOY_DIR_ENV = "SGME_DSH_DEPLOY_DIR"
DEFAULT_DEPLOY_REL = Path(".dsh") / "profiles" / "desktop" / "node_modules" / "dsh-sgme"
DEPLOY_ENTRY_REL = Path("lib") / "index.js"
DEPLOY_PACKAGE_REL = Path("package.json")
ADAPTER_NAME = "dsh"

# parity map 缺失 / 不可解析时的兜底豁免：保持历史口径，避免极简夹具或未同步仓库
# 因缺文件把 append / agent_onboarding 误报成缺口。正常路径读 map，见 dsh_exempt_tools()。
DSH_EXEMPT_TOOLS = ("append", "agent_onboarding")

# 名称不同但语义等价的映射（aliases 段）与永久豁免（exemptions 段）：
# 唯一真源 = scripts/adapter_parity_map.yaml；在这里再抄一份就会漂移，故运行时读，
# 且复用 adapter_parity.load_map（与能力面对账同源）。
PARITY_MAP_REL = Path("scripts") / "adapter_parity_map.yaml"
PARITY_SCRIPT_REL = Path("scripts") / "adapter_parity.py"

_PARITY_CACHE: dict[str, dict] = {}


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


def _load_parity_module(repo_root: Path):
    """按路径加载 adapter_parity.py（隔离实例）。

    复用它的 ``load_map`` 读 parity map，保证本门禁与能力面对账**读同一份真源**：
    豁免/别名不在这里维护第二份，parity map 改了这边自动跟上。
    """
    script = repo_root / PARITY_SCRIPT_REL
    spec = importlib.util.spec_from_file_location(
        "sgme_adapter_parity_freshness", script
    )
    assert spec is not None and spec.loader is not None, f"无法加载 {script}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parity_map(repo_root: Path = ROOT) -> dict:
    """读 adapter_parity_map.yaml；读不到/解析失败返回 {}（调用方退化到兜底口径）。

    fail-open 而不是 fail-closed 的原因：parity map 损坏时 ``adapter_parity.py``
    自己会红（pre-push 两道门禁并联跑），本门禁没必要在缺依赖的极简环境里炸掉；
    退化只影响「哪些工具算豁免」的精度，不影响仓库内两条防线的判定。
    """
    path = repo_root / PARITY_MAP_REL
    key = str(path.resolve()) if path.exists() else str(path)
    if key in _PARITY_CACHE:
        return _PARITY_CACHE[key]
    cfg: dict = {}
    if path.is_file():
        try:
            loaded = _load_parity_module(repo_root).load_map(str(path))
            cfg = loaded if isinstance(loaded, dict) else {}
        except Exception:  # noqa: BLE001 - adapter_parity 缺失：退回直接 YAML 解析
            try:
                import yaml  # PyYAML 是项目锁定依赖（requirements.txt）

                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                cfg = loaded if isinstance(loaded, dict) else {}
            except Exception:  # noqa: BLE001 - 依赖缺失/坏 YAML：退化，不阻断本门禁
                cfg = {}
    _PARITY_CACHE[key] = cfg
    return cfg


def dsh_exempt_tools(repo_root: Path = ROOT) -> set[str]:
    """dsh 的永久豁免工具（唯一真源 = parity map 的 ``exemptions.<tool>.dsh``）。

    2026-11-03 修正：旧实现把豁免写死成 ``append`` / ``agent_onboarding`` 两项，
    漏了 parity map 里 dsh 的 4 个管理面豁免（skill_candidate_pending / list /
    adopt / review）——部署缺口 14 项里有 4 项是这么来的假阳性。现在与
    ``adapter_parity.py`` 同源读取；map 缺失时退回 ``DSH_EXEMPT_TOOLS`` 兜底，
    map 存在但没有 exemptions 段 → 返回空集（不豁免任何工具，符合真源语义）。
    """
    cfg = _parity_map(repo_root)
    if not cfg:
        return set(DSH_EXEMPT_TOOLS)
    exemptions = cfg.get("exemptions")
    if exemptions is None:
        return set()  # map 存在但未声明任何豁免 → 不豁免任何工具（真源语义）
    if not isinstance(exemptions, dict):
        return set(DSH_EXEMPT_TOOLS)
    return {
        str(tool)
        for tool, per_adapter in exemptions.items()
        if isinstance(per_adapter, dict) and per_adapter.get(ADAPTER_NAME)
    }


def dsh_alias_map(repo_root: Path = ROOT) -> dict[str, str]:
    """parity map 的 **aliases 段**里 dsh 的等价名映射（基准名 → dsh 工具名）。

    2026-11-03 修正：旧实现自称「只解析 aliases 段」，实际按缩进扫全 YAML——
    exemptions 段形如::

        skill_candidate_adopt:
          dsh: 同上（需 admin key 的管理动作）

    缩进与 aliases 段完全一样，于是中文豁免理由被当成别名值、豁免工具名被当成
    基准工具名一起进了契约比对（输出里出现「部署副本缺工具 '同上（…）'」）。
    现在只取 aliases 段（段落边界由 YAML 解析器保证），解析不到返回空映射（退化
    为按名字严格比对）。
    """
    cfg = _parity_map(repo_root)
    aliases = cfg.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    out: dict[str, str] = {}
    for tool, per_adapter in aliases.items():
        if isinstance(per_adapter, dict) and per_adapter.get(ADAPTER_NAME):
            out[str(tool)] = str(per_adapter[ADAPTER_NAME])
    return out


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
    """从 ``sgme/mcp_server.py`` 解析 MCP 工具面（与 adapter_parity.py 同口径）。

    解析不到 = 提取口径失效，返回空集由调用方 fail-closed 处理。
    """
    server = repo_root / "sgme" / "mcp_server.py"
    if not server.is_file():
        return set()
    return set(re.findall(r'\{"name":\s*"([a-z_]+)"', server.read_text(encoding="utf-8")))


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


def deploy_dependency_gaps(deploy_dir: Path) -> list[str]:
    """防线 4：部署副本声明的运行时依赖必须能从插件目录解析。

    插件跑不起来的部署同样是缺口。pnpm 隔离布局下依赖链接在插件目录同级的
    ``node_modules``（``<store>/node_modules/<dep>``）；npm 嵌套布局在插件目录
    自己的 ``node_modules``。两处都解析不到即视为缺口。
    """
    pkg_path = Path(deploy_dir) / DEPLOY_PACKAGE_REL
    if not pkg_path.is_file():
        return [
            "[deploy] 部署目录缺 package.json（%s）→ 用官方 dsh plugin 重装" % deploy_dir
        ]
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ["[deploy] 部署 package.json 不可读（%s）：%s" % (pkg_path, exc)]
    deps = pkg.get("dependencies")
    if not isinstance(deps, dict) or not deps:
        return []
    real = Path(deploy_dir).resolve()
    roots = [real.parent, real / "node_modules"]
    missing = [
        name
        for name in sorted(deps)
        if not any((root / name / "package.json").is_file() for root in roots)
    ]
    if not missing:
        return []
    return [
        "[deploy] 部署副本缺运行时依赖 %s（package.json 声明、%s / %s 下均无解析结果）"
        " → 用官方 dsh plugin add/install 重装，不要手工解包 tgz"
        % ("、".join(missing), roots[0], roots[1])
    ]


def check_deploy_contract(repo_root: Path = ROOT, deploy_dir=None) -> list[str]:
    """防线 3：部署副本是否满足能力面契约。返回缺口清单（空 = 无缺口）。

    三类判据：
    - 契约级：MCP 基准工具（去 dsh 永久豁免，豁免读 parity map）必须全部在部署
      产物里——缺 = 部署落后；
    - 哈希级：部署产物与仓库产物逐文件一致——不一致 = 部署副本内容漂移；
    - 依赖级：部署副本声明的运行时依赖必须能从插件目录解析——跑不起来同样是缺口。
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
    exemptions = dsh_exempt_tools(repo_root)
    contract = sorted(baseline - exemptions)

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
    gaps.extend(deploy_dependency_gaps(deploy_dir))
    return gaps


def check(repo_root: Path = ROOT, deploy_dir=None) -> dict:
    """执行四道防线，返回 {"ok": bool, "findings": [...], "gaps": [...], "deploy": str|None}。

    - findings：仓库内派生产物漂移（分发副本 / dsh lib）——必须修，ok=False；
    - gaps：部署态缺口（部署落后于仓库）——正常开发时序里允许存在，默认只警告；
      发布门禁用 ``--strict-deploy`` 把它也算失败（与 adapter_parity 的 pending 口径一致）。
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