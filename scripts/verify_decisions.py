#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_decisions.py：设计决策 ↔ 代码实现的契约对账（流程补全件）。

## 为什么需要它

2026-11-03 的对齐核查发现：文档写「已实现」而代码里根本没有的项有 20+ 处，
反过来「代码已做、文档还挂待施工」也有 15+ 处。根因不是某次失误，而是**流程里
没有"验证做了没有"的机制**——设计文档的 ✅/🔶/⬜ 全靠手工维护，谁也没跑过比对。

本脚本把「每条决策是否真落地」变成**可执行的断言**，退出码可用于门禁：

    0  全部断言通过（或只有待实现项，且文档也标了未实现）
    1  存在 MISMATCH（文档说已完成，代码不满足 / 代码有但断言缺失）

## 用法

    python scripts/verify_decisions.py            # 全量对账
    python scripts/verify_decisions.py --only D-68 D-73
    python scripts/verify_decisions.py --strict   # 把「无自动判据」也算失败

## 设计原则

- **只读源码，不连库**：能在任何机器上跑（含 CI），不依赖生产数据。
- **断言写"存在性"而不是"行为"**：字符串/符号存在即算落地；行为正确性仍归 pytest。
  这是第一道闸——挡住「建了列却没写入点」这类静默失效。
- **每条断言必须能指到文件**：失败时直接给文件路径，便于修复。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "sgme"

# 常用文件（断言按需取用；文件不存在 → 该断言判失败）
FILES = {
    "db": S / "data" / "db.py",
    "memory_dao": S / "data" / "memory_dao.py",
    "memory_stats_dao": S / "data" / "memory_stats_dao.py",
    "llm_usage_dao": S / "data" / "llm_usage_dao.py",
    "l1": S / "engine" / "l1.py",
    "l2_extract": S / "engine" / "l2_extract.py",
    "l3_rules": S / "engine" / "l3_rules.py",
    "dream": S / "engine" / "dream.py",
    "surprise": S / "engine" / "surprise.py",
    "recall_aging": S / "engine" / "recall_aging.py",
    "conversation_state": S / "engine" / "conversation_state.py",
    "inject_ops": S / "operations" / "inject.py",
    "profile_inject": S / "profile" / "inject.py",
    "last_mentions": S / "profile" / "last_mentions.py",
    "care_ops": S / "operations" / "care.py",
    "role_metrics": S / "operations" / "role_metrics.py",
    "privacy_guard": S / "operations" / "privacy_guard.py",
    "routes_care": S / "server" / "routes_care.py",
    "routes_credentials": S / "server" / "routes_credentials.py",
    "routes_ops": S / "server" / "routes_ops.py",
    "routes_admin": S / "server" / "routes_admin.py",
    "routes_skills": S / "server" / "routes_skills.py",
    "cache": S / "operations" / "skill_cache.py",
    "refine": S / "engine" / "refine.py",
    "pipeline": S / "engine" / "pipeline.py",
    "config": S / "config.py",
    "app": S / "server" / "app.py",
}

_cache: dict[str, str] = {}


def src(key: str) -> str:
    """读源码（缓存）。文件不存在返回空串（断言自然失败）。"""
    if key not in _cache:
        path = FILES.get(key)
        _cache[key] = path.read_text(encoding="utf-8") if path and path.exists() else ""
    return _cache[key]


def has(key: str, *needles: str) -> tuple[bool, str]:
    """断言：源码里同时存在这些片段。返回 (是否通过, 说明)。"""
    body = src(key)
    if not body:
        return False, f"{key}: 文件不存在"
    missing = [n for n in needles if n not in body]
    if missing:
        return False, f"{key}: 缺少 {missing}"
    return True, f"{key}: 命中 {len(needles)} 处"


def allof(key: str, needles: list[str]) -> list[tuple[str, bool, str]]:
    out = []
    for n in needles:
        ok, msg = has(key, n)
        out.append((f"{key} 含 `{n}`", ok, msg))
    return out


# ---------------------------------------------------------------------------
# 决策清单：id / 名称 / 文档声明状态 / 断言列表
#   文档声明：done=文档标 ✅ / planned=文档标 🔶/⬜ / partial=文档标部分实现
#   asserts 返回 [(描述, 通过?, 证据)]
# ---------------------------------------------------------------------------
CHECKS: list[dict] = []


def check(did: str, name: str, doc: str):
    def deco(fn):
        CHECKS.append({"id": did, "name": name, "doc": doc, "fn": fn})
        return fn
    return deco


# ---------- 记忆支柱 ----------
@check("D-43", "断言双轴（modality/derivation + 闸门 A）", "done")
def _(self):
    r = []
    r.append(("modality 列已建", *has("db", '"modality"')[::-1][::-1]))
    r.append(("解析层读模型 modality", *has("l2_extract", "fact.get(\"modality\")")))
    r.append(("valid_to 有写入点（闸门 A 输入）", *has("l2_extract", "_valid_to_for")))
    r.append(("落库路径传 valid_to", *has("pipeline", "valid_to=m.get(\"valid_to\")")))
    return r


@check("D-52", "TTL 到期 0-4 五档处置", "planned")
def _(self):
    return [
        ("lifecycle 列", *has("db", "lifecycle")),
        ("ttl_renew_count 列", *has("db", "ttl_renew_count")),
        ("ttl_renewed_at 列", *has("db", "ttl_renewed_at")),
        ("inject_tier 列", *has("db", "inject_tier")),
        ("meaning_anchor 列", *has("db", "meaning_anchor")),
    ]


@check("D-66", "七值 type + certainty + subject 必填", "done")
def _(self):
    return [
        ("七值 type 白名单", *has("l1", "DOMAIN_TYPES")),
        ("certainty 三值", *has("l1", "VALID_CERTAINTY")),
        ("subject 列", *has("db", '"subject"')),
        ("注入侧 subject 过滤（消费侧）", *has("profile_inject", "subject_filter=")),
    ]


@check("D-67", "route 按来源分流 + 候选池消费", "done")
def _(self):
    return [
        ("route 列", *has("db", '"route"')),
        ("route 白名单", *has("l1", "VALID_ROUTES")),
        ("注入侧只放行 memory", *has("profile_inject", 'routes=("memory",)')),
        ("候选池表", *has("db", "skill_candidates")),
    ]


@check("D-68", "软遗忘两层语义（正文脱敏 + 可回溯）", "done")
def _(self):
    return [
        ("正文强制重写", *has("l1", "_enforce_forget_content")),
        ("forget_topic 列", *has("db", "forget_topic")),
        ("forget 落库（extra 白名单）", *has("memory_dao", '"forget_topic"')),
        ("memory_type 含 forget_request", *has("l1", "forget_request")),
        ("写入时置 forget_request", *has("l1", 'memory_type = "forget_request"')),
        ("注入侧排除遗忘请求", *has("profile_inject", "exclude_memory_types")),
    ]


@check("D-70", "记忆回指 last_mentions", "done")
def _(self):
    return [
        ("取数模块", *has("last_mentions", "def pick(")),
        ("注入接线", *has("inject_ops", "_attach_last_mentions")),
        ("默认关闭（首月 off）", *has("config", '"enabled": False')),
    ]


@check("D-73", "召回加固（幂律放大 + 自然日去重）", "done")
def _(self):
    return [
        ("幂律公式", *has("recall_aging", "def compute_ttl_multiplier")),
        ("接线梦境", *has("dream", "compute_ttl_multiplier")),
        ("recall_count 自然日去重", *has("memory_stats_dao", "date(memory_stats.last_injected_at)")),
        ("last_recalled_at 写入", *has("memory_stats_dao", "last_recalled_at")),
    ]


@check("D-74", "角色真实感指标", "done")
def _(self):
    return [
        ("四项指标实现", *has("role_metrics", "AI_TASTE_PATTERNS")),
        ("指标落卡", *has("role_metrics", "sgme_metrics")),
        ("测量脚本", (ROOT / "scripts" / "measure_role_realism.py").exists(), "scripts/measure_role_realism.py"),
    ]


@check("D-76", "人设懒补全", "done")
def _(self):
    return [("persona_fill 实现", *has("care_ops", "def persona_fill"))]


@check("D-77", "越界代价（三级，当前仅观测）", "done")
def _(self):
    return [
        ("软拦截改写", *has("care_ops", "def guard_reply")),
        ("第三级仅观测（不返回 constraint）", *has("care_ops", '"constraint": None')),
        ("阈值常量保留", *has("care_ops", "VIOLATION_SESSION_THRESHOLD")),
    ]


@check("D-78", "惊讶度先验（存同类型分位）", "done")
def _(self):
    return [
        ("surprise 列", *has("db", '"surprise"')),
        ("分位映射", *has("surprise", "_percentile_of")),
        ("落库钩子", *has("surprise", "compute_for_store")),
    ]


# ---------- 角色 / 关怀 ----------
@check("D-65", "角色隐私层访问控制", "done")
def _(self):
    return [
        ("白名单校验", *has("privacy_guard", "def check_access")),
        ("装配接线", *has("care_ops", "privacy_guard.check_access")),
        ("越权审计", *has("privacy_guard", "security_log")),
    ]


@check("D-72", "冷场修复", "done")
def _(self):
    return [
        ("会话状态判定", *has("conversation_state", "def assess")),
        ("一次性闸", *has("conversation_state", "def should_repair")),
        ("注入接线", *has("inject_ops", "coldstart_repair")),
    ]


@check("D-61", "关怀档位按角色配置（静默/会话内/主动推送）", "planned")
def _(self):
    # 角色卡目前只有 greeting_templates / trigger_rules / frequency，没有「档位」语义字段
    return [("角色卡档位字段（care_level / frequency_level）",
             *(lambda ok, msg: (ok, msg))(any(
                 k in src("care_ops") for k in ("care_level", "frequency_level", "care_tier")
             ), "care_ops: 未找到档位字段"))]


# ---------- 知识 / 技能 ----------
@check("D-62", "知识库自动关联与自动分类", "planned")
def _(self):
    return [
        ("wiki_revisions 表", *has("db", "wiki_revisions")),
        ("wiki_categories 表", *has("db", "wiki_categories")),
    ]


@check("D-59", "技能落盘缓存（服务端 + 适配器）", "done")
def _(self):
    return [
        ("catalog_version", *has("routes_skills", "catalog-version")),
        ("manifest 端点", *has("routes_skills", "manifest")),
        ("包端点", *has("routes_skills", "package")),
        ("服务端缓存", *has("cache", "package_sha256")),
    ]


# ---------- 平台 / 迁移 ----------
@check("D-37", "凭据子模块（六端点）", "done")
def _(self):
    return [
        ("读端点 3 个", *has("routes_credentials", "@router.get")),
        ("写端点", *has("routes_credentials", "@router.post")),
        ("删除端点", *has("routes_credentials", "@router.delete")),
        ("导出端点", *has("routes_credentials", "export")),
    ]


@check("D-63", "LLM 成本计量", "done")
def _(self):
    return [
        ("llm_usage_daily 表", *has("llm_usage_dao", "llm_usage_daily")),
        ("成本查询端点", *has("routes_ops", "llm-cost")),
    ]


@check("D-55", "迁移四阶段（加列→双写→回填→切读）", "planned")
def _(self):
    return [
        ("双写开关", *has("config", "dual_write")),
        ("切读开关", *has("config", "read_mode")),
    ]


@check("D-53", "场景层取消（scene_key 替代聚合）", "planned")
def _(self):
    return [
        ("scene_key 列", *has("db", "scene_key")),
        ("scene_key 赋值点", *has("l2_extract", "scene_key")),
        ("场景聚合冻结开关", *has("refine", "scene_aggregate")),
    ]


@check("D-64", "兼容期 409 清单与 capabilities", "planned")
def _(self):
    return [("capabilities 端点", *has("app", "capabilities"))]


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="设计决策 ↔ 代码契约对账（只读）")
    ap.add_argument("--only", nargs="*", help="只查这些决策号（如 D-68 D-73）")
    ap.add_argument("--strict", action="store_true", help="无自动判据也算失败")
    args = ap.parse_args()

    rows = []
    mismatch = 0
    not_done = 0
    for c in CHECKS:
        if args.only and c["id"] not in args.only:
            continue
        try:
            results = c["fn"](None)
        except Exception as e:  # noqa: BLE001
            results = [("断言异常", False, str(e))]
        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        allpass = passed == total

        # 判定：文档说 done 但代码不全 → MISMATCH；文档说 planned 且确实没做 → 正常
        if c["doc"] == "done" and not allpass:
            verdict = "❌ MISMATCH（文档标已实现，代码不满足）"
            mismatch += 1
        elif c["doc"] == "planned" and allpass:
            verdict = "🔁 文档落后（代码已落地，文档仍标待施工）"
        elif c["doc"] == "planned":
            verdict = "⏳ 待施工（与文档一致）"
            not_done += 1
        else:
            verdict = "✅ 一致"
        rows.append((c["id"], c["name"], c["doc"], f"{passed}/{total}", verdict, results))

    print("=" * 88)
    print("设计决策 ↔ 代码契约对账（只读源码，不连库）")
    print("=" * 88)
    for did, name, doc, frac, verdict, results in rows:
        print(f"\n{did} {name}  [{doc}]  断言 {frac}  {verdict}")
        if "MISMATCH" in verdict or "落后" in verdict:
            for desc, ok, ev in results:
                print(f"    {'✓' if ok else '✗'} {desc}  — {ev}")

    print("\n" + "-" * 88)
    print(f"合计 {len(rows)} 条决策；MISMATCH {mismatch} 条；"
          f"待施工 {not_done} 条；文档落后 {sum(1 for r in rows if '落后' in r[4])} 条")
    if mismatch:
        print("\n处置：MISMATCH 项要么补代码、要么把文档状态改回未实现——不能放着。")
    return 1 if mismatch or (args.strict and not_done) else 0


if __name__ == "__main__":
    raise SystemExit(main())
