# -*- coding: utf-8 -*-
"""eval/check_facts.py：T-136 验收冒烟——真实 LLM 三元组抽取成功率（agnes-2.5-flash）。

会话埋 5 个确定性可断言事实 → l1.extract_l1（真实降级链）→ 统计：
- 期望事实被抽取为合法三元组的比例（抽取成功率，AC ≥85%）
- 三元组结构合法率（subject/predicate/object 非空）
- 报告落 eval/results/t136_facts_smoke.md

用法：
  python eval/check_facts.py            # 真实 LLM 冒烟
  python eval/check_facts.py --dry      # 桩函数（无 LLM，验证脚本链路）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgme import config  # noqa: E402
from sgme.engine import l1  # noqa: E402

# 期望事实（确定性、可断言）→ (subject, predicate, object) 允许的归一化形式
EXPECTED_FACTS = [
    {"subject": "张伟", "predicate": "任职于", "object": "腾讯"},
    {"subject": "张伟", "predicate": "负责", "object": "AI 平台"},
    {"subject": "SGME", "predicate": "部署于", "object": "群晖 NAS"},
    {"subject": "李雷", "predicate": "任职于", "object": "阿里巴巴"},
    {"subject": "飞盘俱乐部", "predicate": "训练时间", "object": "每周三"},
]

CONVERSATION = """# 2026-08-31T09:00:00Z user
最近把 SGME 部署到了家里的群晖 NAS 上，跑得很稳。
# 2026-08-31T09:00:05Z assistant
好的，SGME 部署在群晖 NAS 上，端口 9910。
# 2026-08-31T09:01:00Z user
我朋友张伟在腾讯工作，负责 AI 平台。他最近很忙。
# 2026-08-31T09:01:30Z user
李雷在阿里巴巴做后端。
# 2026-08-31T09:02:00Z user
飞盘俱乐部每周三晚上训练，我常去。
"""


def _stub_extract(conversation, dimensions, llm_cfg, **kw):
    """桩：直接返回预置记忆（验证脚本链路，不调 LLM）。"""
    memories = [
        {"content": "SGME 部署在群晖 NAS 上", "dimensions": ["environment"],
         "memory_type": "persona", "priority": 75, "time_velocity": "static",
         "source_message_ids": [0], "supersedes": [], "facts": [
            {"subject": "SGME", "predicate": "部署于", "object": "群晖 NAS"}]},
        {"content": "张伟在腾讯工作，负责 AI 平台", "dimensions": ["goals"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [2], "supersedes": [], "facts": [
            {"subject": "张伟", "predicate": "任职于", "object": "腾讯"},
            {"subject": "张伟", "predicate": "负责", "object": "AI 平台"}]},
    ]
    return memories, "stub", {"stage": "l1_extraction", "version": "stub", "variant": None}


def _norm(s: str) -> str:
    """归一化：去全部空白 + 小写（LLM 输出「群晖NAS」vs 期望「群晖 NAS」空格差异）。"""
    return "".join(str(s).split()).lower()


def _match(fact: dict, expected: dict) -> bool:
    """宽松匹配：subject 归一化相等；predicate/object 归一化后互含子串（任一方向）。"""
    if _norm(fact.get("subject")) != _norm(expected["subject"]):
        return False
    p, o = _norm(fact.get("predicate")), _norm(fact.get("object"))
    ep, eo = _norm(expected["predicate"]), _norm(expected["object"])
    return (ep in p or p in ep) and (eo in o or o in eo)


def main() -> int:
    dry = "--dry" in sys.argv
    cfg = config.load_config()
    if dry:
        l1.extract_l1 = _stub_extract  # type: ignore[assignment]
        provider = "stub"
    else:
        provider = "agnes-2.5-flash（降级链首节点）"

    memories, real_provider, meta = l1.extract_l1(
        CONVERSATION, cfg["dimensions"], cfg["llm"],
    )

    # 统计：期望事实命中 / 合法三元组
    all_facts: list[dict] = []
    for m in memories:
        for f in m.get("facts") or []:
            if f.get("subject") and f.get("predicate") and f.get("object"):
                all_facts.append(f)
    hit = sum(1 for e in EXPECTED_FACTS if any(_match(f, e) for f in all_facts))
    rate = hit / len(EXPECTED_FACTS)

    lines = [
        "# T-136 三元组抽取冒烟报告",
        "",
        f"- 时间：2026-08-31（本地）",
        f"- provider：{provider}（实际 {real_provider}，prompt {meta.get('version')}）",
        f"- 会话记忆数：{len(memories)}",
        f"- 合法三元组总数：{len(all_facts)}",
        f"- 期望事实：{len(EXPECTED_FACTS)} 条",
        f"- 命中：{hit} / {len(EXPECTED_FACTS)}",
        f"- **抽取成功率：{rate:.1%}（AC ≥85% → {'✅ 达标' if rate >= 0.85 else '❌ 未达标'}）**",
        "",
        "## 期望事实 vs 命中",
        "",
        "| 期望 (subject/predicate/object) | 命中 |",
        "|---|---|",
    ]
    for e in EXPECTED_FACTS:
        ok = any(_match(f, e) for f in all_facts)
        lines.append(f"| {e['subject']} / {e['predicate']} / {e['object']} | {'✅' if ok else '❌'} |")

    lines += ["", "## 实际抽取三元组", ""]
    if all_facts:
        for f in all_facts:
            lines.append(f"- {f['subject']} —{f['predicate']}→ {f['object']}")
    else:
        lines.append("（无）")

    out = ROOT / "eval" / "results" / "t136_facts_smoke.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"抽取成功率 {rate:.1%} → {'PASS' if rate >= 0.85 else 'FAIL'}（报告 {out}）")
    return 0 if rate >= 0.85 else 1


if __name__ == "__main__":
    sys.exit(main())
