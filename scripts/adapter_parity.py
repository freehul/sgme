# -*- coding: utf-8 -*-
"""check：SGME 官方适配器能力面对账（T-170 / ST-42）。

为什么需要本脚本
----------------
SGME 有三个平级的官方适配器（hermes / dsh / doubao），它们各自维护自己的一份
「能力面」，而 SGME 的能力全集定义在 MCP 工具面（sgme/mcp_server.py）。历史上
发生过：SGME 一路加能力，DSH 适配器跟着加，Hermes 适配器停在 2026-08-30 没动，
近 20 天无人发现——因为**没有任何地方能看出漂移**。本脚本把漂移变成机器可见。

判定口径
--------
基准 = MCP 工具面全部工具。对每个「基准工具 × 适配器」格子，按序判定：

1. aliases 里显式映射 → 该符号必须真实存在于适配器文件（防映射漂移）
2. exemptions 里有理由 → 永久豁免（放行，打印理由）
3. passthrough 里有通道与理由 → 通配代偿（放行，打印通道）
4. 适配器里存在同名（剥前缀后）符号 → 已覆盖
5. pending 里已登记（带任务号）→ 待补齐（警告；--strict 下失败）
6. 以上都不是 → **漂移**（失败）

另外做反向卫生检查（防声明腐烂）：
- pending/exemptions 声明了但实际已覆盖 → 提示清理（警告）
- aliases 目标符号不存在、extra 声明的自有工具不存在 → 失败

退出码：0 = 无漂移（--strict 时且 pending 为空）；1 = 漂移或声明错误或严格模式未达标。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - 环境缺 PyYAML 时给出可行动提示
    print("需要 PyYAML：pip install pyyaml（或使用项目 venv）", file=sys.stderr)
    raise SystemExit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapter_parity_map.yaml")

# 格子状态
COVERED = "covered"
EXEMPT = "exempt"
PASSTHROUGH = "passthrough"
PENDING = "pending"
DRIFT = "drift"

_MARK = {COVERED: "✓", EXEMPT: "豁免", PASSTHROUGH: "直通", PENDING: "待补", DRIFT: "✗漂移"}


def _read(path: str) -> str | None:
    """读文件；不存在返回 None（调用方决定是失败还是提示）。"""
    full = path if os.path.isabs(path) else os.path.join(BASE, path)
    if not os.path.exists(full):
        return None
    with open(full, encoding="utf-8") as f:
        return f.read()


def parse_symbols(adapter_cfg: dict) -> set[str]:
    """按适配器解析规则抽取能力符号，并剥掉前缀。

    默认丢弃 `_` 开头的名字（私有方法/内部函数），无论解析规则是否列了 exclude。
    """
    src = _read(adapter_cfg["file"])
    if src is None:
        return set()
    names = set(re.findall(adapter_cfg["pattern"], src, re.M))
    for bad in adapter_cfg.get("exclude") or []:
        names.discard(bad)
    names = {n for n in names if not n.startswith("_")}
    prefix = adapter_cfg.get("strip_prefix") or ""
    if prefix:
        names = {n[len(prefix):] if n.startswith(prefix) else n for n in names}
    return names


def parse_secondary_face(adapter_cfg: dict) -> set[str] | None:
    """解析适配器的第二接口面（如豆包的 CLI 命令面）；未声明返回 None。

    背景：某些适配器「有实现」不等于「agent 调得到」——豆包靠 SKILL.md 指示
    agent 调 CLI，方法存在但 CLI 没入口 = 能力黑洞（T-172 实测过这个坑）。
    """
    face = adapter_cfg.get("secondary_face")
    if not face:
        return None
    src = _read(face["file"])
    if src is None:
        return set()
    return set(re.findall(face["pattern"], src, re.M))


def load_map(path: str = MAP_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check(cfg: dict | None = None) -> dict:
    """执行对账，返回结果字典（供 CLI 与 pytest 共用）。"""
    cfg = cfg or load_map()
    baseline = set(re.findall(cfg["baseline"]["pattern"], _read(cfg["baseline"]["file"]) or "", re.M))
    adapters = list(cfg["adapters"].keys())
    symbols = {a: parse_symbols(c) for a, c in cfg["adapters"].items()}
    aliases = cfg.get("aliases") or {}
    exemptions = cfg.get("exemptions") or {}
    passthrough = cfg.get("passthrough") or {}
    extra = cfg.get("extra") or {}
    pending = cfg.get("pending") or {}
    prefixes = {a: (c.get("strip_prefix") or "") for a, c in cfg["adapters"].items()}

    def _has_symbol(tool: str, ad: str) -> bool:
        """该适配器是否真有这个能力的符号（含别名口径）。"""
        if tool in symbols[ad]:
            return True
        alias = (aliases.get(tool) or {}).get(ad)
        if not alias:
            return False
        prefix = prefixes.get(ad, "")
        return (alias[len(prefix):] if prefix and alias.startswith(prefix) else alias) in symbols[ad]

    errors: list[str] = []      # 失败项
    warnings: list[str] = []    # 提示项（不影响退出码，--strict 另行判定）
    matrix: dict[str, dict[str, dict]] = {}

    for tool in sorted(baseline):
        matrix[tool] = {}
        for ad in adapters:
            state, detail = _judge(tool, ad, symbols[ad], aliases, exemptions, passthrough, pending,
                                  {a: (c.get("strip_prefix") or "") for a, c in cfg["adapters"].items()})
            matrix[tool][ad] = {"state": state, "detail": detail}
            if state == DRIFT:
                errors.append(f"漂移：基准工具 {tool} 在适配器 {ad} 上未声明（既无实现、也无豁免/直通/待补）")

    # ---- 第二接口面检查（豆包：CLI 命令面）----
    # 「有实现」≠「agent 调得到」：豆包靠 SKILL.md 指示 agent 调 CLI，
    # 方法存在但 CLI 无入口就是能力黑洞。故对声明了 secondary_face 的适配器，
    # 主面已覆盖的能力必须在第二接口面也有入口。
    faces = {a: f for a, f in ((a, parse_secondary_face(c)) for a, c in cfg["adapters"].items())
             if f is not None}
    for ad, face_names in faces.items():
        rule = (cfg["adapters"][ad].get("secondary_face") or {}).get("name_rule", "dash")
        for tool in sorted(baseline):
            if not _has_symbol(tool, ad):
                continue  # 主面未覆盖时，漂移/豁免已在上面判过，不重复报
            expected = tool.replace("_", "-") if rule == "dash" else tool
            if expected not in face_names:
                errors.append(
                    f"接口面缺口：适配器 {ad} 的能力 {tool} 有实现，但第二接口面无 `{expected}` 入口（agent 调不到）"
                )

    # ---- 反向卫生检查：声明腐烂 ----
    # 注意：腐烂判定必须独立于 _judge 的短路顺序——某格子一旦被声明（豁免/直通/待补）
    # 就不会被判为 covered，所以上面直接用 _has_symbol 比对「适配器里是否真有这个符号」。
    for tool, per in (exemptions or {}).items():
        if tool not in baseline:
            errors.append(f"声明错误：exemptions 里的 {tool} 不在基准工具面（基准已改名或删工具？）")
        for ad in per or {}:
            if ad not in adapters:
                errors.append(f"声明错误：exemptions[{tool}] 引用了未知适配器 {ad}")
            elif _has_symbol(tool, ad):
                warnings.append(f"可清理：exemptions 声明 {tool}@{ad} 为豁免，但该适配器已实现，请删除声明")
    for ad, spec in (passthrough or {}).items():
        if ad not in adapters:
            errors.append(f"声明错误：passthrough 引用了未知适配器 {ad}")
            continue
        for tool in (spec or {}).get("tools") or []:
            if tool not in baseline:
                errors.append(f"声明错误：passthrough[{ad}] 里的 {tool} 不在基准工具面")
            elif _has_symbol(tool, ad):
                warnings.append(f"可清理：passthrough 声明 {tool}@{ad} 为直通，但该适配器已实现，请删除声明")
    # pending：按适配器分组，必须带任务号；工具名须在基准面内
    for ad, spec in (pending or {}).items():
        if ad not in adapters:
            errors.append(f"声明错误：pending 引用了未知适配器 {ad}")
            continue
        if not (spec or {}).get("task"):
            errors.append(f"声明错误：pending[{ad}] 缺少任务号（待补齐必须挂 Backlog 任务）")
        for tool in (spec or {}).get("tools") or []:
            if tool not in baseline:
                errors.append(f"声明错误：pending[{ad}] 里的 {tool} 不在基准工具面")
            elif _has_symbol(tool, ad):
                warnings.append(f"可清理：pending 声明 {tool}@{ad} 待补齐，但已实现，请从 pending 移除")
    if set(cfg["baseline"].keys()) and not baseline:
        errors.append(f"读不到基准工具面：{cfg['baseline']['file']}")
    for ad, cfg_ad in cfg["adapters"].items():
        if not symbols[ad]:
            errors.append(f"读不到适配器能力面：{cfg_ad['file']}（文件缺失或解析规则失效）")
    for tool, per in aliases.items():
        if tool not in baseline:
            errors.append(f"声明错误：aliases 里的 {tool} 不在基准工具面")
        for ad, sym in (per or {}).items():
            if ad not in adapters:
                errors.append(f"声明错误：aliases[{tool}] 引用了未知适配器 {ad}")
                continue
            cfg_ad = cfg["adapters"][ad]
            full = sym if not (cfg_ad.get("strip_prefix") or "") else sym
            if full not in (re.findall(cfg_ad["pattern"], _read(cfg_ad["file"]) or "", re.M) or []):
                errors.append(f"声明错误：aliases[{tool}][{ad}] = {sym}，但该符号在 {cfg_ad['file']} 中不存在")
    for ad, names in (extra or {}).items():
        if ad not in adapters:
            errors.append(f"声明错误：extra 引用了未知适配器 {ad}")
            continue
        for name in names or []:
            if name not in symbols[ad]:
                errors.append(f"声明错误：extra[{ad}] 声明的自有工具 {name} 在适配器中不存在")

    # ---- 统计 ----
    # alias 目标符号不算「自有工具」（它们只是同名映射的替身）
    alias_targets = {ad: {v for per in aliases.values() if (v := (per or {}).get(ad))} for ad in adapters}
    stats = {}
    for ad in adapters:
        st = [matrix[t][ad]["state"] for t in baseline]
        stats[ad] = {
            "covered": st.count(COVERED),
            "exempt": st.count(EXEMPT),
            "passthrough": st.count(PASSTHROUGH),
            "pending": st.count(PENDING),
            "drift": st.count(DRIFT),
            "extra": len(symbols[ad] - baseline - alias_targets[ad]),
        }
    pending_count = sum(s["pending"] for s in stats.values())
    return {
        "baseline_count": len(baseline),
        "baseline_tools": sorted(baseline),
        "adapters": adapters,
        "matrix": matrix,
        "stats": stats,
        "pending_count": pending_count,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def _judge(tool, ad, syms, aliases, exemptions, passthrough, pending, prefixes):
    """判定单个格子的状态。"""
    alias = (aliases.get(tool) or {}).get(ad)
    if alias:
        # 别名要按该适配器的前缀口径剥掉后再比对（符号是否存在已在 check() 阶段验过）
        prefix = prefixes.get(ad, "")
        norm = alias[len(prefix):] if prefix and alias.startswith(prefix) else alias
        return (COVERED, f"别名映射 {alias}") if norm in syms else (DRIFT, f"别名 {alias} 未解析到")
    reason = (exemptions.get(tool) or {}).get(ad)
    if reason:
        return EXEMPT, reason
    spec = passthrough.get(ad) or {}
    if tool in (spec.get("tools") or []):
        return PASSTHROUGH, f"通道 {spec.get('channel', '?')}：{spec.get('reason', '')}"
    if tool in syms:
        return COVERED, "同名实现"
    spec = pending.get(ad) or {}
    if tool in (spec.get("tools") or []):
        return PENDING, f"任务 {spec.get('task', '?')}：{spec.get('note', '')}"
    return DRIFT, ""


def render(res: dict, strict: bool) -> str:
    """人类可读报告。"""
    lines = []
    lines.append("=" * 78)
    lines.append("SGME 官方适配器能力面对账（基准 = MCP 工具面）")
    lines.append("=" * 78)
    ads = res["adapters"]
    lines.append(f"基准工具数：{res['baseline_count']}")
    for ad in ads:
        s = res["stats"][ad]
        lines.append(
            f"  {ad:8s} 覆盖 {s['covered']:2d}  豁免 {s['exempt']:2d}  直通 {s['passthrough']:2d}"
            f"  待补 {s['pending']:2d}  漂移 {s['drift']:2d}  自有 {s['extra']:2d}"
        )
    lines.append("-" * 78)
    head = "工具".ljust(24) + "".join(a.ljust(12) for a in ads)
    lines.append(head)
    lines.append("-" * 78)
    for tool in res["baseline_tools"]:
        row = tool.ljust(24)
        for ad in ads:
            st = res["matrix"][tool][ad]["state"]
            row += _MARK[st].ljust(12)
        lines.append(row)
    lines.append("-" * 78)
    # 豁免/直通/待补明细
    for tool in res["baseline_tools"]:
        for ad in ads:
            cell = res["matrix"][tool][ad]
            if cell["state"] in (EXEMPT, PASSTHROUGH, PENDING):
                lines.append(f"  [{_MARK[cell['state']]}] {tool} @ {ad} — {cell['detail']}")
    if res["warnings"]:
        lines.append("-" * 78)
        for w in res["warnings"]:
            lines.append(f"  ⚠ {w}")
    lines.append("-" * 78)
    if res["errors"]:
        lines.append(f"❌ 失败：{len(res['errors'])} 项")
        for e in res["errors"]:
            lines.append(f"  · {e}")
    else:
        lines.append("✅ 无漂移（所有基准工具在三个适配器上均有实现或显式声明）")
    if strict and res["pending_count"]:
        lines.append(f"❌ 严格模式：仍有 {res['pending_count']} 项待补齐，发布门禁不通过")
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv=None, cfg: dict | None = None) -> int:
    ap = argparse.ArgumentParser(description="SGME 官方适配器能力面对账")
    ap.add_argument("--strict", action="store_true", help="发布门禁模式：待补齐也算失败")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    res = check(cfg)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res, args.strict))

    if not res["ok"] or res["errors"]:
        return 1
    if args.strict and res["pending_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
