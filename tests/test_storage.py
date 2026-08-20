"""T1 测试：storage 层（建表 + DAO + registry 导入幂等）。

所有测试使用 tmp_path 隔离，不污染真实 data/。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def mem_with_registry(mem_conn, cfg):
    """memory.db + registry 已导入。"""
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    return mem_conn


# ---------- T1.1 建表 ----------

def test_memory_db_has_six_tables(mem_conn):
    """memory.db 含 6 张业务表 + schema_versions。"""
    tables = set(db_mod.list_tables(mem_conn))
    expected = {
        "dimension_registry", "dimension_alias", "memories",
        "memory_archive", "memory_tags", "memory_sources",
        "schema_versions",
    }
    missing = expected - tables
    assert not missing, f"缺表: {missing}"


def test_session_db_has_raw_files(session_conn):
    """v0.7 三库拆分：session.db 含 raw_files + refine_cursor + schema_versions。"""
    tables = set(db_mod.list_tables(session_conn))
    assert "raw_files" in tables
    assert "refine_cursor" in tables
    assert "schema_versions" in tables


def test_indexes_created(mem_conn):
    """关键索引创建：idx_tags_dim / idx_mem_updated / idx_mem_priority / idx_sources_mem。"""
    cur = mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    )
    idx = {r["name"] for r in cur.fetchall()}
    for must in ("idx_tags_dim", "idx_mem_updated", "idx_mem_priority", "idx_sources_mem"):
        assert must in idx, f"缺索引 {must}"


def test_session_indexes_created(session_conn):
    cur = session_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    )
    idx = {r["name"] for r in cur.fetchall()}
    assert "idx_raw_status" in idx


def test_schema_version_recorded(mem_conn):
    """schema_versions 表登记了版本 4（中文检索分词 v0.3：content_seg 列）。"""
    assert db_mod.schema_version(mem_conn) == 4


def test_init_databases_idempotent(tmp_path):
    """重复初始化不报错（幂等）。"""
    m1, s1, w1 = db_mod.init_databases(tmp_path)
    m1.close()
    s1.close()
    w1.close()
    m2, s2, w2 = db_mod.init_databases(tmp_path)
    try:
        assert db_mod.schema_version(m2) == 4
        assert db_mod.schema_version(s2) == 4
        assert db_mod.schema_version(w2) == 4
    finally:
        m2.close()
        w2.close()


# ---------- T1.2 registry 导入 ----------

def test_import_registry_inserts_all_dimensions(mem_with_registry, cfg):
    """导入后 dimension_registry 行数 = 配置维度数。"""
    assert memory_dao.count_dimensions(mem_with_registry) == len(cfg["dimensions"])


def test_import_registry_idempotent(mem_conn, cfg):
    """重复导入不报错且行数不变。"""
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    assert memory_dao.count_dimensions(mem_conn) == len(cfg["dimensions"])


def test_import_registry_aliases_loaded(mem_with_registry, cfg):
    """别名表导入完整（技术栈→tech_stack）。"""
    amap = memory_dao.build_alias_map(mem_with_registry)
    assert amap["技术栈"] == "tech_stack"
    assert amap["身份"] == "identity"
    # 别名总数 > 15（每维度多别名）
    assert len(amap) > 15


def test_get_dimension_returns_fields(mem_with_registry):
    d = memory_dao.get_dimension(mem_with_registry, "tech_stack")
    assert d is not None
    assert d["display_name"] == "技术栈"
    assert d["time_velocity"] == "static"
    assert d["ttl_days"] is None


def test_get_dimension_unknown_returns_none(mem_with_registry):
    assert memory_dao.get_dimension(mem_with_registry, "nope") is None


# ---------- T1.2b boundaries 保留（T-11：YAML→DB 不再静默丢弃） ----------

def test_import_registry_preserves_boundaries(mem_with_registry, cfg):
    """导入后 DB 行保留 boundaries（vs 对照消歧说明）。"""
    for dim in cfg["dimensions"]:
        if "boundaries" not in dim:
            continue
        row = memory_dao.get_dimension(mem_with_registry, dim["id"])
        assert row is not None
        assert row.get("boundaries") == dim["boundaries"], f"{dim['id']} boundaries 丢失"


def test_import_registry_boundaries_none_when_missing(mem_conn, cfg):
    """无 boundaries 字段的维度 → DB 中为 None（不报错）。"""
    dims = [dict(cfg["dimensions"][0])]
    dims[0].pop("boundaries", None)
    memory_dao.upsert_dimension(mem_conn, dims[0])
    row = memory_dao.get_dimension(mem_conn, dims[0]["id"])
    assert row.get("boundaries") is None


def test_upsert_dimension_updates_boundaries(mem_with_registry):
    """重复 upsert 时 boundaries 随行更新（ON CONFLICT UPDATE 含 boundaries）。"""
    d = memory_dao.get_dimension(mem_with_registry, "identity")
    d["boundaries"] = "新边界说明"
    memory_dao.upsert_dimension(mem_with_registry, d)
    row = memory_dao.get_dimension(mem_with_registry, "identity")
    assert row["boundaries"] == "新边界说明"


def test_migrate_dim_boundaries_legacy_db(tmp_path):
    """老库（dimension_registry 无 boundaries 列）→ connect_memory 自动补列（T-11 迁移）。"""
    import sqlite3
    data_dir = tmp_path / "mig"
    data_dir.mkdir()
    # 手工建老结构 memory.db（无 boundaries 列）
    raw = sqlite3.connect(data_dir / "memory.db")
    raw.executescript(
        "CREATE TABLE dimension_registry (id TEXT PRIMARY KEY, display_name TEXT NOT NULL,"
        " category TEXT NOT NULL, time_velocity TEXT NOT NULL DEFAULT 'static',"
        " ttl_days INTEGER, description TEXT, active INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT NOT NULL);"
    )
    raw.commit()
    raw.close()
    # 重连触发迁移
    conn = db_mod.connect_memory(data_dir)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(dimension_registry)").fetchall()]
        assert "boundaries" in cols, f"缺 boundaries 列: {cols}"
        # 幂等：再次连接不报错
        conn2 = db_mod.connect_memory(data_dir)
        conn2.close()
    finally:
        conn.close()


# ---------- T1.3 memories CRUD ----------

def test_insert_memory_basic(mem_with_registry):
    """插入记忆 + 标签 + 溯源，get_memory 返回完整结构。"""
    mid = memory_dao.insert_memory(
        mem_with_registry,
        content="用户使用 Python 3.11",
        memory_type="persona",
        priority=80,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["tech_stack", "skills"],
        sources=[("file_abc:1", "session")],
        agent_tag="agent_x",
    )
    mem = memory_dao.get_memory(mem_with_registry, mid)
    assert mem is not None
    assert mem["content"] == "用户使用 Python 3.11"
    assert mem["priority"] == 80
    assert set(mem["tags"]) == {"tech_stack", "skills"}
    assert mem["sources"] == [{"source_ref": "file_abc:1", "source_type": "session"}]
    assert mem["agent_tag"] == "agent_x"


def test_insert_memory_idempotent_tags(mem_with_registry):
    """重复打同一标签 INSERT OR IGNORE 不报错。"""
    mid = memory_dao.insert_memory(
        mem_with_registry, content="x", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    # 直接再插一次同标签（应被 IGNORE）
    mem_with_registry.execute(
        "INSERT OR IGNORE INTO memory_tags (memory_id, dimension_id) VALUES (?,?)",
        (mid, "tech_stack"),
    )
    mem_with_registry.commit()
    mem = memory_dao.get_memory(mem_with_registry, mid)
    assert mem["tags"] == ["tech_stack"]


def test_update_memory_bumps_updated_at(mem_with_registry):
    """update 续期 updated_at（TTL 起算）。"""
    mid = memory_dao.insert_memory(
        mem_with_registry, content="旧", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"], created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    ok = memory_dao.update_memory_content(
        mem_with_registry, mid, content="新", priority=70
    )
    assert ok
    mem = memory_dao.get_memory(mem_with_registry, mid)
    assert mem["content"] == "新"
    assert mem["priority"] == 70
    # updated_at 推进（不等于原值）
    assert mem["updated_at"] != "2026-01-01T00:00:00Z"


def test_archive_memory_moves_to_archive(mem_with_registry):
    """归档：原行入 memory_archive + superseded_by 指向新行 id，memories 表删除。"""
    old_mid = memory_dao.insert_memory(
        mem_with_registry, content="旧事实", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    new_mid = "new-uuid-xxx"
    ok = memory_dao.archive_memory(mem_with_registry, old_mid, superseded_by=new_mid)
    assert ok
    # memories 表已无旧行
    assert memory_dao.get_memory(mem_with_registry, old_mid) is None
    # archive 表有旧行，superseded_by 指向新行
    chain = memory_dao.find_by_superseded_by(mem_with_registry, new_mid)
    assert len(chain) == 1
    assert chain[0]["memory_id"] == old_mid
    assert chain[0]["superseded_by"] == new_mid
    assert chain[0]["content"] == "旧事实"


def test_archive_memory_unknown_returns_false(mem_with_registry):
    assert memory_dao.archive_memory(mem_with_registry, "nope", "x") is False


def test_list_memories_by_dimension_any(mem_with_registry):
    """OR 查询：维度命中任一即返回。"""
    memory_dao.insert_memory(
        mem_with_registry, content="A", memory_type="persona",
        priority=60, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    memory_dao.insert_memory(
        mem_with_registry, content="B", memory_type="persona",
        priority=70, time_velocity="static", ttl_days=None,
        dimension_ids=["skills"],
    )
    memory_dao.insert_memory(
        mem_with_registry, content="C", memory_type="persona",
        priority=80, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],  # 不在查询范围
    )
    res = memory_dao.list_memories_by_dimension(
        mem_with_registry, ["tech_stack", "skills"], match="any", limit=10
    )
    contents = {r["content"] for r in res}
    assert contents == {"A", "B"}


def test_list_memories_by_dimension_all(mem_with_registry):
    """AND 查询：必须同时命中所有维度。"""
    memory_dao.insert_memory(
        mem_with_registry, content="双标签", memory_type="persona",
        priority=60, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack", "skills"],
    )
    memory_dao.insert_memory(
        mem_with_registry, content="单标签", memory_type="persona",
        priority=70, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    res = memory_dao.list_memories_by_dimension(
        mem_with_registry, ["tech_stack", "skills"], match="all", limit=10
    )
    contents = {r["content"] for r in res}
    assert contents == {"双标签"}


def test_list_memories_ttl_filter(mem_with_registry):
    """TTL 过滤：过期记忆不返回。"""
    # 手动插入一条已过期的 status 记忆（TTL 7 天，updated_at 30 天前）
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory_dao.insert_memory(
        mem_with_registry, content="过时状态", memory_type="persona",
        priority=50, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"],
        created_at=old, updated_at=old,
    )
    # 不过期的静态记忆
    memory_dao.insert_memory(
        mem_with_registry, content="身份", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["status"],  # 用同维度便于一并查询
    )
    res = memory_dao.list_memories_by_dimension(
        mem_with_registry, ["status"], match="any", limit=10, include_expired=False
    )
    contents = {r["content"] for r in res}
    assert "过时状态" not in contents
    assert "身份" in contents


def test_list_memories_include_expired(mem_with_registry):
    """include_expired=True 时返回过期记忆。"""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory_dao.insert_memory(
        mem_with_registry, content="过时", memory_type="persona",
        priority=50, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"], created_at=old, updated_at=old,
    )
    res = memory_dao.list_memories_by_dimension(
        mem_with_registry, ["status"], match="any", limit=10, include_expired=True
    )
    assert any(r["content"] == "过时" for r in res)


# ---------- T1.4 session_dao raw_files ----------

def test_raw_file_insert_and_get(session_conn):
    """raw_files 插入与查询。"""
    session_dao.insert_raw_file(
        session_conn, file_id="f1", path="raw/sessions/f1.md",
        session_key="sess_a", started_at="2026-08-04T10:00:00Z",
        agent_id="agent_x", size=100,
    )
    rf = session_dao.get_raw_file(session_conn, "f1")
    assert rf is not None
    assert rf["session_key"] == "sess_a"
    assert rf["status"] == "new"
    assert rf["size"] == 100


def test_raw_file_upsert_idempotent(session_conn):
    """同 file_id 重复插入：更新而非报错。"""
    session_dao.insert_raw_file(
        session_conn, file_id="f1", path="raw/sessions/f1.md",
        session_key="sess_a", started_at="2026-08-04T10:00:00Z",
        size=100,
    )
    session_dao.insert_raw_file(
        session_conn, file_id="f1", path="raw/sessions/f1.md",
        session_key="sess_a", started_at="2026-08-04T10:00:00Z",
        ended_at="2026-08-04T11:00:00Z", size=200,
    )
    rf = session_dao.get_raw_file(session_conn, "f1")
    assert rf["size"] == 200
    assert rf["ended_at"] == "2026-08-04T11:00:00Z"


def test_update_refine_cursor(session_conn):
    """提炼游标更新：last_refined_seq + refined_at + status=refined。"""
    session_dao.insert_raw_file(
        session_conn, file_id="f1", path="raw/sessions/f1.md",
        session_key="sess_a", started_at="2026-08-04T10:00:00Z",
    )
    ok = session_dao.update_refine_cursor(session_conn, "f1", last_refined_seq=5)
    assert ok
    rf = session_dao.get_raw_file(session_conn, "f1")
    assert rf["last_refined_seq"] == 5
    assert rf["status"] == "refined"
    assert rf["refined_at"] is not None


def test_list_by_status(session_conn):
    session_dao.insert_raw_file(session_conn, "f1", "p1", "s1", "2026-08-04T10:00:00Z")
    session_dao.insert_raw_file(session_conn, "f2", "p2", "s2", "2026-08-04T11:00:00Z")
    session_dao.update_refine_cursor(session_conn, "f1", 3)
    new_files = session_dao.list_by_status(session_conn, "new")
    refined_files = session_dao.list_by_status(session_conn, "refined")
    assert {r["file_id"] for r in new_files} == {"f2"}
    assert {r["file_id"] for r in refined_files} == {"f1"}


def test_mark_status_error(session_conn):
    """坏文件标记 status=error。"""
    session_dao.insert_raw_file(session_conn, "f1", "p1", "s1", "2026-08-04T10:00:00Z")
    ok = session_dao.mark_status(session_conn, "f1", "error")
    assert ok
    rf = session_dao.get_raw_file(session_conn, "f1")
    assert rf["status"] == "error"

# ---------- T-69（2026-08-16）：memory_sources 幂等写入防御 ----------

def test_memory_sources_unique_constraint(mem_with_registry):
    """memory_sources 有 UNIQUE(memory_id, source_ref)，同源重复写入幂等忽略。"""
    # 同一记忆写入两个不同 source_ref（正常）
    mid = memory_dao.insert_memory(
        mem_with_registry, content="T-69 测试记忆", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
        sources=[("file_a:1", "session")],
    )
    n = mem_with_registry.execute(
        "SELECT COUNT(*) FROM memory_sources WHERE memory_id=? AND source_ref='file_a:1'", (mid,)
    ).fetchone()[0]
    assert n == 1

    # 直接 INSERT 同源（绕过 DAO）→ 触发 UNIQUE 约束抛 IntegrityError
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        mem_with_registry.execute(
            "INSERT INTO memory_sources (memory_id, source_ref, source_type) VALUES (?,?,?)",
            (mid, "file_a:1", "session"),
        )


def test_memory_sources_schema_has_pk(mem_with_registry):
    """schema 层：PRAGMA 确认复合主键存在。"""
    cols = mem_with_registry.execute("PRAGMA table_info(memory_sources)").fetchall()
    pk_cols = [r[1] for r in cols if r[5] > 0]
    assert sorted(pk_cols) == ["memory_id", "source_ref"]
