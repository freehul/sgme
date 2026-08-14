"""tests/test_operations_append.py：operations 层 L0 捕获（append）测试（v0.7）。

覆盖：
1. append_l0 成功：OperationResult(ok=True)，data 含 file_id/path/status
2. raw 文件实际落盘，内容含解析后的消息头与正文
3. 幂等：同 session_key + 同 started_at 第二次 → idempotent=True 且 file_id 不变
4. 追加：同 session_key + 不同 started_at → 同 file_id 追加，status 重置为 new
5. content 解析出 0 条消息 → ERR_INVALID_ARGS
6. raw 文件丢失（先建后删，再追加）→ ERR_NOT_FOUND
7. engine 抛 RuntimeError → ERR_INTERNAL（monkeypatch pipeline.append_l0）

隔离：tmp_path 建库 + monkeypatch RAW_DIR（照抄 test_operations_health.py），零真实 DB 触碰。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import health as engine_health
from sgme.engine import pipeline as pipeline_mod
from sgme.mcp_server import bind_app_state, build_mcp_server
from sgme.operations.append import append_l0
from sgme.operations.errors import (
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    OperationResult,
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
    """mock LLM 探测为可用（避免实际打 127.0.0.1:1014）。

    注意：monkeypatch 的是 engine.health 模块全局 check_llm_available，
    check_heartbeat 内部按模块全局解析，因此 operations 层调用同样生效。
    """
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
def mcp(conns, cfg, raw_dir, mock_llm):
    """绑定同一批连接的 MCP server。"""
    mem_conn, session_conn, wiki_conn = conns
    bind_app_state({
        "cfg": cfg, "mem_conn": mem_conn,
        "session_conn": session_conn, "wiki_conn": wiki_conn,
    })
    return build_mcp_server()


# ---------- 工具 ----------

def _make_content(text: str = "你好，这是第一条测试消息") -> str:
    """构造合法 content：至少一条 `# {ISO} {role}` 消息头。"""
    return f"# 2026-08-04T10:00:00Z user\n{text}\n"


def _append(conns, cfg, session_key: str = "sess_test",
            started_at: str = "2026-08-04T10:00:00Z", content: str | None = None,
            **kwargs):
    """直调 operations.append_l0（业务参走默认值，MCP 形态：只传 4 参）。"""
    mem_conn, session_conn, _ = conns
    return append_l0(
        session_key=session_key,
        started_at=started_at,
        content=content if content is not None else _make_content(),
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        **kwargs,
    )


def _raw_file_of(res) -> Path:
    """按 file_id 取落盘文件绝对路径（与 raw_store 同一解析口径）。"""
    return Path(raw_store.file_path(res.data["file_id"]))


# ---------- 1. 成功 ----------

def test_append_success_returns_ok_with_file_info(conns, cfg):
    """append_l0 成功：OperationResult(ok=True)，data 含 file_id/path/status。"""
    # Act：只传 4 业务参（MCP 形态），source_type/ended_at/agent_id/metadata 走默认值
    res = _append(conns, cfg)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert res.data["file_id"]
    assert res.data["path"]
    assert res.data["status"] == "new"
    # 成功态不带幂等/追加标记
    assert res.data.get("idempotent") is None
    assert res.data.get("appended") is None


# ---------- 2. raw 文件实际落盘 ----------

def test_append_writes_raw_file_with_message(conns, cfg):
    """raw 文件实际生成，内容含解析后的消息头与正文。"""
    # Act
    res = _append(conns, cfg)

    # Assert：文件存在且内容包含消息
    raw_file = _raw_file_of(res)
    assert raw_file.is_file(), f"raw 文件未生成: {raw_file}"
    text = raw_file.read_text(encoding="utf-8")
    assert "# 2026-08-04T10:00:00Z user" in text
    assert "你好，这是第一条测试消息" in text


# ---------- 3. 幂等 ----------

def test_append_idempotent_same_session_and_started_at(conns, cfg):
    """同 session_key + 同 started_at 第二次 → idempotent=True，file_id 不变。"""
    # Arrange
    first = _append(conns, cfg)

    # Act
    second = _append(conns, cfg)

    # Assert
    assert second.ok is True
    assert second.data["idempotent"] is True
    assert second.data["file_id"] == first.data["file_id"]
    # 文件未被重复写（正文仍只有一条消息）
    text = _raw_file_of(first).read_text(encoding="utf-8")
    assert text.count("你好，这是第一条测试消息") == 1


# ---------- 4. 追加 ----------

def test_append_appends_to_existing_file(conns, cfg):
    """同 session_key + 不同 started_at → 同 file_id 追加，status 重置为 new。"""
    # Arrange
    first = _append(conns, cfg)

    # Act
    second = _append(
        conns, cfg,
        started_at="2026-08-04T11:00:00Z",
        content=_make_content("第二条消息"),
    )

    # Assert
    assert second.ok is True
    assert second.data["file_id"] == first.data["file_id"]
    assert second.data["appended"] is True
    assert second.data["status"] == "new"
    text = _raw_file_of(first).read_text(encoding="utf-8")
    assert "你好，这是第一条测试消息" in text
    assert "第二条消息" in text


# ---------- 5. content 解析 0 条 → ERR_INVALID_ARGS ----------

def test_append_no_messages_invalid_args(conns, cfg):
    """content 解析出 0 条消息 → ERR_INVALID_ARGS（翻译成失败态，不抛异常）。"""
    # Act
    res = _append(conns, cfg, content="没有任何消息头格式的纯文本")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS
    assert "0 条消息" in res.message


# ---------- 6. raw 文件丢失 → ERR_NOT_FOUND ----------

def test_append_raw_file_lost_not_found(conns, cfg):
    """先 append 建文件，再手动删 raw 文件，同 session_key 不同 started_at 追加 → ERR_NOT_FOUND。"""
    # Arrange：建文件后删除
    first = _append(conns, cfg)
    raw_file = _raw_file_of(first)
    assert raw_file.is_file()
    raw_file.unlink()

    # Act：再次 append（不同 started_at → 走追加分支，发现文件丢失）
    res = _append(conns, cfg, started_at="2026-08-04T11:00:00Z")

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert "raw 文件丢失" in res.message


# ---------- 7. engine 异常 → ERR_INTERNAL ----------

def test_append_pipeline_runtime_error_internal(conns, cfg, monkeypatch):
    """engine.append_l0 抛 RuntimeError → ERR_INTERNAL（monkeypatch）。"""
    # Arrange
    def _boom(*args, **kwargs):
        raise RuntimeError("写盘炸了")

    monkeypatch.setattr(pipeline_mod, "append_l0", _boom)

    # Act
    res = _append(conns, cfg)

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert res.message == "写 L0 文件失败: 写盘炸了"
