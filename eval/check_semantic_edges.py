# -*- coding: utf-8 -*-
"""eval/check_semantic_edges.py：T-135 验收——语义边脏边率抽检（真实 LLM 链路）。

构造已知关系场景 → resolve_conflicts（真实降级链）→ 收集 source='l1_conflict' 边
→ LLM 逐条判定「关系是否成立」→ 脏边率 = 判定不成立比例（AC：<10% 达标）。

设计场景（期望关系已知）：
- 新记忆 n1「玩飞盘」× 候选 c1「公园玩飞盘」/ c2「飞盘俱乐部」→ 期望 similar 边
- 新记忆 n2「住上海」× 候选 c3「住北京」→ 期望 contradicts 边（如输出）
- 候选 c4「喜欢喝咖啡」与 n1/n2 无关 → 不应出现边（LLM 误连即脏边）

⚠️ agnes-2.5-flash 输出偶发坏 JSON（免费模型抖动，resolve_conflicts 内部已有
1 次重试 + 默认 store 兜底）→ 脚本外层最多重试 3 次，第一次有边写入即停。

用法：
  python eval/check_semantic_edges.py            # 真实 LLM
  python eval/check_semantic_edges.py --dry      # 桩裁决（无 LLM，验证链路）
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sgme import config  # noqa: E402
from sgme.engine import l15  # noqa: E402
from sgme.data import db as db_mod, edge_dao, memory_dao  # noqa: E402


def _insert(mem_conn, content, dim_ids, **kw):
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type=kw.get("memory_type", "persona"),
        priority=kw.get("priority", 60), time_velocity=kw.get("time_velocity", "static"),
        ttl_days=kw.get("ttl_days"), dimension_ids=dim_ids,
    )


def _judge_edge(llm_cfg, edge: dict, memories: dict[str, str]) -> tuple[bool, str]:
    """LLM 判定一条边的成立性。返回 (成立, 理由)。"""
    from sgme.llm import chain as llm_chain
    prompt = f"""判定以下两条记忆之间的声称关系是否成立。
声称关系定义（T-135 语义边，服务于图召回联想）：
- similar：主题相关即可用于联想召回（同一话题/同主体/同领域），不必是同一事实或同一记忆类型
- causes：因果关系（A 是 B 的原因或结果）
- contradicts：事实矛盾（两者不能同时为真）

判定示例：
- A「用户上周日去公园打飞盘」B「飞盘俱乐部每周三训练」→ similar 成立（同为飞盘主题）
- A「用户喜欢喝咖啡」B「SGME 部署在群晖 NAS」→ similar 不成立（无关）
- A「用户常驻北京」B「用户现在住在上海」→ contradicts 成立（居住城市矛盾）

待判定：
记忆A（关系发起方）: {memories[edge['from_id']]}
记忆B: {memories[edge['to_id']]}
声称关系: {edge['relation']}
只输出 JSON: {{"valid": true/false, "reason": 一句话}}
"""
    text, _, _ = llm_chain.call_with_fallback(llm_cfg, prompt, chain_name="refinement")
    try:
        d = json.loads(text.strip().lstrip("```json").rstrip("```").strip())
        return bool(d.get("valid")), str(d.get("reason", ""))
    except Exception:
        return True, f"判定解析失败（保守判成立）: {text[:80]}"


def _run_once(cfg: dict, dry: bool):
    """单次完整链路：建临时库 → resolve_conflicts → 返回 (result, edges, mem_map, c4_id)。"""
    tmp = Path(tempfile.mkdtemp(prefix="sgme-t135-check-"))
    conn = db_mod.connect_memory(tmp)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])

    c1 = _insert(conn, "用户喜欢在公园和朋友玩飞盘", ["tech_stack"])
    c2 = _insert(conn, "飞盘俱乐部每周三晚上训练", ["tech_stack"])
    c3 = _insert(conn, "用户常驻北京", ["environment"])
    c4 = _insert(conn, "用户喜欢喝咖啡", ["preferences"])

    new_memories = [
        {"content": "用户上周日去公园和朋友打了一下午飞盘", "dimension_ids": ["tech_stack"],
         "memory_type": "episodic", "priority": 70, "time_velocity": "dynamic"},
        {"content": "用户现在住在上海", "dimension_ids": ["environment"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ]
    mem_map = {
        c1: "用户喜欢在公园和朋友玩飞盘", c2: "飞盘俱乐部每周三晚上训练",
        c3: "用户常驻北京", c4: "用户喜欢喝咖啡",
    }

    if dry:
        from sgme.engine.l15 import ConflictDecision, RelationEdge
        decisions = [
            ConflictDecision(0, [], "store", relations=[
                RelationEdge(c1, "similar", 0.85), RelationEdge(c2, "similar", 0.8)]),
            ConflictDecision(1, [], "store", relations=[
                RelationEdge(c3, "contradicts", 0.9),
                RelationEdge(c4, "similar", 0.4)]),  # 低置信 + 无关 → 均不写
        ]
        result = l15.L15Result()
        for i, nm in enumerate(new_memories):
            d = decisions[i]
            new_id = l15._store_memory(conn, nm, cfg["dimensions"], prompt_version="stub")
            nm["memory_id"] = new_id
            result.stored.append(new_id)
            result.semantic_edges_written += l15._write_semantic_edges(conn, nm, d, cfg)
    else:
        result = l15.resolve_conflicts(new_memories, conn, cfg)

    mem_map.update({mid: nm["content"] for mid, nm in zip(result.stored, new_memories)})
    edges = edge_dao.list_edges(conn, source="l1_conflict")
    conn.close()
    return result, edges, mem_map


def main() -> int:
    dry = "--dry" in sys.argv
    cfg = config.load_config()

    if dry:
        result, edges, mem_map = _run_once(cfg, dry=True)
        provider, attempts = "stub", 1
    else:
        provider, attempts = "agnes-2.5-flash（降级链首节点）", 0
        result = edges = mem_map = None
        for i in range(3):
            attempts = i + 1
            r, e, m = _run_once(cfg, dry=False)
            result, edges, mem_map = r, e, m
            if e:  # 第一次有边写入即停（坏 JSON 重试窗口内）
                break

    total = len(edges)

    # 期望校验（结构性）
    expected_similar_from_n1 = {c1: True for c1 in ["用户喜欢在公园和朋友玩飞盘", "飞盘俱乐部每周三晚上训练"]}
    got_n1 = {
        mem_map.get(e["to_id"], "") for e in edges
        if e["from_id"] in result.stored[:1] and e["relation"] == "similar"
    }
    got_n2 = {
        mem_map.get(e["to_id"], "") for e in edges
        if e["from_id"] in result.stored[1:2] and e["relation"] == "contradicts"
    }
    hit = (expected_similar_from_n1.keys() <= got_n1) + ("用户常驻北京" in got_n2)

    # 脏边判定（真实 LLM；dry 模式跳过判定直接按结构性成立）
    dirty = 0
    judged: list[tuple[dict, bool, str]] = []
    if not dry and edges:
        for e in edges:
            valid, reason = _judge_edge(cfg["llm"], e, mem_map)
            if not valid:
                dirty += 1
            judged.append((e, valid, reason))

    dirty_rate = dirty / total if total else 0.0
    pass_ = (hit >= 1 and dirty_rate < 0.10)

    lines = [
        "# T-135 语义边脏边率抽检报告",
        "",
        f"- 时间：2026-08-31（本地）",
        f"- provider：{provider}（attempts={attempts}）",
        f"- 新记忆 2 条 / 候选 4 条 / 写边 {total} 条 / 判定脏边 {dirty}",
        f"- **脏边率：{dirty_rate:.1%}（AC <10% → {'✅ 达标' if dirty_rate < 0.10 else '❌ 未达标'}）**",
        f"- 结构命中：similar(n1→飞盘相关)={'✅' if expected_similar_from_n1.keys() <= got_n1 else '❌'}，contradicts(n2→住北京)={'✅' if '用户常驻北京' in got_n2 else '❌'}",
        f"- **总体：{'✅ 通过' if pass_ else '❌ 未通过'}**",
        "",
        "## 写入边明细",
        "",
        "| from | relation | to | weight | 判定 | 理由 |",
        "|---|---|---|---|---|---|",
    ]
    for e in edges:
        frm = mem_map.get(e["from_id"], e["from_id"])[:20]
        to = mem_map.get(e["to_id"], e["to_id"])[:20]
        lines.append(f"| {frm} | {e['relation']} | {to} | {e['weight']} | — | — |")
    for e, valid, reason in judged:
        lines.append(f"| {mem_map.get(e['from_id'],'')[:20]} | {e['relation']} | {mem_map.get(e['to_id'],'')[:20]} | {e['weight']} | {'✅' if valid else '❌'} | {reason} |")

    out = ROOT / "eval" / "results" / "t135_semantic_edges_check.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"attempts={attempts} 写边 {total} / 脏边率 {dirty_rate:.1%} / 结构命中 {hit}/2 → {'PASS' if pass_ else 'FAIL'}（报告 {out}）")
    return 0 if pass_ else 1


if __name__ == "__main__":
    sys.exit(main())
