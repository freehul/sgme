"""T9 测试：v0.4 storage 扩展（六表 DDL + 索引 + DAO CRUD）。

覆盖：
- 建表自检：scenes/scene_memories/scene_versions/memory_vectors/signal_events/signal_subscribers
- 索引存在性：idx_scenes_status/idx_scene_memories_memory/idx_signal_events_ts/idx_signal_events_type
- scenes CRUD：insert（heat=1）/ update_content（heat+1）/ update_status（archived）
- scene_memories：add_memory_link 幂等
- scene_versions：insert + list
- memory_vectors：upsert + get + list_without_vector
- signal_events：insert + list_since（按 ts / event_id / type 过滤）+ mark_consumed
- signal_subscribers：upsert + get + list_unconsumed（游标推进）

所有测试使用 tmp_path 隔离，不污染真实 data/。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao, scene_dao, signal_dao


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
def wiki_conn(tmp_path):
    conn = db_mod.connect_wiki(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def mem_with_registry(mem_conn, cfg):
    """memory.db + registry 已导入（memories 写入需 FK 校验维度存在）。"""
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    return mem_conn


# ---------- 建表自检：六表存在 ----------

def test_memory_db_has_v04_scene_tables(mem_conn):
    """v0.7 三库拆分：scenes / scene_memories / scene_versions 迁入 memory.db。"""
    tables = set(db_mod.list_tables(mem_conn))
    for t in ("scenes", "scene_memories", "scene_versions"):
        assert t in tables, f"缺表 {t}"


def test_memory_db_has_v04_signal_vector_tables(mem_conn):
    """memory.db 含 memory_vectors / signal_events / signal_subscribers。"""
    tables = set(db_mod.list_tables(mem_conn))
    for t in ("memory_vectors", "signal_events", "signal_subscribers"):
        assert t in tables, f"缺表 {t}"


# ---------- 索引存在性 ----------

def test_scene_v04_indexes(mem_conn):
    """scenes / scene_memories 关键索引就位（v0.7 起随表迁入 memory.db）。"""
    cur = mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    )
    idx = {r["name"] for r in cur.fetchall()}
    for must in ("idx_scenes_status", "idx_scene_memories_memory"):
        assert must in idx, f"缺索引 {must}"


def test_memory_v04_indexes(mem_conn):
    """signal_events 关键索引就位。"""
    cur = mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    )
    idx = {r["name"] for r in cur.fetchall()}
    for must in ("idx_signal_events_ts", "idx_signal_events_type"):
        assert must in idx, f"缺索引 {must}"


# ---------- schema 版本升级到 4 ----------

def test_schema_version_is_four(mem_conn):
    """#33 + 中文分词 v0.3 升级后 schema_version = 4（v3 → v4：content_seg 列）。"""
    assert db_mod.schema_version(mem_conn) == 4


def test_schema_version_wiki_is_four(wiki_conn):
    assert db_mod.schema_version(wiki_conn) == 4


# ---------- scenes CRUD ----------

def test_insert_scene_defaults(mem_conn):
    """新建场景 heat=1, status='active'。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(mem_conn, sid, "测试场景", "场景正文")
    s = scene_dao.get_scene(mem_conn, sid)
    assert s is not None
    assert s["scene_id"] == sid
    assert s["title"] == "测试场景"
    assert s["content"] == "场景正文"
    assert s["heat"] == 1
    assert s["status"] == "active"
    assert s["created_at"] is not None
    assert s["updated_at"] is not None
    assert s["last_memory_added_at"] is None


def test_get_scene_unknown_returns_none(mem_conn):
    assert scene_dao.get_scene(mem_conn, "nope") is None


def test_update_scene_content_heat_increment(mem_conn):
    """更新场景内容后 heat 自增 +1。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(mem_conn, sid, "t", "旧内容")
    ok = scene_dao.update_scene_content(mem_conn, sid, "新内容")
    assert ok
    s = scene_dao.get_scene(mem_conn, sid)
    assert s["content"] == "新内容"
    assert s["heat"] == 2  # 1 + 1


def test_update_scene_content_custom_heat_increment(mem_conn):
    """合并场景时调用方可传 heat_increment（如 sum+1）。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(mem_conn, sid, "t", "c1")
    # 模拟 MERGE：原 heat=1 + 另一场景 heat=2 + 1 = 4
    ok = scene_dao.update_scene_content(mem_conn, sid, "合并后", heat_increment=3)
    assert ok
    s = scene_dao.get_scene(mem_conn, sid)
    assert s["heat"] == 4


def test_update_scene_content_with_last_memory_added_at(mem_conn):
    """更新内容时同步更新 last_memory_added_at。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(mem_conn, sid, "t", "c")
    scene_dao.update_scene_content(
        mem_conn, sid, "c2", last_memory_added_at="2026-08-04T10:00:00Z"
    )
    s = scene_dao.get_scene(mem_conn, sid)
    assert s["last_memory_added_at"] == "2026-08-04T10:00:00Z"


def test_update_scene_status_archived(mem_conn):
    """软删除：status=archived。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(mem_conn, sid, "t", "c")
    ok = scene_dao.update_scene_status(mem_conn, sid, "archived")
    assert ok
    s = scene_dao.get_scene(mem_conn, sid)
    assert s["status"] == "archived"


def test_update_scene_status_restore(mem_conn):
    """软删除可恢复：archived → active。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(mem_conn, sid, "t", "c")
    scene_dao.update_scene_status(mem_conn, sid, "archived")
    scene_dao.update_scene_status(mem_conn, sid, "active")
    s = scene_dao.get_scene(mem_conn, sid)
    assert s["status"] == "active"


def test_list_active_scenes(mem_conn):
    """list_active_scenes 只返回 active，按 updated_at DESC。"""
    scene_dao.insert_scene(mem_conn, "s1", "t1", "c1",
                          created_at="2026-08-01T00:00:00Z", updated_at="2026-08-01T00:00:00Z")
    scene_dao.insert_scene(mem_conn, "s2", "t2", "c2",
                          created_at="2026-08-02T00:00:00Z", updated_at="2026-08-02T00:00:00Z")
    scene_dao.insert_scene(mem_conn, "s3", "t3", "c3",
                          created_at="2026-08-03T00:00:00Z", updated_at="2026-08-03T00:00:00Z")
    scene_dao.update_scene_status(mem_conn, "s2", "archived")
    active = scene_dao.list_active_scenes(mem_conn)
    ids = [s["scene_id"] for s in active]
    assert ids == ["s3", "s1"]  # DESC by updated_at


def test_list_active_scenes_with_limit(mem_conn):
    for i in range(5):
        scene_dao.insert_scene(mem_conn, f"s{i}", f"t{i}", f"c{i}",
                              created_at=f"2026-08-0{i+1}T00:00:00Z",
                              updated_at=f"2026-08-0{i+1}T00:00:00Z")
    active = scene_dao.list_active_scenes(mem_conn, limit=2)
    assert len(active) == 2


def test_list_scenes_over_threshold(mem_conn):
    """返回 active 场景总数（调用方与 threshold 比较判断红/橙/黄）。"""
    for i in range(5):
        scene_dao.insert_scene(mem_conn, f"s{i}", f"t{i}", f"c{i}")
    # threshold=3，当前 5 > 3 → 触发预警
    assert scene_dao.list_scenes_over_threshold(mem_conn, 3) == 5


def test_count_scenes_by_status(mem_conn):
    scene_dao.insert_scene(mem_conn, "s1", "t1", "c1")
    scene_dao.insert_scene(mem_conn, "s2", "t2", "c2")
    scene_dao.update_scene_status(mem_conn, "s2", "archived")
    assert scene_dao.count_scenes(mem_conn, "active") == 1
    assert scene_dao.count_scenes(mem_conn, "archived") == 1


# ---------- scene_memories ----------

def test_add_memory_link_idempotent(mem_conn):
    """重复添加同一关联不报错（INSERT OR IGNORE）。"""
    scene_dao.insert_scene(mem_conn, "s1", "t1", "c1")
    scene_dao.add_memory_link(mem_conn, "s1", "m1")
    scene_dao.add_memory_link(mem_conn, "s1", "m1")  # 重复
    mids = scene_dao.list_memories_for_scene(mem_conn, "s1")
    assert mids == ["m1"]


def test_list_memories_for_scene(mem_conn):
    """返回场景关联的记忆 id 列表（按 memory_id ASC）。"""
    scene_dao.insert_scene(mem_conn, "s1", "t1", "c1")
    scene_dao.add_memory_link(mem_conn, "s1", "m2")
    scene_dao.add_memory_link(mem_conn, "s1", "m1")
    scene_dao.add_memory_link(mem_conn, "s1", "m3")
    mids = scene_dao.list_memories_for_scene(mem_conn, "s1")
    assert mids == ["m1", "m2", "m3"]


def test_list_memories_for_scene_empty(mem_conn):
    scene_dao.insert_scene(mem_conn, "s1", "t1", "c1")
    assert scene_dao.list_memories_for_scene(mem_conn, "s1") == []


# ---------- scene_versions ----------

def test_insert_scene_version_and_list(mem_conn):
    """insert_scene_version 后 list_scene_versions 返回归档记录。"""
    scene_dao.insert_scene(mem_conn, "s1", "t1", "v1内容")
    vid = str(uuid.uuid4())
    scene_dao.insert_scene_version(
        mem_conn, vid, "s1", "v1内容快照", reason="更新前"
    )
    versions = scene_dao.list_scene_versions(mem_conn, "s1")
    assert len(versions) == 1
    assert versions[0]["version_id"] == vid
    assert versions[0]["scene_id"] == "s1"
    assert versions[0]["content"] == "v1内容快照"
    assert versions[0]["reason"] == "更新前"
    assert versions[0]["version_at"] is not None


def test_list_scene_versions_multiple_ordered(mem_conn):
    """多版本按 version_at ASC 返回。"""
    scene_dao.insert_scene(mem_conn, "s1", "t1", "v3")
    scene_dao.insert_scene_version(
        mem_conn, "vid1", "s1", "v1快照",
        version_at="2026-08-01T00:00:00Z", reason="first"
    )
    scene_dao.insert_scene_version(
        mem_conn, "vid2", "s1", "v2快照",
        version_at="2026-08-02T00:00:00Z", reason="second"
    )
    versions = scene_dao.list_scene_versions(mem_conn, "s1")
    assert [v["version_id"] for v in versions] == ["vid1", "vid2"]


def test_list_scene_versions_empty(mem_conn):
    scene_dao.insert_scene(mem_conn, "s1", "t1", "c1")
    assert scene_dao.list_scene_versions(mem_conn, "s1") == []


# ---------- memory_vectors ----------

def test_upsert_and_get_vector(mem_with_registry):
    """upsert_vector 后 get_vector 返回 BLOB + 元数据。"""
    mid = memory_dao.insert_memory(
        mem_with_registry, content="测试向量", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    emb = b"\x00\x01\x02\x03\x04"
    memory_dao.upsert_vector(
        mem_with_registry, mid, emb, "test-model", dims=4,
        embedded_at="2026-08-04T10:00:00Z"
    )
    v = memory_dao.get_vector(mem_with_registry, mid)
    assert v is not None
    assert v["memory_id"] == mid
    assert v["embedding"] == emb
    assert v["model"] == "test-model"
    assert v["dims"] == 4
    assert v["embedded_at"] == "2026-08-04T10:00:00Z"


def test_upsert_vector_replaces_existing(mem_with_registry):
    """同 memory_id 重复 upsert 替换（模型切换重嵌场景）。"""
    mid = memory_dao.insert_memory(
        mem_with_registry, content="测试", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    memory_dao.upsert_vector(mem_with_registry, mid, b"\x00", "model-a", 1)
    memory_dao.upsert_vector(mem_with_registry, mid, b"\x01\x02", "model-b", 2)
    v = memory_dao.get_vector(mem_with_registry, mid)
    assert v["embedding"] == b"\x01\x02"
    assert v["model"] == "model-b"
    assert v["dims"] == 2


def test_get_vector_unknown_returns_none(mem_with_registry):
    assert memory_dao.get_vector(mem_with_registry, "nope") is None


def test_list_memories_without_vector(mem_with_registry):
    """list_memories_without_vector 返回缺向量的记忆。"""
    mid_with_vec = memory_dao.insert_memory(
        mem_with_registry, content="有向量", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    mid_without_vec = memory_dao.insert_memory(
        mem_with_registry, content="无向量", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    memory_dao.upsert_vector(mem_with_registry, mid_with_vec, b"\x00", "m", 1)
    missing = memory_dao.list_memories_without_vector(mem_with_registry)
    mids = {m["memory_id"] for m in missing}
    assert mid_without_vec in mids
    assert mid_with_vec not in mids


def test_list_memories_without_vector_with_limit(mem_with_registry):
    for i in range(5):
        memory_dao.insert_memory(
            mem_with_registry, content=f"m{i}", memory_type="persona",
            priority=50, time_velocity="static", ttl_days=None,
            dimension_ids=["tech_stack"],
        )
    missing = memory_dao.list_memories_without_vector(mem_with_registry, limit=2)
    assert len(missing) == 2


def test_list_memories_without_vector_empty_when_all_indexed(mem_with_registry):
    """所有记忆都有向量时返回空。"""
    mid = memory_dao.insert_memory(
        mem_with_registry, content="x", memory_type="persona",
        priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["tech_stack"],
    )
    memory_dao.upsert_vector(mem_with_registry, mid, b"\x00", "m", 1)
    assert memory_dao.list_memories_without_vector(mem_with_registry) == []


# ---------- signal_events ----------

def test_insert_and_get_event(mem_conn):
    """insert_event 后 get_event 返回完整事件信封。"""
    eid = str(uuid.uuid4())
    signal_dao.insert_event(
        mem_conn, eid, "memory_updated", "refine",
        '{"batch_size":10}', "2026-08-04T10:00:00Z"
    )
    e = signal_dao.get_event(mem_conn, eid)
    assert e is not None
    assert e["event_id"] == eid
    assert e["type"] == "memory_updated"
    assert e["source"] == "refine"
    assert e["payload"] == '{"batch_size":10}'
    assert e["ts"] == "2026-08-04T10:00:00Z"
    assert e["consumed_at"] is None


def test_get_event_unknown_returns_none(mem_conn):
    assert signal_dao.get_event(mem_conn, "nope") is None


def test_list_events_since_ts(mem_conn):
    """按 since_ts 过滤（ts > since_ts）。"""
    signal_dao.insert_event(mem_conn, "e1", "t", "s", "{}", "2026-08-04T10:00:00Z")
    signal_dao.insert_event(mem_conn, "e2", "t", "s", "{}", "2026-08-04T11:00:00Z")
    signal_dao.insert_event(mem_conn, "e3", "t", "s", "{}", "2026-08-04T12:00:00Z")
    events = signal_dao.list_events_since(mem_conn, since_ts="2026-08-04T10:30:00Z")
    ids = [e["event_id"] for e in events]
    assert ids == ["e2", "e3"]


def test_list_events_since_event_id(mem_conn):
    """按 since_event_id 游标式过滤（event_id > since_event_id）。"""
    signal_dao.insert_event(mem_conn, "e1", "t", "s", "{}", "2026-08-04T10:00:00Z")
    signal_dao.insert_event(mem_conn, "e2", "t", "s", "{}", "2026-08-04T11:00:00Z")
    signal_dao.insert_event(mem_conn, "e3", "t", "s", "{}", "2026-08-04T12:00:00Z")
    events = signal_dao.list_events_since(mem_conn, since_event_id="e1")
    ids = [e["event_id"] for e in events]
    assert ids == ["e2", "e3"]


def test_list_events_by_type(mem_conn):
    """按 event_type 过滤。"""
    signal_dao.insert_event(mem_conn, "e1", "memory_updated", "s", "{}", "2026-08-04T10:00:00Z")
    signal_dao.insert_event(mem_conn, "e2", "anomaly_warn", "s", "{}", "2026-08-04T11:00:00Z")
    signal_dao.insert_event(mem_conn, "e3", "memory_updated", "s", "{}", "2026-08-04T12:00:00Z")
    events = signal_dao.list_events_since(mem_conn, event_type="memory_updated")
    types = {e["type"] for e in events}
    assert types == {"memory_updated"}
    assert len(events) == 2


def test_list_events_limit(mem_conn):
    """limit 截断。"""
    for i in range(5):
        signal_dao.insert_event(
            mem_conn, f"e{i}", "t", "s", "{}",
            f"2026-08-04T1{i}:00:00Z"
        )
    events = signal_dao.list_events_since(mem_conn, limit=2)
    assert len(events) == 2
    assert events[0]["event_id"] == "e0"
    assert events[1]["event_id"] == "e1"


def test_mark_consumed(mem_conn):
    """mark_consumed 写入 consumed_at。"""
    eid = str(uuid.uuid4())
    signal_dao.insert_event(mem_conn, eid, "t", "s", "{}", "2026-08-04T10:00:00Z")
    ok = signal_dao.mark_consumed(mem_conn, eid)
    assert ok
    e = signal_dao.get_event(mem_conn, eid)
    assert e["consumed_at"] is not None


def test_mark_consumed_unknown_returns_false(mem_conn):
    assert signal_dao.mark_consumed(mem_conn, "nope") is False


# ---------- signal_subscribers ----------

def test_upsert_and_get_subscriber(mem_conn):
    """upsert_subscriber 后 get_subscriber 返回游标。"""
    signal_dao.upsert_subscriber(
        mem_conn, "sub1", "e1", "2026-08-04T10:00:00Z"
    )
    s = signal_dao.get_subscriber(mem_conn, "sub1")
    assert s is not None
    assert s["subscriber_id"] == "sub1"
    assert s["last_signal_id"] == "e1"
    assert s["last_consumed_ts"] == "2026-08-04T10:00:00Z"


def test_upsert_subscriber_replaces(mem_conn):
    """同 subscriber_id 重复 upsert 替换游标。"""
    signal_dao.upsert_subscriber(mem_conn, "sub1", "e1", "2026-08-04T10:00:00Z")
    signal_dao.upsert_subscriber(mem_conn, "sub1", "e2", "2026-08-04T11:00:00Z")
    s = signal_dao.get_subscriber(mem_conn, "sub1")
    assert s["last_signal_id"] == "e2"
    assert s["last_consumed_ts"] == "2026-08-04T11:00:00Z"


def test_get_subscriber_unknown_returns_none(mem_conn):
    assert signal_dao.get_subscriber(mem_conn, "nope") is None


def test_list_unconsumed_creates_subscriber_if_absent(mem_conn):
    """subscriber 不存在时自动创建并从头拉取。"""
    signal_dao.insert_event(mem_conn, "e1", "t", "s", "{}", "2026-08-04T10:00:00Z")
    signal_dao.insert_event(mem_conn, "e2", "t", "s", "{}", "2026-08-04T11:00:00Z")
    events = signal_dao.list_unconsumed(mem_conn, "sub1")
    assert len(events) == 2
    # 游标推进到最后一条
    s = signal_dao.get_subscriber(mem_conn, "sub1")
    assert s["last_signal_id"] == "e2"
    assert s["last_consumed_ts"] == "2026-08-04T11:00:00Z"


def test_list_unconsumed_advances_cursor(mem_conn):
    """list_unconsumed 第二次拉取时游标已推进，无新事件返回空。"""
    signal_dao.insert_event(mem_conn, "e1", "t", "s", "{}", "2026-08-04T10:00:00Z")
    signal_dao.insert_event(mem_conn, "e2", "t", "s", "{}", "2026-08-04T11:00:00Z")
    signal_dao.insert_event(mem_conn, "e3", "t", "s", "{}", "2026-08-04T12:00:00Z")

    # 第一次拉取：全部 3 条 + 游标推进到 e3
    events = signal_dao.list_unconsumed(mem_conn, "sub1")
    assert [e["event_id"] for e in events] == ["e1", "e2", "e3"]
    s = signal_dao.get_subscriber(mem_conn, "sub1")
    assert s["last_signal_id"] == "e3"

    # 第二次拉取：无新事件
    events = signal_dao.list_unconsumed(mem_conn, "sub1")
    assert events == []


def test_list_unconsumed_continues_from_cursor(mem_conn):
    """已有游标的 subscriber 从 last_signal_id 之后继续拉取。"""
    signal_dao.insert_event(mem_conn, "e1", "t", "s", "{}", "2026-08-04T10:00:00Z")
    signal_dao.insert_event(mem_conn, "e2", "t", "s", "{}", "2026-08-04T11:00:00Z")
    signal_dao.insert_event(mem_conn, "e3", "t", "s", "{}", "2026-08-04T12:00:00Z")
    # 预设游标在 e1
    signal_dao.upsert_subscriber(mem_conn, "sub1", "e1", "2026-08-04T10:00:00Z")
    events = signal_dao.list_unconsumed(mem_conn, "sub1")
    assert [e["event_id"] for e in events] == ["e2", "e3"]
    s = signal_dao.get_subscriber(mem_conn, "sub1")
    assert s["last_signal_id"] == "e3"


def test_list_unconsumed_with_limit(mem_conn):
    """limit 截断 + 游标推进到本次最后一条。"""
    for i in range(5):
        signal_dao.insert_event(
            mem_conn, f"e{i}", "t", "s", "{}",
            f"2026-08-04T1{i}:00:00Z"
        )
    events = signal_dao.list_unconsumed(mem_conn, "sub1", limit=2)
    assert [e["event_id"] for e in events] == ["e0", "e1"]
    s = signal_dao.get_subscriber(mem_conn, "sub1")
    assert s["last_signal_id"] == "e1"
    # last_consumed_ts = e1 的 ts（i=1 → T11）
    assert s["last_consumed_ts"] == "2026-08-04T11:00:00Z"

    # 再拉一次：从 e1 之后继续
    events = signal_dao.list_unconsumed(mem_conn, "sub1", limit=2)
    assert [e["event_id"] for e in events] == ["e2", "e3"]


# ---------- 迁移幂等：v1 库升级到 v2 ----------

def test_v2_migration_idempotent_on_existing_db(tmp_path):
    """已有 v1 库重新连接 → 升级到 v2，新增六表存在且原数据不丢。"""
    # 模拟 v1 库：手动建 schema_versions 表 + 插入 v1 行（不建新表）
    raw = sqlite3.connect(str(tmp_path / "memory.db"))
    raw.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL)
    """)
    raw.execute(
        "INSERT INTO schema_versions (version, name, applied_at) VALUES (?,?,?)",
        (1, "memory_v1", "2026-01-01T00:00:00Z"),
    )
    raw.commit()
    raw.close()

    # 调用 connect_memory（v4）→ 应建全部新表 + 追加 v2/v3/v4 行
    conn2 = db_mod.connect_memory(tmp_path)
    try:
        tables = set(db_mod.list_tables(conn2))
        for t in ("memory_vectors", "signal_events", "signal_subscribers"):
            assert t in tables
        # schema_versions 表中 v1 与 v4 都登记（DDL 累积式，直接跳到当前版本）
        cur = conn2.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        )
        versions = [r["version"] for r in cur.fetchall()]
        assert 1 in versions
        assert 4 in versions
        # 当前版本 = 4
        assert db_mod.schema_version(conn2) == 4
    finally:
        conn2.close()


def test_v2_migration_scene_tables_idempotent(tmp_path):
    """memory.db 重复初始化幂等，scenes 表不丢（v0.7 起 scenes 归 memory.db）。"""
    c1 = db_mod.connect_memory(tmp_path)
    c1.close()
    c2 = db_mod.connect_memory(tmp_path)
    try:
        tables = set(db_mod.list_tables(c2))
        for t in ("scenes", "scene_memories", "scene_versions"):
            assert t in tables
        assert db_mod.schema_version(c2) == 4
    finally:
        c2.close()
