# -*- coding: utf-8 -*-
"""sgme/regression_inject.py：9.4 #9 注入回归对照（记忆路零漂移，2026-11-04）。

设计 §9.4 第 9 项口径：
- 10 条固定查询（身份 / 家庭 / 健康 / 技术栈 / 状态 / 目标 / 角色 / 技能 / 知识 / 空分类）；
- 两侧都关场景路（检索统一 scopes=["memory"]；/v1/inject 本身不含场景路径）；
- 结果哈希差异率 ≤1% 才允许切读。

本模块只做**记忆路**的确定性对照：用与注入相同的读取口径
（needs_review/routes/forget/subject/lifecycle/锚点/失效断言）取每个查询的
(memory_id, content 哈希) 序列，算 sha256。基线文件由调用方保存/比对——
阶段 0 的 1.x 镜像基线属人工步骤（设计明写），这里提供可重复的对照器。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

# 10 条固定查询（标签对应设计清单；维度映射按 13 维注册表，健康并入 status，
# 「角色」用社交+价值观两条身份近邻维度、「知识」用环境维度、空分类用创意维度）。
QUERIES: list[dict] = [
    {"name": "身份", "dims": ["identity"], "match": "any", "limit": 10},
    {"name": "家庭", "dims": ["family"], "match": "any", "limit": 10},
    {"name": "健康", "dims": ["status"], "match": "any", "limit": 10},
    {"name": "技术栈", "dims": ["tech_stack"], "match": "any", "limit": 10},
    {"name": "状态", "dims": ["focus", "habits"], "match": "any", "limit": 10},
    {"name": "目标", "dims": ["goals"], "match": "any", "limit": 10},
    {"name": "角色", "dims": ["social", "values"], "match": "any", "limit": 10},
    {"name": "技能", "dims": ["skills"], "match": "any", "limit": 10},
    {"name": "知识", "dims": ["environment"], "match": "any", "limit": 10},
    {"name": "空分类", "dims": ["ideas"], "match": "any", "limit": 5},
]

# 与注入主路径一致的可见性口径（D-23/D-67/D-68/D-66/D-52）
INJECT_FILTERS = {
    "include_expired": False,
    "exclude_review": True,
    "routes": ("memory",),
    "exclude_memory_types": ("forget_request",),
    "subject_filter": ("self",),
    "lifecycle_active": True,
    "exclude_anchor": True,
    "exclude_valid_expired": True,
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_queries(conn: sqlite3.Connection) -> list[dict]:
    """执行 10 条固定查询，返回每条的确定性指纹（顺序即哈希输入）。"""
    from sgme.data import memory_dao

    out: list[dict] = []
    for q in QUERIES:
        rows = memory_dao.list_memories_by_dimension(
            conn, q["dims"], match=q["match"], limit=q["limit"], **INJECT_FILTERS
        )
        signature = [[r["memory_id"], _sha(r.get("content") or ""), r.get("status")]
                     for r in rows]
        payload = json.dumps(signature, ensure_ascii=False, separators=(",", ":"))
        out.append({
            "name": q["name"],
            "dims": q["dims"],
            "count": len(rows),
            "hash": _sha(payload),
            "memory_ids": [r["memory_id"] for r in rows],
        })
    return out


def compare_results(current: list[dict], baseline: list[dict],
                    threshold: float = 0.01) -> dict:
    """逐查询比对哈希；差异率 = 变化查询数 / 基线查询数。"""
    base_by = {b["name"]: b for b in baseline}
    diffs: list[dict] = []
    for cur in current:
        base = base_by.get(cur["name"])
        if base is None:
            diffs.append({"name": cur["name"], "reason": "基线缺少该查询",
                          "current_count": cur["count"], "baseline_count": None})
            continue
        if cur["hash"] != base["hash"]:
            diffs.append({
                "name": cur["name"],
                "reason": "哈希不一致",
                "current_count": cur["count"],
                "baseline_count": base.get("count"),
            })
    total = len(baseline) or 1
    rate = len(diffs) / total
    return {
        "total_queries": total,
        "changed": len(diffs),
        "diff_rate": round(rate, 4),
        "threshold": threshold,
        "passed": rate <= threshold,
        "diffs": diffs,
    }


def save_baseline(results: list[dict], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_baseline(path: Path | str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scenes_facts(conn: sqlite3.Connection, inject_src: Path | str | None = None) -> dict:
    """9.4 #10 的**事实核验**：注入侧确无场景路径 + 场景表冻结现状。

    带 scenes scope 的检索结果变化清单需在线向量，属人工执行项（设计原文），
    这里只给「注入不含场景」的代码事实与场景数据量，供人工清单对照。
    """
    src = Path(inject_src) if inject_src else (
        Path(__file__).resolve().parent / "operations" / "inject.py"
    )
    text = src.read_text(encoding="utf-8", errors="replace")
    # 只数 ASCII 标识（"scene"/scenes/scene_vectors）；中文「场景」会误命中
    # 「多场景」（人格四条件）这类无关文案。
    scene_refs = text.count("scene")
    tables: dict[str, int | None] = {}
    for table in ("scenes", "scene_vectors"):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            tables[table] = None
        else:
            tables[table] = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    return {
        "inject_scene_refs": scene_refs,
        "inject_has_scene_path": scene_refs > 0,
        "scenes_rows": tables,
        "manual_step": "带 scenes scope 的检索 top50 变化清单需在线向量与人工抽查 20 条（9.4 #10）",
    }
