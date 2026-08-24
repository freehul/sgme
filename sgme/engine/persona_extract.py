"""engine/persona_extract.py：实时规则特质抽取（ST-35 T-99）。

从本次提炼落库的记忆中按规则抽取人格特质信号，累积写入 persona_traits。
**零 LLM 调用**——挂在 finalize_refinement 收尾（与 L2/embedding/信号同层），
失败不阻塞主路径。

设计原则（Backlog ST-35）：
- 倾向而非判决：每条记忆最多贡献一个证据点，置信度由 persona_dao 累积
- 规则可配置：config persona.rules 段（dimension + keywords），缺省内置六维
- 单条记忆不改写整体：本模块只做 upsert_trait，不做任何覆盖/supersede 判断
  （supersede 仅由月度校准 T-100 触发）
"""

from __future__ import annotations

import logging

from sgme.data import persona_dao

logger = logging.getLogger("sgme.engine.persona_extract")

# 内置规则表（缺省；config persona.rules 可追加/覆盖同名 dimension）
# value = 特质倾向名，keywords = 记忆 content 命中任一关键词即算一条证据
DEFAULT_RULES: list[dict] = [
    {
        "dimension": "decision_style",
        "values": [
            {"value": "原则先于利益", "keywords": ["沉没成本", "原件永不删", "正确大于能跑", "底线"]},
            {"value": "成本敏感", "keywords": ["成本", "费用", "token 消耗", "烧钱", "账单"]},
        ],
    },
    {
        "dimension": "work_style",
        "values": [
            {"value": "计划驱动", "keywords": ["先列清单", "报备", "方案确认", "计划先行", "文档第一"]},
            {"value": "发现即修复", "keywords": ["发现问题就要处理", "立即修", "马上修", "不能留"]},
        ],
    },
    {
        "dimension": "quality_standard",
        "values": [
            {"value": "真实高于效率", "keywords": ["mock 数据", "真实链路", "拒绝模拟", "不要伪造"]},
            {"value": "像素级验收", "keywords": ["像素级", "逐项验收", "现象确认"]},
        ],
    },
    {
        "dimension": "responsibility",
        "values": [
            {"value": "长期负重型", "keywords": ["透析", "照护", "照顾"]},
        ],
    },
]

# 单次提炼单维度最多累计的证据数（防止单会话刷爆某特质）
MAX_EVIDENCE_PER_DIM_PER_RUN = 3


def extract_rules_config(cfg: dict) -> list[dict]:
    """读 config persona.rules；无配置返回内置缺省。"""
    persona_cfg = (cfg or {}).get("persona") or {}
    return persona_cfg.get("rules") or DEFAULT_RULES


def _match_memories(
    memories: list[dict], rules: list[dict]
) -> dict[str, dict[str, int]]:
    """规则匹配：{dimension: {value: 命中次数}}。"""
    hits: dict[str, dict[str, int]] = {}
    for m in memories:
        content = m.get("content") or ""
        if not content:
            continue
        for rule in rules:
            dim = rule.get("dimension")
            if not dim:
                continue
            for v in rule.get("values", []):
                val = v.get("value")
                kws = v.get("keywords") or []
                if any(kw in content for kw in kws):
                    bucket = hits.setdefault(dim, {})
                    # 单记忆单值只记一次（同一事实反复提及不算多证据）
                    if bucket.get(val, 0) < MAX_EVIDENCE_PER_DIM_PER_RUN:
                        bucket[val] = bucket.get(val, 0) + 1
    return hits


def extract_and_store(
    result, mem_conn, cfg: dict
) -> dict:
    """从 RefineResult 的已落库记忆抽取特质信号并累积写入。

    Args:
        result: refine_mod.RefineResult（memories 需已带 memory_id 供溯源）。
        mem_conn: memory.db 连接。
        cfg: 全量配置（读 persona.rules / persona.enabled）。

    Returns:
        统计 dict：{"enabled", "traits_touched", "evidence_added"}。
    """
    stats = {"enabled": False, "traits_touched": 0, "evidence_added": 0}
    persona_cfg = (cfg or {}).get("persona") or {}
    if not persona_cfg.get("enabled", True):
        return stats
    stats["enabled"] = True
    memories = [m for m in (result.memories or []) if m.get("memory_id")]
    if not memories:
        return stats

    rules = extract_rules_config(cfg)
    hits = _match_memories(memories, rules)
    file_id = getattr(result, "file_id", "") or ""
    try:
        for dim, values in hits.items():
            stats["traits_touched"] += 1
            for val, count in values.items():
                for _ in range(count):
                    # 溯源：file_id 级别即可（同批多记忆命中同一值合并为一条证据链节点）
                    persona_dao.upsert_trait(
                        mem_conn, dim, val,
                        evidence_ref=f"refine:{file_id}",
                        scene_context="general",
                        source="rule",
                    )
                    stats["evidence_added"] += 1
    except Exception as e:  # noqa: BLE001 —— 特质抽取失败不阻塞主路径
        logger.warning("特质抽取失败（不阻塞）: %s", e)
        stats["error"] = str(e)
    return stats
