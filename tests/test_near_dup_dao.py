"""L1.5 T1.3 测试：near_dup_quarantine 隔离表 DAO（near_dup_dao）。

覆盖：
- upsert_hit 幂等去重：同 (matched_memory_id, content_hash) 只更分不增行（MAX 语义）
- mark_discarded：pending → discarded；非 pending / 不存在 → False
- restore 集成：全字段落 memories（tags / sources / content_seg）+ 向量即时回填
  （mock embed 返回假向量，让真实 upsert_memory_vector 落库）+ 隔离行回写
- 裸连接兜底：未开 row_factory 的 sqlite3 连接调 list_items / get_item 不崩
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_dao, near_dup_dao
from sgme.data.search import vector as vector_mod


@pytest.fixture
def cfg():
    from sgme import config
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    # 导入注册表（memory_tags 外键需要 dimension_registry 行）
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


# ---------- upsert_hit：幂等去重 + MAX 语义 ----------

def test_upsert_hit_dedup_keeps_max_score(mem_conn):
    """同键重复只更分不增行：先 0.90 后 0.86 仍 0.90（MAX），更高分则抬升。"""
    content = "用户偏好深色主题界面"
    near_dup_dao.upsert_hit(mem_conn, content, "mem-1", 0.90)
    near_dup_dao.upsert_hit(mem_conn, content, "mem-1", 0.86)
    items = near_dup_dao.list_items(mem_conn)
    assert len(items) == 1
    assert items[0]["score"] == pytest.approx(0.90)
    assert items[0]["status"] == "pending"
    # 秒精度时间戳（与 refine_dao._now_iso 口径一致）
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", items[0]["created_at"])
    # 更高分抬升
    near_dup_dao.upsert_hit(mem_conn, content, "mem-1", 0.95)
    items = near_dup_dao.list_items(mem_conn)
    assert len(items) == 1
    assert items[0]["score"] == pytest.approx(0.95)
    # 不同 matched_memory_id → 新行（去重键含 matched）
    near_dup_dao.upsert_hit(mem_conn, content, "mem-2", 0.93)
    assert len(near_dup_dao.list_items(mem_conn)) == 2


def test_upsert_hit_dimension_ids_roundtrip(mem_conn):
    """dimension_ids 落库为 JSON（ensure_ascii=False），读取解析为 list；None → []。"""
    near_dup_dao.upsert_hit(
        mem_conn, "含维度内容", "mem-1", 0.93,
        source_ref="file-x:12", dimension_ids=["identity", "偏好"],
    )
    near_dup_dao.upsert_hit(mem_conn, "无维度内容", "mem-1", 0.91)
    items = near_dup_dao.list_items(mem_conn)
    by_content = {it["content"]: it for it in items}
    assert by_content["含维度内容"]["dimension_ids"] == ["identity", "偏好"]
    assert by_content["含维度内容"]["source_ref"] == "file-x:12"
    assert by_content["无维度内容"]["dimension_ids"] == []
    # list 过滤 + 分页
    assert len(near_dup_dao.list_items(mem_conn, status="pending")) == 2
    assert near_dup_dao.list_items(mem_conn, status="restored") == []
    assert len(near_dup_dao.list_items(mem_conn, limit=1, offset=0)) == 1


# ---------- mark_discarded：状态流转 ----------

def test_mark_discarded_only_pending(mem_conn):
    near_dup_dao.upsert_hit(mem_conn, "待丢弃内容", "mem-1", 0.93)
    item = near_dup_dao.list_items(mem_conn)[0]
    assert item["status"] == "pending"
    assert near_dup_dao.mark_discarded(mem_conn, item["id"]) is True
    assert near_dup_dao.get_item(mem_conn, item["id"])["status"] == "discarded"
    # 非 pending（已丢弃）→ False
    assert near_dup_dao.mark_discarded(mem_conn, item["id"]) is False
    # 不存在 → False
    assert near_dup_dao.mark_discarded(mem_conn, 99999) is False


# ---------- restore：全字段补录 + 向量即时回填 ----------

def test_restore_full_backfill(mem_conn, cfg, monkeypatch):
    """restore 集成：memories 全字段 + tags/sources/content_seg + 向量落库 + 隔离行回写。"""
    near_dup_dao.upsert_hit(
        mem_conn, content="用户偏好浅色主题界面", matched_memory_id="mem-old",
        score=0.93, source_ref="file-x:12", dimension_ids=["identity"],
    )
    item = near_dup_dao.list_items(mem_conn)[0]

    # mock embed 返回假向量（零网络），让真实 upsert_memory_vector 走完全链路落库
    monkeypatch.setattr(vector_mod, "embed", lambda text, cfg_, client=None: [0.1] * 8)

    new_id = near_dup_dao.restore(mem_conn, item["id"], "复核判定为新事实", cfg)
    assert isinstance(new_id, str) and new_id

    # memories 可读 + 全字段
    mem = memory_dao.get_memory(mem_conn, new_id)
    assert mem is not None
    assert mem["content"] == "用户偏好浅色主题界面"
    assert mem["memory_type"] == "persona"
    assert mem["priority"] == 50
    assert mem["time_velocity"] == "static"
    assert mem["status"] == "active"
    assert mem["tags"] == ["identity"]
    assert mem["sources"] == [{"source_ref": "file-x:12", "source_type": "session"}]
    assert mem["content_seg"]  # insert_memory 同事务写分词列
    # TTL 按维度默认回填（与 ttl_policy 同源同参）
    from sgme.engine.ttl_policy import backfill_ttl
    assert mem["ttl_days"] == backfill_ttl(
        None, ["identity"], cfg["dimensions"], memory_type="persona"
    )
    # 向量落库（dims=假向量长度）
    vrow = memory_dao.get_vector(mem_conn, new_id)
    assert vrow is not None
    assert vrow["dims"] == 8
    # 隔离行回写
    after = near_dup_dao.get_item(mem_conn, item["id"])
    assert after["status"] == "restored"
    assert after["restored_memory_id"] == new_id
    assert after["restore_reason"] == "复核判定为新事实"
    # 幂等预检可命中（同源 + 同内容 → 新记忆 id）
    assert memory_dao.find_active_by_source_ref_content(
        mem_conn, "file-x:12", "用户偏好浅色主题界面"
    ) == new_id
    # 重复 restore → ValueError（已补录）
    with pytest.raises(ValueError, match="已补录"):
        near_dup_dao.restore(mem_conn, item["id"], "再来一次", cfg)
    # id 不存在 → ValueError
    with pytest.raises(ValueError, match="不存在"):
        near_dup_dao.restore(mem_conn, 4242, "x", cfg)


def test_restore_without_source_ref_and_embed_unreachable(mem_conn, cfg, monkeypatch):
    """source_ref 为 None → 不写 memory_sources；embed 不可达（返回 None）不阻塞补录。"""
    near_dup_dao.upsert_hit(mem_conn, "无源隔离内容", "mem-1", 0.92, dimension_ids=["identity"])
    item = near_dup_dao.list_items(mem_conn)[0]
    monkeypatch.setattr(vector_mod, "embed", lambda text, cfg_, client=None: None)

    new_id = near_dup_dao.restore(mem_conn, item["id"], "无源补录", cfg)
    mem = memory_dao.get_memory(mem_conn, new_id)
    assert mem is not None
    assert mem["sources"] == []
    assert memory_dao.get_vector(mem_conn, new_id) is None  # 回填失败不阻塞
    assert near_dup_dao.get_item(mem_conn, item["id"])["status"] == "restored"


# ---------- 裸连接兜底 ----------

def test_bare_connection_fallback(tmp_path):
    """裸 sqlite3 连接（未开 row_factory）调 list_items / get_item 不崩。"""
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        conn.executescript(db_mod.MEMORY_DDL)
        near_dup_dao.upsert_hit(
            conn, "裸连接内容", "mem-9", 0.91, dimension_ids=["identity"],
        )
        items = near_dup_dao.list_items(conn)
        assert len(items) == 1
        assert items[0]["matched_memory_id"] == "mem-9"
        assert items[0]["dimension_ids"] == ["identity"]
        got = near_dup_dao.get_item(conn, items[0]["id"])
        assert got is not None and got["content"] == "裸连接内容"
        assert near_dup_dao.get_item(conn, 99999) is None
    finally:
        conn.close()
