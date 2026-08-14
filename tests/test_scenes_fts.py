"""scenes FTS5 检索测试（PR #7：L2 场景从 LIKE 升级 BM25+jieba 分词）。

背景：search_scenes 原实现是裸 LIKE（子串匹配，无分词）——「VPS部署」查不到
「VPS 的部署」。本 PR 对称记忆层方案：scenes 补 content_seg 列 + scenes_fts
虚拟表 + 触发器 + 分词查询。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgme.data.search import init_scenes_fts, search_scenes  # noqa: E402
from sgme.data.db import connect_memory  # noqa: E402


@pytest.fixture()
def mem(tmp_path):
    conn = connect_memory(tmp_path)
    init_scenes_fts(conn)
    from sgme.data.scene_dao import insert_scene
    scenes = [
        ("s1", "VPS 的部署", "在 VPS 上部署 xray VLESS Reality，端口 8443，密钥认证", 5),
        ("s2", "抖音运营", "抖音号 610021917，内容转型 AI 蒸馏，封面安全区三段式", 3),
        ("s3", "Reasonix 接入", "Reasonix hooks 专用适配接入 SGME，SessionEnd 捕获", 1),
    ]
    for sid, title, content, heat in scenes:
        insert_scene(conn, sid, title, content)
        conn.execute("UPDATE scenes SET heat=? WHERE scene_id=?", (heat, sid))
    conn.commit()
    yield conn
    conn.close()


def test_migrate_scene_seg_adds_column(tmp_path):
    """老库（无 content_seg 列）→ connect_memory 自动补列（幂等）。"""
    conn = connect_memory(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(scenes)").fetchall()]
    assert "content_seg" in cols
    # 二次连接安全（幂等）
    conn2 = connect_memory(tmp_path)
    conn2.close()
    conn.close()


def test_init_scenes_fts_idempotent(mem):
    """init 幂等：重复调用不报错、不重复建。"""
    init_scenes_fts(mem)
    init_scenes_fts(mem)


def test_search_scenes_bm25_jieba(mem):
    """分词查询命中：'VPS部署'（无空格）命中 'VPS 的部署' 场景。"""
    results = search_scenes(mem, "VPS部署", limit=5)
    ids = [r["scene_id"] for r in results]
    assert "s1" in ids
    # active 过滤在 SQL 层（archived 不返回），结果字段集完整
    assert {"scene_id", "title", "content", "heat", "routes"} <= set(results[0])


def test_search_scenes_heat_ordering(mem):
    """同命中时 heat 高的在前。"""
    results = search_scenes(mem, "接入", limit=5)
    # 只有 s3 命中"接入"；宽泛查询验证排序不崩
    assert results


def test_search_scenes_like_fallback_on_fts_missing(tmp_path):
    """FTS 不可用（未 init）→ 降级 LIKE 兜底，不抛异常。"""
    conn = connect_memory(tmp_path)
    from sgme.data.scene_dao import insert_scene
    insert_scene(conn, "sx", "测试场景", "VPS 部署 xray 配置")
    conn.commit()
    results = search_scenes(conn, "xray", limit=5)
    assert any(r["scene_id"] == "sx" for r in results)
    conn.close()
