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
            {"value": "长期负重型", "keywords": ["照护", "照顾", "陪护"]},
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
) -> dict[str, dict[str, list[dict]]]:
    """规则匹配：{dimension: {value: [命中的记忆, ...]}}（每值最多 N 条）。

    D-48（2026-11-04）：返回**记忆对象**而不是计数——四条件闸门需要每条证据的
    会话/日期/场景/来源字段，计数无法支撑判定。同一记忆对同一值只记一次。
    """
    hits: dict[str, dict[str, list[dict]]] = {}
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
                    bucket = hits.setdefault(dim, {}).setdefault(val, [])
                    if len(bucket) < MAX_EVIDENCE_PER_DIM_PER_RUN:
                        bucket.append(m)
    return hits


def _evidence_for(mem: dict, mem_conn, file_id: str) -> dict:
    """构造结构化证据条目（D-48）：从落库行取权威字段，缺失回落 L2 字段。"""
    mid = str(mem.get("memory_id") or "")
    row = None
    if mid:
        row = mem_conn.execute(
            "SELECT occurred_at, updated_at, scene_key, derivation, modality "
            "FROM memories WHERE memory_id=?", (mid,)
        ).fetchone()
    occurred = (row["occurred_at"] if row else None) or mem.get("occurred_at") or mem.get("updated_at")
    scene = (row["scene_key"] if row else None) or mem.get("scene_key") or ""
    derivation = (row["derivation"] if row else None) or mem.get("derivation") or ""
    modality = (row["modality"] if row else None) or mem.get("modality") or ""
    return {
        "memory_id": mid,
        "file_id": str(file_id or ""),
        "occurred_at": occurred or "",
        "scene_key": str(scene or ""),
        "derivation": str(derivation or ""),
        "modality": str(modality or ""),
    }


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
            for val, hit_memories in values.items():
                for mem in hit_memories:
                    # D-48：结构化证据（会话/日期/场景/来源/模态）——四条件闸门的输入
                    persona_dao.upsert_trait(
                        mem_conn, dim, val,
                        evidence_ref=_evidence_for(mem, mem_conn, file_id),
                        scene_context="general",
                        source="rule",
                    )
                    stats["evidence_added"] += 1
    except Exception as e:  # noqa: BLE001 —— 特质抽取失败不阻塞主路径
        logger.warning("特质抽取失败（不阻塞）: %s", e)
        stats["error"] = str(e)
    return stats
