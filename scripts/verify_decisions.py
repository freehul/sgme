#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_decisions.py：设计决策 ↔ 代码实现的契约对账（流程补全件）。

## 为什么需要它

2026-11-03 的对齐核查发现：文档写「已实现」而代码里根本没有的项有 20+ 处，
反过来「代码已做、文档还挂待施工」也有 15+ 处。根因不是某次失误，而是**流程里
没有"验证做了没有"的机制**——设计文档的 ✅/🔶/⬜ 全靠手工维护，谁也没跑过比对。

本脚本把「每条决策是否真落地」变成**可执行的断言**，退出码可用于门禁：

    0  无 MISMATCH（待施工 / 部分实现不算失败，它们是文档已声明的事实；
       --strict 时还要求无「文档落后」）
    1  存在 MISMATCH（文档说已完成或部分完成，代码不满足文档声明的已落地部分）
       或（--strict 下）存在「文档落后」

## 用法

    python scripts/verify_decisions.py             # 全量对账（默认口径）
    python scripts/verify_decisions.py --only D-68 D-73   # 只看指定决策号
    python scripts/verify_decisions.py --strict    # 发布门禁：文档落后也算失败

## 状态口径（重要，2026-11-03 修正）

每条决策带一个 doc 字段，含义 = **设计文档对该项的实现状态声明**，不是
「设计是否拍板」（几乎所有决策都已拍板，拿拍板当 done 会让门禁失去意义）：

- done    文档明确声明该决策已实现（"✅已实现 / ✅已落库 / 已完成"），
          脚本断言必须全过，否则 **MISMATCH**；
- partial 文档把它列为 🔶 或明写「已实现 X / 未实现 Y」，脚本只断言
          **文档声明已落地的 X 部分**；X 不成立同样算 MISMATCH，
          未实现的 Y 由文档自己记录（不在这里重复刷红）；
- planned 文档声明整体未开工 / 待施工 / 草案未拍板，断言用来探测
          「是否已偷偷落地」：全过 = **文档落后**（代码超前文档，默认警告、
          --strict 下失败）；未过 = **待施工**（与文档一致，不是失败）。

--strict 用于发布门禁：把「文档落后」也升级为失败（默认只警告）。
**待施工项在两种模式下都不算失败**——它们是**已登记、已排期的已知缺口**，
拿它们把门禁常驻弄红只会让人忽略真正的 MISMATCH（2026-11-03 口径修正，
旧口径「--strict 把无自动判据/待施工也算失败」已作废）。

## 覆盖口径

决策全集 = 设计文档编号 D-1～D-78 去掉 D-38～D-42 五个保留未用编号 = **73 项**。
本脚本当前覆盖 len(CHECKS) 项；未覆盖项不是「已实现」，而是「还没有廉价
源码判据」——新增决策时必须同步在 CHECKS 里补断言（AGENTS「决策级对账」）。

## 设计原则

- **只读源码，不连库**：能在任何机器上跑（含 CI），不依赖生产数据。
- **断言写"存在性"而不是"行为"**：字符串/符号存在即算落地；行为正确性仍归 pytest。
  这是第一道闸——挡住「建了列却没写入点」这类静默失效。
- **每条断言必须能指到文件**：失败时给「文件路径 + 缺失符号」，便于直接修。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "sgme"

# 决策全集条数（D-1～D-78，其中 D-38～D-42 五个编号保留未用）
TOTAL_DECISIONS = 73

# 常用文件（断言按需取用；文件不存在 → 该断言判失败，并给出预期路径）
FILES = {
    "db": S / "data" / "db.py",
    "memory_dao": S / "data" / "memory_dao.py",
    "memory_stats_dao": S / "data" / "memory_stats_dao.py",
    "llm_usage_dao": S / "data" / "llm_usage_dao.py",
    "l1": S / "engine" / "l1.py",
    "l2": S / "engine" / "l2.py",
    "l2_extract": S / "engine" / "l2_extract.py",
    "l2_split": S / "engine" / "l2_split.py",
    "l3_rules": S / "engine" / "l3_rules.py",
    "dream": S / "engine" / "dream.py",
    "surprise": S / "engine" / "surprise.py",
    "recall_aging": S / "engine" / "recall_aging.py",
    "conversation_state": S / "engine" / "conversation_state.py",
    "inject_ops": S / "operations" / "inject.py",
    "review_ops": S / "operations" / "review.py",
    "memory_ops": S / "operations" / "memory.py",
    "profile_inject": S / "profile" / "inject.py",
    "last_mentions": S / "profile" / "last_mentions.py",
    "care_ops": S / "operations" / "care.py",
    "care_roles": S / "care" / "roles.py",
    "role_metrics": S / "operations" / "role_metrics.py",
    "privacy_guard": S / "operations" / "privacy_guard.py",
    "routes_care": S / "server" / "routes_care.py",
    "routes_credentials": S / "server" / "routes_credentials.py",
    "routes_ops": S / "server" / "routes_ops.py",
    "routes_admin": S / "server" / "routes_admin.py",
    "routes_review": S / "server" / "routes_review.py",
    "routes_skills": S / "server" / "routes_skills.py",
    "cache": S / "operations" / "skill_cache.py",
    "refine": S / "engine" / "refine.py",
    "pipeline": S / "engine" / "pipeline.py",
    "prompts_manager": S / "prompts" / "manager.py",
    "raw_store": S / "raw" / "store.py",
    "backup_manager": S / "backup" / "manager.py",
    "mcp_server": S / "mcp_server.py",
    "config": S / "config.py",
    "app": S / "server" / "app.py",
    # 仓库级脚本/配置（决策断言需要，仍走同一套 has() 口径）
    "adapter_parity": ROOT / "scripts" / "adapter_parity.py",
    "parity_map": ROOT / "scripts" / "adapter_parity_map.yaml",
}

_cache: dict[str, str] = {}


def rel(key: str) -> str:
    """文件键 → 仓库相对路径字符串（断言消息用，统一正斜杠）。"""
    path = FILES.get(key)
    if not path:
        return key
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def src(key: str) -> str:
    """读源码（缓存）。文件不存在返回空串（断言自然失败）。"""
    if key not in _cache:
        path = FILES.get(key)
        _cache[key] = path.read_text(encoding="utf-8") if path and path.exists() else ""
    return _cache[key]


def has(key: str, *needles: str) -> tuple[bool, str]:
    """断言：源码里同时存在这些片段。返回 (是否通过, 说明)。

    失败消息必须能让修复者直接定位：给出「文件路径 + 缺失符号」。
    """
    body = src(key)
    if not body:
        return False, f"{rel(key)}: 文件不存在"
    missing = [n for n in needles if n not in body]
    if missing:
        return False, f"{rel(key)}: 缺少 {missing}"
    return True, f"{rel(key)}: 命中 {len(needles)} 处"


def exists(relpath: str) -> tuple[bool, str]:
    """断言：仓库内相对路径存在（文件或目录）。"""
    return (ROOT / relpath).exists(), relpath


def json_has(relpath: str, *keys: str) -> tuple[bool, str]:
    """断言：JSON 文件存在且嵌套键（点分）齐全。"""
    path = ROOT / relpath
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # noqa: BLE001 - 文件缺失/坏 JSON 都判失败
        return False, f"{relpath}: {e!r}"
    cur = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return False, f"{relpath}: 缺少 {'.'.join(keys)}"
        cur = cur[k]
    return True, f"{relpath}: 命中 {'.'.join(keys)}"


# ---------------------------------------------------------------------------
# 决策清单：id / 名称 / 文档状态声明（done|partial|planned）/ 断言列表
#   asserts 返回 [(描述, 通过?, 证据)]
# ---------------------------------------------------------------------------
CHECKS: list[dict] = []


def check(did: str, name: str, doc: str):
    def deco(fn):
        CHECKS.append({"id": did, "name": name, "doc": doc, "fn": fn})
        return fn
    return deco


# ---------- 记忆支柱 ----------
@check("D-5", "分层整数编号 L1 捕获→L2 提取→L3 裁决→L4 梦境", "done")
def _(self):
    return [
        ("L1 捕获模块", *has("l1", "def parse_l1_output")),
        ("L2 提取模块", *has("l2_extract", "def parse_l2_output")),
        ("L3 裁决模块", *has("l3_rules", "def adjudicate")),
        ("L4 梦境模块", *has("dream", "def ensure_scheduler")),
    ]


@check("D-7", "梦境 = 内置守护定时器（ensure_scheduler，不依赖外部调度）", "done")
def _(self):
    return [
        ("梦境 ensure_scheduler 幂等启动", *has("dream", "def ensure_scheduler")),
        ("由 app.py 生命周期统一拉起", *has("app", "dream.ensure_scheduler")),
        ("不引入 APScheduler", *has("dream", "不引入 APScheduler")),
    ]


@check("D-18", "L1 捕获字段落库（§八第11条口径）", "partial")
def _(self):
    # 文档表头标 🔶 草案未拍板；§八第 11 条又声明「字段已落库」——按 partial 处理，
    # 只断言文档声明已落地的字段面（幂等 ALTER 到 memories + memory_archive）。
    return [
        (
            "memories 2.0 字段已建列",
            *has(
                "db",
                '"type"',
                '"subject"',
                '"certainty"',
                '"derivation"',
                '"modality"',
                '"route"',
                '"l2_facts_json"',
                '"feeling"',
                '"trait"',
                '"surprise"',
            ),
        ),
        ("归档表同步 ALTER", *has("db", 'for table in ("memories", "memory_archive")')),
    ]


@check("D-19", "捕获过滤（think 块/心跳/错误回灌，纯规则零 LLM）", "planned")
def _(self):
    return [
        ("keep_think 配置开关", *has("config", "keep_think")),
        ("捕获侧 think 剥离", *has("raw_store", "keep_think")),
    ]


@check("D-20", "L2 三件套（提示词 schema + 规则拆条表 + 静态标注集）", "partial")
def _(self):
    # 已落地 = 提示词阶段注册 + 规则拆条表；未落地 = 静态标注集（§八第 5 条 🔶）
    return [
        ("l1_extraction 提示词阶段", *has("prompts_manager", '"l1_extraction"')),
        ("规则拆条表模块", *has("l2_split", "def decide_split")),
    ]


@check("D-21", "主干+侧面模型（三问规则化，LLM 不裁决）", "done")
def _(self):
    return [
        ("拆条判定入口", *has("l2_split", "def decide_split")),
        ("长/短生命周期规则常量", *has("l2_split", "LONG_LIVED_TYPES", "SHORT_LIVED_TYPES")),
        ("解析层接线（不是死代码）", *has("l2_extract", "decide_split(")),
    ]


@check("D-22", "三问规则表（约束/偏好必拆、状态必不拆、事件看时间锚）", "done")
def _(self):
    return [
        ("长生命周期类型集合", *has("l2_split", "LONG_LIVED_TYPES")),
        ("经验分流技能库", *has("l2_split", "SKILL_ROUTE_TYPES")),
        ("事件独立时间锚判据", *has("l2_split", "occurred_at")),
    ]


@check("D-23", "L2 输出 schema + 解析失败降级默认值", "done")
def _(self):
    return [
        ("2.0 解析入口", *has("l2_extract", "def parse_l2_output")),
        ("needs_review 待审闸门", *has("l2_extract", "needs_review")),
        ("降级默认 derivation=inferred", *has("l2_extract", '"inferred"')),
        ("复用 l1 校验链（主干仍入库）", *has("l2_extract", "_validate_item")),
    ]


@check("D-26", "时间线：occurred_at 与记录时刻分离", "done")
def _(self):
    return [
        ("occurred_at 列", *has("db", "occurred_at")),
        ("落库路径传 occurred_at", *has("memory_dao", "occurred_at")),
        ("老库迁移函数", *has("db", "_migrate_mem_occurred_at")),
    ]


@check("D-28", "Supersession 严格化（三关 + 归档链 + superseded_by）", "done")
def _(self):
    return [
        ("归一化判重", *has("l3_rules", "def normalize_text")),
        ("显式改口信号检测", *has("l3_rules", "def has_change_signal")),
        ("三关合流裁决", *has("l3_rules", "def adjudicate")),
        ("superseded_by 指针写入", *has("memory_dao", "superseded_by")),
        ("归档表 memory_archive", *has("db", "memory_archive")),
    ]


@check("D-29", "合并语义三闸（精确归一→语义 top-5→LLM 1v1）", "planned")
def _(self):
    # 文档 §八第 25 条明写「D-29 合并闸无代码」→ 断言用于探测是否已偷偷落地。
    return [
        ("merge 三闸实现", *has("l2", "proof_count", "merge_gate")),
    ]


@check("D-30", "人工裁决队列 + 七天保守消化", "partial")
def _(self):
    # 已落地 = 待审队列/放行/拒绝/超期自动消化；未落地 = my_notes 独立列 + 工作台纠错按钮
    return [
        ("待审列表操作", *has("review_ops", "def list_pending")),
        ("超期保守直存", *has("review_ops", "def auto_resolve_expired")),
        ("HTTP 待审端点", *has("routes_review", "/v1/admin/review/pending")),
        ("MCP 超期消化工具", *has("mcp_server", "review_auto_resolve")),
    ]


@check("D-31", "双分数不加 + 手动文档对账（七条对账规则）", "planned")
def _(self):
    # 文档 §八第 9 条：「引擎侧 P0（规则预判层、手动文档对账、召回排除会话）——待施工排期」；
    # D-31 的对账七条（手动优先/触发苛刻/软停用旧记忆/不全文导入/不确定不动手/批量上限/
    # 快照去重）当前无实现符号 → 待施工，与文档一致。
    return [
        ("手动档案对账实现", *has("refine", "manual_profile", "doc_reconcile")),
    ]


@check("D-43", "断言双轴（modality/derivation + 闸门 A）", "done")
def _(self):
    r = []
    r.append(("modality 列已建", *has("db", '"modality"')))
    r.append(("解析层读模型 modality", *has("l2_extract", 'fact.get("modality")')))
    r.append(("valid_to 有写入点（闸门 A 输入）", *has("l2_extract", "_valid_to_for")))
    r.append(("落库路径传 valid_to", *has("pipeline", 'valid_to=m.get("valid_to")')))
    return r


@check("D-44", "删除两态 + 备份 30 天边界", "planned")
def _(self):
    # 文档 §3.8 明写 2.0 要加「增量快照保留 30 天 + --purge-backups」，
    # 当前备份轮转仍是 1.x 口径（增量快照永久保留）→ 待施工。
    return [
        ("增量快照保留期开关", *has("backup_manager", "keep_incremental_days")),
        ("逻辑删除原因标记 user_delete", *has("memory_ops", "user_delete")),
    ]


@check("D-46", "三轴分离（生命周期/质量/来源）", "partial")
def _(self):
    # 已落地 = 来源双轴列；未落地 = lifecycle 列与 TTL 到期不写 status（见 9.2 实际进度 4/9）
    return [
        ("来源轴 derivation 列", *has("db", '"derivation"')),
        ("来源轴 modality 列", *has("db", '"modality"')),
    ]


@check("D-48", "人格沉淀四条件闸门（多轮/跨天/多场景/来源可信）", "planned")
def _(self):
    # 文档 §八第 6 条把「D-48 四条件闸门在 scene_key 上的实际通过率」列入测试验证队列，
    # 但代码里只有 1.x 的 persona_block（confidence>=0.45 且 evidence>=3，ST-35），
    # 四条件（多轮>=3 会话 / 跨天>=2 / COUNT(DISTINCT scene_key)>=2 / 来源可信）无实现 → 待施工。
    return [
        ("四条件闸门（scene_key 消费侧）", *has("profile_inject", "scene_key")),
        ("多轮/跨天/多场景判据", *has("profile_inject", "多场景", "跨天")),
    ]


@check("D-50", "意义锚 M（四主料 + 三加分项，先等权跑分布）", "planned")
def _(self):
    return [
        ("meaning_anchor 列", *has("db", "meaning_anchor")),
        ("M 计算实现", *has("dream", "meaning_anchor")),
    ]


@check("D-52", "TTL 到期 0-4 五档处置", "planned")
def _(self):
    return [
        ("lifecycle 列", *has("db", "lifecycle")),
        ("ttl_renew_count 列", *has("db", "ttl_renew_count")),
        ("ttl_renewed_at 列", *has("db", "ttl_renewed_at")),
        ("inject_tier 列", *has("db", "inject_tier")),
        ("meaning_anchor 列", *has("db", "meaning_anchor")),
    ]


@check("D-53", "场景层取消（scene_key 替代聚合）", "planned")
def _(self):
    return [
        ("scene_key 列", *has("db", "scene_key")),
        ("scene_key 赋值点", *has("l2_extract", "scene_key")),
        ("场景聚合冻结开关", *has("refine", "scene_aggregate")),
    ]


@check("D-54", "两态删除的边界与入口（逻辑/彻底 + deletion_audit）", "planned")
def _(self):
    return [
        ("deletion_audit 事件表", *has("db", "deletion_audit")),
        ("内存操作层彻底删除入口", *has("memory_ops", "purge")),
        ("内存操作层删除审计", *has("memory_ops", "deletion_audit")),
    ]


@check("D-55", "迁移四阶段（加列→双写→回填→切读）", "planned")
def _(self):
    # 2026-11-03 修正：旧断言 has(config, "read_mode") 命中的是 guardrail 的
    # read_mode（与迁移无关）→ 假阳性。改断言「migration 配置段 + dual_write 开关」
    # 同时存在；9.2「实际进度」明写阶段 2–4 未开工（无 migration.dual_write），
    # 当前 0/2 → 与文档 planned 一致。
    return [
        ("migration 配置段", *has("config", '"migration"')),
        ("双写开关 migration.dual_write", *has("config", "dual_write")),
    ]


@check("D-56", "存量 NULL 语义（lifecycle/meaning_anchor/scene_key 回填）", "planned")
def _(self):
    return [
        ("lifecycle 列（回填载体）", *has("db", "lifecycle")),
        ("迁移配置段", *has("config", '"migration"')),
    ]


@check("D-57", "迁移校验十项（全绿才准切读）", "planned")
def _(self):
    # 设计报告落 docs/migration/2.0-迁移校验-YYYYMMDD.md（9.4 末）；9.2 结论：0/10 脚本化
    return [
        ("迁移校验报告目录", *exists("docs/migration")),
    ]


@check("D-58", "兼容期与旧适配器降级（capabilities.api_compat）", "planned")
def _(self):
    return [
        ("capabilities 端点", *has("app", "capabilities")),
        ("api_compat 字段", *has("app", "api_compat")),
    ]


@check("D-61", "关怀档位按角色配置（静默/会话内/主动推送）", "planned")
def _(self):
    # 角色卡目前只有 greeting_templates / trigger_rules / frequency，没有「档位」语义字段
    hit = any(k in src("care_ops") for k in ("care_level", "frequency_level", "care_tier"))
    return [("角色卡档位字段（care_level / frequency_level）", hit,
             f"{rel('care_ops')}: 未找到档位字段")]


@check("D-62", "知识库自动关联与自动分类", "planned")
def _(self):
    return [
        ("wiki_revisions 表", *has("db", "wiki_revisions")),
        ("wiki_categories 表", *has("db", "wiki_categories")),
    ]


# ---------- 角色 / 关怀 ----------
@check("D-65", "角色隐私层访问控制", "partial")
def _(self):
    # 已落地 = ①②③（白名单/403/审计/告警）；未落地 = ④ 工作台授权页（§八第 8 条 ⬜）
    return [
        ("白名单校验", *has("privacy_guard", "def check_access")),
        ("装配接线", *has("care_ops", "privacy_guard.check_access")),
        ("越权审计", *has("privacy_guard", "security_log")),
    ]


@check("D-66", "七值 type + certainty + subject 必填", "done")
def _(self):
    return [
        ("七值 type 白名单", *has("l1", "DOMAIN_TYPES")),
        ("certainty 三值", *has("l1", "VALID_CERTAINTY")),
        ("subject 列", *has("db", '"subject"')),
        ("注入侧 subject 过滤（消费侧）", *has("profile_inject", "subject_filter=")),
    ]


@check("D-67", "route 按来源分流 + 候选池消费", "partial")
def _(self):
    # 已落地 = 分流白名单 + 候选池；未落地 = ⑤ route 代码侧后置校验（§八第 17 条 ⬜）
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


@check("D-69", "六槽位做工规范（四卡 sgme_profile）", "partial")
def _(self):
    # 已落地 = 四卡 sgme_profile 五键 + 六槽位；未落地 = 「禁忌」三元组完整化、伴侣/朋友卡字数
    # （§八第 19 条：缺"禁忌"三元组，完整版仍在旧 personality 字段）
    r = []
    slots = ("语言风格", "情绪反应", "价值观取舍", "社交姿态", "冲突处理", "亲密表达")
    for role in ("butler", "companion", "friend", "mentor"):
        relpath = f"roles/{role}.json"
        ok, msg = json_has(relpath, "data", "extensions", "sgme_profile", "personality_slots")
        detail = msg
        if ok:
            card = json.loads((ROOT / relpath).read_text(encoding="utf-8"))
            got = card["data"]["extensions"]["sgme_profile"]["personality_slots"]
            missing = [s for s in slots if s not in got]
            ok = not missing
            detail = f"{relpath}: 六槽位齐全" if ok else f"{relpath}: 缺槽位 {missing}"
        r.append((f"{role} 卡六槽位", ok, detail))
    return r


@check("D-70", "记忆回指 last_mentions", "done")
def _(self):
    return [
        ("取数模块", *has("last_mentions", "def pick(")),
        ("注入接线", *has("inject_ops", "_attach_last_mentions")),
        ("默认关闭（首月 off）", *has("config", '"enabled": False')),
    ]


@check("D-71", "人设演化（首版只留数据结构位，不做自动改写）", "done")
def _(self):
    return [
        ("演化层读取", *has("care_roles", "def load_evolution")),
        ("演化层追加", *has("care_roles", "def append_evolution")),
        ("演化层 HTTP 端点", *has("routes_care", "/evolution")),
    ]


@check("D-72", "冷场修复", "done")
def _(self):
    return [
        ("会话状态判定", *has("conversation_state", "def assess")),
        ("一次性闸", *has("conversation_state", "def should_repair")),
        ("注入接线", *has("inject_ops", "coldstart_repair")),
    ]


@check("D-73", "召回加固（幂律放大 + 自然日去重）", "partial")
def _(self):
    # 已落地 = 公式/接线/去重；未落地 = 系数标定、ttl_disposition.reason 列（§八第 21 条 ⬜）
    return [
        ("幂律公式", *has("recall_aging", "def compute_ttl_multiplier")),
        ("接线梦境", *has("dream", "compute_ttl_multiplier")),
        ("recall_count 自然日去重", *has("memory_stats_dao", "date(memory_stats.last_injected_at)")),
        ("last_recalled_at 写入", *has("memory_stats_dao", "last_recalled_at")),
    ]


@check("D-74", "角色真实感指标", "partial")
def _(self):
    # 已落地 = 四指标 + 基线脚本；未落地 = 四卡快照与阈值拍板（§八第 21 条 ⬜）
    return [
        ("四项指标实现", *has("role_metrics", "AI_TASTE_PATTERNS")),
        ("指标落卡", *has("role_metrics", "sgme_metrics")),
        ("测量脚本", (ROOT / "scripts" / "measure_role_realism.py").exists(),
         "scripts/measure_role_realism.py"),
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


@check("D-78", "惊讶度先验（存同类型分位）", "partial")
def _(self):
    # 已落地 = surprise 列 + 分位映射 + 落库钩子；未落地 = 接 D-50 第五先验 / D-29 合并闸 /
    # forget 豁免（§八第 23 条 ⬜ 缺口）
    return [
        ("surprise 列", *has("db", '"surprise"')),
        ("分位映射", *has("surprise", "_percentile_of")),
        ("落库钩子", *has("surprise", "compute_for_store")),
    ]


# ---------- 知识 / 技能 ----------
@check("D-59", "技能落盘缓存（服务端 + 适配器）", "done")
def _(self):
    return [
        ("catalog_version", *has("routes_skills", "catalog-version")),
        ("manifest 端点", *has("routes_skills", "manifest")),
        ("包端点", *has("routes_skills", "package")),
        ("服务端缓存", *has("cache", "package_sha256")),
    ]


@check("D-60", "八适配器统一改技能获取层（adapter_parity 全绿）", "partial")
def _(self):
    # 已落地 = 八适配器对账基建 + 动态转发器守卫；未落地 = codex/claude-code 缓存层（T-406b）
    return [
        ("能力面对账脚本", *has("adapter_parity", "def load_map")),
        ("动态转发器守卫", *has("adapter_parity", "dynamic_forwarder")),
        ("八适配器清单入 map", *has("parity_map", "hermes", "dsh", "doubao", "mimo",
                                "workbuddy", "zcode", "codex", "claude-code")),
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


@check("D-63", "LLM 成本计量", "partial")
def _(self):
    # 已落地 = llm_usage_daily + 三阶段记账 + llm-cost 端点；未落地 = 告警线/04:00 汇总/
    # 超限停摆/分块失败账本（§八第 12 条 ⬜）
    return [
        ("llm_usage_daily 表", *has("llm_usage_dao", "llm_usage_daily")),
        ("成本查询端点", *has("routes_ops", "llm-cost")),
    ]


@check("D-64", "兼容期 409 清单与 capabilities", "planned")
def _(self):
    return [("capabilities 端点", *has("app", "capabilities"))]


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="设计决策 ↔ 代码契约对账（只读）")
    ap.add_argument("--only", nargs="*", help="只查这些决策号（如 D-68 D-73）")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="发布门禁模式：把「文档落后」（代码已落地但文档仍标未实现）升级为失败；待施工（文档已声明未实现）不算失败",
    )
    args = ap.parse_args(argv)

    rows = []
    mismatch = 0
    not_done = 0
    partial_n = 0
    lag = 0
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

        # 判定：文档声明已实现/部分实现，但声明的已落地部分代码不满足 → MISMATCH。
        if c["doc"] in ("done", "partial") and not allpass:
            verdict = "❌ MISMATCH（文档声明已实现，代码不满足）"
            mismatch += 1
        elif c["doc"] == "planned" and allpass:
            verdict = "🔁 文档落后（代码已落地，文档仍标待施工）"
            lag += 1
        elif c["doc"] == "planned":
            verdict = "⏳ 待施工（与文档一致）"
            not_done += 1
        elif c["doc"] == "partial":
            verdict = "🟡 部分实现（文档已声明剩余缺口）"
            partial_n += 1
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

    done_n = sum(1 for r in rows if r[2] == "done" and "MISMATCH" not in r[4])
    print("\n" + "-" * 88)
    if args.only:
        scope = f"本次检查 {len(rows)} 条"
    else:
        scope = (f"覆盖 {len(rows)}/{TOTAL_DECISIONS} 条决策"
                 f"（{len(rows) / TOTAL_DECISIONS * 100:.1f}%）")
    print(
        f"{scope}；MISMATCH {mismatch} 条；待施工 {not_done} 条；"
        f"部分实现 {partial_n} 条；文档落后 {lag} 条；已实现一致 {done_n} 条"
    )
    if mismatch:
        print("\n处置：MISMATCH 项要么补代码、要么把文档状态改回未实现——不能放着。")
    if args.strict and lag:
        print(f"\n❌ 严格模式：{lag} 条文档落后（代码已落地但文档仍标待施工）→ 发布门禁不通过。")
    return 1 if mismatch or (args.strict and lag) else 0


if __name__ == "__main__":
    raise SystemExit(main())
