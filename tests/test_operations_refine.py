"""tests/test_operations_refine.py：operations 层 refine 操作测试（v0.7 P2-T3 refine 切片）。

覆盖：
1. 同步触发（单文件）成功：ok=True，data 为 HTTP 历史形态
   （triggered=file，status/memories_count/l15/new_last_refined_seq/prompt_versions 齐全）
2. 同步触发（批量）成功：processed/total_memories/results 正确
3. 异步触发成功：立即返回 queued 排队语义；后台线程按
   (file_id, limit, mem_conn, session_conn, cfg) 参数、daemon=True 启动
4. file_id 不存在 → ERR_NOT_FOUND（文案沿用 engine「raw_files 表无记录」）
5. limit 非法（0 / 负数 / None）→ InvalidArgs（ERR_INVALID_ARGS），同步/异步两操作一致
6. 底层异常 → ERR_INTERNAL（pipeline 抛异常 / 线程启动失败）
7. mcp_payload 投影：async/file/batch 三态的 MCP 历史子集；http_payload 恒等

测试隔离：tmp_path 建库，不触碰 data/*.db；提炼全程 mock engine.pipeline
（refine_one / refine_many / async_refine_worker），不真调 LLM。
fixtures 照抄 test_operations_health.py（cfg/raw_dir/mock_llm/conns/app/client/mcp），
其中 app/client/mcp 预留供「入口接线」切片复用——本切片只直调操作函数，不触发。
"""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import pipeline as pipeline_mod
from sgme.engine.refine import RefineResult
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations import refine as refine_op
from sgme.operations.errors import ERR_INTERNAL, ERR_NOT_FOUND, InvalidArgs, OperationResult
from sgme.operations.refine import (
    ASYNC_QUEUED_NOTE,
    http_payload,
    mcp_payload,
    refine_trigger,
    refine_trigger_async,
)
from sgme.raw import store as raw_store
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# ---------- fixtures（照抄 test_operations_health.py） ----------

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
    """mock LLM 探测为可用（避免实际打 127.0.0.1:1014）。"""
    from sgme.engine import health as engine_health

    monkeypatch.setattr(
        engine_health, "check_llm_available",
        lambda c, client=None: {
            "available": True, "provider": "lm-studio",
            "model": "mock-model", "error": None,
        },
    )


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
    """隔离 FastAPI 应用（预留：入口接线切片复用，本切片不触发）。"""
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
def mcp(conns, cfg, raw_dir, mock_llm):
    """绑定同一批连接的 MCP server（预留：入口接线切片复用）。"""
    mem_conn, session_conn, wiki_conn = conns
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
    })
    return build_mcp_server()


# ---------- 工具 ----------

def _insert_raw_file(session_conn: sqlite3.Connection, file_id: str) -> None:
    """插入 raw_files 行（直接 SQL，让存在性预检通过）。"""
    session_conn.execute(
        """
        INSERT INTO raw_files
          (file_id, path, session_key, agent_id, started_at, ended_at,
           refined_at, last_refined_seq, status, size)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (file_id, f"raw/sessions/{file_id}.md", f"sess_{file_id}", "test",
         "2026-08-04T10:00:00Z", None, None, 1, "new", 100),
    )
    session_conn.commit()


def _refine_result(file_id: str, memories: list | None = None,
                   status: str = "refined") -> RefineResult:
    """构造固定 RefineResult（提炼结果由测试精确控制，不真调 LLM）。"""
    return RefineResult(
        file_id=file_id,
        memories=memories or [],
        new_last_refined_seq=2,
        status=status,
        anomaly_warn=False,
        error=None,
        prompt_versions={"l1_extraction": {"version": "v1", "variant": "A"}},
    )


L15_STATS: dict[str, Any] = {
    "stored": 1, "skipped": 0, "updated": 0, "merged": 0, "archived": 0,
}

# 单文件响应的 v0.6 冻结键序（改造前逐字段抄录，任何变动即破坏性变更）
FILE_RESPONSE_KEYS = [
    "triggered", "file_id", "status", "memories_count",
    "new_last_refined_seq", "anomaly_warn", "error", "l15", "prompt_versions",
]
# 批量响应 results 内单文件项的 v0.6 冻结键序
RESULT_ITEM_KEYS = [
    "file_id", "status", "memories_count",
    "new_last_refined_seq", "anomaly_warn", "error", "l15", "prompt_versions",
]


# ---------- 1. 同步触发（单文件）成功 ----------

def test_refine_trigger_file_success(conns, cfg, monkeypatch):
    """同步单文件：ok=True，data 为 HTTP 历史形态（键序/字段齐全）。"""
    # Arrange：文件存在 + mock pipeline.refine_one 返回固定结果
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-sync")
    calls = []

    def _fake_refine_one(file_id, mem, sess, c):
        calls.append(file_id)
        return _refine_result(file_id, memories=[{"content": "m1"}]), dict(L15_STATS)

    monkeypatch.setattr(pipeline_mod, "refine_one", _fake_refine_one)

    # Act
    res = refine_trigger(mem_conn, session_conn, cfg, file_id="f-sync")

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert res.data is not None
    assert list(res.data.keys()) == FILE_RESPONSE_KEYS
    assert res.data["triggered"] == "file"
    assert res.data["file_id"] == "f-sync"
    assert res.data["status"] == "refined"
    assert res.data["memories_count"] == 1
    assert res.data["new_last_refined_seq"] == 2
    assert res.data["anomaly_warn"] is False
    assert res.data["error"] is None
    assert res.data["l15"] == L15_STATS
    assert res.data["prompt_versions"]["l1_extraction"]["version"] == "v1"
    assert calls == ["f-sync"]  # pipeline 只被调一次


# ---------- 2. 同步触发（批量）成功 ----------

def test_refine_trigger_batch_success(conns, cfg, monkeypatch):
    """同步批量：processed/total_memories/results 正确，results 键序为 v0.6 形态。"""
    # Arrange：mock pipeline.refine_many 返回两个文件
    mem_conn, session_conn, _ = conns
    pairs = [
        (_refine_result("f-a", memories=[{"content": "a1"}, {"content": "a2"}]), dict(L15_STATS)),
        (_refine_result("f-b", memories=[{"content": "b1"}]), dict(L15_STATS)),
    ]
    monkeypatch.setattr(pipeline_mod, "refine_many", lambda limit, mem, sess, c: pairs)

    # Act
    res = refine_trigger(mem_conn, session_conn, cfg, file_id=None, limit=5)

    # Assert
    assert res.ok is True
    assert res.data is not None
    assert res.data["triggered"] == "batch"
    assert res.data["processed"] == 2
    assert res.data["total_memories"] == 3
    assert len(res.data["results"]) == 2
    assert list(res.data["results"][0].keys()) == RESULT_ITEM_KEYS
    assert res.data["results"][0]["file_id"] == "f-a"
    assert res.data["results"][0]["status"] == "refined"
    assert res.data["results"][0]["memories_count"] == 2
    assert res.data["results"][0]["l15"] == L15_STATS
    assert res.data["results"][1]["file_id"] == "f-b"
    assert res.data["results"][1]["memories_count"] == 1


# ---------- 3. 异步触发成功（queued 语义） ----------

def test_refine_trigger_async_queued_and_thread_args(conns, cfg, monkeypatch):
    """异步：立即返回 queued 排队语义；后台线程按 (file_id, limit, 连接, cfg) 参数、
    daemon=True 启动且 target 是 async_refine_worker（不真起线程，确定性断言）。"""
    # Arrange：替换 threading.Thread 为记录器（不实际启动）
    mem_conn, session_conn, _ = conns
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(refine_op.threading, "Thread", _FakeThread)

    # Act
    res = refine_trigger_async(mem_conn, session_conn, cfg, file_id="f-async", limit=3)

    # Assert：返回排队语义（v0.6 响应键序）
    assert res.ok is True
    assert res.data is not None
    assert list(res.data.keys()) == ["triggered", "file_id", "status", "note"]
    assert res.data == {
        "triggered": "async",
        "file_id": "f-async",
        "status": "queued",
        "note": ASYNC_QUEUED_NOTE,
    }
    # 后台线程参数：target 是 worker、位置参数与连接身份一致、daemon=True、已 start
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["target"] is pipeline_mod.async_refine_worker
    f_id, limit, m_conn, s_conn, c_cfg = captured["args"]
    assert f_id == "f-async"
    assert limit == 3
    assert m_conn is mem_conn
    assert s_conn is session_conn
    assert c_cfg is cfg


def test_refine_trigger_async_empty_file_id_means_batch(conns, cfg, monkeypatch):
    """异步空串 file_id → data["file_id"]="batch"（v0.6 路由 ``or "batch"`` 假值语义）。"""
    # Arrange
    mem_conn, session_conn, _ = conns
    monkeypatch.setattr(
        refine_op.threading, "Thread",
        lambda target=None, args=(), daemon=None: type(
            "_NoopThread", (), {"start": lambda self: None})(),
    )

    # Act
    res = refine_trigger_async(mem_conn, session_conn, cfg, file_id="")

    # Assert
    assert res.ok is True
    assert res.data["file_id"] == "batch"
    assert res.data["status"] == "queued"


# ---------- 4. file_id 不存在 → ERR_NOT_FOUND ----------

def test_refine_trigger_file_not_found(conns, cfg, monkeypatch):
    """file_id 不存在 → ok=False + ERR_NOT_FOUND，文案沿用 engine 既有错误串；
    pipeline.refine_one 不得被调用（预检短路）。"""
    # Arrange：库中无该 raw_files 行
    mem_conn, session_conn, _ = conns
    called = []
    monkeypatch.setattr(
        pipeline_mod, "refine_one",
        lambda *a, **kw: called.append(a) or (_ for _ in ()).throw(AssertionError("不应被调用")),
    )

    # Act
    res = refine_trigger(mem_conn, session_conn, cfg, file_id="no-such-file")

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "raw_files 表无记录: no-such-file"
    assert called == []


# ---------- 5. 参数错误 → ERR_INVALID_ARGS ----------

@pytest.mark.parametrize("op", [refine_trigger, refine_trigger_async])
@pytest.mark.parametrize("bad_limit", [0, -1, None])
def test_refine_limit_invalid_raises_invalid_args(conns, cfg, op, bad_limit):
    """limit 非法（0/负数/None）→ InvalidArgs（ERR_INVALID_ARGS），同步/异步一致。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    # Act / Assert
    with pytest.raises(InvalidArgs) as ei:
        op(mem_conn, session_conn, cfg, limit=bad_limit)
    assert ei.value.error_code == "ERR_INVALID_ARGS"
    assert "limit" in ei.value.message


# ---------- 6. 底层异常 → ERR_INTERNAL ----------

def test_refine_trigger_underlying_exception_maps_to_internal(conns, cfg, monkeypatch):
    """pipeline.refine_one 抛非预期异常 → ok=False + ERR_INTERNAL（不向上炸）。"""
    # Arrange：文件存在 + mock 抛 RuntimeError
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-boom")

    def _boom(*a, **kw):
        raise RuntimeError("LLM 链路崩溃")

    monkeypatch.setattr(pipeline_mod, "refine_one", _boom)

    # Act
    res = refine_trigger(mem_conn, session_conn, cfg, file_id="f-boom")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert "LLM 链路崩溃" in res.message


def test_refine_trigger_async_thread_start_failure_maps_to_internal(conns, cfg, monkeypatch):
    """线程启动失败（start 抛异常）→ ok=False + ERR_INTERNAL（排队语义不成立）。"""
    # Arrange
    mem_conn, session_conn, _ = conns

    class _BoomThread:
        def __init__(self, target=None, args=(), daemon=None):
            pass

        def start(self):
            raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(refine_op.threading, "Thread", _BoomThread)

    # Act
    res = refine_trigger_async(mem_conn, session_conn, cfg, file_id="f-x")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert "cannot start new thread" in res.message


# ---------- 7. 投影函数 ----------

@pytest.mark.parametrize(
    "data, expected",
    [
        # async：MCP 子集 {triggered, status}
        ({"triggered": "async", "file_id": "f1", "status": "queued", "note": "n"},
         {"triggered": "async", "status": "queued"}),
        # file：MCP 子集 {triggered, status, memories_count}
        ({"triggered": "file", "file_id": "f1", "status": "refined",
          "memories_count": 2, "new_last_refined_seq": 2, "anomaly_warn": False,
          "error": None, "l15": L15_STATS, "prompt_versions": {}},
         {"triggered": "file", "status": "refined", "memories_count": 2}),
        # batch：MCP 子集 {triggered, processed}
        ({"triggered": "batch", "processed": 3, "total_memories": 5, "results": []},
         {"triggered": "batch", "processed": 3}),
    ],
)
def test_mcp_payload_projects_historical_shapes(data, expected):
    """mcp_payload：按 triggered 裁剪为 MCP 历史子集（v0.6 逐字段等价）。"""
    assert mcp_payload(data) == expected


def test_http_payload_is_identity(conns, cfg, monkeypatch):
    """http_payload：data 即 HTTP 响应体，恒等返回（对象同一性）。"""
    # Arrange：走一次真实同步操作拿超集
    mem_conn, session_conn, _ = conns
    _insert_raw_file(session_conn, "f-http")
    monkeypatch.setattr(
        pipeline_mod, "refine_one",
        lambda file_id, mem, sess, c: (_refine_result(file_id, memories=[{"content": "x"}]), dict(L15_STATS)),
    )
    data = refine_trigger(mem_conn, session_conn, cfg, file_id="f-http").data

    # Act / Assert
    assert http_payload(data) is data
    assert list(http_payload(data).keys()) == FILE_RESPONSE_KEYS
