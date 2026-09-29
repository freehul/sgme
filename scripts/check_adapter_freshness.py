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

1. 分发副本新鲜度（六个 ``skills/adapter-<host>/``）
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

退出码：0 = 全部新鲜；1 = 存在漂移（逐项列出）。

修复方式：① 分发副本漂移 → ``python scripts/publish_adapter_skills.py``；
② dsh 产物漂移 → ``pnpm -C adapters/dsh/sgme-bridge run build`` 后提交 lib。
注意顺序：先 build 再 publish（publish 从真源打包，产物新了副本才会跟着新）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
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
    """防线 1：六个分发副本 vs 真源重新打包的逐文件比对。返回漂移清单。"""
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
    lib_names = set(NAME_RE.findall(lib_text))

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


def check(repo_root: Path = ROOT) -> dict:
    """执行两道防线，返回 {"ok": bool, "findings": [...]}（供 CLI 与 pytest 共用）。"""
    findings = check_skill_copies(repo_root) + check_dsh_bundle(repo_root)
    return {"ok": not findings, "findings": findings}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SGME 适配器分发新鲜度门禁")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--root", default=str(ROOT), help="仓库根（默认本文件所在仓库）")
    args = ap.parse_args(argv)

    res = check(Path(args.root))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif res["ok"]:
        print("✅ 适配器分发新鲜：六副本与真源一致；dsh lib 与 src 同步")
    else:
        print(f"❌ 适配器分发漂移 {len(res['findings'])} 项：")
        for f in res["findings"]:
            print(f"  · {f}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
