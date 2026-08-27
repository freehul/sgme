"""engine/scene_gc.py：L2 场景主动治理（T-97 治本方案）。

背景
----
L2 场景数超 max_scenes 后，finalize_refinement 仅调 l2.check_scene_threshold 发软告警，
并不降数——新场景持续累积（生产 active 350 > max 300 红警常年触发）。手动跑
merge_similar_scenes.py 只是临时降，治标不治本。

本模块把「场景相似度检测 -> 自动合并 -> 自动归档」做成可复用治理逻辑，由 Dream 夜间
整理（run_dream 生命周期步骤）定时调用，亦可经 /v1/admin/scene-gc/trigger 手动触发。

复用（不重写归档机制）
----------------------
- 相似度检测直接读 scene_vectors 表（T-97 B102/B103 已回填，无需重新 embed）
- 合并落库直接调 l2._apply_merge：一处完成「合并新场景 + 自动归档旧场景
  (status='archived'，可恢复) + 写 scene_versions 快照 + 刷新场景向量」，
  符合「原件永不删」铁律与 Supersession 约束

触发策略
--------
- 仅当 active 场景数 >= trigger_at（默认 = l2.warn_thresholds.orange）才执行，避免无谓 LLM 消耗
- 单次上限 max_merges（默认 20）：仍超限则次日 Dream 继续压，渐进收敛（防一次 LLM 烧太多）
- 相似度 >= merge_threshold（默认 0.80）的对入选

调度
----
本模块不自带定时器（用户决策：并入 Dream 夜间整理，复用其 03:00 节奏与执行锁）。
RUN_LOCK 防手动 trigger 与 Dream 并发合并（同场景被两次合并 -> scene_id 失效）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from sgme.data import scene_dao
from sgme.engine import l2
from sgme.llm import chain as llm_chain

logger = logging.getLogger("sgme.engine.scene_gc")

#: 执行锁（防手动 trigger 与 Dream 并发合并导致 scene_id 竞争）
RUN_LOCK = threading.Lock()

#: 合并提示词模板（忠实聚合两场景正文，复用 merge_similar_scenes.py 的范式）
MERGE_PROMPT_TMPL = """你是记忆整合架构师。以下两个场景主题高度相似（相似度 {sim}），请将它们合并为一个**场景叙事文档**。

要求：
1. 忠实事实聚合，保留双方关键细节（数字/日期/名称/技术名词），不虚构、不文学修饰
2. 重复信息去重，用简洁条目或短段落组织
3. 第一行必须是标题：`# 合并后标题`
4. 只输出合并后的场景正文，无其他文字

## 场景 A（{aid}）
{ac}

## 场景 B（{bid}）
{bc}
"""

#: 单轮合并 LLM 连续失败退避上限（连续失败即中止本轮，避免死循环烧钱）
_MAX_CONSECUTIVE_FAILS = 3


@dataclass
class SceneGcResult:
    """单次场景治理结果。"""
    trigger_at: int = 0
    active_before: int = 0
    active_after: int = 0
    candidates: int = 0          # 入选（去重叠后）待合并对总数
    considered: int = 0           # 实际参与合并尝试的对数
    merged: int = 0               # 合并产生的新场景数
    archived: int = 0             # 被归档的旧场景数
    skipped: int = 0              # 跳过（已非 active/已处理）
    failed: int = 0               # 合并失败（LLM/落库异常）
    skipped_reason: str | None = None  # 不达标/未执行原因


def _gc_cfg(cfg: dict) -> dict:
    """取 scene_gc 段，缺省空字典。"""
    return cfg.get("scene_gc") or {}


def list_merge_candidates(mem_conn: sqlite3.Connection, cfg: dict) -> list[dict]:
    """纯检测（dry-run，不执行合并）：读 active 场景向量 -> 两两相似度 -> 取超阈值对 -> 贪心去重叠。

    Returns:
        [{"sim","scene_a","scene_b","title_a","title_b","heat_a","heat_b"}]，
        按 sim 降序。空列表 = 无可合并对。
    """
    gc = _gc_cfg(cfg)
    threshold = float(gc.get("merge_threshold", 0.80))

    rows = scene_dao.list_active_scene_vectors(mem_conn)
    if len(rows) < 2:
        return []

    vecs: list[np.ndarray] = []
    meta: list[dict] = []
    for r in rows:
        emb = r.get("embedding")
        dims = r.get("dims")
        if not emb or not dims:
            continue
        try:
            v = np.frombuffer(emb, dtype=np.float32)
        except Exception:
            continue
        if v.shape[0] != int(dims):
            continue
        vecs.append(v.astype(np.float32))
        meta.append({
            "scene_id": r["scene_id"],
            "title": r.get("title") or "",
            "heat": int(r.get("heat") or 1),
            "content": r.get("content") or "",
        })
    n = len(vecs)
    if n < 2:
        return []

    V = np.stack(vecs)
    V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    S = V @ V.T

    # 上三角 >= 阈值
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = float(S[i][j])
            if sim >= threshold:
                pairs.append((sim, i, j))
    pairs.sort(reverse=True)

    # 贪心去重叠：同一场景不出现在多个对（否则前面合并后 scene_id 失效）
    used: set[int] = set()
    selected: list[dict] = []
    for sim, i, j in pairs:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        a, b = meta[i], meta[j]
        selected.append({
            "sim": sim,
            "scene_a": a["scene_id"], "scene_b": b["scene_id"],
            "title_a": a["title"], "title_b": b["title"],
            "heat_a": a["heat"], "heat_b": b["heat"],
        })
    logger.info(
        "场景治理候选检测：active 向量=%d 超阈值对=%d 去重叠后=%d（threshold=%.2f）",
        n, len(pairs), len(selected), threshold,
    )
    return selected


def run_scene_gc(
    mem_conn: sqlite3.Connection,
    cfg: dict,
    client=None,
) -> SceneGcResult:
    """执行一次场景治理（带 RUN_LOCK 防并发）。

    - scene_gc.enabled=false -> skipped_reason='disabled'
    - active < trigger_at -> skipped_reason='below_trigger'（避免无谓 LLM 消耗）
    - 否则逐对调 l2._apply_merge（自动合并 + 归档旧场景 + 刷向量），受 max_merges 上限
    """
    if not RUN_LOCK.acquire(blocking=False):
        return SceneGcResult(skipped_reason="running")
    try:
        return _run_gc_locked(mem_conn, cfg, client)
    finally:
        RUN_LOCK.release()


def _run_gc_locked(mem_conn, cfg, client) -> SceneGcResult:
    gc = _gc_cfg(cfg)
    if not bool(gc.get("enabled", True)):
        return SceneGcResult(skipped_reason="disabled")
    l2_cfg = cfg.get("l2") or {}
    thresholds = l2_cfg.get("warn_thresholds") or {}
    orange = int(thresholds.get("orange", 180))
    _trigger_cfg = gc.get("trigger_at")
    trigger_at = int(_trigger_cfg) if _trigger_cfg is not None else orange
    max_merges = int(gc.get("max_merges", 20))

    active_before = scene_dao.count_scenes(mem_conn, "active")
    result = SceneGcResult(trigger_at=trigger_at, active_before=active_before)
    if active_before < trigger_at:
        result.skipped_reason = f"active({active_before}) < trigger_at({trigger_at})"
        logger.info("场景治理跳过：%s", result.skipped_reason)
        return result

    candidates = list_merge_candidates(mem_conn, cfg)
    result.candidates = len(candidates)
    if not candidates:
        result.skipped_reason = "no_candidates"
        logger.info("场景治理：无超阈值相似对，无需合并")
        return result

    consec_fails = 0
    for cand in candidates[:max_merges]:
        a, b, sim = cand["scene_a"], cand["scene_b"], cand["sim"]
        # 重新取最新状态（前面合并可能已改状态/场景）
        sa = scene_dao.get_scene(mem_conn, a)
        sb = scene_dao.get_scene(mem_conn, b)
        if not sa or not sb or sa.get("status") != "active" or sb.get("status") != "active":
            result.skipped += 1
            continue
        prompt = MERGE_PROMPT_TMPL.format(
            sim=f"{sim:.3f}", aid=a[:8], ac=sa["content"], bid=b[:8], bc=sb["content"],
        )
        try:
            text, _provider, _usage = llm_chain.call_with_fallback(
                cfg["llm"], prompt, chain_name="refinement", client=client,
            )
        except Exception as e:
            logger.warning("场景治理 LLM 调用失败（%s~%s）: %s", a[:8], b[:8], e)
            result.failed += 1
            consec_fails += 1
            if consec_fails >= _MAX_CONSECUTIVE_FAILS:
                logger.warning("场景治理连续失败 %d 次，中止本轮", consec_fails)
                break
            continue
        consec_fails = 0
        merged_content = text.strip().strip("`").strip()
        action = {
            "action": "merge",
            "target_scene_id": "placeholder",
            "merged_content": merged_content,
            "merged_from": [a, b],
            "reason": f"相似度 {sim:.3f} 自动合并（scene_gc）",
        }
        l2res = l2.L2Result()
        try:
            l2._apply_merge(mem_conn, action, [], l2res, cfg)
            result.merged += len(l2res.merged)
            result.archived += len(l2res.archived)
        except Exception as e:
            logger.warning("场景治理合并落库失败（%s~%s）: %s", a[:8], b[:8], e)
            result.failed += 1
        result.considered += 1

    result.active_after = scene_dao.count_scenes(mem_conn, "active")
    # 信号发布：治理结果（供 WebUI/SCSM 感知场景数变化）
    try:
        from sgme.signal import engine as signal_engine
        signal_engine.publish(
            event_type="memory_updated",
            source="scene_gc",
            payload={
                "active_before": result.active_before,
                "active_after": result.active_after,
                "merged": result.merged,
                "archived": result.archived,
                "failed": result.failed,
            },
            mem_conn=mem_conn,
        )
    except Exception as e:
        logger.warning("场景治理信号发布失败（不阻塞）: %s", e)
    logger.info(
        "场景治理完成：before=%d after=%d 候选=%d 合并=%d 归档=%d 失败=%d",
        result.active_before, result.active_after, result.candidates,
        result.merged, result.archived, result.failed,
    )
    return result
