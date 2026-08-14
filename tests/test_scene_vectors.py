"""场景向量测试（PR #8：scene_vectors + search_scenes 向量路 + RRF 融合）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgme.data.search import search_scenes  # noqa: E402
from sgme.data.search import vector as vector_mod  # noqa: E402
from sgme.data.search.rrf import rrf_merge  # noqa: E402
from sgme.data.db import connect_memory  # noqa: E402
from sgme.data.scene_dao import insert_scene  # noqa: E402


@pytest.fixture()
def mem(tmp_path):
    conn = connect_memory(tmp_path)
    insert_scene(conn, "s1", "VPS 部署", "xray VLESS Reality 配置，端口 8443")
    insert_scene(conn, "s2", "抖音运营", "抖音号 610021917，AI 蒸馏内容")
    insert_scene(conn, "s3", "Reasonix 接入", "Reasonix hooks 专用适配接入 SGME")
    conn.commit()
    yield conn
    conn.close()


def test_upsert_scene_vector(mem):
    cfg = {"search": {"vector": {"enabled": True, "model": "m", "base_url": "http://x/v1"}}}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    class FakeClient:
        def post(self, url, json=None, headers=None):
            return FakeResp()

        def close(self):
            pass

    ok = vector_mod.upsert_scene_vector(mem, "s1", "VPS 部署内容", cfg, client=FakeClient())
    assert ok
    row = mem.execute("SELECT model, dims FROM scene_vectors WHERE scene_id='s1'").fetchone()
    assert row["dims"] == 3
    # 幂等 upsert：重复写不炸
    assert vector_mod.upsert_scene_vector(mem, "s1", "新内容", cfg, client=FakeClient())


def test_rrf_merge_scene_id():
    bm25 = [{"scene_id": "s1", "content": "a"}, {"scene_id": "s2", "content": "b"}]
    vec = [{"scene_id": "s2", "content": "b"}, {"scene_id": "s3", "content": "c"}]
    merged = rrf_merge(bm25, vec, id_key="scene_id")
    ids = [r["scene_id"] for r in merged]
    assert "s3" in ids  # 纯向量路条目也进入融合
    assert merged[0]["scene_id"] == "s2"  # 双路命中 score 最高
    assert "bm25" in merged[0]["sources"] and "vector" in merged[0]["sources"]


def test_search_scenes_vector_rrf(mem, monkeypatch):
    """mock embed → search_scenes 返回 RRF 融合结果（routes 含 wiki_rrf）。"""
    cfg = {"search": {"vector": {"enabled": True, "model": "m", "base_url": "http://x/v1"},
                      "rrf": {"k": 60}}}

    # 预置向量：s1/s2/s3 用确定性向量（768 维手工构造太繁——用 3 维即可，维度任意）
    def fake_embed(text, cfg, client=None):
        # 相同文本 → 相同向量（s2 与查询最相似）
        table = {
            "xray VLESS Reality 配置，端口 8443": [1.0, 0.0, 0.0],
            "抖音号 610021917，AI 蒸馏内容": [0.0, 1.0, 0.0],
            "Reasonix hooks 专用适配接入 SGME": [0.0, 0.0, 1.0],
        }
        for k, v in table.items():
            if k in text:
                return v
        return [0.5, 0.5, 0.0]

    monkeypatch.setattr(vector_mod, "embed", fake_embed)
    # 用场景 content 预生成向量
    for sid, content in [("s1", "xray VLESS Reality 配置，端口 8443"),
                         ("s2", "抖音号 610021917，AI 蒸馏内容"),
                         ("s3", "Reasonix hooks 专用适配接入 SGME")]:
        assert vector_mod.upsert_scene_vector(mem, sid, content, cfg)

    results = search_scenes(mem, "抖音 蒸馏", limit=5, cfg=cfg)
    assert results
    assert "wiki_rrf" in results[0]["routes"]
    # 向量命中 s2（查询向量 [0.5,0.5,0] 与 s2 [0,1,0] 相似度高）
    assert any(r["scene_id"] == "s2" for r in results)


def test_search_scenes_vector_disabled(mem, monkeypatch):
    """vector.enabled=false → 纯 BM25，不走向量路。"""
    cfg = {"search": {"vector": {"enabled": False}}}
    monkeypatch.setattr(vector_mod, "embed", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 embed")))
    results = search_scenes(mem, "抖音", limit=5, cfg=cfg)
    assert any(r["scene_id"] == "s2" for r in results)
    assert "wiki_rrf" not in results[0]["routes"]
