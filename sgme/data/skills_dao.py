"""sgme/data/skills_dao.py：skills.db 数据访问层（T-112 技能索引持久化）。

架构约束：data 层是**唯一**数据库操作出口（AGENTS.md 模块边界），
operations 层不得直接写 SQL；本模块只管库，不管业务语义与降级策略。

设计要点（2026-08-29）：
- 全文检索走 FTS5 外部内容表 `skills_fts`，索引列为 jieba 分词后的 `*_seg`
  （分词由 `sgme.segment` 统一提供，写入时计算，触发器同步虚表）。
- **列加权**：技能名是最强信号，BM25 按 `name_seg:description_seg:content_seg = 10:5:1`
  加权——修「库里有名为 pdf 的技能却搜不到」这类长句稀释问题。
- **停用词过滤**：中文虚词/口语填料（帮我、一下、里的…）不进查询串，
  否则长句里噪声词累加会盖过高 IDF 的实词（实测长句 vs 关键词 top-5 交集仅 33%）。
- 向量读写**不在本模块**（走 `data/search/vector.py`，sqlite-vec + numpy 双路）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sgme.segment import segment, segment_terms

# ---------- 中文停用词（保守：只滤虚词与口语填料，保留动词/名词实词） ----------
# 收录原则：①语法性虚词（助词/介词/代词/量词）②高频口语填料（帮我、一下）
# 不收录：可能承载检索意图的实词（如「读取」「生成」「分析」）
_STOPWORDS: frozenset[str] = frozenset(
    {
        # 助词 / 语气词
        "的", "了", "着", "过", "得", "地", "吧", "呢", "啊", "哦", "嗯", "嘛", "呀",
        # 代词
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "咱", "人家",
        "这", "那", "哪", "这个", "那个", "哪个", "这些", "那些", "此", "该", "其",
        # 介词 / 连词
        "在", "从", "到", "向", "往", "对", "对于", "关于", "给", "让", "被", "把",
        "和", "与", "或", "及", "而", "且", "则", "若", "如", "为", "为了", "以",
        "之", "所", "由", "于",
        # 副词 / 程度
        "很", "太", "非", "不", "没", "无", "也", "都", "就", "还", "又", "再",
        "更", "最", "只", "才", "已", "正", "正在",
        # 疑问 / 泛指
        "什么", "怎么", "怎样", "如何", "为什么", "哪", "谁",
        # 量词 / 泛指数量
        "一", "一个", "一下", "一些", "一点", "个", "们", "些", "种",
        # 口语填料（长句查询的主要噪声源）
        "帮", "帮我", "帮我看", "我想", "我要", "可以", "能否", "麻烦", "请",
        "里", "里面", "中的", "上面", "下面", "这里", "那里",
    }
)

# BM25 列权重（顺序对应 skills_fts 的列：name_seg, description_seg, content_seg, name）
# 技能名命中权重最高——name 是唯一性最强、噪声最小的信号。
_BM25_WEIGHTS = (10.0, 5.0, 1.0)

# T-401：依赖强度白名单——strong=缺了就跑不通（写侧删除应拦），weak=顺带一提（只提示）
VALID_STRENGTHS: frozenset[str] = frozenset({"strong", "weak"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_skill(r: sqlite3.Row) -> dict[str, Any]:
    """库行 → 技能字典（tags 由 JSON 字符串还原为列表）。"""
    d = dict(r)
    tags = d.get("tags")
    if isinstance(tags, str):
        try:
            d["tags"] = json.loads(tags)
        except Exception:
            d["tags"] = [t for t in tags.split(",") if t]
    elif tags is None:
        d["tags"] = []
    # 分词列不对外（内部索引列）
    d.pop("content_seg", None)
    d.pop("description_seg", None)
    return d


# ---------- 写入 ----------


def upsert_skill(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    """写入/更新单条技能（幂等，按 name 主键冲突即更新）。

    Args:
        rec: 至少含 name / sha256 / content；description/category/tags/version/
             pattern/source/origin_path 可选。tags 接受列表或逗号串。
    """
    tags = rec.get("tags") or []
    tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else str(tags)
    content = rec.get("content") or ""
    now = _now()
    conn.execute(
        """
        INSERT INTO skills (name, sha256, name_seg, description, description_seg, category, tags,
                            version, pattern, source, origin_path, content, content_seg,
                            content_len, updated_at, synced_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET
          sha256=excluded.sha256, name_seg=excluded.name_seg,
          description=excluded.description,
          description_seg=excluded.description_seg, category=excluded.category,
          tags=excluded.tags, version=excluded.version, pattern=excluded.pattern,
          source=excluded.source, origin_path=excluded.origin_path,
          content=excluded.content, content_seg=excluded.content_seg,
          content_len=excluded.content_len, updated_at=excluded.updated_at,
          synced_at=excluded.synced_at
        """,
        (
            rec["name"],
            rec.get("sha256") or "",
            segment(rec["name"]),
            rec.get("description") or "",
            segment(rec.get("description") or ""),
            rec.get("category") or None,
            tags_json,
            rec.get("version") or None,
            rec.get("pattern") or None,
            rec.get("source") or None,
            rec.get("origin_path") or None,
            content,
            segment(content),
            len(content),
            now,
            now,
            rec.get("status") or "active",
        ),
    )


def delete_skill(conn: sqlite3.Connection, name: str) -> None:
    """删除技能（连带清向量与 uses 边；FTS 由触发器同步）。"""
    conn.execute("DELETE FROM skills WHERE name=?", (name,))
    conn.execute("DELETE FROM skill_vectors WHERE name=?", (name,))
    conn.execute("DELETE FROM skill_uses WHERE src=? OR dst=?", (name, name))


def set_status(conn: sqlite3.Connection, name: str, status: str) -> bool:
    """改技能生命周期状态（active / deprecated / tombstone）。

    软删/恢复的入口用它，让删除动作**立即**在检索层生效，而不必等下一次
    sync_index 才把 frontmatter 的 deprecated 标记带进库。
    返回是否命中一行；老库没有 status 列时直接返回 False（不抛错）。
    """
    if status not in ("active", "deprecated", "tombstone"):
        raise ValueError(f"非法状态: {status}")
    if not _has_status_column(conn):
        return False
    cur = conn.execute("UPDATE skills SET status=? WHERE name=?", (status, name))
    return cur.rowcount > 0


def replace_uses(
    conn: sqlite3.Connection,
    name: str,
    uses: list[str],
    strengths: dict[str, str] | None = None,
    strength: str = "weak",
) -> None:
    """全量替换某技能的 uses 出向依赖（幂等）。

    Args:
        name: 源技能名。
        uses: 依赖目标名列表（空/None = 清空出边）。
        strengths: {dst: 'strong'|'weak'} 按边指定强度（T-402），优先于 strength。
        strength: 统一强度兜底（T-401），默认 weak——存量 frontmatter 无强度信息时
            按最宽松语义落库，不阻塞既有删除/改名。
        strengths/strength 均先校验后删边，避免「删了旧边却写不进新边」的半截状态。
    """
    from sgme.data.skill_gate import normalize_strength

    strengths = strengths or {}
    if strength not in VALID_STRENGTHS:
        raise ValueError(
            f"非法依赖强度: {strength!r}（合法值: {sorted(VALID_STRENGTHS)}）"
        )
    conn.execute("DELETE FROM skill_uses WHERE src=?", (name,))
    for dst in uses or []:
        if dst:
            conn.execute(
                "INSERT OR IGNORE INTO skill_uses (src, dst, strength) VALUES (?,?,?)",
                (name, dst, normalize_strength(strengths.get(dst, strength))),
            )


def set_skill_status(conn: sqlite3.Connection, name: str, status: str) -> None:
    """标记技能状态（active/tombstone，T-402 门禁用）。"""
    conn.execute("UPDATE skills SET status=? WHERE name=?", (status, name))


# ---------- 读取 ----------


def get_skill(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """取单条技能（含 content 全文）。不存在返回 None。"""
    cur = conn.execute("SELECT * FROM skills WHERE name=?", (name,))
    r = cur.fetchone()
    return _row_to_skill(r) if r else None


def _has_status_column(conn: sqlite3.Connection) -> bool:
    """skills 表是否有 status 列（T-402 迁移后才会有；老库/只读测试库可能没有）。

    不假设列一定存在，避免在未迁移的库上直接抛 "no such column: status"。
    """
    try:
        return any(r[1] == "status" for r in conn.execute("PRAGMA table_info(skills)"))
    except sqlite3.Error:
        return False


def list_skills(
    conn: sqlite3.Connection,
    offset: int = 0,
    limit: int | None = None,
    category: str | None = None,
    include_tombstone: bool = False,
    include_deprecated: bool = True,
) -> list[dict[str, Any]]:
    """L0 索引列表（轻量投影，不含 content；按名排序，支持 category 过滤）。

    T-401/T-402 生命周期：默认**排除 tombstone**（已改名/已废弃的墓碑技能不该
    出现在列表里让人误以为可用）；``include_tombstone=True`` 供管理面/排查用。
    ``include_deprecated=False`` 时连 deprecated 一起排除——面向 Agent 的列表
    走这个口径（停用件不该出现在可选项里）；管理面/追溯保持默认 True。
    老库没有 status 列时按 active 处理（COALESCE 兜底）。
    """
    sql = "SELECT name, description, category, tags, version, pattern, source, updated_at FROM skills"
    conds: list[str] = []
    params: list[Any] = []
    if category:
        conds.append("category=?")
        params.append(category)
    if not include_tombstone and _has_status_column(conn):
        conds.append("COALESCE(status,'active') != 'tombstone'")
    if not include_deprecated and _has_status_column(conn):
        conds.append("COALESCE(status,'active') != 'deprecated'")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY name ASC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return [_row_to_skill(r) for r in conn.execute(sql, params).fetchall()]


def count_skills(
    conn: sqlite3.Connection, category: str | None = None, include_tombstone: bool = False,
    include_deprecated: bool = True,
) -> int:
    """技能总数（可选按 category 过滤；与 list_skills 口径一致，默认排除墓碑）。"""
    sql = "SELECT COUNT(*) AS n FROM skills"
    conds: list[str] = []
    params: list[Any] = []
    if category:
        conds.append("category=?")
        params.append(category)
    if not include_tombstone and _has_status_column(conn):
        conds.append("COALESCE(status,'active') != 'tombstone'")
    if not include_deprecated and _has_status_column(conn):
        conds.append("COALESCE(status,'active') != 'deprecated'")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return int(conn.execute(sql, params).fetchone()["n"])


def list_categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """分类目录（含每类计数）——冷启动「地图」的结构化数据源。"""
    cur = conn.execute(
        """
        SELECT COALESCE(category,'(uncategorized)') AS category, COUNT(*) AS n
        FROM skills GROUP BY category ORDER BY n DESC, category ASC
        """
    )
    return [{"category": r["category"], "count": int(r["n"])} for r in cur.fetchall()]


# ---------- 全文检索（FTS5 + BM25 列加权 + 停用词过滤） ----------


def fts_query_terms(query: str) -> list[str]:
    """查询拆词：jieba 分词 → 去停用词 → 去重保序。

    停用词过滤是长句召回的关键（噪声词累加会盖过实词）。
    """
    terms: list[str] = []
    for t in segment_terms(query):
        t = t.strip()
        if not t or t in _STOPWORDS:
            continue
        if t not in terms:
            terms.append(t)
    return terms


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    category: str | None = None,
    include_deprecated: bool = False,
) -> list[dict[str, Any]]:
    """FTS5 BM25 检索（列加权 10:5:1，支持 category 结构化过滤）。

    返回 [{name, score, description, category, ...}]。⚠️ score 为 bm25() 原始
    **负分**，越小越相关（调用方若做 min-max 归一必须先取反，见 T-217 教训）。
    停用词过滤后无有效词 → 返回空列表（不报错）。

    ``include_deprecated=False``（默认）时排除 ``status='deprecated'`` 的技能：
    软删只是「停用」，停用件不该继续抢占召回位；``skill_get`` 仍可按名取全文。
    """
    terms = fts_query_terms(query)
    if not terms:
        return []
    # OR 语义（对称 wiki_fts）；每个词加引号防 FTS5 语法注入
    match_expr = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    sql = f"""
        SELECT s.*, bm25(skills_fts, {weights}) AS score
        FROM skills_fts f
        JOIN skills s ON s.rowid = f.rowid
        WHERE skills_fts MATCH ?
    """
    params: list[Any] = [match_expr]
    if category:
        sql += " AND s.category=?"
        params.append(category)
    if not include_deprecated and _has_status_column(conn):
        sql += " AND COALESCE(s.status,'active')='active'"
    sql += " ORDER BY score ASC LIMIT ?"  # bm25() 返回负值，越小越相关
    params.append(limit)
    return [_row_to_skill(r) for r in conn.execute(sql, params).fetchall()]


# ---------- 增量同步支撑 ----------


def list_all_sha(conn: sqlite3.Connection) -> dict[str, str]:
    """返回 {name: sha256} 全表指纹（增量比对用）。"""
    return {r["name"]: r["sha256"] for r in conn.execute("SELECT name, sha256 FROM skills")}


def diff_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """比对源记录与库内指纹，返回需要变更的名字集合。

    Returns:
        {"insert": [...], "update": [...], "delete": [...], "unchanged": [...]}
        —— insert/update 需要（重）计算向量；delete 需要清理向量与 uses 边。

    T-124b：sha 口径为 body（不含 frontmatter），category/version/pattern/description/
    tags 等元数据变更不改变 sha——sha 相同者追加元数据指纹比对，防 sync_index 对
    元数据变更失明（T-123 分类治理实测撞出）。
    T-402：uses 边与 strength 同样纳入指纹——只改 frontmatter.uses（body 不变）
    时依赖图也会刷新。
    """
    db_sha = list_all_sha(conn)
    src = {r["name"]: (r.get("sha256") or "") for r in records}

    def _meta_fp(r: dict[str, Any]) -> tuple:
        return (
            r.get("category") or "",
            r.get("version") or "",
            r.get("pattern") or "",
            r.get("description") or "",
            ",".join(sorted(r.get("tags") or [])),
            # T-402：uses 边与强弱标记也是同步判据——否则只改 frontmatter.uses
            # （body sha 不变）时依赖图永不更新
            ",".join(sorted(r.get("uses") or [])),
            ",".join(
                f"{k}:{v}" for k, v in sorted((r.get("uses_strength") or {}).items())
            ),
        )

    update: list[str] = []
    unchanged: list[str] = []
    for rec in records:
        n = rec["name"]
        s = rec.get("sha256") or ""
        if n not in db_sha:
            continue
        if db_sha[n] != s:
            update.append(n)
            continue
        # sha 相同：元数据指纹比对（frontmatter 五字段 + T-402 uses/strength）
        row = conn.execute(
            "SELECT category, version, pattern, description, tags FROM skills WHERE name=?",
            (n,),
        ).fetchone()
        if row is None:
            unchanged.append(n)
            continue
        try:
            db_tags = json.loads(row[4]) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            db_tags = []
        db_edges = conn.execute(
            "SELECT dst, strength FROM skill_uses WHERE src=? ORDER BY dst", (n,)
        ).fetchall()
        db_fp = (
            row[0] or "",
            row[1] or "",
            row[2] or "",
            row[3] or "",
            ",".join(sorted(db_tags)),
            ",".join(r["dst"] for r in db_edges),
            ",".join(f"{r['dst']}:{r['strength'] or 'weak'}" for r in db_edges),
        )
        (update if db_fp != _meta_fp(rec) else unchanged).append(n)

    return {
        "insert": [n for n in src if n not in db_sha],
        "update": update,
        "delete": [n for n in db_sha if n not in src],
        "unchanged": unchanged,
    }


def vector_covered(conn: sqlite3.Connection) -> set[str]:
    """已有向量的技能名集合（避免重复 embed）。"""
    return {r["name"] for r in conn.execute("SELECT name FROM skill_vectors")}


def vector_state(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str]]:
    """已有向量的 {name: (content_sha256, model)} 状态（T-287 版本化判据用）。

    与 vector_covered 的区别：带出向量对应的**内容指纹与模型**——
    判「是否需重嵌」= 无行 ‖ 指纹 ≠ 当前内容指纹 ‖ 模型 ≠ 当前配置模型。
    存量行 content_sha256 为 NULL（老库迁移后）→ 自然判待重嵌（清欠账）。
    """
    return {
        r["name"]: (r["content_sha256"], r["model"])
        for r in conn.execute(
            "SELECT name, content_sha256, model FROM skill_vectors"
        )
    }


# ---------- 依赖图 ----------


def find_incoming(conn: sqlite3.Connection, name: str) -> list[str]:
    """入向引用：哪些技能 uses 了 name（写侧删除/改名的阻塞信号源）。"""
    cur = conn.execute("SELECT src FROM skill_uses WHERE dst=? ORDER BY src", (name,))
    return [r["src"] for r in cur.fetchall()]


def find_incoming_edges(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """入向引用边（含 strength，T-401）：strong=硬拦，weak=只提示。

    与 find_incoming 同源同序；需要分级门禁的调用方用本函数。
    """
    cur = conn.execute(
        "SELECT src, strength FROM skill_uses WHERE dst=? ORDER BY src", (name,)
    )
    return [dict(r) for r in cur.fetchall()]


def find_outgoing(conn: sqlite3.Connection, name: str) -> list[str]:
    """出向依赖：name uses 了哪些技能。"""
    cur = conn.execute("SELECT dst FROM skill_uses WHERE src=? ORDER BY dst", (name,))
    return [r["dst"] for r in cur.fetchall()]


def find_outgoing_edges(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """出向依赖边（含 strength，T-402 读侧）：name uses 了哪些技能、各是什么强度。

    与 ``find_incoming_edges`` 对称；库优先读路径用它把 ``SkillRecord.uses`` /
    ``use_strengths`` 从 ``skill_uses`` 还原（扫盘路径是解析 frontmatter 得到）。
    """
    cur = conn.execute(
        "SELECT dst, strength FROM skill_uses WHERE src=? ORDER BY dst", (name,)
    )
    return [dict(r) for r in cur.fetchall()]


# ---------- 同步水位（对账 cron 用） ----------


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    cur = conn.execute("SELECT value FROM skill_sync_meta WHERE key=?", (key,))
    r = cur.fetchone()
    return r["value"] if r else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO skill_sync_meta (key, value, updated_at) VALUES (?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, _now()),
    )
