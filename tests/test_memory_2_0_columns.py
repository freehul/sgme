"""2.0 L2 提取层新增列迁移测试（设计依据 D-23 / D-30 / D-53）。

覆盖三条契约：
1. 新库经 connect_memory 建表后即带 feeling / trait / scene_key / needs_review
2. 老库（只有 1.x 列的 memories 表）跑迁移能补齐，且**存量行 needs_review 默认 0**
   ——不因新增待审闸门把历史记忆挡在注入之外
3. 迁移幂等：重复调用不报错、列集合不变
"""

from __future__ import annotations

import sqlite3

from sgme.data import db

NEW_COLS = {"feeling", "trait", "scene_key", "needs_review", "l2_facts_json",
            "type", "subject", "certainty", "derivation", "modality", "route"}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_new_db_has_2_0_columns(tmp_path):
    conn = db.connect_memory(tmp_path / "data")
    for table in ("memories", "memory_archive"):
        missing = NEW_COLS - _cols(conn, table)
        assert not missing, f"{table} 缺列: {missing}"


def test_scene_index_created(tmp_path):
    conn = db.connect_memory(tmp_path / "data")
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memories'").fetchall()}
    assert "idx_mem_scene" in idx


def test_migration_adds_columns_to_old_db(tmp_path):
    """模拟 1.x 老库：memories 只有最少的列，迁移后补齐且存量行可读。"""
    d = tmp_path / "data"
    d.mkdir(parents=True)
    conn = sqlite3.connect(d / "memory.db")
    conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    conn.execute("CREATE TABLE memory_archive (memory_id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    conn.execute("INSERT INTO memories(memory_id, content) VALUES ('m1', '旧记忆')")

    db._migrate_memory_2_0_columns(conn)

    assert NEW_COLS <= _cols(conn, "memories")
    assert NEW_COLS <= _cols(conn, "memory_archive")
    # 存量行不得被待审闸门误挡：needs_review 默认 0
    row = conn.execute("SELECT needs_review, feeling, trait, scene_key FROM memories WHERE memory_id='m1'").fetchone()
    assert row[0] == 0
    assert row[1] is None and row[2] is None and row[3] is None


def test_migration_is_idempotent(tmp_path):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    conn = sqlite3.connect(d / "memory.db")
    conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    conn.execute("CREATE TABLE memory_archive (memory_id TEXT PRIMARY KEY, content TEXT NOT NULL)")

    db._migrate_memory_2_0_columns(conn)
    cols_first = _cols(conn, "memories")
    db._migrate_memory_2_0_columns(conn)  # 第二次不得抛错

    assert _cols(conn, "memories") == cols_first


def test_2_0_metadata_roundtrip(tmp_path):
    """2.0 元数据必须能落库并读回——1.x 解析层算了这些字段但落库时全丢弃。"""
    from sgme.data import memory_dao

    conn = db.connect_memory(tmp_path / "data")
    memory_dao.import_registry(
        conn,
        [{"id": "preferences", "display_name": "偏好", "category": "persona",
          "time_velocity": "static", "ttl_days": None, "description": "x"}],
        {},
    )
    mid = memory_dao.insert_memory(
        conn,
        content="用户要求删除操作一律走回收站",
        memory_type="persona",
        priority=70,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["preferences"],
        extra={
            "type": "约束",
            "subject": "self",
            "certainty": "confirmed",
            "derivation": "fact",
            "modality": "done",
            "route": "memory",
            "feeling": "平静",
            "trait": "谨慎",
            "scene_key": "整理文件",
            "needs_review": 0,
            "l2_facts_json": {"facts": [], "causes": [{"from": "a", "to": "b"}]},
        },
    )
    row = conn.execute(
        "SELECT type, subject, certainty, derivation, modality, route, feeling, trait, "
        "scene_key, needs_review, l2_facts_json FROM memories WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row[0] == "约束" and row[1] == "self" and row[5] == "memory"
    assert row[7] == "谨慎" and row[8] == "整理文件" and row[9] == 0
    assert "causes" in row[10]


def test_insert_without_extra_still_works(tmp_path):
    """不带 2.0 元数据的老调用保持可用（向后兼容）。"""
    from sgme.data import memory_dao

    conn = db.connect_memory(tmp_path / "data")
    memory_dao.import_registry(
        conn,
        [{"id": "preferences", "display_name": "偏好", "category": "persona",
          "time_velocity": "static", "ttl_days": None, "description": "x"}],
        {},
    )
    mid = memory_dao.insert_memory(
        conn, content="老调用", memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["preferences"],
    )
    row = conn.execute("SELECT needs_review, type FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row[0] == 0 and row[1] is None


def test_exclude_review_filters_pending_candidates(tmp_path):
    """D-23：needs_review=1 不进主注入块（默认不过滤，检索仍可见）。"""
    from sgme.data import memory_dao

    conn = db.connect_memory(tmp_path / "data")
    memory_dao.import_registry(
        conn,
        [{"id": "preferences", "display_name": "偏好", "category": "persona",
          "time_velocity": "static", "ttl_days": None, "description": "x"}],
        {},
    )
    for i, review in enumerate((0, 1)):
        memory_dao.insert_memory(
            conn, content=f"记忆{i}", memory_type="persona", priority=70,
            time_velocity="static", ttl_days=None, dimension_ids=["preferences"],
            extra={"needs_review": review},
        )
    all_rows = memory_dao.list_memories_by_dimension(conn, ["preferences"], limit=10)
    injected = memory_dao.list_memories_by_dimension(
        conn, ["preferences"], limit=10, exclude_review=True)
    assert len(all_rows) == 2
    assert len(injected) == 1
    assert injected[0]["content"] == "记忆0"


def test_archive_preserves_2_0_metadata(tmp_path):
    """归档必须保留 2.0 元数据——归档链是对账/回溯的唯一依据。"""
    from sgme.data import memory_dao

    conn = db.connect_memory(tmp_path / "data")
    memory_dao.import_registry(
        conn,
        [{"id": "preferences", "display_name": "偏好", "category": "persona",
          "time_velocity": "static", "ttl_days": None, "description": "x"}],
        {},
    )
    mid = memory_dao.insert_memory(
        conn, content="用户要求删除操作走回收站", memory_type="persona", priority=80,
        time_velocity="static", ttl_days=None, dimension_ids=["preferences"],
        extra={"type": "约束", "subject": "self", "route": "memory",
               "feeling": "平静", "trait": "谨慎", "scene_key": "整理文件",
               "needs_review": 0},
    )
    assert memory_dao.archive_memory(conn, mid, superseded_by=None) is True
    row = conn.execute(
        "SELECT type, subject, route, feeling, trait, scene_key FROM memory_archive "
        "WHERE memory_id=?", (mid,)
    ).fetchone()
    assert row[0] == "约束" and row[2] == "memory"
    assert row[3] == "平静" and row[4] == "谨慎" and row[5] == "整理文件"
