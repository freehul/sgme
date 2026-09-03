"""T-136 测试：原子事实三元组（搭 l1_extraction 顺风车，D4 JSON 列 MVP）。

覆盖：
- insert_memory facts 落库（规范化 / 非法项丢弃 / 缺省 None）
- facts_dao 符号层查询（精确 / 子串 / 组合 / status 过滤 / 对账）
- l1 _validate_item facts 解析容错
- l1 → l15 全链路：extract_l1(mock) 带 facts → resolve_conflicts store → 查询命中
- _migrate_mem_facts_json 幂等
"""

from __future__ import annotations

import json

import httpx
import pytest

from sgme import config
from sgme.engine import l1, l15
from sgme.data import db as db_mod, facts_dao, memory_dao


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _insert(mem_conn, content, dim_ids, **kw):
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type=kw.get("memory_type", "persona"),
        priority=kw.get("priority", 60),
        time_velocity=kw.get("time_velocity", "static"),
        ttl_days=kw.get("ttl_days"),
        dimension_ids=dim_ids,
        facts=kw.get("facts"),
    )


def _mock_llm_client(response_body: str) -> httpx.Client:
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- 落库 ----------

def test_insert_memory_facts_json(mem_conn):
    mid = _insert(mem_conn, "张伟在腾讯工作", ["goals"], facts=[
        {"subject": "张伟", "predicate": "任职于", "object": "腾讯"},
    ])
    raw = facts_dao.get_memory_facts_json(mem_conn, mid)
    assert json.loads(raw) == [{"subject": "张伟", "predicate": "任职于", "object": "腾讯"}]


def test_insert_memory_facts_normalize_drops_bad(mem_conn):
    """非法三元组（缺键/空值/非 dict）丢弃；全部非法 → facts_json NULL。"""
    mid = _insert(mem_conn, "内容", ["goals"], facts=[
        {"subject": "A", "predicate": "P", "object": "O"},     # 合法
        {"subject": "", "predicate": "P", "object": "O"},       # 空 subject → 丢
        {"subject": "B"},                                       # 缺 predicate/object → 丢
        "not-a-dict",                                           # → 丢
    ])
    raw = facts_dao.get_memory_facts_json(mem_conn, mid)
    assert json.loads(raw) == [{"subject": "A", "predicate": "P", "object": "O"}]

    mid2 = _insert(mem_conn, "内容2", ["goals"], facts=[{"subject": "x"}])
    assert facts_dao.get_memory_facts_json(mem_conn, mid2) is None

    mid3 = _insert(mem_conn, "内容3", ["goals"], facts=None)
    assert facts_dao.get_memory_facts_json(mem_conn, mid3) is None


# ---------- 符号层查询 ----------

def test_query_facts_exact(mem_conn):
    _insert(mem_conn, "张伟在腾讯工作", ["goals"],
            facts=[{"subject": "张伟", "predicate": "任职于", "object": "腾讯"}])
    _insert(mem_conn, "李雷在阿里工作", ["goals"],
            facts=[{"subject": "李雷", "predicate": "任职于", "object": "阿里巴巴"}])
    hits = facts_dao.query_facts(mem_conn, subject="张伟")
    assert len(hits) == 1
    assert hits[0]["object"] == "腾讯"
    assert hits[0]["content"] == "张伟在腾讯工作"

    hits = facts_dao.query_facts(mem_conn, predicate="任职于", object="腾讯")
    assert len(hits) == 1 and hits[0]["subject"] == "张伟"

    hits = facts_dao.query_facts(mem_conn, subject="不存在")
    assert hits == []


def test_query_facts_substring(mem_conn):
    _insert(mem_conn, "SGME 部署在群晖 NAS", ["environment"],
            facts=[{"subject": "SGME", "predicate": "部署于", "object": "群晖 NAS"}])
    hits = facts_dao.query_facts(mem_conn, subject="SGME", exact=False)
    assert len(hits) == 1
    hits = facts_dao.query_facts(mem_conn, object="群晖", exact=False)
    assert len(hits) == 1
    # 精确模式下子串不命中
    assert facts_dao.query_facts(mem_conn, object="群晖") == []


def test_query_facts_active_filter(mem_conn):
    mid = _insert(mem_conn, "被拒绝事实", ["goals"],
                  facts=[{"subject": "A", "predicate": "P", "object": "O"}])
    hits = facts_dao.query_facts(mem_conn, subject="A")
    assert len(hits) == 1
    memory_dao.reject_memory(mem_conn, mid, "测试 reject")
    assert facts_dao.query_facts(mem_conn, subject="A") == []           # 默认只查 active
    hits = facts_dao.query_facts(mem_conn, subject="A", only_active=False)
    assert len(hits) == 1


def test_count_facts_and_list(mem_conn):
    m1 = _insert(mem_conn, "m1", ["goals"], facts=[{"subject": "A", "predicate": "P", "object": "O"}])
    _insert(mem_conn, "m2", ["goals"], facts=[
        {"subject": "B", "predicate": "P", "object": "O1"},
        {"subject": "B", "predicate": "P", "object": "O2"},
    ])
    _insert(mem_conn, "m3", ["goals"], facts=None)
    stats = facts_dao.count_facts(mem_conn)
    assert stats["facts_total"] == 3
    assert stats["memories_with_facts"] == 2
    assert len(facts_dao.list_facts_by_memory(mem_conn, m1)) == 1
    assert facts_dao.list_facts_by_memory(mem_conn, "no-such") == []


# ---------- L1 解析容错 ----------

def test_validate_item_facts_parse(mem_conn, cfg):
    dims = cfg["dimensions"]
    item = {
        "content": "张伟在腾讯工作",
        "dimensions": ["goals"],
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "static",
        "source_message_ids": [0],
        "facts": [
            {"subject": "张伟", "predicate": "任职于", "object": "腾讯"},
            {"subject": "", "predicate": "x", "object": "y"},   # 非法 → 丢
            "bad",                                              # → 丢
        ],
    }
    v = l1._validate_item(item, dims)
    assert v["facts"] == [{"subject": "张伟", "predicate": "任职于", "object": "腾讯"}]

    item2 = dict(item, facts="not-a-list")
    assert l1._validate_item(item2, dims)["facts"] == []


# ---------- l1 → l15 全链路 ----------

def test_full_pipeline_facts_stored_and_queryable(monkeypatch, mem_conn, cfg):
    """extract_l1(mock 带 facts) → resolve_conflicts store → facts_json 落库 → 符号层命中。"""
    monkeypatch.setattr(l1.llm_chain, "batch_budget", lambda *a, **k: 10000)
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)

    l1_body = json.dumps([{
        "content": "张伟在腾讯工作，负责 AI 平台",
        "dimensions": ["goals"],
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "static",
        "source_message_ids": [0],
        "facts": [
            {"subject": "张伟", "predicate": "任职于", "object": "腾讯"},
            {"subject": "张伟", "predicate": "负责", "object": "AI 平台"},
        ],
    }])
    memories, _, _ = l1.extract_l1(
        "# 2026-08-31T00:00:00Z user\n张伟在腾讯工作",
        cfg["dimensions"], cfg["llm"], client=_mock_llm_client(l1_body),
    )
    assert memories[0]["facts"]  # L1 解析出 facts

    l15_body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [], "action": "store", "reason": "r",
    }])
    result = l15.resolve_conflicts(
        memories, mem_conn, cfg, client=_mock_llm_client(l15_body),
    )
    assert len(result.stored) == 1
    mid = result.stored[0]

    # 落库对账
    assert facts_dao.count_facts(mem_conn) == {"facts_total": 2, "memories_with_facts": 1}
    # 符号层查询命中（精确）
    hits = facts_dao.query_facts(mem_conn, subject="张伟", predicate="任职于")
    assert len(hits) == 1
    assert hits[0]["object"] == "腾讯"
    assert hits[0]["memory_id"] == mid
    # 与基线对比：自然语言 content LIKE 也能命中，但 facts 是确定性命中
    assert len(facts_dao.query_facts(mem_conn, object="AI 平台")) == 1


# ---------- 迁移幂等 ----------

def test_migrate_facts_json_idempotent(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    db_mod._migrate_mem_facts_json(conn)
    db_mod._migrate_mem_facts_json(conn)   # 第二次无副作用
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    assert "facts_json" in cols
    conn.close()


# ---------- 全链回归：refine_file 归一化不得丢 facts（2026-09-03 生产实证修复） ----------

@pytest.fixture
def session_conn(tmp_path):
    from sgme.data import db as _db
    conn = _db.connect_session(tmp_path)
    yield conn
    conn.close()


def test_facts_survive_refine_file_normalization(monkeypatch, mem_conn, session_conn, cfg, tmp_path):
    """回归（B147）：refine.py 归一化重塑 dict 的字段白名单此前漏 facts →
    L1 产出的三元组在进入 l15 落库前被静默丢弃，生产 facts_json 全 NULL
    （冒烟实证 0 条；test_full_pipeline_facts_stored_and_queryable 因绕过
    refine_file 归一化层而未拦住）。修复后全链 facts 必须存活。"""
    from sgme import config as sgme_config
    from sgme.engine import refine as refine_mod
    from sgme.data import session_dao
    from sgme.raw import store as raw_store

    # 隔离 raw/ 目录（仿 test_e2e_v04）
    rd = tmp_path / "raw"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)

    fid = "f-facts-norm-regression"
    msgs = [{"timestamp": "2026-09-03T02:00:00Z", "role": "user",
             "content": "王五住在上海市浦东新区，在字节跳动做后端工程师"}]
    raw_store.write_new_file(
        file_id=fid, session_key="sess_facts_norm",
        started_at="2026-09-03T02:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess_facts_norm", started_at="2026-09-03T02:00:00Z",
        agent_id="test", status="new", size=raw_store.file_size(fid),
    )

    # mock L1：返回带 facts 的 raw 记忆（facts 是本测试的断言焦点）
    raw_with_facts = [{
        "content": "王五住在上海市浦东新区，在字节跳动做后端工程师",
        "dimensions": ["goals"],
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "static",
        "source_message_ids": [1],
        "facts": [{"subject": "王五", "predicate": "任职于", "object": "字节跳动"}],
    }]
    monkeypatch.setattr(
        refine_mod.l1, "extract_l1",
        lambda *a, **k: (raw_with_facts, "mock",
                         {"stage": "l1_extraction", "version": "working-TEST"}),
    )

    # Act：refine_file 全链（含归一化白名单重塑）
    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)

    assert result.memories, "归一化后应至少剩 1 条记忆"
    assert result.memories[0].get("facts"), "归一化白名单必须透传 facts"

    # 继续走 l15 store → 落库 → 符号层可查
    l15_body = json.dumps([{
        "new_memory_index": 0, "candidate_ids": [], "action": "store", "reason": "r",
    }])
    monkeypatch.setattr(l15.llm_chain, "batch_budget", lambda *a, **k: 10000)
    res = l15.resolve_conflicts(
        result.memories, mem_conn, cfg, client=_mock_llm_client(l15_body),
    )
    assert len(res.stored) == 1
    assert facts_dao.count_facts(mem_conn) == {"facts_total": 1, "memories_with_facts": 1}
    hits = facts_dao.query_facts(mem_conn, subject="王五", predicate="任职于")
    assert len(hits) == 1 and hits[0]["object"] == "字节跳动"
