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

数据源：``sgme.skills.index_all(source_dirs)``（git 工作区 SKILL.md，按名排序）。
2026-08-28 起 wiki 桥接已移除，技能唯一来源为 ``skills.source_dirs`` 的 SKILL.md；
``wiki_conn`` 形参在部分操作（cold_start 手册检索）中仍用于取 SGME 操作手册，
但不参与技能索引。本模块**不做索引缓存**（百条规模毫秒级重建，
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


def _load_builtin_protocol() -> SkillRecord | None:
    """加载内置「技能检索协议」skill（sgme/skills/protocol/SKILL.md）。

    该技能随 sgme 包进镜像、不被 hub 同步覆盖，是 cold_start 唯一注入项；
    此处让其 skill_get/skill_digest 也能解析，避免协议本身 404（agent 从
    coldstart 读到摘要后，可再 skill_get 拉全文复核）。
    """
    from sgme.skills.indexer import _record_from_meta, parse_skill_md, validate_name

    p = Path(__file__).resolve().parent.parent / "skills" / "protocol" / "SKILL.md"
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    parsed = parse_skill_md(text)
    name = validate_name(str(parsed["meta"].get("name") or p.parent.name))
    return _record_from_meta(name, parsed["meta"], parsed["body"], "builtin", str(p))


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
    skills_conn: sqlite3.Connection | None = None,
) -> OperationResult:
    """L0 索引列表：name/description/category/tags（受 budget 截断，按名排序）。

    T-112 双写过渡：``skills_conn`` 非 None 时走库（支持 category 过滤的
    结构化查询、无需每次全量扫描目录），异常时回退内存索引路径。

    Raises:
        InvalidArgs: skills.enabled=false（模块禁用不该走到读侧端点）。
    """
    from sgme.skills.config import parse_skills_config

    sc = parse_skills_config(cfg)
    if not sc.enabled:
        raise InvalidArgs("skills 模块未启用（skills.enabled=false）")
    effective_limit = limit if limit is not None else sc.budget
    # 库有数据才走库（空库=冷启动未同步，走内存索引避免返回空列表）
    if _db_ready(skills_conn):
        try:
            from sgme.data import skills_dao

            items = skills_dao.list_skills(
                skills_conn, offset=offset, limit=effective_limit
            )
            total = skills_dao.count_skills(skills_conn)
            return OperationResult.succeed(
                {
                    "skills": items,
                    "total": total,
                    "returned": len(items),
                    "offset": offset,
                    "budget": sc.budget,
                }
            )
        except Exception as e:  # 容错隔离：库路径异常回退内存索引
            logger.warning("技能库内列表失败，回退内存索引: %s", e)
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
        bp = _load_builtin_protocol()
        if bp is not None and bp.name == name:
            rec = bp
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
        bp = _load_builtin_protocol()
        if bp is not None and bp.name == name:
            rec = bp
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
    skills_conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """技能检索：BM25 打分 + 向量余弦融合（0.6/0.4 加权和）→ [{name, score, source}]。

    - 向量不可达（未配置/网络失败）自动降级纯 BM25（_query_embedding_safe 吞异常）；
    - 本函数为统一搜索 skills 层与 MCP skill_search 的共用实现，**返回裸 list**
      （非 OperationResult：调用方在层内做容错隔离）；
    - 空 query 返回空列表（对齐 memory 层空 query 不报错语义）。

    T-112 双写过渡：``skills_conn`` 非 None 时优先走**库内检索**
    （FTS5 持久化索引 + skill_vectors 持久化向量 + 停用词过滤 + name 加权），
    库不可用/异常时自动回退内存索引路径，行为与改造前一致。
    """
    if not isinstance(query, str) or not query.strip() or limit <= 0:
        return []
    # 库有数据才走库（空库=冷启动未同步，走内存索引避免返回空结果）
    if _db_ready(skills_conn):
        try:
            return search_skills_db(query, skills_conn, cfg, limit=limit)
        except Exception as e:  # 容错隔离：库路径异常不拖垮检索
            logger.warning("技能库内检索失败，回退内存索引: %s", e)
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

# ---------------------------------------------------------------------------
# 冷启动包（T-106 M5）：新 agent 一次拉取即刻可用——索引全量 + 热集全文 + 操作手册


def cold_start(
    cfg: dict[str, Any],
    wiki_conn: sqlite3.Connection | None,
) -> OperationResult:
    """冷启动包：GET /v1/skills/coldstart 的业务实现（设计 §三「冷启动包」）。

    渐进式披露范式（2026-08-28 定稿）：coldstart **只注入 1 个技能**——技能检索协议
    （``sgme/skills/protocol/SKILL.md``），教 agent 用 SGME 的 ``skill_search`` 按需检索、
    挑合适的技能再 ``skill_get`` 注入上下文。全量 403 技能**不预载**，由 agent 运行时检索，
    既不占上下文、也无需维护热层。

    返回三件套：
    - index：仅含协议 skill 的索引（name/description/tags/category/pattern）
    - hotset：恒定空（单协议范式下无热集；按需检索取代热常驻）
    - manual：SGME 操作手册页（wiki 检索标题含「SGME操作手册」或 tags 含 onboarding；
      找不到返回 None，不阻塞——agent 仍可按 MCP onboarding 工具指引使用）
    """
    from pathlib import Path as _Path

    from sgme.data import wiki_dao
    from sgme.skills.indexer import _record_from_meta, parse_skill_md, validate_name

    if not (cfg.get("skills") or {}).get("enabled", False):
        raise InvalidArgs("skills 模块未启用（skills.enabled=false）")

    # 单一注入：技能检索协议 skill（内置源，随 sgme 包进镜像，不被 hub 同步覆盖）
    proto_path = _Path(__file__).resolve().parent.parent / "skills" / "protocol" / "SKILL.md"
    items: list[dict] = []
    if proto_path.is_file():
        text = proto_path.read_text(encoding="utf-8")
        parsed = parse_skill_md(text)
        name = validate_name(str(parsed["meta"].get("name") or proto_path.parent.name))
        rec = _record_from_meta(name, parsed["meta"], parsed["body"], "builtin", str(proto_path))
        items = [{
            "name": rec.name,
            "description": rec.description,
            "category": rec.category,
            "tags": list(rec.tags),
            "pattern": rec.pattern,
            "content": rec.content,
        }]
    else:
        logger.warning("cold_start: 协议 skill 缺失（%s），coldstart 将为空", proto_path)

    hotset: list[dict] = []

    manual: dict | None = None
    if wiki_conn is not None:
        try:
            pages = wiki_dao.list_pages(wiki_conn, limit=500)
        except Exception:
            pages = []
        hit = next((
            p for p in pages
            if ("onboarding" in (p.get("tags") or [])
                or "sgme操作手册" in str(p.get("title", "")).replace(" ", "").lower())
        ), None)
        if hit is not None:
            full = wiki_dao.get_page(wiki_conn, hit["page_id"]) or {}
            manual = {
                "page_id": full.get("page_id"),
                "title": full.get("title"),
                "content": full.get("content"),
            }

    return OperationResult.succeed(
        {
            "index": {"items": items, "total": len(items)},
            "hotset": hotset,
            "manual": manual,
        }
    )


# ---------- T-112：skills.db 同步与库内检索 ----------


def _record_to_dict(rec: SkillRecord | dict) -> dict:
    """SkillRecord（或等价字典）→ 库写入字典（tags/uses 原样，由 dao 序列化）。

    同时兼容 dataclass 与 dict：同步源既可能来自 indexer 的 SkillRecord，
    也可能来自测试/离线脚本构造的普通字典。
    """
    if isinstance(rec, dict):
        get = lambda k, d="": rec.get(k, d)  # noqa: E731
    else:
        get = lambda k, d="": getattr(rec, k, d)  # noqa: E731
    return {
        "name": get("name"),
        "sha256": get("sha256") or "",
        "description": get("description") or "",
        "tags": list(get("tags", []) or []),
        "category": get("category") or None,
        "version": get("version") or None,
        "pattern": get("pattern") or None,
        "source": get("source") or None,
        "origin_path": get("origin_path") or None,
        "content": get("content") or "",
        "uses": list(get("uses", []) or []),
    }


def sync_index(
    cfg: dict,
    skills_conn: sqlite3.Connection,
    wiki_conn: sqlite3.Connection | None = None,
    max_embed: int | None = 20,
    embed: bool = True,
) -> OperationResult:
    """把 source_dirs 的技能增量同步进 skills.db（T-112）。

    增量判据为 **内容 sha256**（对称向量缓存的失效语义）：
    - insert/update 的技能重写主表（分词随写入计算，FTS 由触发器同步）
    - delete 的技能连向量与 uses 边一并清理
    - 向量只补缺失的，且受 ``max_embed`` 限批（首次 403 条约 13 分钟，
      不能放在请求线程里一次跑完；剩余交后台预热逐轮补齐）

    Args:
        max_embed: 本次最多新嵌入多少条；None=不限（离线/后台场景用）。
        embed: False 时只同步结构化数据（跳过向量，快速路径）。

    Returns:
        OperationResult.data = {inserted, updated, deleted, unchanged,
        embedded, pending_embed, total}
    """
    from sgme.data import skills_dao

    records = _load_records(cfg, wiki_conn)
    dicts = [_record_to_dict(r) for r in records]
    diff = skills_dao.diff_records(skills_conn, dicts)

    for name in diff["delete"]:
        skills_dao.delete_skill(skills_conn, name)

    touched = set(diff["insert"]) | set(diff["update"])
    # 兼容 SkillRecord / dict 两种源（_record_name 与 _record_to_dict 同款双形态处理）
    by_name = {_record_to_dict(r)["name"]: r for r in records}
    for name in touched:
        rec = by_name.get(name)
        if rec is None:
            continue
        rec_dict = _record_to_dict(rec)
        skills_dao.upsert_skill(skills_conn, rec_dict)
        skills_dao.replace_uses(skills_conn, name, rec_dict["uses"])
    skills_conn.commit()

    embedded = 0
    pending = 0
    if embed:
        # ⚠️ 待补向量必须按**全表**算，不能只算本次 touched——否则第二次调用时
        # 所有技能都是 unchanged，todo 恒为空 → 后台预热第一轮就退出 →
        # 向量永远补不上（2026-08-29 部署实测踩到）。
        covered = skills_dao.vector_covered(skills_conn)
        todo = sorted({r["name"] for r in dicts} - covered)
        pending = len(todo)
        if max_embed is not None and max_embed > 0:
            todo = todo[:max_embed]
        if todo:
            embedded = _embed_into_db(cfg, skills_conn, by_name, todo)

    return OperationResult.succeed(
        {
            "inserted": len(diff["insert"]),
            "updated": len(diff["update"]),
            "deleted": len(diff["delete"]),
            "unchanged": len(diff["unchanged"]),
            "embedded": embedded,
            "pending_embed": pending,
            "total": len(records),
        }
    )


def _embed_into_db(
    cfg: dict,
    skills_conn: sqlite3.Connection,
    by_name: dict[str, SkillRecord],
    names: list[str],
) -> int:
    """把指定技能嵌入并写入 skill_vectors（分批，失败批跳过）。

    Returns:
        成功写入的条数。
    """
    from sgme.data.search import vector as vector_mod
    from sgme.skills.vectors import embed_texts

    texts = [(_record_to_dict(by_name[n])["content"] or "")[:2000] for n in names]
    try:
        embs = embed_texts(texts, cfg)
    except Exception as e:  # 容错：向量路失败不影响结构化同步
        logger.warning("技能向量嵌入失败（本次同步跳过向量）: %s", e)
        return 0

    model = ((cfg.get("search") or {}).get("vector") or {}).get(
        "model", vector_mod._DEFAULT_EMBED_MODEL
    )
    items: list[tuple[str, list[float]]] = []
    for i, name in enumerate(names):
        vec = embs.get(str(i))
        if vec:
            items.append((name, vec))
    if not items:
        return 0
    return vector_mod.upsert_skill_vectors(skills_conn, items, model=model)


def _db_ready(skills_conn: sqlite3.Connection | None) -> bool:
    """skills.db 是否已有数据（冷启动空窗期判据）。

    ⚠️ 必要性：容器重启后库文件可能是新建的空库，而同步在**后台线程**跑
    （首次结构化同步 1-3 秒，向量预热更久）。若此时读侧直接走库，
    L0 列表与检索会**返回空**——用户看到「技能库空了」。故库内无数据时
    一律回退内存索引（内存索引每次现扫，立即可用）。
    """
    if skills_conn is None:
        return False
    try:
        from sgme.data import skills_dao

        return skills_dao.count_skills(skills_conn) > 0
    except Exception:
        return False


def search_skills_db(
    query: str,
    skills_conn: sqlite3.Connection,
    cfg: dict,
    limit: int = 10,
    category: str | None = None,
) -> list[dict]:
    """库内技能检索：FTS5 BM25（加权 + 停用词）∪ 向量余弦，0.6/0.4 融合。

    与内存版 ``search_skills`` 的差别（T-112 的收益所在）：
    - 全文索引**持久化**（重启不重建），且支持 ``category`` 结构化过滤；
    - 向量**持久化**在 skill_vectors（sqlite-vec 加速 + numpy 降级）；
    - BM25 对技能名列加权 10×，并对查询做停用词过滤（长句不再被虚词稀释）。

    Returns:
        [{name, score, source, description, category, _routes}]（按融合分降序）
    """
    from sgme.data import skills_dao
    from sgme.data.search import vector as vector_mod
    from sgme.skills.vectors import embed_texts

    if not isinstance(query, str) or not query.strip() or limit <= 0:
        return []

    # 路 1：FTS5 BM25（bm25() 返回负值，越小越相关 → 归一化为 0-1）
    fts_hits = skills_dao.fts_search(skills_conn, query, limit=limit * 3, category=category)
    bm25_raw = {h["name"]: h["score"] for h in fts_hits}
    bm25 = _normalize_minmax(bm25_raw)

    # 路 2：向量余弦（查询串单独 embed，1 条不触发分批）
    vec: dict[str, float] = {}
    try:
        qv_list = embed_texts([query[:2000]], cfg)
        qv = qv_list.get("0")
        if qv:
            rows = vector_mod.skill_vector_search(skills_conn, qv, limit=limit * 3)
            vec = {r["name"]: r["score"] for r in rows}
    except Exception as e:  # 容错隔离：向量路任何失败都不拖累 FTS 主路
        logger.info("技能向量路降级（FTS 单路）: %s", e)

    fused = _fuse_scores(bm25, vec)
    routes = ["skills_rrf"] if (vec and set(fused) & set(vec)) else ["skills_bm25"]
    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    meta = {h["name"]: h for h in fts_hits}
    out: list[dict] = []
    for name, score in ranked:
        row = meta.get(name)
        out.append(
            {
                "name": name,
                "score": round(score, 6),
                "source": (row or {}).get("source") or "git",
                "description": (row or {}).get("description") or "",
                "category": (row or {}).get("category"),
                "_routes": routes,
            }
        )
    return out
