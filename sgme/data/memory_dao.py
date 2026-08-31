"""storage/memory_dao.py：memory.db 的 DAO。

职责：
- dimension_registry / dimension_alias 启动时从 registry/*.yaml 幂等 upsert 导入
- memories CRUD（insert / update / archive / get）+ memory_tags + memory_sources 写入
- 模板查询的纯 SQL 执行（供 profile/inject.py 调用，本文件不依赖 LLM）

所有写入使用参数化查询，防 SQL 注入。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sgme.segment import segment
from sgme.data import db as db_mod


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid() -> str:
    return str(uuid.uuid4())


def _facts_to_json(facts: Iterable[dict] | None) -> str | None:
    """T-136：三元组列表 → facts_json 列值（规范化后 JSON 序列化，None/空 → None）。"""
    if not facts:
        return None
    cleaned: list[dict] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        s = str(f.get("subject") or "").strip()
        p = str(f.get("predicate") or "").strip()
        o = str(f.get("object") or "").strip()
        if s and p and o:
            cleaned.append({"subject": s, "predicate": p, "object": o})
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False)


# ---------- 维度注册表导入（幂等 upsert） ----------

def upsert_dimension(conn: sqlite3.Connection, dim: dict) -> None:
    """幂等 upsert 单条维度到 dimension_registry（含 boundaries，T-11）。"""
    conn.execute(
        """
        INSERT INTO dimension_registry
          (id, display_name, category, time_velocity, ttl_days, description, active, created_at, boundaries)
        VALUES (?,?,?,?,?,?,1,?,?)
        ON CONFLICT(id) DO UPDATE SET
          display_name=excluded.display_name,
          category=excluded.category,
          time_velocity=excluded.time_velocity,
          ttl_days=excluded.ttl_days,
          description=excluded.description,
          boundaries=excluded.boundaries
          -- active 不在此更新：保留 DB 现值（停用维度重启不复活）
        """,
        (
            dim["id"], dim["display_name"], dim["category"],
            dim["time_velocity"], dim["ttl_days"], dim["description"], _now_iso(),
            dim.get("boundaries"),
        ),
    )


def upsert_alias(conn: sqlite3.Connection, alias: str, dimension_id: str) -> None:
    """幂等 upsert 单条别名到 dimension_alias。"""
    conn.execute(
        """
        INSERT INTO dimension_alias (alias, dimension_id)
        VALUES (?,?)
        ON CONFLICT(alias) DO UPDATE SET dimension_id=excluded.dimension_id
        """,
        (alias, dimension_id),
    )


def import_registry(conn: sqlite3.Connection, dimensions: list[dict], aliases: dict[str, list[str]]) -> int:
    """从 registry/*.yaml 导入全部维度与别名（幂等）。

    返回导入的维度数量。在单事务内完成，失败回滚。
    """
    try:
        conn.execute("BEGIN")
        for d in dimensions:
            upsert_dimension(conn, d)
        # 先清空旧别名再重写（别名是 append-only 配置，重写最简单且幂等）
        # 注意：不 DELETE，避免事务内 FK 检查问题；用 INSERT OR IGNORE / ON CONFLICT 即可
        for dim_id, alias_list in aliases.items():
            for alias in alias_list:
                upsert_alias(conn, alias, dim_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(dimensions)


def count_dimensions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) AS c FROM dimension_registry WHERE active=1")
    return cur.fetchone()["c"]


def get_dimension(conn: sqlite3.Connection, dim_id: str) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM dimension_registry WHERE id=?", (dim_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_dimensions(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM dimension_registry"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def build_alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    """返回 {alias: dimension_id}（归一化用）。"""
    cur = conn.execute("SELECT alias, dimension_id FROM dimension_alias")
    return {r["alias"]: r["dimension_id"] for r in cur.fetchall()}


# ---------- 维度维护（2026-08-07 模块化重构：路由层 SQL 收编，B30） ----------

def list_aliases_by_dimension(conn: sqlite3.Connection, dim_id: str) -> list[dict]:
    """查询维度别名列表（返回 sqlite3.Row 列表，调用方按 r["alias"] 取值）。"""
    return conn.execute(
        "SELECT alias FROM dimension_alias WHERE dimension_id=?", (dim_id,)
    ).fetchall()


def update_dimension_fields(conn: sqlite3.Connection, dim_id: str, updates: dict) -> None:
    """更新维度字段（白名单：active/display_name/ttl_days/description）。"""
    if "active" in updates:
        conn.execute(
            "UPDATE dimension_registry SET active=? WHERE id=?",
            (1 if updates["active"] else 0, dim_id),
        )
    for field in ("display_name", "ttl_days", "description"):
        if field in updates:
            conn.execute(
                f"UPDATE dimension_registry SET {field}=? WHERE id=?",
                (updates[field], dim_id),
            )


def check_dimension_consistency(
    conn: sqlite3.Connection, yaml_dim_ids: set[str]
) -> dict:
    """T-128：校验 DB 维度注册表与源 YAML 维度集的一致性（防 B81 漏停用复发）。

    语义（YAML=种子，DB=运行时真相，T-2 v0.7 裁决）：
    - YAML 声明「应当 active 的维度集」；DB active=1 集合应 == YAML 集
    - 孤儿：DB active=1 但 YAML 未声明 → 仍在打标（脏数据来源），须告警 + 禁用
    - 缺失：YAML 声明但 DB 完全无此行 → 导入遗漏，须告警 + 导入
    - 未激活：YAML 声明但 DB active!=1 → 启用遗漏，须告警 + 启用
    - DB 中 YAML 未声明且已停用（active=0）属预期（溯源保留），不告警

    Args:
        conn: memory.db 连接。
        yaml_dim_ids: registry/dimensions.yaml 声明的维度 id 集合（YAML 为种子）。

    Returns:
        {
          consistent: bool,
          yaml_count, db_total_count, db_active_count: int,
          orphan_active_in_db: list[str],   # 应禁用
          missing_in_db: list[str],         # 应导入
          inactive_in_db: list[str],        # 应启用
        }
    """
    rows = list_dimensions(conn, active_only=False)
    db_all = {r["id"]: r for r in rows}
    db_active = {r["id"] for r in rows if r.get("active") == 1}
    yaml_set = set(yaml_dim_ids)

    orphan_active = sorted(db_active - yaml_set)
    missing_in_db = sorted(yaml_set - set(db_all.keys()))
    inactive_in_db = sorted(yaml_set & {r["id"] for r in rows if r.get("active") != 1})

    return {
        "consistent": not (orphan_active or missing_in_db or inactive_in_db),
        "yaml_count": len(yaml_set),
        "db_total_count": len(db_all),
        "db_active_count": len(db_active),
        "orphan_active_in_db": orphan_active,
        "missing_in_db": missing_in_db,
        "inactive_in_db": inactive_in_db,
    }


def delete_alias(conn: sqlite3.Connection, alias: str):
    """删除别名（返回 cursor 供调用方检查 rowcount）。"""
    return conn.execute("DELETE FROM dimension_alias WHERE alias=?", (alias,))


# ---------- memories CRUD ----------

def insert_memory(
    conn: sqlite3.Connection,
    content: str,
    memory_type: str,
    priority: int,
    time_velocity: str,
    ttl_days: int | None,
    dimension_ids: Iterable[str],
    sources: Iterable[tuple[str, str]] | None = None,
    agent_tag: str | None = None,
    memory_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    prompt_version: str | None = None,
    occurred_at: str | None = None,
    facts: Iterable[dict] | None = None,
) -> str:
    """插入一条记忆 + 标签 + 溯源。

    - dimension_ids：维度 id 列表（必须存在于注册表）
    - sources：[(source_ref, source_type), ...]，可选
    - prompt_version：产出该记忆的 L1 提示词版本（形如 `l1_extraction:v002`），
      可选参数缺省 None（旧调用签名不变，向后兼容）
    - content_seg：同一事务内写 `segment(content)`（中文检索分词 v0.3 闭环，
      触发器只同步、不分词——写路径由 data 层填 content_seg）
    - occurred_at（v0.5，2026-08-06）：会话事件的真实发生时刻
      （vs created_at=提炼落库时刻）；缺省 None 时回退为 created_at
    - facts（T-136，2026-08-31）：原子事实三元组列表
      [{"subject":..., "predicate":..., "object":...}, ...]，序列化为 JSON 写 facts_json 列
    - 返回 memory_id
    """
    mid = memory_id or _uuid()
    c_at = created_at or _now_iso()
    u_at = updated_at or c_at
    o_at = occurred_at or c_at
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO memories
              (memory_id, content, content_seg, memory_type, priority, time_velocity,
               ttl_days, created_at, updated_at, agent_tag, prompt_version, occurred_at,
               facts_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (mid, content, segment(content), memory_type, priority, time_velocity,
             ttl_days, c_at, u_at, agent_tag, prompt_version, o_at,
             _facts_to_json(facts)),
        )
        for dim_id in dimension_ids:
            conn.execute(
                "INSERT OR IGNORE INTO memory_tags (memory_id, dimension_id) VALUES (?,?)",
                (mid, dim_id),
            )
        if sources:
            for src_ref, src_type in sources:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_sources (memory_id, source_ref, source_type) VALUES (?,?,?)",
                    (mid, src_ref, src_type),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return mid


def get_memory(conn: sqlite3.Connection, memory_id: str) -> dict | None:
    """返回单条记忆（含 tags 与 sources）。"""
    cur = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
    row = cur.fetchone()
    if not row:
        return None
    mem = dict(row)
    mem["tags"] = [
        r["dimension_id"]
        for r in conn.execute(
            "SELECT dimension_id FROM memory_tags WHERE memory_id=? ORDER BY dimension_id",
            (memory_id,),
        ).fetchall()
    ]
    # dimensions 别名（2026-08-18 修复）：列表 API（list_memories_page）返回
    # dimensions、详情 API（get_memory）返回 tags——字段名不一致导致 WebUI
    # 详情页读 detail.memory.dimensions 拿不到维度。加别名统一契约（只增不改）。
    mem["dimensions"] = mem["tags"]
    mem["sources"] = [
        {"source_ref": r["source_ref"], "source_type": r["source_type"]}
        for r in conn.execute(
            "SELECT source_ref, source_type FROM memory_sources WHERE memory_id=?",
            (memory_id,),
        ).fetchall()
    ]
    return mem


def reject_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    reason: str,
) -> bool:
    """用户纠错「不采用」：标记记忆为 rejected（不删除、可恢复）。

    2026-08-06 新增（用户明确：删除会造成更多问题，打标记以后不加载显示）。
    - 只更新 status='rejected' + rejected_at + reject_reason，数据完整保留
    - 查询/搜索/候选池等读取路径过滤 status='rejected'
    - 幂等：重复 reject 更新 reject_reason，不报错
    """
    cur = conn.execute(
        """
        UPDATE memories SET status='rejected', rejected_at=?, reject_reason=?
        WHERE memory_id=?
        """,
        (_now_iso(), reason, memory_id),
    )
    conn.commit()
    return cur.rowcount > 0


def unreject_memory(
    conn: sqlite3.Connection,
    memory_id: str,
) -> bool:
    """撤销「不采用」：恢复为 active（rejected 误操作时用）。"""
    cur = conn.execute(
        """
        UPDATE memories SET status='active', rejected_at=NULL, reject_reason=NULL
        WHERE memory_id=?
        """,
        (memory_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def update_memory_content(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    priority: int | None = None,
    ttl_days: int | None = None,
    bump_updated_at: bool = True,
) -> bool:
    """更新记忆内容（TTL 续期：bump_updated_at=True 时 updated_at=now）。

    content 变更时同一事务刷新 `content_seg = segment(content)`（中文检索
    分词 v0.3 闭环：写路径由 data 层填，触发器只同步、不分词）。
    """
    sets = ["content=?", "content_seg=?"]
    params: list[Any] = [content, segment(content)]
    if priority is not None:
        sets.append("priority=?")
        params.append(priority)
    if ttl_days is not None:
        sets.append("ttl_days=?")
        params.append(ttl_days)
    if bump_updated_at:
        sets.append("updated_at=?")
        params.append(_now_iso())
    params.append(memory_id)
    cur = conn.execute(
        f"UPDATE memories SET {', '.join(sets)} WHERE memory_id=?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def archive_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    superseded_by: str,
) -> bool:
    """归档记忆：原行复制到 memory_archive + 从 memories 删除（事务）。

    supersession 锚点 = memory_id（归档前后同 id）。
    """
    try:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if not row:
            conn.rollback()
            return False
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_archive
              (memory_id, content, memory_type, priority, time_velocity, ttl_days,
               created_at, updated_at, agent_tag, prompt_version, archived_at, superseded_by,
               occurred_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (row["memory_id"], row["content"], row["memory_type"], row["priority"],
             row["time_velocity"], row["ttl_days"], row["created_at"], row["updated_at"],
             row["agent_tag"], row["prompt_version"], _now_iso(), superseded_by,
             row["occurred_at"] if "occurred_at" in row.keys() else None),
        )
        # 标签、溯源与向量一并清除（归档行保留可溯源；archive 表无 FK 到 memories）
        conn.execute("DELETE FROM memory_tags WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM memory_sources WHERE memory_id=?", (memory_id,))
        # memory_stats 是 v0.7 后加的表，有 FK 到 memories，归档时必须先清理
        conn.execute("DELETE FROM memory_stats WHERE memory_id=?", (memory_id,))
        # 向量是派生数据，随记忆归档删除（memory_vectors 有 FK 到 memories，必须先删）
        conn.execute("DELETE FROM memory_vectors WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def get_archive_chain(conn: sqlite3.Connection, memory_id: str) -> list[dict]:
    """返回该 memory_id 的归档链（按 archived_at ASC）。

    注：归档时原 memory_id 写入 archive.memory_id（同 id），所以同一逻辑事实的
    多次归档行在 archive 表中 memory_id 重复（INSERT OR REPLACE 会覆盖）。
    本实现采用 INSERT OR REPLACE 保证归档行最新，溯源链通过 superseded_by 跳转。
    """
    cur = conn.execute(
        "SELECT * FROM memory_archive WHERE memory_id=? ORDER BY archived_at ASC",
        (memory_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def find_by_superseded_by(conn: sqlite3.Connection, superseded_by: str) -> list[dict]:
    """查找被指定新 memory_id 取代的归档行。"""
    cur = conn.execute(
        "SELECT * FROM memory_archive WHERE superseded_by=? ORDER BY archived_at ASC",
        (superseded_by,),
    )
    return [dict(r) for r in cur.fetchall()]


def list_memories_by_dimension(
    conn: sqlite3.Connection,
    dimension_ids: Iterable[str],
    match: str = "any",
    limit: int = 50,
    include_expired: bool = False,
    time_window_start: str | None = None,
    order_by: str = "updated_at DESC",
) -> list[dict]:
    """按维度过滤记忆（模板查询用，纯 SQL 零 LLM）。

    - match='any'：维度 OR（IN）
    - match='all'：维度 AND（GROUP BY HAVING COUNT(DISTINCT)=N）
    - include_expired=False：TTL 过滤（ttl_days IS NULL OR updated_at > now-ttl）
    - time_window_start：updated_at > 阈值
    - order_by：动态维度 updated_at DESC / 静态 priority DESC（调用方决定）
    """
    dims = list(dimension_ids)
    if not dims:
        return []
    placeholders = ",".join("?" * len(dims))
    where = f"t.dimension_id IN ({placeholders})"
    sql = f"""
        SELECT m.* FROM memories m
        JOIN memory_tags t ON m.memory_id = t.memory_id
        WHERE {where}
    """
    params: list[Any] = list(dims)
    # 用户纠错「不采用」的记忆不参与候选池（2026-08-06）
    sql += " AND m.status != 'rejected'"
    if not include_expired:
        sql += " AND (m.ttl_days IS NULL OR julianday(m.updated_at) > julianday('now') - m.ttl_days)"
    if time_window_start:
        sql += " AND m.updated_at > ?"
        params.append(time_window_start)
    if match == "all":
        sql += " GROUP BY m.memory_id HAVING COUNT(DISTINCT t.dimension_id)=?"
        params.append(len(dims))
    else:
        # OR 时 JOIN 会产生重复行，去重
        sql += " GROUP BY m.memory_id"
    # ORDER BY 必须在 GROUP BY 之后；m.* 在 GROUP BY 下取聚合行的字段
    sql += f" ORDER BY m.{order_by} LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


# ---------- memory_vectors CRUD（v0.4 T9：向量检索索引） ----------

def upsert_vector(
    conn: sqlite3.Connection,
    memory_id: str,
    embedding_bytes: bytes,
    model: str,
    dims: int,
    embedded_at: str | None = None,
) -> None:
    """插入或替换记忆向量（INSERT OR REPLACE；模型切换后重嵌靠 dims 判断）。

    embedded_at 缺省取当前 UTC ISO 时间戳。
    """
    e_at = embedded_at or _now_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_vectors
          (memory_id, embedding, model, dims, embedded_at)
        VALUES (?,?,?,?,?)
        """,
        (memory_id, embedding_bytes, model, dims, e_at),
    )
    conn.commit()


def get_vector(conn: sqlite3.Connection, memory_id: str) -> dict | None:
    """返回单条向量记录（含 embedding BLOB）或 None。"""
    cur = conn.execute(
        "SELECT * FROM memory_vectors WHERE memory_id=?", (memory_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_memories_without_vector(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[dict]:
    """找出缺向量的记忆（LEFT JOIN memory_vectors WHERE embedding IS NULL）。

    用于增量补嵌：新记忆入库后未生成 embedding 的待补列表。
    """
    sql = """
        SELECT m.* FROM memories m
        LEFT JOIN memory_vectors v ON m.memory_id = v.memory_id
        WHERE v.embedding IS NULL
        ORDER BY m.memory_id
    """
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]

# ---------- 浏览分页（0.8 T-15 / 契约 §5.3） ----------

#: 允许的排序字段 → 实际列名。**白名单是此处 SQL 注入的唯一防线**：
#: 排序字段无法参数化（占位符只能替值、不能替标识符），故调用方传入的字符串
#: 必须先经本表映射，绝不直接拼进 ORDER BY。
_BROWSE_SORT_COLUMNS: dict[str, str] = {
    "updated_at": "m.updated_at",
    "occurred_at": "m.occurred_at",
    "priority": "m.priority",
}

#: 0.8 ST-14 才加的列（创意池备注 / 人工标记）。本分支基线尚无，
#: 按存在性探测后决定是否 SELECT——保证「基线独立可跑」与「合并 ST-14 后
#: 自动生效」两种状态都正确，无需二次改码。
_BROWSE_OPTIONAL_COLUMNS: tuple[str, ...] = ("notes", "custom_flag")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回表的列名集合（``PRAGMA table_info``）；表不存在时返回空集合。

    ⚠️ 表名无法参数化，``table`` 必须是**代码内常量**，不得接受外部输入。

    Args:
        conn: 数据库连接。
        table: 表名（内部常量）。

    Returns:
        列名集合；表不存在或查询失败时为空集合。
    """
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row[1] for row in rows}


def tags_by_memory(
    conn: sqlite3.Connection,
    memory_ids: Iterable[str],
) -> dict[str, list[str]]:
    """批量取维度标签：``{memory_id: [dimension_id, ...]}``。

    一次 IN 查询取回整页标签，避免逐条 N+1。页大小上限 200（契约硬约束），
    远低于 SQLite 的变量数上限，无需分批。

    Args:
        conn: memory.db 连接。
        memory_ids: 记忆 id 集合。

    Returns:
        id → 维度列表（按 dimension_id 升序）；无标签的 id 不出现在结果中。
    """
    ids = list(memory_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    out: dict[str, list[str]] = {}
    rows = conn.execute(
        f"""
        SELECT memory_id, dimension_id FROM memory_tags
        WHERE memory_id IN ({placeholders})
        ORDER BY memory_id, dimension_id
        """,
        ids,
    ).fetchall()
    for r in rows:
        out.setdefault(r["memory_id"], []).append(r["dimension_id"])
    return out


def first_source_by_memory(
    conn: sqlite3.Connection,
    memory_ids: Iterable[str],
) -> dict[str, str]:
    """批量取**首条**溯源引用：``{memory_id: source_ref}``（契约 §5.3.3）。

    memory_sources 无显式序号列，以 ``rowid``（插入序）为「首条」口径——
    与 insert_memory 写入 sources 的顺序一致。

    Args:
        conn: memory.db 连接。
        memory_ids: 记忆 id 集合。

    Returns:
        id → 首条 source_ref；无溯源的 id 不出现在结果中（调用方补 null）。
    """
    ids = list(memory_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    out: dict[str, str] = {}
    rows = conn.execute(
        f"""
        SELECT memory_id, source_ref FROM memory_sources
        WHERE memory_id IN ({placeholders})
        ORDER BY memory_id, rowid
        """,
        ids,
    ).fetchall()
    for r in rows:
        out.setdefault(r["memory_id"], r["source_ref"])
    return out


def find_active_by_source_ref_content(
    conn: sqlite3.Connection,
    source_ref: str,
    content: str,
) -> str | None:
    """幂等去重查询：查是否存在「同源 + 同内容」的 active 记忆（2026-08-22 幂等修复）。

    用于 L1.5 落库前拦截「同一 source_ref 重抽出的重复记忆」：重试提炼时旧记忆
    已在库，新记忆内容相同 → 直接复用既有 id，不新增重复行。

    - 匹配口径：``memory_sources.source_ref`` 精确 + ``TRIM(memories.content)`` 精确
      （去除首尾空白后比对，容忍 L1 输出首尾空白差异）+ ``memories.status='active'``
    - ``source_ref`` 一个文件的所有记忆共享同一值（``file_id:{首个msg seq}``），
      故「同源 + 同内容」精确等价于「同一文件重试抽出的同一条记忆」
    - 返回既有 ``memory_id``；无匹配返回 None
    """
    norm = (content or "").strip()
    if not source_ref or not norm:
        return None
    row = conn.execute(
        """
        SELECT m.memory_id FROM memories m
        JOIN memory_sources s ON s.memory_id = m.memory_id
        WHERE s.source_ref = ? AND TRIM(m.content) = ? AND m.status = 'active'
        LIMIT 1
        """,
        (source_ref, norm),
    ).fetchone()
    return row["memory_id"] if row else None


def list_memories_page(
    conn: sqlite3.Connection,
    *,
    page: int = 1,
    limit: int = 50,
    dimension_ids: Iterable[str] | None = None,
    statuses: Iterable[str] = ("active",),
    sort: str = "updated_at",
    order: str = "desc",
    since: str | None = None,
    until: str | None = None,
    ttl_filter: bool = False,
) -> tuple[list[dict], int]:
    """记忆分页查询（契约 §5.3；WebUI 浏览 / SCSM 记忆面板数据源）。

    与 ``list_memories_by_dimension`` 的分工：那个是**注入候选池**查询
    （固定过滤 rejected、按模板语义取 TopN）；本函数是**浏览**查询
    （状态由调用方显式指定、需要 total 计数与稳定分页），语义不同故不合并。

    实现要点：
    - 维度过滤用 ``EXISTS`` 子查询而非 JOIN——JOIN 会因一记忆多标签产生重复行，
      使 COUNT 与 LIMIT 双双失真；多维度为 **AND 语义**（2026-08-13 用户定：
      每个勾选维度都必须命中，每维度一个 EXISTS AND 连接）；
    - ORDER BY 追加 ``memory_id`` 作为**决胜键**，保证同值时分页不重不漏；
    - ``since`` / ``until`` 作用于 ``sort`` 指定的列（契约明确语义，非固定 updated_at）。

    Args:
        conn: memory.db 连接。
        page: 页码（≥ 1，调用方已校验）。
        limit: 页大小（1..200，调用方已校验）。
        dimension_ids: 维度注册表 id 列表；空/None 不过滤（任一命中即入选）。
        statuses: 状态白名单；空集合表示不按状态过滤。
        sort: ``updated_at`` / ``occurred_at`` / ``priority``。
        order: ``desc`` / ``asc``。
        since / until: 作用于 sort 列的闭区间边界。
        ttl_filter: True 时额外应用 TTL 过滤（浏览语义默认 False）。

    Returns:
        ``(items, total)``——items 为当前页条目（含 dimensions / source_ref /
        notes / custom_flag），total 为过滤条件下的总条数。

    Raises:
        ValueError: ``sort`` 不在白名单内（正常路径下 operations 层已拦截）。
    """
    sort_col = _BROWSE_SORT_COLUMNS.get(sort)
    if sort_col is None:
        raise ValueError(f"不支持的排序字段: {sort}")
    direction = "DESC" if str(order).lower() == "desc" else "ASC"

    where: list[str] = []
    params: list[Any] = []
    dims = [d for d in (dimension_ids or ()) if d]
    # 多维度为 AND 语义（2026-08-13 用户定）：每个勾选维度都必须命中——
    # 每个维度一个独立 EXISTS 子查询 AND 连接（GROUP BY HAVING 会影响 COUNT 语义）
    for d in dims:
        where.append(
            "EXISTS (SELECT 1 FROM memory_tags t "
            "WHERE t.memory_id = m.memory_id AND t.dimension_id = ?)"
        )
        params.append(d)
    status_list = [s for s in (statuses or ())]
    if status_list:
        where.append(f"m.status IN ({','.join('?' * len(status_list))})")
        params.extend(status_list)
    if since:
        where.append(f"{sort_col} >= ?")
        params.append(since)
    if until:
        where.append(f"{sort_col} <= ?")
        params.append(until)
    if ttl_filter:
        where.append(
            "(m.ttl_days IS NULL OR "
            "julianday(m.updated_at) > julianday('now') - m.ttl_days)"
        )
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM memories m{where_sql}", params
    ).fetchone()["c"]

    cols = [
        "m.memory_id", "m.content", "m.memory_type", "m.priority", "m.status",
        "m.created_at", "m.updated_at", "m.occurred_at",
    ]
    present = table_columns(conn, "memories")
    cols.extend(f"m.{c}" for c in _BROWSE_OPTIONAL_COLUMNS if c in present)

    rows = conn.execute(
        f"""
        SELECT {', '.join(cols)} FROM memories m{where_sql}
        ORDER BY {sort_col} {direction}, m.memory_id {direction}
        LIMIT ? OFFSET ?
        """,
        params + [int(limit), max(0, (int(page) - 1) * int(limit))],
    ).fetchall()

    items = [dict(r) for r in rows]
    for item in items:
        # 列不存在时补 null 占位，保证响应键集恒定（契约 §5.3.3）
        for c in _BROWSE_OPTIONAL_COLUMNS:
            item.setdefault(c, None)

    ids = [item["memory_id"] for item in items]
    tag_map = tags_by_memory(conn, ids)
    source_map = first_source_by_memory(conn, ids)
    for item in items:
        item["dimensions"] = tag_map.get(item["memory_id"], [])
        item["source_ref"] = source_map.get(item["memory_id"])

    return items, int(total)
