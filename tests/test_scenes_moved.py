"""scenes 已迁移至 memory.db 测试（v0.7 阶段 1）。

验证：
1. scenes / scene_memories / scene_versions 物理位于 memory.db，
   wiki.db 不再含这些表（B4 拆分落地）。
2. scene_dao 对 memory.db 的 CRUD 正确（insert / get / update / list / link / versions）。
3. init_scenes_fts(mem) 仍正确重建 FTS（形参名 wiki_conn 实际传 mem 连接），
   search_scenes(mem_conn, ...) 命中。
"""

import pytest

from sgme.data.search import init_scenes_fts, search_scenes
from sgme.data import db as db_mod
from sgme.data import scene_dao


@pytest.fixture
def mem(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    yield conn
    conn.close()


def test_scenes_live_in_memory_db_not_wiki(tmp_path):
    """scenes 系列位于 memory.db；wiki.db 不含 scenes / scene_memories / scene_versions。"""
    mem_conn = db_mod.connect_memory(tmp_path)
    mem_tables = set(db_mod.list_tables(mem_conn))
    mem_conn.close()

    wiki_conn = db_mod.connect_wiki(tmp_path)
    wiki_tables = set(db_mod.list_tables(wiki_conn))
    wiki_conn.close()

    for t in ("scenes", "scene_memories", "scene_versions"):
        assert t in mem_tables, f"{t} 应在 memory.db"
        assert t not in wiki_tables, f"{t} 不应在 wiki.db"


def test_scene_dao_crud(mem):
    sid = scene_dao.insert_scene(mem, "s1", "VPS 部署", "xray VLESS Reality 配置端口 8443")
    assert sid == "s1"
    row = scene_dao.get_scene(mem, "s1")
    assert row["title"] == "VPS 部署"
    assert row["heat"] == 1
    assert row["status"] == "active"
    assert row["content_seg"]  # content_seg 分词列已填

    # 更新内容 + heat 自增
    ok = scene_dao.update_scene_content(mem, "s1", "新内容", heat_increment=2)
    assert ok
    row = scene_dao.get_scene(mem, "s1")
    assert row["content"] == "新内容"
    assert row["heat"] == 3

    # 状态软删除
    assert scene_dao.update_scene_status(mem, "s1", "archived")
    assert scene_dao.get_scene(mem, "s1")["status"] == "archived"


def test_scene_list_and_count(mem):
    scene_dao.insert_scene(mem, "s1", "场景一", "内容一")
    scene_dao.insert_scene(mem, "s2", "场景二", "内容二")
    scene_dao.update_scene_status(mem, "s2", "archived")

    active = scene_dao.list_active_scenes(mem)
    assert {r["scene_id"] for r in active} == {"s1"}
    assert scene_dao.count_scenes(mem, "active") == 1
    assert scene_dao.count_scenes(mem, "archived") == 1
    assert scene_dao.list_scenes_over_threshold(mem, threshold=10) == 1


def test_scene_memory_links_and_versions(mem):
    scene_dao.insert_scene(mem, "s1", "场景一", "内容一")
    scene_dao.add_memory_link(mem, "s1", "m1")
    scene_dao.add_memory_link(mem, "s1", "m1")  # 幂等：重复不报错
    scene_dao.add_memory_link(mem, "s1", "m2")
    linked = scene_dao.list_memories_for_scene(mem, "s1")
    assert linked == ["m1", "m2"]

    scene_dao.insert_scene_version(mem, "v1", "s1", "历史内容", reason="update")
    versions = scene_dao.list_scene_versions(mem, "s1")
    assert len(versions) == 1
    assert versions[0]["reason"] == "update"


def test_scenes_fts_rebuild_and_search(mem):
    """init_scenes_fts(mem) 在 memory.db 重建 FTS，search_scenes 命中。"""
    init_scenes_fts(mem)  # 形参名 wiki_conn，实际传 mem 连接（scenes 已居 memory.db）
    scene_dao.insert_scene(mem, "s1", "VPS 的部署", "在 VPS 上部署 xray VLESS Reality 端口 8443")
    scene_dao.insert_scene(mem, "s2", "抖音运营", "抖音号 610021917 AI 蒸馏内容")
    results = search_scenes(mem, "VPS部署", limit=5)
    ids = [r["scene_id"] for r in results]
    assert "s1" in ids


def test_scenes_fts_idempotent(mem):
    init_scenes_fts(mem)
    init_scenes_fts(mem)
    scene_dao.insert_scene(mem, "s1", "VPS 部署", "xray 配置")
    results = search_scenes(mem, "xray", limit=5)
    assert any(r["scene_id"] == "s1" for r in results)
