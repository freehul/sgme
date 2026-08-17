"""tests/test_operations_inject.py：operations 层 inject 操作测试（v0.7 P2-T3）。

覆盖：
1. mode 分支成功（ok=True，blocks 存在，tier0.present/content 正确）
2. mode 分支 Tier0 摘要缺失 → present=False / content=None（静态降级语义保留）
3. custom_filter 分支成功（过滤正确 + stats.mode="custom" + tier0 字段）
4. custom_filter 无 dimensions → ERR_INVALID_ARGS「custom_filter 需指定 dimensions」
5. custom_filter 含未注册维度 id → ERR_INVALID_ARGS
6. mode 模板不存在 / load_template 抛 TemplateError → ERR_INVALID_ARGS「模板加载失败:」
7. pipeline 层意外异常 → ERR_INTERNAL
8. mode 与 custom_filter 均未指定 → ERR_INVALID_ARGS
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.operations.errors import ERR_INTERNAL, ERR_INVALID_ARGS, OperationResult
from sgme.operations.inject import inject as inject_operation
from sgme.profile import template as template_mod
from sgme.profile import tier0 as tier0_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# ---------- fixtures（照抄 test_operations_health.py，inject 不调 LLM） ----------


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
def mock_llm(monkeypatch):
    """mock LLM 探测为可用（inject 不调 LLM，仅为与 health 样板 fixture 一致）。"""


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
def app(conns, cfg, raw_dir, mock_llm, tmp_path, monkeypatch):
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


@pytest.fixture
def summary_path(tmp_path, monkeypatch):
    """隔离 tier0_summary.json 路径到 tmp_path（照 test_server_v04.py 模式）。"""
    p = tmp_path / "tier0_summary.json"
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", p)
    return p


# ---------- 工具 ----------


def _insert(
    mem_conn: sqlite3.Connection,
    content: str,
    dims: list[str],
    priority: int = 90,
    ttl_days: int | None = None,
    time_velocity: str = "static",
) -> str:
    """插入一条测试记忆（静态维度默认 priority DESC 排序）。"""
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="persona",
        priority=priority, time_velocity=time_velocity, ttl_days=ttl_days,
        dimension_ids=dims,
    )


def _block_items(blocks: list[dict]) -> list[str]:
    """汇总所有 block 的 items 内容（用于断言命中集合）。"""
    return [item["content"] for b in blocks for item in b.get("items", [])]


# ---------- 1. mode 分支成功 ----------

def test_inject_mode_ok_with_tier0(conns, cfg, summary_path):
    """mode 分支：ok=True，blocks 存在，tier0.present/content 正确（有摘要）。"""
    # Arrange：写一条 identity+family 记忆（daily 首段 match=all，需双标签）+ 生成 Tier0 摘要
    mem_conn, _session_conn, _ = conns
    _insert(mem_conn, "我是测试用户", dims=["identity", "family"])
    tier0_mod.save_summary("画像摘要：测试用户", path=summary_path)

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert：返回契约 + tier0 字段（契约 4.2）
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    data = res.data
    assert isinstance(data["blocks"], list) and len(data["blocks"]) >= 2
    assert data["stats"]["mode"] == "daily"
    assert data["stats"]["tier0_present"] is True
    assert data["tier0"] == {"present": True, "content": "画像摘要：测试用户"}
    # Tier0 摘要块在最前
    assert data["blocks"][0]["title"] == "画像摘要"
    assert data["blocks"][0]["present"] is True
    # 记忆出现在某 section 块中
    assert "我是测试用户" in _block_items(data["blocks"])


def test_inject_mode_ok_tier0_absent(conns, cfg, summary_path):
    """mode 分支：无 Tier0 摘要 → present=False / content=None（静态降级语义保留）。"""
    # Arrange：不写摘要文件
    mem_conn, _session_conn, _ = conns

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert
    assert res.ok is True
    assert res.data["tier0"] == {"present": False, "content": None}
    assert res.data["stats"]["tier0_present"] is False
    # 无摘要 → 不以「画像摘要」开头
    assert res.data["blocks"][0]["title"] != "画像摘要"


# ---------- 2. custom_filter 分支成功 ----------

def test_inject_custom_filter_ok(conns, cfg, summary_path):
    """custom_filter 分支：过滤正确，stats.mode="custom"，tier0 字段齐全。"""
    # Arrange：goals 与 identity 各一条
    mem_conn, _session_conn, _ = conns
    _insert(mem_conn, "目标 A 进行中", dims=["goals"], time_velocity="dynamic", ttl_days=90)
    _insert(mem_conn, "我是测试用户", dims=["identity"])
    tier0_mod.save_summary("画像摘要：测试用户", path=summary_path)

    # Act：只查 goals
    res = inject_operation(
        mem_conn, cfg, custom_filter={"dimensions": ["goals"]},
    )

    # Assert
    assert res.ok is True
    data = res.data
    assert data["stats"]["mode"] == "custom"
    assert data["stats"]["queries"] == 1
    assert data["tier0"] == {"present": True, "content": "画像摘要：测试用户"}
    # 自定义 section 只含 goals 记忆
    assert "目标 A 进行中" in _block_items(data["blocks"])
    assert "我是测试用户" not in _block_items(data["blocks"])
    # 兼容 memory_types 别名
    res2 = inject_operation(
        mem_conn, cfg, custom_filter={"memory_types": ["identity"]},
    )
    assert res2.ok is True
    assert "我是测试用户" in _block_items(res2.data["blocks"])


# ---------- 3. custom_filter 无 dimensions ----------

def test_inject_custom_filter_missing_dimensions(conns, cfg):
    """custom_filter 无 dimensions → ERR_INVALID_ARGS。"""
    # Arrange / Act
    mem_conn, _session_conn, _ = conns
    res = inject_operation(mem_conn, cfg, custom_filter={"match": "any"})

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert res.message == "custom_filter 需指定 dimensions"


def test_inject_custom_filter_unregistered_dimension(conns, cfg):
    """custom_filter 含未注册维度 id → ERR_INVALID_ARGS。"""
    # Arrange / Act
    mem_conn, _session_conn, _ = conns
    res = inject_operation(mem_conn, cfg, custom_filter={"dimensions": ["not_a_dim"]})

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert res.message == "未注册的维度 id: not_a_dim"


# ---------- 4. mode 模板不存在 / 加载失败 ----------

def test_inject_mode_template_missing(conns, cfg):
    """mode 模板不存在 → ERR_INVALID_ARGS「模板加载失败:」。"""
    # Arrange / Act
    mem_conn, _session_conn, _ = conns
    res = inject_operation(mem_conn, cfg, mode="no_such_mode")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert res.message.startswith("模板加载失败:")


def test_inject_mode_template_error_raised(conns, cfg, monkeypatch):
    """load_template 抛 TemplateError → ERR_INVALID_ARGS「模板加载失败:」。"""
    # Arrange：monkeypatch 使加载必失败
    mem_conn, _session_conn, _ = conns

    def _boom(mode: str, dimensions):
        raise template_mod.TemplateError("模拟模板校验失败")

    monkeypatch.setattr(template_mod, "load_template", _boom)

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert res.message == "模板加载失败: 模拟模板校验失败"


# ---------- 5. pipeline 层异常 → ERR_INTERNAL ----------

def test_inject_pipeline_error_is_internal(conns, cfg, monkeypatch):
    """query_section 抛意外异常 → ERR_INTERNAL（非参数问题不落 ERR_INVALID_ARGS）。"""
    # Arrange：monkeypatch 查询引擎抛运行时错误
    mem_conn, _session_conn, _ = conns

    def _boom(mem_conn, section, dimensions):
        raise RuntimeError("模拟查询引擎崩溃")

    monkeypatch.setattr("sgme.profile.inject.query_section", _boom)

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert "模拟查询引擎崩溃" in res.message


# ---------- 6. 未指定 mode 与 custom_filter ----------

def test_inject_requires_mode_or_custom_filter(conns, cfg):
    """mode 与 custom_filter 均未指定 → ERR_INVALID_ARGS。"""
    # Arrange / Act
    mem_conn, _session_conn, _ = conns
    res = inject_operation(mem_conn, cfg)

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert res.message == "需指定 mode 或 custom_filter"


# ---------- 7. 空结果引导提示（ST-22④） ----------

def test_inject_empty_result_has_note(conns, cfg, summary_path):
    """空库 + 无 Tier0 → 全空 block + stats.note 引导（先 append 沉淀记忆）。"""
    # Arrange：不写任何记忆与摘要
    mem_conn, _session_conn, _ = conns

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert：block 全空但响应给出可行动提示
    assert res.ok is True
    data = res.data or {}
    assert all(b["present"] is False for b in data["blocks"])
    assert "note" in data["stats"]
    assert "POST /v1/append" in data["stats"]["note"]


def test_inject_empty_custom_filter_has_note(conns, cfg, summary_path):
    """custom_filter 空结果 → 同样附加 note。"""
    # Arrange
    mem_conn, _session_conn, _ = conns

    # Act
    res = inject_operation(mem_conn, cfg, custom_filter={"dimensions": ["goals"]})

    # Assert
    assert res.ok is True
    assert "note" in (res.data or {})["stats"]


def test_inject_nonempty_no_note(conns, cfg, summary_path):
    """有记忆命中 → 不加 note（字段不存在，向后兼容）。"""
    # Arrange
    mem_conn, _session_conn, _ = conns
    _insert(mem_conn, "我是测试用户", dims=["identity", "family"])

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert
    assert res.ok is True
    assert "note" not in (res.data or {})["stats"]


def test_inject_empty_with_tier0_no_note(conns, cfg, summary_path):
    """空库但有 Tier0 摘要 → 摘要块占 1 item，不加 note。"""
    # Arrange：只写摘要
    mem_conn, _session_conn, _ = conns
    tier0_mod.save_summary("画像摘要：测试用户", path=summary_path)

    # Act
    res = inject_operation(mem_conn, cfg, mode="daily")

    # Assert
    assert res.ok is True
    assert "note" not in (res.data or {})["stats"]
