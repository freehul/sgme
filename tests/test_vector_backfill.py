"""E1 测试：记忆落库后的同步向量回填（2026-11-05，B314 待办 1）。

背景：_store_memory 只写 memories 表，向量靠 finalize_refinement 批末统一回填，
于是同一批里先落的记忆对后续 near_dup 判定不可见（检出滞后一个 finalize 周期）。
E1 在落库成功后同步 embed + upsert 向量。本文件验收：
① 落库后向量表立即可查、vector_search 命中自己（同批可见的前提）；
② embed 抛异常 / 返回 None 时 fail-open——记忆仍在、只 WARNING、不阻塞落库；
③ 同一 memory_id 重放 upsert 幂等（单行 + 覆盖自己、不串号）；
④ 批量落库路径每条记忆只回填一次（不重复 embed）+ 同批先落记忆立即可被
   后一条的 near_dup 检出（E1 验收口径）；
⑤ update 动作路径（_update_memory）新行同样回填、归档链不变；
⑥ 向量检索未启用（search.vector.enabled=false）时零 embed 调用；
⑦ 旧调用签名（不传 cfg）不触网、不写向量。

全部用例零网络：embed 一律 mock，vector_search 走真实 DB（numpy/sqlite-vec 本地实现）。
"""

from __future__ import annotations

import logging

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
from sgme.data.search import vector as vector_mod
from sgme.engine import l3_adjudicate


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    """启用向量检索的配置（回填开关读 search.vector.enabled）。"""
    c = config.load_config()
    c["search"] = dict(c.get("search") or {})
    c["search"]["vector"] = {
        **((c.get("search") or {}).get("vector") or {}),
        "enabled": True,
    }
    return c


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


# ---------- 工具 ----------

def _new_mem(content: str) -> dict:
    return {
        "content": content,
        "dimension_ids": ["goals"],
        "memory_type": "persona",
        "priority": 50,
        "time_velocity": "static",
    }


def _mock_embed(monkeypatch, vec=(0.6, 0.8, 0.0)) -> list[str]:
    """mock embed：记录每次调用文本、返回固定向量（零网络）。"""
    calls: list[str] = []

    def _embed(text, cfg, client=None):
        calls.append(text)
        return list(vec)

    monkeypatch.setattr(vector_mod, "embed", _embed)
    return calls


def _vector_rows(mem_conn) -> int:
    return mem_conn.execute("SELECT COUNT(*) AS c FROM memory_vectors").fetchone()["c"]


# ---------- ① 落库后向量立即可查 ----------

def test_store_backfills_vector_immediately(mem_conn, cfg, monkeypatch, caplog):
    """store 落库成功后同步 embed + upsert：向量行存在、检索命中自己、单条耗时落日志。"""
    caplog.set_level(logging.DEBUG, logger="sgme.engine.l3_adjudicate")
    calls = _mock_embed(monkeypatch)
    content = "E1 落库后立即可查"

    mid = l3_adjudicate._store_memory(
        mem_conn, _new_mem(content), cfg["dimensions"], cfg=cfg,
    )

    # 记忆本体 + 向量行都在（dims 跟随 mock 向量长度）
    assert memory_dao.get_memory(mem_conn, mid) is not None
    vec_row = memory_dao.get_vector(mem_conn, mid)
    assert vec_row is not None
    assert vec_row["dims"] == 3
    assert vec_row["model"] == cfg["search"]["vector"]["model"]
    # near_dup 用的检索链路立刻能命中本条（同批可见的前提）
    rows = vector_mod.vector_search(mem_conn, [0.6, 0.8, 0.0], limit=5)
    assert [r["memory_id"] for r in rows] == [mid]
    assert rows[0]["score"] > 0.99
    # 补数脚本的扫描口径：本条不再算「active 且无向量」
    missing = [r["memory_id"] for r in memory_dao.list_memories_without_vector(mem_conn)]
    assert mid not in missing
    # 每条只 embed 一次 + 单条耗时证据落日志
    assert calls == [content]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("向量回填完成" in m and "耗时=" in m for m in msgs), msgs


# ---------- ② fail-open：embed 异常/None 不阻塞落库 ----------

def test_embed_exception_does_not_block_store(mem_conn, cfg, monkeypatch, caplog):
    """embed 抛异常 → 记忆照常落库、无向量行、只 WARNING（不抛出）。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l3_adjudicate")

    def boom(text, cfg, client=None):
        raise RuntimeError("embed 端点挂了")

    monkeypatch.setattr(vector_mod, "embed", boom)
    content = "embed 异常也要落库"

    mid = l3_adjudicate._store_memory(
        mem_conn, _new_mem(content), cfg["dimensions"], cfg=cfg,
    )

    assert memory_dao.get_memory(mem_conn, mid) is not None
    assert memory_dao.get_vector(mem_conn, mid) is None
    assert _vector_rows(mem_conn) == 0
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("向量回填异常" in m for m in warns), warns


def test_embed_none_does_not_block_store(mem_conn, cfg, monkeypatch, caplog):
    """embed 不可达返回 None → 记忆照常落库 + WARNING 指向补数脚本（fail-open）。"""
    caplog.set_level(logging.WARNING, logger="sgme.engine.l3_adjudicate")
    monkeypatch.setattr(vector_mod, "embed", lambda text, cfg, client=None: None)

    mid = l3_adjudicate._store_memory(
        mem_conn, _new_mem("embed 不可达也要落库"), cfg["dimensions"], cfg=cfg,
    )

    assert memory_dao.get_memory(mem_conn, mid) is not None
    assert memory_dao.get_vector(mem_conn, mid) is None
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("向量回填失败" in m and "backfill_vectors.py" in m for m in warns), warns


# ---------- ③ 同一 memory_id 重放 upsert 幂等 ----------

def test_backfill_replay_is_idempotent(mem_conn, cfg, monkeypatch):
    """同一 memory_id 重放 upsert（如 finalize 批量回填重跑）→ 仍单行、覆盖自己、不串号。"""
    _mock_embed(monkeypatch, vec=(0.6, 0.8, 0.0))
    content = "幂等重放的内容"
    mid = l3_adjudicate._store_memory(
        mem_conn, _new_mem(content), cfg["dimensions"], cfg=cfg,
    )
    assert _vector_rows(mem_conn) == 1

    # 模拟另一条路径（finalize_refinement 批量回填）以不同向量重放同一 memory_id
    monkeypatch.setattr(vector_mod, "embed", lambda text, cfg, client=None: [1.0, 0.0, 0.0])
    assert l3_adjudicate._backfill_memory_vector(mem_conn, mid, content, cfg) is True
    assert l3_adjudicate._backfill_memory_vector(mem_conn, mid, content, cfg) is True

    # 单行（不重复），内容为最后一次回填值，且没有写到别的 memory_id
    assert _vector_rows(mem_conn) == 1
    row = memory_dao.get_vector(mem_conn, mid)
    assert row is not None
    assert vector_mod._deserialize_vector(row["embedding"]) == pytest.approx([1.0, 0.0, 0.0])
    assert memory_dao.get_vector(mem_conn, "no-such-memory-id") is None


# ---------- ④ 批量落库路径：每条只 embed 一次 ----------

def test_batch_path_embeds_once_per_memory(mem_conn, cfg, monkeypatch):
    """全空短路批量落库：N 条记忆 → N 次回填 embed（不重复），向量行 N 条。"""
    # near_dup 关：隔离回填口径（near_dup 判定自带的 embed 属另一条链，不计入回填）
    cfg["l3_adjudicate"] = {"prescreen": None, "near_dup": {"enabled": False}}
    calls = _mock_embed(monkeypatch)
    mems = [_new_mem("批量落库记忆一"), _new_mem("批量落库记忆二"), _new_mem("批量落库记忆三")]

    res = l3_adjudicate.resolve_conflicts(mems, mem_conn, cfg)

    assert len(res.stored) == 3
    assert calls == ["批量落库记忆一", "批量落库记忆二", "批量落库记忆三"]
    assert _vector_rows(mem_conn) == 3
    assert memory_dao.list_memories_without_vector(mem_conn) == []


def test_same_batch_store_visible_to_near_dup(mem_conn, cfg, monkeypatch):
    """E1 验收（同批可见）：批内先落的记忆立刻能被后一条的 near_dup 检出并隔离。

    与旧行为对照：E1 之前批内落库不写向量 → 后一条的向量检索看不到前一条，
    两条重复内容会一起入库。这里 mock embed 返回同一向量模拟「近重复内容」。
    """
    # 候选池为空（无既有同维度记忆）→ 走全空短路批量落库路径；near_dup 用出厂阈值
    cfg["l3_adjudicate"] = {
        "prescreen": None,
        "near_dup": {"enabled": True, "threshold": 0.92, "gray_zone": 0.85},
    }
    _mock_embed(monkeypatch)  # 所有内容 → 同一向量（相似度 1.0）
    first, second = "批内重复内容 A", "批内重复内容 A（换个说法）"

    res = l3_adjudicate.resolve_conflicts(
        [_new_mem(first), _new_mem(second)], mem_conn, cfg, source_ref="F:E1",
    )

    # 第一条正常落库并回填向量；第二条被 near_dup 检出 → 隔离、不入库
    assert len(res.stored) == 1
    assert res.skipped == [1]
    assert res.near_dup_quarantined == 1
    assert memory_dao.find_active_by_source_ref_content(mem_conn, "F:E1", second) is None
    rows = mem_conn.execute(
        "SELECT matched_memory_id FROM near_dup_quarantine"
    ).fetchall()
    assert [r["matched_memory_id"] for r in rows] == [res.stored[0]]


# ---------- ④b 短超时客户端：端点挂死快速失败 ----------

def test_backfill_uses_short_timeout_client(mem_conn, cfg, monkeypatch):
    """回填走专用短超时客户端（端点挂死时快速失败，不拖垮提炼主链）。"""
    seen: dict = {}
    real_make = l3_adjudicate.llm_provider.make_client

    def fake_make(timeout_s=120.0):
        seen["timeout"] = timeout_s
        return real_make(timeout_s=timeout_s)

    monkeypatch.setattr(l3_adjudicate.llm_provider, "make_client", fake_make)
    _mock_embed(monkeypatch)

    mid = l3_adjudicate._store_memory(
        mem_conn, _new_mem("短超时客户端回填"), cfg["dimensions"], cfg=cfg,
    )

    assert seen["timeout"] == l3_adjudicate.VECTOR_BACKFILL_TIMEOUT_S
    assert memory_dao.get_vector(mem_conn, mid) is not None


# ---------- ④c update 动作路径同样回填 ----------

def test_update_memory_backfills_new_row(mem_conn, cfg, monkeypatch):
    """update 动作（_update_memory → _store_memory）新行同样回填；归档链不变。"""
    _mock_embed(monkeypatch)
    old_id = memory_dao.insert_memory(
        mem_conn, content="旧的待更新内容", memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["goals"],
    )

    new_id = l3_adjudicate._update_memory(
        mem_conn, _new_mem("更新后的新内容"), old_id, cfg["dimensions"], cfg=cfg,
    )

    assert new_id != old_id
    assert memory_dao.get_vector(mem_conn, new_id) is not None
    assert memory_dao.get_vector(mem_conn, old_id) is None  # 旧行不写向量（只归档）
    # 归档链不变：旧 id 进 memory_archive 且 superseded_by 指向新 id
    row = mem_conn.execute(
        "SELECT superseded_by FROM memory_archive WHERE memory_id=?", (old_id,)
    ).fetchone()
    assert row is not None and row["superseded_by"] == new_id


# ---------- ⑤ 向量检索未启用：不回填 ----------

def test_disabled_vector_skips_backfill(mem_conn, cfg, monkeypatch):
    """search.vector.enabled=false → 直接跳过回填（索引不被读，回填零收益）。"""
    cfg["search"]["vector"]["enabled"] = False
    monkeypatch.setattr(
        vector_mod, "embed", lambda *a, **kw: pytest.fail("enabled=false 不应调用 embed")
    )

    mid = l3_adjudicate._store_memory(
        mem_conn, _new_mem("向量关闭时不回填"), cfg["dimensions"], cfg=cfg,
    )

    assert memory_dao.get_memory(mem_conn, mid) is not None
    assert memory_dao.get_vector(mem_conn, mid) is None
    assert _vector_rows(mem_conn) == 0


# ---------- ⑥ 旧调用签名（无 cfg）不触网 ----------

def test_store_without_cfg_does_not_embed(mem_conn, cfg, monkeypatch):
    """向后兼容：不传 cfg 的旧调用不读向量开关、不 embed、不写向量。"""
    monkeypatch.setattr(
        vector_mod, "embed", lambda *a, **kw: pytest.fail("无 cfg 不应调用 embed")
    )

    mid = l3_adjudicate._store_memory(mem_conn, _new_mem("旧签名落库"), cfg["dimensions"])

    assert memory_dao.get_memory(mem_conn, mid) is not None
    assert memory_dao.get_vector(mem_conn, mid) is None

