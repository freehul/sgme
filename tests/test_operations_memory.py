"""tests/test_operations_memory.py：operations 层 memory 多操作模块测试（v0.7 Batch-1）。

覆盖：
1. get_memory / reject_memory / unreject_memory 均返回 OperationResult
2. 信息超集字段完整（memory + archive_chain）
3. **失败路径**（health 切片漏掉的错误翻译分支，此处全部补齐）：
   - get_memory 记忆不存在 → ERR_NOT_FOUND（HTTP 404 / MCP 固定文案）
   - reject_memory 记忆不存在 → ERR_NOT_FOUND
   - reject_memory DAO 返回 False（罕见竞态）→ ERR_INTERNAL（HTTP 500）
   - unreject_memory 记忆不存在 → ERR_NOT_FOUND（靠 rowcount，不预查）
4. 投影函数的历史契约差异：HTTP 三键包裹体 vs MCP 裸记忆对象
5. **契约等价性**（最关键）：GET /v1/memory/{id}、POST reject/unreject、
   MCP memory_get 经 operations 包装后，字段集合与顺序仍与 v0.6 逐字段一致
6. 缺省纠错原因回落 "用户纠错"

零真实 DB：全部走 tmp_path 建库 + monkeypatch，绝不触碰 data/*.db。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.errors import ERR_INTERNAL, ERR_NOT_FOUND, OperationResult
from sgme.operations.memory import (
    DEFAULT_REJECT_REASON,
    MCP_GET_NOT_FOUND_MESSAGE,
    get_http_payload,
    get_mcp_error_payload,
    get_mcp_payload,
    get_memory,
    reject_memory,
    unreject_memory,
)
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

# ---------- v0.6 冻结契约（改造前逐字段抄录，任何变动即破坏性变更） ----------

# GET /v1/memory/{id} 成功体：三键包裹，sources 与 memory.sources 同值冗余
HTTP_GET_TOP_KEYS = ["memory", "sources", "archive_chain"]
# MCP memory_get 成功体：裸记忆对象（memories 表列 + tags + sources）
MCP_GET_REQUIRED_KEYS = {"memory_id", "content", "memory_type", "priority",
                         "time_velocity", "ttl_days", "created_at", "updated_at",
                         "status", "tags", "sources"}
MCP_GET_NOT_FOUND_BODY = {"error": "记忆不存在"}
# POST /v1/memory/{id}/reject 成功体
HTTP_REJECT_KEYS = ["memory_id", "status", "reject_reason"]
# POST /v1/memory/{id}/unreject 成功体
HTTP_UNREJECT_KEYS = ["memory_id", "status"]
# 404 体：error 只有 code / message 两键（无 details）
HTTP_ERROR_KEYS = ["code", "message"]

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path，零真实 DB）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def mem_conn(conns):
    return conns[0]


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    m, s, w = conns
    return create_app(
        cfg=cfg, mem_conn=m, session_conn=s, wiki_conn=w,
        admin_key="test-admin-key", agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mcp(conns, cfg):
    """绑定同一批连接的 MCP server。"""
    m, s, w = conns
    bind_app_state({"cfg": cfg, "mem_conn": m, "session_conn": s, "wiki_conn": w})
    return build_mcp_server()


# ---------- 工具 ----------

def _insert(conn: sqlite3.Connection, memory_id: str = "mem-1",
            content: str = "用户偏好深色主题") -> str:
    """插入一条带溯源与标签的记忆，返回 memory_id。"""
    return memory_dao.insert_memory(
        conn,
        content=content,
        memory_type="preference",
        priority=3,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=[],
        sources=[("raw/sessions/f-1.md#L1", "session")],
        memory_id=memory_id,
    )


def _call_mcp(mcp_server, name: str, args: dict) -> str:
    """同步包装 async call_tool → 返回文本。"""
    raw = asyncio.run(mcp_server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    return "\n".join(c.text for c in results if getattr(c, "text", None))


# ---------- 1. 返回类型 ----------

def test_get_memory_returns_operation_result_ok(mem_conn):
    """get_memory 返回 OperationResult 且 ok=True。"""
    # Arrange
    _insert(mem_conn)

    # Act
    res = get_memory(mem_conn, "mem-1")

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert isinstance(res.data, dict)


def test_reject_and_unreject_return_operation_result(mem_conn):
    """reject_memory / unreject_memory 均返回 OperationResult(ok=True)。"""
    # Arrange
    _insert(mem_conn)

    # Act
    rejected = reject_memory(mem_conn, "mem-1", reason="记错了")
    restored = unreject_memory(mem_conn, "mem-1")

    # Assert
    assert isinstance(rejected, OperationResult) and rejected.ok is True
    assert isinstance(restored, OperationResult) and restored.ok is True


# ---------- 2. 信息超集完整性 ----------

def test_get_memory_data_is_superset(mem_conn):
    """data 是协议无关超集：memory（含 tags/sources）+ archive_chain。"""
    # Arrange
    _insert(mem_conn)

    # Act
    data = get_memory(mem_conn, "mem-1").data

    # Assert
    assert set(data.keys()) == {"memory", "archive_chain"}
    assert data["memory"]["memory_id"] == "mem-1"
    assert data["memory"]["content"] == "用户偏好深色主题"
    assert data["memory"]["sources"] == [
        {"source_ref": "raw/sessions/f-1.md#L1", "source_type": "session"},
    ]
    assert data["memory"]["tags"] == []
    # 未归档 → 归档链为空列表（不是 None）
    assert data["archive_chain"] == []


def test_reject_data_uses_default_reason(mem_conn):
    """reason 为 None / 空串 → 回落 DEFAULT_REJECT_REASON。"""
    # Arrange
    _insert(mem_conn)

    # Act
    none_res = reject_memory(mem_conn, "mem-1", reason=None)
    empty_res = reject_memory(mem_conn, "mem-1", reason="")

    # Assert
    assert DEFAULT_REJECT_REASON == "用户纠错"
    assert none_res.data["reject_reason"] == DEFAULT_REJECT_REASON
    assert empty_res.data["reject_reason"] == DEFAULT_REJECT_REASON
    # 落库真的改了 status（幂等：重复 reject 不报错）
    assert memory_dao.get_memory(mem_conn, "mem-1")["status"] == "rejected"


def test_unreject_restores_active_status(mem_conn):
    """unreject 把 rejected 恢复为 active，并清空 reject_reason。"""
    # Arrange
    _insert(mem_conn)
    reject_memory(mem_conn, "mem-1", reason="记错了")

    # Act
    res = unreject_memory(mem_conn, "mem-1")

    # Assert
    assert res.data == {"memory_id": "mem-1", "status": "active"}
    row = memory_dao.get_memory(mem_conn, "mem-1")
    assert row["status"] == "active"
    assert row["reject_reason"] is None


# ---------- 3. 失败路径（本切片重点补齐的错误翻译分支） ----------

def test_get_memory_not_found_fails(mem_conn):
    """记忆不存在 → ok=False / ERR_NOT_FOUND，文案带 id，details 为空。"""
    # Act
    res = get_memory(mem_conn, "no-such-id")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "记忆不存在: no-such-id"
    assert not res.details  # 空/None —— 404 体必须只有 code/message 两键


@pytest.mark.parametrize("blank_id", ["", "   "])
def test_get_memory_blank_id_is_not_found_not_invalid_args(mem_conn, blank_id):
    """空 memory_id 走「不存在」而非参数错误——v0.6 两端都未做非空校验。"""
    # Act
    res = get_memory(mem_conn, blank_id)

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_reject_memory_not_found_fails(mem_conn):
    """reject 不存在的记忆 → ERR_NOT_FOUND（先查后改，不靠 rowcount）。"""
    # Act
    res = reject_memory(mem_conn, "no-such-id")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "记忆不存在: no-such-id"


def test_reject_memory_dao_zero_rowcount_is_internal_error(mem_conn, monkeypatch):
    """记忆存在但 DAO 返回 False（罕见竞态）→ ERR_INTERNAL「标记失败」。

    这是 v0.6 就有、却从未被测试覆盖的分支；operations 化后用 monkeypatch
    精确构造（不需要真造竞态）。
    """
    # Arrange
    _insert(mem_conn)
    monkeypatch.setattr(
        "sgme.operations.memory.memory_dao.reject_memory",
        lambda conn, mid, reason: False,
    )

    # Act
    res = reject_memory(mem_conn, "mem-1")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert res.message == "标记失败"


def test_unreject_memory_not_found_fails(mem_conn):
    """unreject 不存在的记忆 → ERR_NOT_FOUND（靠 rowcount，不预查存在性）。"""
    # Act
    res = unreject_memory(mem_conn, "no-such-id")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "记忆不存在: no-such-id"


def test_unexpected_sqlite_error_bubbles_up(mem_conn, monkeypatch):
    """非预期异常（sqlite 故障）原样上抛，不被 catch-all 吞掉。"""
    # Arrange
    def _boom(conn, mid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("sgme.operations.memory.memory_dao.get_memory", _boom)

    # Act / Assert
    with pytest.raises(sqlite3.OperationalError):
        get_memory(mem_conn, "mem-1")


# ---------- 4. 投影函数的历史契约差异 ----------

def test_http_and_mcp_projections_differ_as_expected(mem_conn):
    """HTTP 是三键包裹体，MCP 是裸记忆对象——历史差异，v0.8 待统一。"""
    # Arrange
    _insert(mem_conn)
    data = get_memory(mem_conn, "mem-1").data

    # Act
    http_body = get_http_payload(data)
    mcp_body = get_mcp_payload(data)

    # Assert：HTTP 形态
    assert list(http_body.keys()) == HTTP_GET_TOP_KEYS
    # sources 冗余提升：两处同值（v0.6 如此）
    assert http_body["sources"] == http_body["memory"]["sources"]

    # Assert：MCP 形态——裸对象，无包裹层、无归档链
    assert MCP_GET_REQUIRED_KEYS <= set(mcp_body.keys())
    assert "memory" not in mcp_body
    assert "archive_chain" not in mcp_body
    # 浅拷贝：改投影结果不回流污染操作层 data
    mcp_body["content"] = "被改了"
    assert data["memory"]["content"] == "用户偏好深色主题"


def test_mcp_error_projection_drops_memory_id():
    """MCP 失败文案是固定串（不带 id），与 HTTP 的带 id 文案是历史差异。"""
    # Act
    body = get_mcp_error_payload({"error": "记忆不存在: mem-999"})

    # Assert
    assert body == MCP_GET_NOT_FOUND_BODY
    assert MCP_GET_NOT_FOUND_MESSAGE == "记忆不存在"


# ---------- 5. 契约等价性（最关键） ----------

def test_http_get_contract_unchanged(client, mem_conn):
    """GET /v1/memory/{id} 经 operations 包装后，字段集合与顺序仍与 v0.6 一致。"""
    # Arrange
    _insert(mem_conn)

    # Act
    resp = client.get("/v1/memory/mem-1", headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_GET_TOP_KEYS
    assert body["memory"]["memory_id"] == "mem-1"
    assert body["sources"] == [
        {"source_ref": "raw/sessions/f-1.md#L1", "source_type": "session"},
    ]
    assert body["archive_chain"] == []


def test_http_get_404_contract_unchanged(client):
    """GET /v1/memory/{不存在} → 404，error 体只有 code/message 两键。"""
    # Act
    resp = client.get("/v1/memory/no-such-id", headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 404, resp.text
    err = resp.json()["error"]
    assert list(err.keys()) == HTTP_ERROR_KEYS
    assert err["code"] == "ERR_NOT_FOUND"
    assert err["message"] == "记忆不存在: no-such-id"


def test_http_reject_contract_unchanged(client, mem_conn):
    """POST /v1/memory/{id}/reject 响应键集合与顺序仍与 v0.6 一致。"""
    # Arrange
    _insert(mem_conn)

    # Act
    resp = client.post("/v1/memory/mem-1/reject", headers=AGENT_HEADERS,
                       json={"reason": "记错了"})

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_REJECT_KEYS
    assert body == {"memory_id": "mem-1", "status": "rejected", "reject_reason": "记错了"}


def test_http_reject_default_reason_contract_unchanged(client, mem_conn):
    """POST reject 不带 body → reject_reason 回落「用户纠错」（v0.6 行为）。"""
    # Arrange
    _insert(mem_conn)

    # Act
    resp = client.post("/v1/memory/mem-1/reject", headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    assert resp.json()["reject_reason"] == "用户纠错"


def test_http_reject_404_contract_unchanged(client):
    """POST reject 不存在的记忆 → 404 + 带 id 文案。"""
    # Act
    resp = client.post("/v1/memory/no-such-id/reject", headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == "记忆不存在: no-such-id"


def test_http_unreject_contract_unchanged(client, mem_conn):
    """POST /v1/memory/{id}/unreject 响应键集合与顺序仍与 v0.6 一致。"""
    # Arrange
    _insert(mem_conn)
    client.post("/v1/memory/mem-1/reject", headers=AGENT_HEADERS)

    # Act
    resp = client.post("/v1/memory/mem-1/unreject", headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == HTTP_UNREJECT_KEYS
    assert body == {"memory_id": "mem-1", "status": "active"}


def test_http_unreject_404_contract_unchanged(client):
    """POST unreject 不存在的记忆 → 404（rowcount 判定路径）。"""
    # Act
    resp = client.post("/v1/memory/no-such-id/unreject", headers=AGENT_HEADERS)

    # Assert
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_mcp_get_contract_unchanged(mcp, mem_conn):
    """MCP memory_get 经 operations 包装后仍是裸记忆对象（v0.6 逐字段等价）。"""
    # Arrange
    _insert(mem_conn)

    # Act
    body = json.loads(_call_mcp(mcp, "memory_get", {"memory_id": "mem-1"}))

    # Assert
    assert MCP_GET_REQUIRED_KEYS <= set(body.keys())
    assert body["memory_id"] == "mem-1"
    assert body["content"] == "用户偏好深色主题"
    # 无 HTTP 的包裹层与归档链
    assert "memory" not in body
    assert "archive_chain" not in body


def test_mcp_get_not_found_contract_unchanged(mcp):
    """MCP memory_get 不存在 → 固定文案 {"error": "记忆不存在"}（不带 id）。"""
    # Act
    body = json.loads(_call_mcp(mcp, "memory_get", {"memory_id": "no-such-id"}))

    # Assert
    assert body == MCP_GET_NOT_FOUND_BODY


def test_http_and_mcp_agree_on_shared_fields(client, mcp, mem_conn):
    """同一条记忆，两端共有字段取值一致（差异只在包裹形态与失败文案）。"""
    # Arrange
    _insert(mem_conn)

    # Act
    http_body = client.get("/v1/memory/mem-1", headers=AGENT_HEADERS).json()
    mcp_body = json.loads(_call_mcp(mcp, "memory_get", {"memory_id": "mem-1"}))

    # Assert：MCP 的裸对象 == HTTP 的 memory 子对象
    assert mcp_body == http_body["memory"]
