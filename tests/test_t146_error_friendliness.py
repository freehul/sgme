"""tests/test_t146_error_friendliness.py：T-146 报错友好度三缺陷修复验证。

覆盖：
1. ① 单文件提炼业务失败（status=error）→ HTTP 503（不再 200 伪装成功）
2. ① 批量提炼部分失败 → 保持 HTTP 200（部分成功语义不受影响）
3. ② 端口占用 OSError（errno 10048/98）→ 中文可行动提示 + 退出码非 0
4. ③ 双 uvicorn 实例（HTTP+MCP）构建后 sgme.* logger 仍只有 root 单 handler（无双打印回归）

零真实网络：TestClient + mock pipeline，端口占用走函数级注入测试。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    from sgme import config as _c
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(_c, "RAW_DIR", raw)
    return raw


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
    # 测试日志落 tmp（conftest 已注入 SGME_LOG_OUTPUT；此处显式兜底）
    monkeypatch.setenv("SGME_LOG_OUTPUT", str(tmp_path / "test.log"))
    # MCP 不起线程
    monkeypatch.setenv("SGME_MCP_DISABLED", "1")

    cfg = sgme_config.load_config()
    cfg["skills_hub"] = {"enabled": False}
    cfg["skills"] = {"enabled": False, "source_dirs": []}
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    application = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield application
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- ① refine 单文件业务失败 → 503 ----------

def test_refine_single_file_error_returns_503(app, client, monkeypatch):
    """单文件提炼 LLM 全链失败（status=error）→ HTTP 503 + error body（T-146①）。"""
    from sgme.engine import pipeline as pipeline_mod
    from sgme.engine.refine import RefineResult

    def _fake_refine_one(file_id, mem_conn, session_conn, cfg):
        return (
            RefineResult(
                file_id=file_id,
                status="error",
                memories=[],
                new_last_refined_seq=None,
                anomaly_warn=False,
                error="全链降级失败，drop_batch",
                prompt_versions={},
            ),
            None,
        )

    # 造一个 raw 文件让存在性预检通过
    rf = app.state.session_conn.execute(
        "INSERT INTO raw_files (file_id, session_key, started_at, path, status)"
        " VALUES ('f-err', 's-err', '2026-09-07T00:00:00Z', 'raw/x.md', 'new')"
    )
    app.state.session_conn.commit()

    monkeypatch.setattr(pipeline_mod, "refine_one", _fake_refine_one)
    resp = client.post(
        "/v1/admin/refine/trigger",
        headers=ADMIN_HEADERS,
        json={"file_id": "f-err"},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == "ERR_LLM_UNAVAILABLE"
    assert "drop_batch" in body["error"]["message"] or "提炼失败" in body["error"]["message"]


def test_refine_batch_partial_error_keeps_200(app, client, monkeypatch):
    """批量提炼（无 file_id）即使含失败项也保持 200（部分成功语义，T-146① 边界）。"""
    from sgme.engine import pipeline as pipeline_mod

    def _fake_refine_many(limit, mem_conn, session_conn, cfg):
        class _R:
            file_id = "f-batch"
            status = "error"
            memories = []
            new_last_refined_seq = None
            anomaly_warn = None
            error = "单文件失败"
            prompt_versions = {}

        return [(_R(), None)]

    monkeypatch.setattr(pipeline_mod, "refine_many", _fake_refine_many)
    resp = client.post(
        "/v1/admin/refine/trigger",
        headers=ADMIN_HEADERS,
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "error"


# ---------- ② 端口占用提示 ----------

def test_port_in_use_hint_message():
    """errno 10048 → _port_in_use_hint 返回含 SGME_PORT 的中文提示（T-146②）。"""
    from sgme.__main__ import _port_in_use_hint

    hint = _port_in_use_hint(9910)
    assert "9910" in hint
    assert "SGME_PORT" in hint
    assert "已被占用" in hint


def test_port_in_use_oserror_branch(monkeypatch, capsys):
    """main() 收到端口占用 OSError → 打印提示 + SystemExit(1)（T-146②）。"""
    import sgme.__main__ as main_mod

    class _FakeUvicorn:
        @staticmethod
        def run(*args, **kwargs):
            raise OSError(10048, "通常每个套接字地址只允许使用一次")

    monkeypatch.setattr(main_mod, "create_app", lambda **kw: object())
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "已被占用" in err
    assert "SGME_PORT" in err


# ---------- ③ 双 uvicorn 不叠加 sgme 日志 handler ----------

def test_double_uvicorn_config_no_handler_duplication():
    """HTTP+MCP 两个 uvicorn.Config 构造后 root/sgme.* handler 数量不变（T-146③ 回归）。"""
    import uvicorn

    root = logging.getLogger()
    root_before = len(root.handlers)
    sgme_log = logging.getLogger("sgme.server")
    sgme_before = len(sgme_log.handlers)

    uvicorn.Config(app=None, log_level="info")
    uvicorn.Config(app=None, log_level="warning")

    assert len(root.handlers) == root_before
    assert len(sgme_log.handlers) == sgme_before


def test_setup_idempotent_no_handler_accumulation(tmp_path, monkeypatch):
    """重复 create_app 场景：log.setup 幂等，root 不累积 managed handler（T-146③）。"""
    from sgme.log import setup, _MANAGED_HANDLERS

    log_file = tmp_path / "dedupe.log"
    setup(level="INFO", format="console", output=str(log_file))
    n1 = len(_MANAGED_HANDLERS)
    setup(level="INFO", format="console", output=str(log_file))
    n2 = len(_MANAGED_HANDLERS)
    assert n1 == n2 == 1  # 幂等：不累积
