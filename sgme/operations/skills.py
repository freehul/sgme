"""operations/skills.py：技能四级披露读侧操作（ST-36 M2，HTTP 与 MCP 共用唯一实现）。

承接入口
--------
- HTTP  ``GET  /v1/skills``                        → ``list_skills``（L0）
- HTTP  ``GET  /v1/skills/{name}/digest``          → ``skill_digest``（L1）
- HTTP  ``GET  /v1/skills/{name}?section=``        → ``skill_get``（L2）
- HTTP  ``POST /v1/skills/{name}/materialize``     → ``materialize``（L3）
- MCP   ``skill_search / skill_digest / skill_get / skill_materialize``
- 统一搜索 ``scopes=["skills"]``                    → ``search_skills``

三级结构（照抄 health.py 样板）：常量/私有工具 → 操作函数（OperationResult
信息超集）→ 无投影函数（新端点无历史契约，data 即响应）。

四级披露（设计 §三，token 纪律落地）
----------------------------------
- L0 索引：名称+简介+分类+标签，受 budget 截断（常驻上下文的最小集合）
- L1 摘要：frontmatter 字段 + 正文骨架（各标题行）+ uses 清单——审核媒介，
  agent 先看 L1 决定是否值得拉 L2
- L2 全文：正文全文注入上下文（显式调用）；section 为标题名时截取该节省 token
- L3 物化：字节保真写盘 dest_dir/<name>/SKILL.md（不走 LLM 转写），遥测一条

数据源：``sgme.skills.index_all(source_dirs, wiki_conn)``（git 工作区 ∪ wiki
skill 标记页，按名去重 git 优先）。本模块**不做索引缓存**（百条规模毫秒级重建，
入口层可用 app.state 缓存 BM25 索引对象复用，见 routes_skills._get_records）。

search_skills 融合：BM25 分数 + 向量余弦相似度简单加权和（0.6/0.4）；
向量不可达（未配置/网络失败/维度不符）自动降级纯 BM25——镜像统一搜索
「向量不可达不拖累主路」的容错语义。

依赖方向：operations → skills 包（业务层），engine 是禁区；本模块不认识协议。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult
from sgme.skills.bm25 import SkillsBm25
from sgme.skills.indexer import SkillRecord, index_all, parse_skill_md, validate_name

logger = logging.getLogger("sgme.operations.skills")

# 搜索融合权重（任务定稿：BM25 主路 0.6 + 向量余弦 0.4）
W_BM25 = 0.6
W_VEC = 0.4

# 标题行（ATX 风格 # ~ ######）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


# ---------- 私有工具 ----------


def _load_records(cfg: dict, wiki_conn: sqlite3.Connection | None) -> list[SkillRecord]:
    """从 cfg['skills'] 解析并全量索引（每次现扫，纯函数无全局状态）。"""
    from sgme.skills.config import parse_skills_config

    sc = parse_skills_config(cfg)
    return index_all(sc.source_dirs, wiki_conn)


def _find_record(records: list[SkillRecord], name: str) -> SkillRecord | None:
    """按名取记录：非法名抛 InvalidArgs（400）；不存在返回 None（调用方转 NOT_FOUND）。"""
    try:
        name = validate_name(name)
    except ValueError as e:
        raise InvalidArgs(str(e)) from e
    return next((rec for rec in records if rec.name == name), None)


def _skeleton(content: str) -> list[str]:
    """正文骨架 = 各标题行原文（含 # 前缀，保层级可读）。"""
    return [m.group(0).strip() for m in _HEADING_RE.finditer(content or "")]


def _extract_section(body: str, section: str) -> str | None:
    """按标题名截取小节：从匹配标题行起至同级或更高级标题前。

    - 匹配规则：标题文本去空白后精确等于 section（忽略级别）；
    - 找不到返回 None（调用方转 NOT_FOUND）。
    """
    if not body or not section:
        return None
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        title_text = m.group(2).strip()
        if title_text != section.strip():
            continue
        level = len(m.group(1))
        end = len(body)
        for nxt in matches[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        return body[m.start():end].strip()
    return None


def _normalize_minmax(scores: dict[str, float]) -> dict[str, float]:
    """分数归一到 [0,1]（min-max；单元素或空档安全兜底）。"""
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    span = hi - lo
    if span <= 0:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / span for k, v in scores.items()}


def _fuse_scores(
    bm25: dict[str, float],
    vec: dict[str, float],
    w_bm25: float = W_BM25,
    w_vec: float = W_VEC,
) -> dict[str, float]:
    """两路归一化后加权和融合（各自 min-max 到 [0,1] 再加权）。"""
    nb, nv = _normalize_minmax(bm25), _normalize_minmax(vec)
    names = set(nb) | set(nv)
    out: dict[str, float] = {}
    for n in names:
        out[n] = w_bm25 * nb.get(n, 0.0) + w_vec * nv.get(n, 0.0)
    return out


def _query_embedding_safe(query: str, records: list[SkillRecord], cfg: dict) -> dict[str, float]:
    """查询向量 + 全库余弦 top（**永不抛异常**：向量路任何失败都降级空 dict）。"""
    try:
        from sgme.skills.vectors import build_vectors, cosine_topk, embed_texts

        qv_list = embed_texts([query[:2000]], cfg)
        qv = qv_list.get("0")
        if not qv:
            return {}
        cache_dir = Path((cfg.get("paths") or {}).get("data_dir") or ".") / "cache" / "skills"
        from sgme.skills.config import parse_skills_config

        policy = parse_skills_config(cfg).vector_cache_policy
        vectors = build_vectors(records, cfg, cache_dir, policy=policy)
        return cosine_topk(qv, vectors, top_k=max(len(records), 1))
    except Exception as e:  # 容错隔离：向量不可达 ≠ 技能搜索失败
        logger.info("skills 向量路降级（BM25 单路）: %s", e)
        return {}


# ---------- 操作函数（L0/L1/L2/L3 + 搜索） ----------


def list_skills(
    cfg: dict[str, Any],
    wiki_conn: sqlite3.Connection | None,
    offset: int = 0,
    limit: int | None = None,
) -> OperationResult:
    """L0 索引列表：name/description/category/tags（受 budget 截断，按名排序）。

    Raises:
        InvalidArgs: skills.enabled=false（模块禁用不该走到读侧端点）。
    """
    from sgme.skills.config import parse_skills_config

    sc = parse_skills_config(cfg)
    if not sc.enabled:
        raise InvalidArgs("skills 模块未启用（skills.enabled=false）")
    all_records = index_all(sc.source_dirs, wiki_conn)
    # budget=L0 常驻预算；支持分页（offset/limit）浏览全量，limit 缺省=budget（PR-7：385 件迁移后全量列表需求）
    effective_limit = limit if limit is not None else sc.budget
    records = all_records[offset : offset + effective_limit]
    items = [
        {
            "name": r.name,
            "description": r.description,
            "category": r.category,
            "tags": list(r.tags),
            "source": r.source,
            "version": r.version,
        }
        for r in records
    ]
    return OperationResult.succeed({"skills": items, "total": len(all_records), "returned": len(items), "offset": offset, "budget": sc.budget})


def skill_digest(
    cfg: dict[str, Any],
    wiki_conn: sqlite3.Connection | None,
    *,
    name: str,
) -> OperationResult:
    """L1 摘要：frontmatter 字段 + 正文骨架（各标题行）+ uses 清单。审核媒介层。"""
    records = _load_records(cfg, wiki_conn)
    rec = _find_record(records, name)
    if rec is None:
        return not_found(name)
    return OperationResult.succeed(
        {
            "name": rec.name,
            "description": rec.description,
            "version": rec.version,
            "pattern": rec.pattern,
            "category": rec.category,
            "tags": list(rec.tags),
            "uses": list(rec.uses),
            "sections": _skeleton(rec.content),
            "sha256": rec.sha256,
            "source": rec.source,
            "origin_path": rec.origin_path,
        }
    )


def skill_get(
    cfg: dict[str, Any],
    wiki_conn: sqlite3.Connection | None,
    *,
    name: str,
    section: str | None = None,
) -> OperationResult:
    """L2 全文：正文全文；section 给定时只回该节（找不到该节 → NOT_FOUND）。"""
    records = _load_records(cfg, wiki_conn)
    rec = _find_record(records, name)
    if rec is None:
        return not_found(name)
    content = rec.content
    if section is not None and section.strip():
        seg = _extract_section(rec.content, section)
        if seg is None:
            return not_found(rec.name, extra=f"无标题节: {section}（先 digest 看骨架确认节名）")
        content = seg
    return OperationResult.succeed(
        {
            "name": rec.name,
            "content": content,
            "sha256": rec.sha256,
            "section": section or None,
            "truncated_by_section": bool(section),
            "source": rec.source,
        }
    )


def not_found(name: str, extra: str = "") -> OperationResult:
    """技能不存在/小节不存在 → 可预期业务失败（入口层映射 HTTP 404 / MCP error）。"""
    msg = f"技能不存在: {name}" + (f"——{extra}" if extra else "")
    return OperationResult.fail(ERR_NOT_FOUND, msg)


def materialize(
    cfg: dict[str, Any],
    wiki_conn: sqlite3.Connection | None,
    *,
    name: str,
    dest_dir: str,
) -> OperationResult:
    """L3 物化：SKILL.md **原文件字节**写盘 dest_dir/<name>/SKILL.md（不走 LLM 转写）。

    - git 源技能直接读 origin_path 字节流（真源字节保真）；
      wiki 源技能用索引内容（wiki_pages 本身存全文，等价保真）；
    - 成功记遥测日志一条（name/sha/ts），供使用统计与排障；
    - dest_dir 必填且非空（InvalidArgs）。
    """
    import hashlib

    if not isinstance(dest_dir, str) or not dest_dir.strip():
        raise InvalidArgs("dest_dir 不能为空（物化目标目录必填）")
    records = _load_records(cfg, wiki_conn)
    rec = _find_record(records, name)
    if rec is None:
        return not_found(name)

    data_bytes: bytes | None = None
    if rec.source == "git" and rec.origin_path:
        p = Path(rec.origin_path)
        if p.is_file():
            data_bytes = p.read_bytes()
    if data_bytes is None:
        # wiki 源（或缺文件兜底）：索引内容即全文（wiki_pages 存的是原文）
        data_bytes = rec.content.encode("utf-8")

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    target_dir = Path(dest_dir.strip()) / rec.name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "SKILL.md"
    target.write_bytes(data_bytes)

    # 遥测：一条 info（使用统计的原始素材，M3 写侧消费）
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("skill materialize: name=%s sha=%s ts=%s dest=%s", rec.name, sha256[:12], ts, target)

    return OperationResult.succeed({"name": rec.name, "path": str(target), "sha256": sha256})


def search_skills(
    query: str,
    cfg: dict[str, Any],
    wiki_conn: sqlite3.Connection | None,
    limit: int = 10,
) -> list[dict]:
    """技能检索：BM25 打分 + 向量余弦融合（0.6/0.4 加权和）→ [{name, score, source}]。

    - 向量不可达（未配置/网络失败）自动降级纯 BM25（_query_embedding_safe 吞异常）；
    - 本函数为统一搜索 skills 层与 MCP skill_search 的共用实现，**返回裸 list**
      （非 OperationResult：调用方在层内做容错隔离）；
    - 空 query 返回空列表（对齐 memory 层空 query 不报错语义）。
    """
    if not isinstance(query, str) or not query.strip() or limit <= 0:
        return []
    records = _load_records(cfg, wiki_conn)
    if not records:
        return []
    bm25 = SkillsBm25(records).score(query)
    try:
        vec = _query_embedding_safe(query.strip(), records, cfg)
    except Exception as e:  # 容错隔离：向量路任何异常都不拖累 BM25 主路
        logger.info("skills 向量路异常降级（BM25 单路）: %s", e)
        vec = {}
    fused = _fuse_scores(bm25, vec)
    # 融合路径标记：向量路有贡献（任一命中与 BM25 命中交集非空）→ skills_rrf，
    # 否则视为纯 BM25 → skills_bm25
    routes = ["skills_rrf"] if (vec and set(fused) & set(vec)) else ["skills_bm25"]
    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    by_name = {r.name: r for r in records}
    return [
        {
            "name": n,
            "score": round(s, 6),
            "source": by_name[n].source,
            "description": by_name[n].description,
            "category": by_name[n].category,
            "_routes": routes,
        }
        for n, s in ranked
        if n in by_name
    ]
