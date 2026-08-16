"""tests/test_wiki_evolve.py：W4 自进化管线测试（方案 v0.3 §5.4）。

覆盖：
1. evolve_dao：wiki_evolve 表 CRUD
2. evolve_trigger 费用门禁：短会话（< min_rounds）→ skipped
3. 提炼成功 append：mock LLM → 追加到现有手册 + wiki_evolve done
4. 提炼成功 create：mock LLM → 新建手册页
5. 规则闸门：非法 type → rejected
6. 幂等：同 session_key 已处理 → 跳过
"""
from __future__ import annotations

import json

import pytest

from sgme.data import db as db_mod
from sgme.data import evolve_dao
from sgme.operations import evolve as evolve_mod


@pytest.fixture
def conn_wiki(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


@pytest.fixture
def conn_session(tmp_path):
    c = db_mod.connect_session(tmp_path / "data")
    yield c
    db_mod.close(c)


def _write_session(tmp_path, session_key: str, blocks: int, extra: str = "") -> str:
    """构造会话文件（frontmatter + blocks 个消息块），返回 path。"""
    lines = [
        "---",
        "format_version: 1",
        f"file_id: {session_key}-f1",
        f"session_key: {session_key}",
        "agent_id: default",
        "source_type: session",
        "started_at: '2026-08-16T00:00:00Z'",
        "---",
    ]
    for i in range(blocks):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(f"# 2026-08-16T00:00:0{i}Z {role}")
        lines.append(f"消息内容 {i} {extra}")
    p = tmp_path / "sessions" / f"{session_key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def _register_raw(conn_session, session_key: str, path: str):
    conn_session.execute(
        "INSERT INTO raw_files (file_id, path, session_key, started_at, status) VALUES (?,?,?,?,?)",
        (f"{session_key}-f1", path, session_key, "2026-08-16T00:00:00Z", "new"),
    )
    conn_session.commit()


def _mock_llm(monkeypatch, payload):
    """mock LLM：返回 payload（JSON 字符串或 dict 自动转）。"""
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(evolve_mod, "_llm_call", lambda cfg, prompt: payload)
    monkeypatch.setattr(evolve_mod, "_load_prompt", lambda stage: "模板")


# ---------- evolve_dao ----------

def test_evolve_dao_crud(conn_wiki):
    evolve_dao.create_run(conn_wiki, "sess-1")
    row = evolve_dao.get_run(conn_wiki, "sess-1")
    assert row["status"] == "queued"
    assert evolve_dao.update_run(conn_wiki, "sess-1", status="done", action="appended",
                                 entry_hash="abc123", page_id="p1") is True
    row = evolve_dao.get_run(conn_wiki, "sess-1")
    assert row["status"] == "done"
    assert row["action"] == "appended"
    assert evolve_dao.has_run(conn_wiki, "sess-1") is True
    assert evolve_dao.has_run(conn_wiki, "nope") is False


# ---------- evolve_trigger ----------

def test_evolve_skips_short_session(tmp_path, conn_wiki, conn_session, monkeypatch):
    """费用门禁：会话消息块 < min_rounds → skipped（不调 LLM）。"""
    _mock_llm(monkeypatch, [])
    p = _write_session(tmp_path, "short-s", blocks=3)
    _register_raw(conn_session, "short-s", p)
    result = evolve_mod.evolve_trigger(
        conn_wiki, conn_session, {}, session_key="short-s", min_rounds=5,
        data_dir=str(tmp_path),
    )
    assert result.ok
    assert result.data["status"] == "skipped"
    row = evolve_dao.get_run(conn_wiki, "short-s")
    assert row["status"] == "skipped"


def test_evolve_appends_to_handbook(tmp_path, conn_wiki, conn_session, monkeypatch):
    """提炼成功（type=append）：追加到现有手册「踩坑记录」。"""
    from sgme.data import wiki_dao
    wiki_dao.insert_page(conn_wiki, "hb1", "SGME操作手册", "原有正文",
                         category="skill/sgme", tags=["skill", "sgme"])
    _mock_llm(monkeypatch, [{
        "type": "append", "category": "skill/sgme", "title": "SGME操作手册",
        "entry": "踩坑：PATCH 前需先起服务",
    }])
    p = _write_session(tmp_path, "sess-append", blocks=6, extra="有坑")
    _register_raw(conn_session, "sess-append", p)
    result = evolve_mod.evolve_trigger(
        conn_wiki, conn_session, {}, session_key="sess-append", min_rounds=5,
        data_dir=str(tmp_path),
    )
    assert result.ok
    assert result.data["status"] == "done"
    page = wiki_dao.get_page(conn_wiki, "hb1")
    assert "踩坑：PATCH 前需先起服务" in page["content"]
    assert "hash: " in page["content"]  # entry hash 标记
    row = evolve_dao.get_run(conn_wiki, "sess-append")
    assert row["status"] == "done" and row["action"] == "appended"


def test_evolve_creates_handbook(tmp_path, conn_wiki, conn_session, monkeypatch):
    """提炼成功（type=create）：新建手册页（category/title/entry 落库）。"""
    _mock_llm(monkeypatch, [{
        "type": "create", "category": "skill/github", "title": "GitHub操作手册",
        "entry": "新建手册内容：PR 流程要点",
    }])
    p = _write_session(tmp_path, "sess-create", blocks=6, extra="新流程")
    _register_raw(conn_session, "sess-create", p)
    result = evolve_mod.evolve_trigger(
        conn_wiki, conn_session, {}, session_key="sess-create", min_rounds=5,
        data_dir=str(tmp_path),
    )
    assert result.ok
    assert result.data["status"] == "done"
    from sgme.data import wiki_dao
    pages = wiki_dao.list_pages(conn_wiki, category="skill/github")
    assert any("GitHub操作手册" in p["title"] for p in pages)


def test_evolve_rejects_invalid_type(tmp_path, conn_wiki, conn_session, monkeypatch):
    """规则闸门：非法 type → rejected（不写入）。"""
    _mock_llm(monkeypatch, [{
        "type": "hack", "category": "skill/x", "title": "坏条目", "entry": "x",
    }])
    p = _write_session(tmp_path, "sess-bad", blocks=6)
    _register_raw(conn_session, "sess-bad", p)
    result = evolve_mod.evolve_trigger(
        conn_wiki, conn_session, {}, session_key="sess-bad", min_rounds=5,
        data_dir=str(tmp_path),
    )
    assert result.ok
    assert result.data["status"] == "rejected"
    assert evolve_dao.get_run(conn_wiki, "sess-bad")["status"] == "rejected"


def test_evolve_idempotent(tmp_path, conn_wiki, conn_session, monkeypatch):
    """幂等：session_key 已在 wiki_evolve（done）→ 跳过不重复提炼。"""
    _mock_llm(monkeypatch, [])
    evolve_dao.create_run(conn_wiki, "done-s")
    evolve_dao.update_run(conn_wiki, "done-s", status="done", action="appended",
                          entry_hash="x", page_id="hb1")
    p = _write_session(tmp_path, "done-s", blocks=6)
    _register_raw(conn_session, "done-s", p)
    result = evolve_mod.evolve_trigger(
        conn_wiki, conn_session, {}, session_key="done-s", min_rounds=5,
        data_dir=str(tmp_path),
    )
    assert result.data["status"] == "skipped"  # 已处理 → 跳过


# ---------- cfg 透传（P0 修复回归：入口层把真实 cfg 的 llm 段透传给 evolve 管线） ----------

def test_evolve_passes_cfg_with_chains_to_llm(tmp_path, conn_wiki, conn_session, monkeypatch):
    """cfg 透传：evolve_trigger 把含顶层 chains 的 cfg 原样传给降级链 _llm_call。

    根因（P0）：入口层传空配置 {} → call_with_fallback 读不到 chains 必抛
    ValueError("未知链名: refinement")。本测试断言 cfg 非空且含 chains 键。
    """
    captured: dict = {}

    def _capture(cfg, prompt):
        captured["cfg"] = cfg
        return json.dumps([{
            "type": "create", "category": "skill/cfg", "title": "cfg透传手册",
            "entry": "验证 cfg 透传",
        }])

    monkeypatch.setattr(evolve_mod, "_llm_call", _capture)
    monkeypatch.setattr(evolve_mod, "_load_prompt", lambda stage: "模板")

    # 模拟真实 cfg 的 llm 段：call_with_fallback 读顶层 chains 键，完整 cfg 的 chains 在 cfg["llm"] 下
    cfg_real = {"chains": {"refinement": [{"provider": "mock", "model": "mock-model"}]}}
    p = _write_session(tmp_path, "sess-cfg", blocks=6)
    _register_raw(conn_session, "sess-cfg", p)
    result = evolve_mod.evolve_trigger(
        conn_wiki, conn_session, cfg_real, session_key="sess-cfg", min_rounds=5,
        data_dir=str(tmp_path),
    )
    assert result.ok
    assert captured["cfg"] is cfg_real  # 同一对象透传，未被替换
    assert "chains" in captured["cfg"]   # 含 chains 键
    assert "refinement" in captured["cfg"]["chains"]  # 降级链能读到 refinement 链


def test_evolve_http_endpoint_passes_cfg(tmp_path, monkeypatch):
    """HTTP 端点层（P0 修复）：/v1/wiki/evolve/trigger 把 app.state.cfg 的 llm 段透传给 evolve_trigger。

    monkeypatch 替换 evolve_trigger 本身，断言第三个位置参数 cfg 非空且含 chains 键。
    """
    from fastapi.testclient import TestClient
    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.operations.errors import OperationResult
    from sgme.server.app import create_app

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key", bearer_token="",
    )
    client = TestClient(app)

    captured: dict = {}

    def _fake_evolve(conn, session_conn, cfg_arg, **kwargs):
        captured["cfg"] = cfg_arg
        return OperationResult.succeed({"status": "skipped"})

    # routes.py 端点函数体内 `from ... import evolve_trigger`，按模块属性解析 → 字符串路径 patch 生效
    monkeypatch.setattr("sgme.operations.evolve.evolve_trigger", _fake_evolve)

    try:
        resp = client.post(
            "/v1/wiki/evolve/trigger",
            json={"session_key": "sess-http", "limit": 5, "min_rounds": 5},
            headers={"X-API-Key": "test-agent-key"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["cfg"] is not None  # 不再是空配置 {}
        assert "chains" in captured["cfg"]   # 含 chains 键
        assert "refinement" in captured["cfg"]["chains"]  # 降级链能读到 refinement 链
    finally:
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)

