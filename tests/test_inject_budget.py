"""tests/test_inject_budget.py：注入预算门控（D-49）+ 去重计量（E7，2026-11-05）。

覆盖：
1. 不传 max_tokens → blocks 与 tokens_est 完全不变、无 budget_* 字段（向后兼容）
2. 传 1200 且超限 → tokens_est ≤ 1200、core 块保留且不动、stats 字段如实
3. persona_block 优先整块丢弃（section 一条不裁）
4. section 从最后一个块尾部逐条丢 item（前面的 section 不动）
5. max_tokens 恰好等于估算值 → 不裁剪
6. core 自身超预算（极端）→ 不硬裁 + budget_unmet=True
7. 非正数预算按 0 处理（只保留 core）
8. E7：跨 section 去重条数进 stats.dedupe_dropped + INFO 日志
9. HTTP /v1/inject 透传 max_tokens（不再「收而不用」）
10. MCP inject 工具透传 max_tokens（lead 补充：同属「收而不用」缺陷）
"""
from __future__ import annotations

import asyncio
import json
import logging
import math

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_card_dao, memory_dao
from sgme.operations.errors import OperationResult
from sgme.operations.inject import inject as inject_operation
from sgme.profile import core_cards
from sgme.raw import store as raw_store
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

# ---------- fixtures（照抄 test_operations_inject.py；inject 不调 LLM） ----------


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------


def _insert(
    mem_conn,
    content: str,
    dims: list[str],
    priority: int = 90,
    ttl_days: int | None = None,
    time_velocity: str = "static",
) -> str:
    """插入一条测试记忆（默认 priority=90，高于各模板 priority_min）。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=priority, time_velocity=time_velocity, ttl_days=ttl_days,
        dimension_ids=dims,
    )


def _seed_full_34(mem_conn) -> None:
    """写满 full 模板四个 section（10+8+8+8=34 条 → 估算 1020 token）。"""
    for i in range(10):
        _insert(mem_conn, f"身份记忆{i}", ["identity"])
    for i in range(8):
        # section2 为 match=all（默认），需要两个标签同时命中
        _insert(mem_conn, f"技术记忆{i}", ["skills", "tech_stack"])
    for i in range(8):
        _insert(mem_conn, f"偏好记忆{i}", ["preferences"])
    for i in range(8):
        _insert(mem_conn, f"状态记忆{i}", ["status"], ttl_days=90, time_velocity="dynamic")


def _fill_core_cards(mem_conn) -> None:
    """写满 identity 卡 4 槽（394 字 → 按 core_cards 口径约 280 token）。"""
    for slot in ("称呼", "语言", "城市"):
        memory_card_dao.upsert_slot(mem_conn, "identity", slot, "字" * 118, changed_by="user")
    memory_card_dao.upsert_slot(mem_conn, "identity", "时区", "字" * 40, changed_by="user")


def _item_counts(data: dict) -> dict[str, int]:
    """block 标题 → items 条数（用于断言行数分布）。"""
    return {b.get("title"): len(b.get("items") or []) for b in data["blocks"]}


def _block_projection(data: dict) -> list[dict]:
    """块结构投影（去掉 relative_time 之类随调用时刻浮动的字段）。"""
    return [
        {
            "title": b.get("title"),
            "kind": b.get("kind"),
            "present": b.get("present"),
            "items": [i.get("content") for i in b.get("items") or []],
        }
        for b in data["blocks"]
    ]


def _no_persona(monkeypatch) -> None:
    """关掉性格参考块（本文件多数用例只验证 core/section 两级）。"""
    monkeypatch.setattr("sgme.profile.persona_block.build_persona_block", lambda *a, **k: None)


def _fake_persona(tokens: int = 100, items: int = 4):
    """构造一个「注定被丢」的性格参考块（tokens 由 persona_block 口径给出）。"""
    def _build(_conn, **_kwargs):
        return {
            "block": {
                "title": "性格参考",
                "items": [{"content": f"特质{i}"} for i in range(items)],
                "present": True,
            },
            "tokens": tokens,
        }
    return _build


def _seed_work_8(mem_conn) -> None:
    """work 模板两段写满：5 条 goals（动态）+ 3 条 social（静态）= 240 token。"""
    for i in range(5):
        _insert(mem_conn, f"目标{i}", ["goals"], ttl_days=90, time_velocity="dynamic")
    for i in range(3):
        _insert(mem_conn, f"关系{i}", ["social"])


# ---------- 1. 不传 max_tokens → 行为完全不变 ----------


def test_no_max_tokens_keeps_behavior(conns, cfg):
    """不传 max_tokens：blocks 不变、tokens_est 沿用旧口径、无 budget_* 字段。"""
    mem_conn, _s, _w = conns
    _fill_core_cards(mem_conn)
    _seed_full_34(mem_conn)
    core_tokens = core_cards.build_core_block(mem_conn)["tokens"]

    base = inject_operation(mem_conn, cfg, mode="full")
    assert base.ok is True
    data = base.data
    # 旧口径：section 条数×30 + core tokens（与 D-49 前完全一致）
    assert data["stats"]["tokens_est"] == 34 * 30 + core_tokens
    assert "budget_max_tokens" not in data["stats"]
    assert "budget_trimmed" not in data["stats"]
    assert "persona_dropped" not in data["stats"]
    # E7：去重计量恒定在场（无重复记 0）
    assert data["stats"]["dedupe_dropped"] == 0

    # 显式给一个足够大的预算 → 块内容与不传时逐字段一致（只是多了统计字段）
    generous = inject_operation(mem_conn, cfg, mode="full", max_tokens=10**6)
    assert _block_projection(generous.data) == _block_projection(data)
    assert generous.data["stats"]["tokens_est"] == data["stats"]["tokens_est"]
    assert generous.data["stats"]["budget_trimmed"] is False
    assert generous.data["stats"]["dropped_items"] == 0


# ---------- 2. 传 1200 且超限 → 裁到 ≤1200 且 core 保留 ----------


def test_budget_1200_trims_to_cap_and_keeps_core(conns, cfg, monkeypatch):
    """full 模式 34 条 + core 超 1200 → 从最后一个 section 尾部裁到 ≤1200。"""
    _no_persona(monkeypatch)
    mem_conn, _s, _w = conns
    _fill_core_cards(mem_conn)
    _seed_full_34(mem_conn)
    core_tokens = core_cards.build_core_block(mem_conn)["tokens"]

    base = inject_operation(mem_conn, cfg, mode="full").data
    tokens_before = base["stats"]["tokens_est"]
    assert tokens_before == 34 * 30 + core_tokens > 1200  # 前置：确实超限

    data = inject_operation(mem_conn, cfg, mode="full", max_tokens=1200).data
    stats = data["stats"]
    assert stats["budget_max_tokens"] == 1200
    assert stats["budget_tokens_before"] == tokens_before
    assert stats["budget_trimmed"] is True
    assert stats["budget_unmet"] is False
    assert stats["tokens_est"] <= 1200

    # 裁剪量 = 超出部分按单条 30 token 向上取整（只从尾部丢）
    expected_drop = math.ceil((tokens_before - 1200) / 30)
    assert stats["dropped_items"] == expected_drop

    # core 块保留且排最前，items 一条不少
    head = data["blocks"][0]
    assert head["kind"] == "core_cards"
    assert len(head["items"]) == 1
    assert stats["core_present"] is True
    assert stats["core_tokens"] == core_tokens

    # section 分布：前面的 section 不动，最后一个 section 少了 expected_drop 条
    before = _item_counts(base)
    after = _item_counts(data)
    titles = list(before)
    for title in titles[:-1]:
        assert after[title] == before[title]
    assert after[titles[-1]] == before[titles[-1]] - expected_drop
    # stats.tokens_est 必须与最终 blocks 重算一致（只数模板 section 块）
    section_items = sum(
        len(b.get("items") or []) for b in data["blocks"] if b.get("kind") is None
    )
    assert stats["tokens_est"] == section_items * 30 + core_tokens
    # D-47 同款纪律：裁剪不静默——note 写明裁了什么、去哪里取全文
    assert "注入预算裁剪" in stats["note"]
    assert "/v1/search" in stats["note"]


# ---------- 3. persona_block 优先整块丢弃 ----------


def test_persona_dropped_before_sections(conns, cfg, monkeypatch):
    """persona_block 优先级最低：超限时先整块丢，section 一条都不裁。"""
    mem_conn, _s, _w = conns
    _seed_work_8(mem_conn)
    monkeypatch.setattr(
        "sgme.profile.persona_block.build_persona_block", _fake_persona(tokens=100),
    )

    def _section_item_total(d: dict) -> int:
        return sum(
            len(b.get("items") or []) for b in d["blocks"] if b.get("kind") is None
        )

    base = inject_operation(mem_conn, cfg, mode="work").data
    assert base["stats"]["persona_present"] is True
    assert base["stats"]["tokens_est"] == 8 * 30 + 100  # 240 + 100
    assert _section_item_total(base) == 8

    # 260 预算：只靠丢 persona（100）就够（240 ≤ 260）
    data = inject_operation(mem_conn, cfg, mode="work", max_tokens=260).data
    stats = data["stats"]
    assert stats["persona_dropped"] is True
    assert stats["persona_present"] is False
    assert stats["dropped_items"] == 0
    assert all(b.get("kind") != "persona" for b in data["blocks"])
    assert _section_item_total(data) == 8
    assert stats["tokens_est"] == 240
    assert stats["budget_trimmed"] is True
    assert "persona 块已整块丢弃" in stats["note"]


# ---------- 4. section 从最后一个块尾部丢 item ----------


def test_section_items_dropped_from_tail(conns, cfg, monkeypatch):
    """无 persona 时：从最后一个 section 尾部逐条丢，前面的 section 不动。"""
    _no_persona(monkeypatch)
    mem_conn, _s, _w = conns
    _seed_work_8(mem_conn)

    base = inject_operation(mem_conn, cfg, mode="work").data
    assert base["stats"]["tokens_est"] == 8 * 30  # 240

    data = inject_operation(mem_conn, cfg, mode="work", max_tokens=150).data
    stats = data["stats"]
    assert stats["dropped_items"] == 3
    assert stats["tokens_est"] == 150
    assert stats["budget_trimmed"] is True
    assert stats["persona_dropped"] is False
    # 第一个 section 5 条全留；最后一个 section 被清空且 present=False
    goals, social = data["blocks"][0], data["blocks"][1]
    assert len(goals["items"]) == 5 and goals["present"] is True
    assert social["items"] == [] and social["present"] is False


# ---------- 5. 预算恰好等于估算值 → 不裁剪 ----------


def test_budget_equal_estimate_no_trim(conns, cfg, monkeypatch):
    """max_tokens == tokens_est → budget_trimmed=False，块结构不变。"""
    _no_persona(monkeypatch)
    mem_conn, _s, _w = conns
    _seed_work_8(mem_conn)

    data = inject_operation(mem_conn, cfg, mode="work", max_tokens=240).data
    stats = data["stats"]
    assert stats["budget_trimmed"] is False
    assert stats["dropped_items"] == 0
    assert stats["persona_dropped"] is False
    assert stats["budget_unmet"] is False
    assert stats["tokens_est"] == 240
    assert sum(len(b["items"]) for b in data["blocks"]) == 8


# ---------- 6. core 自身超预算 → 不硬裁 + 诚实标注 ----------


def test_core_never_trimmed_even_when_over_budget(conns, cfg, monkeypatch):
    """预算小于 core 自身 token → core 保留，其余裁光，budget_unmet=True。"""
    _no_persona(monkeypatch)
    mem_conn, _s, _w = conns
    _fill_core_cards(mem_conn)
    _insert(mem_conn, "唯一目标", ["goals"], ttl_days=90, time_velocity="dynamic")
    core_tokens = core_cards.build_core_block(mem_conn)["tokens"]

    data = inject_operation(mem_conn, cfg, mode="work", max_tokens=100).data
    stats = data["stats"]
    assert data["blocks"][0]["kind"] == "core_cards"
    assert len(data["blocks"][0]["items"]) == 1      # core 永不裁
    assert stats["dropped_items"] == 1               # 唯一的 section item 被丢
    assert stats["tokens_est"] == core_tokens
    assert stats["budget_unmet"] is True             # 诚实标注：core 自身就超预算
    assert "budget_unmet" in stats["note"]


# ---------- 7. 非正数预算按 0 处理 ----------


def test_non_positive_budget_keeps_core_only(conns, cfg, monkeypatch):
    """max_tokens=0/负数 → 按 0 预算处理：只保留 core，其余裁光。"""
    _no_persona(monkeypatch)
    mem_conn, _s, _w = conns
    _fill_core_cards(mem_conn)
    _insert(mem_conn, "唯一目标", ["goals"], ttl_days=90, time_velocity="dynamic")
    core_tokens = core_cards.build_core_block(mem_conn)["tokens"]

    for budget in (0, -5):
        stats = inject_operation(
            mem_conn, cfg, mode="work", max_tokens=budget,
        ).data["stats"]
        assert stats["budget_max_tokens"] == 0
        assert stats["dropped_items"] == 1
        assert stats["tokens_est"] == core_tokens
        assert stats["budget_unmet"] is True


# ---------- 8. E7：去重计量进 stats + INFO 日志 ----------


def test_dedupe_dropped_in_stats_and_info_log(conns, cfg, caplog):
    """跨 section 同文去重：丢弃条数进 stats.dedupe_dropped，并打一行 INFO。"""
    mem_conn, _s, _w = conns
    # 同一条内容落在两个不同 section 的维度上（habits=日常节奏，status=当前状态）
    _insert(mem_conn, "跨维度同一条内容", ["habits"])
    _insert(
        mem_conn, "跨维度同一条内容", ["status"],
        ttl_days=90, time_velocity="dynamic",
    )

    with caplog.at_level(logging.INFO, logger="sgme.operations.inject"):
        data = inject_operation(mem_conn, cfg, mode="daily").data

    assert data["stats"]["dedupe_dropped"] == 1
    contents = [i["content"] for b in data["blocks"] for i in b.get("items") or []]
    assert contents.count("跨维度同一条内容") == 1
    assert any(
        "注入去重" in r.getMessage() and "丢弃 1 条" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ---------- 9. HTTP /v1/inject 透传 max_tokens ----------


def test_route_passes_max_tokens(client):
    """HTTP 路由不再「收而不用」：max_tokens 透传并真实生效。"""
    mem_conn = client.app.state.mem_conn
    _insert(mem_conn, "路由预算测试身份", ["identity"])

    # 不传 → 无预算字段（行为完全不变）
    r0 = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT_HEADERS)
    assert r0.status_code == 200, r0.text
    assert r0.json()["stats"]["tokens_est"] == 30
    assert "budget_max_tokens" not in r0.json()["stats"]

    # 传 10（< 单条 30）→ 1 条被裁光
    r1 = client.post(
        "/v1/inject", json={"mode": "daily", "max_tokens": 10}, headers=AGENT_HEADERS,
    )
    assert r1.status_code == 200, r1.text
    stats = r1.json()["stats"]
    assert stats["budget_max_tokens"] == 10
    assert stats["budget_trimmed"] is True
    assert stats["dropped_items"] == 1
    assert stats["tokens_est"] == 0


# ---------- 10. MCP inject 工具透传 max_tokens ----------


def _mcp_call(mcp, name: str, args: dict):
    """同步包装 async call_tool → (text, meta)（与 test_mcp_server.py 同款）。"""
    raw = asyncio.run(mcp.call_tool(name, args))
    results, meta = raw if isinstance(raw, tuple) else (raw, None)
    text = "\n".join(c.text for c in results if getattr(c, "text", None))
    return text, meta


@pytest.fixture
def mcp(conns, cfg, tmp_path):
    """构建绑定隔离 app_state 的 MCP server（inject 工具直调用）。"""
    from sgme.mcp_server import bind_app_state, build_mcp_server
    from sgme.server.app import AgentKeyStore

    mem_conn, session_conn, wiki_conn = conns
    key_store = AgentKeyStore(
        admin_key="test-admin-key", agent_key="test-agent-key",
        store_path=tmp_path / "agent_keys.json",
    )
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
        "key_store": key_store,
    })
    return build_mcp_server()


def test_mcp_inject_passes_max_tokens(mcp, monkeypatch):
    """MCP inject 把 max_tokens 透传给 operations（此前 800 默认收而不用）。

    用 mock 操作层捕获 kwargs：显式传 → 原值透传；不传 → None（不裁剪，
    与 HTTP 缺省同语义），不再假装有 800 预算。
    """
    captured: dict = {}

    def _fake_inject(mem_conn, cfg, **kwargs):
        captured.update(kwargs)
        return OperationResult.succeed({"blocks": [], "stats": {}})

    monkeypatch.setattr("sgme.operations.inject.inject", _fake_inject)

    # ① 显式传 → 原值透传（门控在 operations 层执行）
    text, _meta = _mcp_call(mcp, "inject", {"mode": "daily", "max_tokens": 1200})
    assert json.loads(text) == {"blocks": [], "stats": {}}
    assert captured["mode"] == "daily"
    assert captured["max_tokens"] == 1200

    # ② 不传 → None（缺省不裁剪；旧声明 800 从未生效，故行为与接线前一致）
    captured.clear()
    _mcp_call(mcp, "inject", {"mode": "daily"})
    assert captured["max_tokens"] is None
