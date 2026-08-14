"""tests/test_ingest_persist.py：ingest 任务持久化测试（0.8 T-13）。

覆盖：
1. ingest_tasks 建表幂等（connect_wiki 重复调用无副作用 + 列结构符合数据模型）
2. ingest_dao CRUD（create/get/update_status/list_tasks + 非法状态拒绝）
3. 状态流转（queued → done / error，终态落 finished_at）
4. 启动恢复（模拟：running → error 标记中断；queued 保留可重跑；done/error 不动；幂等）
5. 路由落库（POST 创建即落库；GET 读库 + result_page_id → page_id 契约映射；
   重启恢复惰性触发；参数校验/404 契约零破坏）
"""
from __future__ import annotations

import importlib
import time

import pytest
from fastapi.testclient import TestClient

from sgme.data import db as db_mod
from sgme.data import ingest_dao
from sgme.server.app import create_app
from sgme.wiki import routes as wiki_routes


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect_wiki(tmp_path / "data")
    yield c
    db_mod.close(c)


@pytest.fixture
def app(tmp_path):
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    cfg = {"paths": {"data_dir": str(tmp_path / "data")}, "dimensions": {}, "aliases": {}}
    application = create_app(
        cfg=cfg, mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
    )
    yield application
    db_mod.close(mem)
    db_mod.close(session)
    db_mod.close(wiki)


@pytest.fixture
def client(app):
    return TestClient(app)


AGENT = {"X-API-Key": "test-agent"}


def _wait_finished(client, task_id, max_iters=50):
    """轮询任务直至离开 queued，返回最终状态 dict。"""
    st = {"status": "queued"}
    for _ in range(max_iters):
        st = client.get(f"/v1/wiki/ingest/{task_id}", headers=AGENT).json()
        if st["status"] != "queued":
            return st
        time.sleep(0.05)
    return st


# ---------- 建表 ----------

def test_ingest_tasks_table_idempotent(tmp_path):
    """建表幂等：重复 connect_wiki 无副作用；列结构符合数据模型。"""
    d = tmp_path / "data"
    c1 = db_mod.connect_wiki(d)
    cols1 = [r[1] for r in c1.execute("PRAGMA table_info(ingest_tasks)").fetchall()]
    pk = [r[1] for r in c1.execute("PRAGMA table_info(ingest_tasks)").fetchall() if r[5] == 1]
    idx = [r[1] for r in c1.execute("PRAGMA index_list(ingest_tasks)").fetchall()]
    n_tables_1 = len(db_mod.list_tables(c1))
    db_mod.close(c1)

    c2 = db_mod.connect_wiki(d)
    cols2 = [r[1] for r in c2.execute("PRAGMA table_info(ingest_tasks)").fetchall()]
    n_tables_2 = len(db_mod.list_tables(c2))
    db_mod.close(c2)

    assert cols1 == cols2  # 幂等：重复建表列不变
    assert cols1 == [
        "task_id", "source_type", "source_ref", "title", "status",
        "result_page_id", "error", "created_at", "updated_at", "finished_at",
    ]
    assert pk == ["task_id"]  # 主键符合数据模型
    assert "idx_ingest_tasks_status" in idx
    assert n_tables_1 == n_tables_2  # 不产生重复表


def test_ingest_tasks_table_in_wiki_db(conn):
    """ingest_tasks 落在 wiki.db（与 wiki_pages 同库，路由同一连接读写）。"""
    assert "ingest_tasks" in db_mod.list_tables(conn)
    assert "wiki_pages" in db_mod.list_tables(conn)


# ---------- CRUD ----------

def test_crud_create_get(conn):
    task = ingest_dao.create_task(
        conn, "t1", source_type="text", source_ref="内容A", title="标题",
    )
    assert task["task_id"] == "t1"
    assert task["status"] == "queued"
    assert task["created_at"] and task["updated_at"]
    assert task["finished_at"] is None
    # 查询
    got = ingest_dao.get_task(conn, "t1")
    assert got["source_type"] == "text"
    assert got["source_ref"] == "内容A"
    assert got["title"] == "标题"
    assert ingest_dao.get_task(conn, "不存在") is None


def test_crud_update_status_and_list(conn):
    ingest_dao.create_task(conn, "t1", source_type="text", source_ref="a")
    ingest_dao.create_task(conn, "t2", source_type="url", source_ref="https://b")
    ingest_dao.update_status(conn, "t2", status="done", page_id="p2")
    # 列表 + status 过滤（守护重试策略扫描入口）
    assert {t["task_id"] for t in ingest_dao.list_tasks(conn)} == {"t1", "t2"}
    assert [t["task_id"] for t in ingest_dao.list_tasks(conn, status="done")] == ["t2"]
    assert [t["task_id"] for t in ingest_dao.list_tasks(conn, status="queued")] == ["t1"]
    # 非法状态拒绝
    with pytest.raises(ValueError):
        ingest_dao.update_status(conn, "t1", status="hacked")
    # 不存在的任务返回 False
    assert ingest_dao.update_status(conn, "nope", status="done") is False


# ---------- 状态流转 ----------

def test_status_flow_done(conn):
    ingest_dao.create_task(conn, "t1", source_type="text", source_ref="x")
    assert ingest_dao.update_status(conn, "t1", status="running") is True
    t = ingest_dao.get_task(conn, "t1")
    assert t["status"] == "running"
    assert t["finished_at"] is None  # 非终态不落 finished_at
    assert ingest_dao.update_status(conn, "t1", status="done", page_id="p1") is True
    t = ingest_dao.get_task(conn, "t1")
    assert t["status"] == "done"
    assert t["result_page_id"] == "p1"
    assert t["finished_at"]  # 终态落 finished_at


def test_status_flow_error(conn):
    ingest_dao.create_task(conn, "t1", source_type="url", source_ref="https://x")
    assert ingest_dao.update_status(conn, "t1", status="error", error="LLM 提取失败") is True
    t = ingest_dao.get_task(conn, "t1")
    assert t["status"] == "error"
    assert t["error"] == "LLM 提取失败"
    assert t["finished_at"]


# ---------- 启动恢复 ----------

def test_recover_interrupted_tasks(conn):
    """模拟重启恢复：running → error 标记中断；queued 保留可重跑；done/error 不动。"""
    # 上次进程遗留状态：queued 未执行 / running 执行中断 / done 完成 / error 失败
    ingest_dao.create_task(conn, "t_queued", source_type="text", source_ref="q")
    ingest_dao.create_task(conn, "t_running", source_type="url", source_ref="https://r")
    ingest_dao.update_status(conn, "t_running", status="running")
    ingest_dao.create_task(conn, "t_done", source_type="text", source_ref="d")
    ingest_dao.update_status(conn, "t_done", status="done", page_id="p9")
    ingest_dao.create_task(conn, "t_error", source_type="text", source_ref="e")
    ingest_dao.update_status(conn, "t_error", status="error", error="旧错误")

    result = ingest_dao.recover_interrupted_tasks(conn)
    assert result == {"kept_queued": 1, "marked_error": 1}

    # queued → 置回 queued（可重跑）
    assert ingest_dao.get_task(conn, "t_queued")["status"] == "queued"
    # running → error（标记中断）+ 落 finished_at
    t = ingest_dao.get_task(conn, "t_running")
    assert t["status"] == "error"
    assert "中断" in t["error"]
    assert t["finished_at"]
    # done/error 终态不动（error 文案不被覆盖）
    assert ingest_dao.get_task(conn, "t_done")["status"] == "done"
    t = ingest_dao.get_task(conn, "t_error")
    assert t["status"] == "error"
    assert t["error"] == "旧错误"

    # 恢复幂等：再跑一次无副作用
    assert ingest_dao.recover_interrupted_tasks(conn) == {"kept_queued": 1, "marked_error": 0}


# ---------- 路由落库 ----------

def test_ingest_route_persists_task(client, monkeypatch):
    """POST 创建即落库（不等后台线程）；GET 读库 + result_page_id → page_id 契约映射。"""
    refinery_mod = importlib.import_module("sgme.refinery")

    class FakeResult:
        ok = True
        error = None
        source_type = "text"
        title = "提炼页"
        content = "# 提炼内容"
        category = "tech"
        tags = ["AI"]
        ingested_at = "2026-08-08T00:00:00Z"

    def fake_refine(source, *a, **kw):
        assert source == "持久化测试材料"
        return FakeResult()

    monkeypatch.setattr(refinery_mod, "refine", fake_refine)

    r = client.post("/v1/wiki/ingest", json={
        "source_type": "text", "content": "持久化测试材料", "title": "持久化标题",
    }, headers=AGENT)
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    # 创建即落库（路由同步写 ingest_tasks 后才起后台线程）
    conn = client.app.state.wiki_conn
    row = ingest_dao.get_task(conn, task_id)
    assert row is not None
    assert row["source_type"] == "text"
    assert row["source_ref"] == "持久化测试材料"
    assert row["title"] == "持久化标题"
    assert row["status"] in ("queued", "done")  # 线程可能已推进，但行必须已存在

    # 等待后台线程完成
    st = _wait_finished(client, task_id)
    assert st["status"] == "done"
    assert st["page_id"]  # 契约字段 page_id 由 result_page_id 映射而来
    assert "result_page_id" not in st  # 表列名不出现在对外响应
    assert st["source_type"] == "text"
    assert st["title"] == "持久化标题"
    # 落库终态
    row = ingest_dao.get_task(conn, task_id)
    assert row["status"] == "done"
    assert row["result_page_id"] == st["page_id"]
    assert row["finished_at"]


def test_ingest_route_error_persisted(client, monkeypatch):
    """refine 失败 → 任务 error 落库（含 error 文案与 finished_at）。"""
    refinery_mod = importlib.import_module("sgme.refinery")

    class FakeFail:
        ok = False
        error = "LLM 提取失败"
        source_type = "text"

    monkeypatch.setattr(refinery_mod, "refine", lambda source, *a, **kw: FakeFail())

    r = client.post("/v1/wiki/ingest", json={
        "source_type": "text", "content": "坏材料",
    }, headers=AGENT)
    task_id = r.json()["task_id"]
    st = _wait_finished(client, task_id)
    assert st["status"] == "error"
    assert "LLM 提取失败" in st["error"]
    row = ingest_dao.get_task(client.app.state.wiki_conn, task_id)
    assert row["status"] == "error"
    assert row["error"] == "LLM 提取失败"
    assert row["finished_at"]


def test_ingest_route_recovery_on_first_touch(client, monkeypatch):
    """模拟重启：进程内恢复开关复位后，首次触碰 ingest API 即执行启动恢复。"""
    conn = client.app.state.wiki_conn
    # 直接落库模拟上次进程遗留任务
    ingest_dao.create_task(conn, "old_queued", source_type="text", source_ref="q")
    ingest_dao.create_task(conn, "old_running", source_type="url", source_ref="https://r")
    ingest_dao.update_status(conn, "old_running", status="running")

    # 模拟进程重启：复位惰性恢复开关
    monkeypatch.setattr(wiki_routes, "_RECOVERED", False)

    # 首次 GET 触发恢复：running 标记中断，且对外可见
    st = client.get("/v1/wiki/ingest/old_running", headers=AGENT).json()
    assert st["status"] == "error"
    assert "中断" in st["error"]
    # queued 保留可重跑
    st = client.get("/v1/wiki/ingest/old_queued", headers=AGENT).json()
    assert st["status"] == "queued"
    # 恢复只执行一次（开关置位）
    assert wiki_routes._RECOVERED is True


def test_ingest_route_contract_unchanged(client):
    """既有端点契约零破坏：参数校验 400 / 任务不存在 404 行为不变。"""
    r = client.post("/v1/wiki/ingest", json={"source_type": "video"}, headers=AGENT)
    assert r.status_code == 400
    r = client.post("/v1/wiki/ingest", json={"source_type": "text"}, headers=AGENT)
    assert r.status_code == 400
    r = client.get("/v1/wiki/ingest/nonexistent", headers=AGENT)
    assert r.status_code == 404
