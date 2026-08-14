"""tests/test_dream.py：ST-10 Dream 夜间整理（四步编排 + 定时器 + 端点）。

覆盖验收标准（`SGME-Dream夜间整理设计-v0.1.md` §7）：
1. 定时自动执行（到点跑，日报生成）——test_scheduler_*
2. 手动触发可用；执行中重复触发 409——test_trigger_*
3. 模拟漏提炼（status=new 文件）→ 运行后被提炼——test_run_dream_refines_stale_files
4. TTL 超期记忆被标记 expired；>90 天 refined 文件被归档——test_run_dream_ttl_mark /
   test_run_dream_cold_archive
5. 单文件构造失败 → 其余继续，错误进日报 + signal_events——test_run_dream_single_file_failure
6. 全量 pytest 全绿（本文件即新增测试）

测试基建说明：
- 零 LLM 调用：raw 文件用「增量游标 = 消息数」构造（last_refined_seq=n_msgs），
  refine_file 提取到空增量直接标记 refined，不触发 L1 LLM 链路。
- RAW_DIR / 三库 / 日报目录全部隔离到 tmp_path，零真实库/真实 raw 触碰。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, session_dao
from sgme.engine import dream as dream_mod
from sgme.raw import store as raw_store
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}


# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def _stop_dream_scheduler():
    """每个测试后停止 Dream 定时器线程（防跨测试/跨文件连接泄漏崩溃）。

    触发端点会拉起常驻 daemon 线程（持有 conn 引用）；若不在测试结束时
    停止，线程会在其他测试文件 teardown 关闭连接后访问已关闭的 SQLite
    连接 → Windows access violation（实测全量跑崩溃根因）。
    """
    yield
    dream_mod.stop_scheduler(timeout=2.0)


@pytest.fixture
def cfg(tmp_path):
    """load_config + 日报目录隔离到 tmp（防真实 data/reports/ 落盘）。"""
    base = sgme_config.load_config()
    base["dream"] = {**base.get("dream", {}), "report_dir": str(tmp_path / "reports")}
    return base


@pytest.fixture
def conns(tmp_path, cfg, monkeypatch):
    """隔离的 memory.db / session.db / wiki.db + raw/ 根目录。"""
    monkeypatch.setattr(sgme_config, "RAW_DIR", tmp_path / "raw")
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        data_dir=tmp_path / "data",
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具函数 ----------

def _seed_raw_file(
    session_conn: sqlite3.Connection,
    *,
    file_id: str,
    session_key: str = "dream-test",
    started_at: str = "2026-08-01T00:00:00Z",
    status: str = "new",
    write_disk: bool = True,
    n_msgs: int = 2,
) -> None:
    """构造 raw_files 行（+ 可选磁盘 L0 文件）。

    磁盘文件消息数与 last_refined_seq 相等 → refine_file 提取到空增量，
    不触发 L1 LLM 链路（零 LLM 测试基建）。
    """
    msgs = [
        {"timestamp": f"2026-08-01T00:00:0{i}Z", "role": "user", "content": f"消息 {i}"}
        for i in range(1, n_msgs + 1)
    ]
    if write_disk:
        raw_store.write_new_file(
            file_id=file_id,
            session_key=session_key,
            started_at=started_at,
            agent_id=None,
            source_type="session",
            first_messages=msgs,
        )
    path = raw_store.relative_path(file_id) if write_disk else f"raw/sessions/{file_id}.md"
    session_dao.insert_raw_file(
        session_conn,
        file_id=file_id,
        path=path,
        session_key=session_key,
        started_at=started_at,
        status=status,
        last_refined_seq=n_msgs,
        size=0,
        content_hash=None,
    )


def _now_minus_hours(hours: int) -> str:
    """返回 N 小时前的 UTC ISO 时间戳（测试用动态相对时间，防日期漂移）。"""
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_memory(
    mem_conn: sqlite3.Connection,
    *,
    memory_id: str,
    ttl_days: int | None,
    updated_at: str,
    status: str = "active",
) -> None:
    """构造 memories 行（可直接指定 updated_at，供 TTL 标记测试）。"""
    memory_dao.insert_memory(
        mem_conn,
        content=f"记忆 {memory_id}",
        memory_type="persona",
        priority=50,
        time_velocity="dynamic" if ttl_days else "static",
        ttl_days=ttl_days,
        dimension_ids=[],
        memory_id=memory_id,
        updated_at=updated_at,
    )
    if status != "active":
        mem_conn.execute("UPDATE memories SET status=? WHERE memory_id=?", (status, memory_id))
        mem_conn.commit()


def _raw_status(session_conn: sqlite3.Connection, file_id: str) -> str:
    row = session_dao.get_raw_file(session_conn, file_id)
    return row["status"] if row else "MISSING"


def _memory_row(mem_conn: sqlite3.Connection, memory_id: str) -> dict | None:
    row = mem_conn.execute(
        "SELECT * FROM memories WHERE memory_id=?", (memory_id,)
    ).fetchone()
    return dict(row) if row else None


def _report_count(mem_conn: sqlite3.Connection) -> int:
    row = mem_conn.execute("SELECT COUNT(*) AS c FROM dream_reports").fetchone()
    return int(row["c"]) if row else 0


def _wait_until(cond, timeout: float = 5.0) -> bool:
    """轮询等待后台线程产出的条件成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------- 1. 配置段 ----------

def test_dream_config_defaults_and_merge(tmp_path):
    """dream 段缺失 → 默认兜底；部分配置 → 与默认合并；类型非法 → 回退默认。"""
    yml = tmp_path / "sgme.yaml"
    yml.write_text("l2:\n  max_scenes: 10\n", encoding="utf-8")
    cfg = sgme_config.load_sgme_config(yml)
    assert cfg["dream"] == {
        "enabled": True, "schedule": "03:00", "max_files": 200,
        "ttl_mark": True, "archive_days": 90, "report_dir": "data/reports/",
    }

    yml2 = tmp_path / "sgme2.yaml"
    yml2.write_text(
        "dream:\n  enabled: false\n  schedule: \"23:59\"\n  max_files: 5\n"
        "  ttl_mark: false\n  archive_days: 30\n  report_dir: data/x/\n",
        encoding="utf-8",
    )
    cfg2 = sgme_config.load_sgme_config(yml2)
    assert cfg2["dream"]["enabled"] is False
    assert cfg2["dream"]["schedule"] == "23:59"
    assert cfg2["dream"]["max_files"] == 5
    assert cfg2["dream"]["ttl_mark"] is False
    assert cfg2["dream"]["archive_days"] == 30
    assert cfg2["dream"]["report_dir"] == "data/x/"

    yml3 = tmp_path / "sgme3.yaml"
    yml3.write_text("dream:\n  max_files: -3\n  archive_days: \"90\"\n", encoding="utf-8")
    cfg3 = sgme_config.load_sgme_config(yml3)
    assert cfg3["dream"]["max_files"] == 200  # 非法值回退默认
    assert cfg3["dream"]["archive_days"] == 90

    # 可写段白名单含 dream（/v1/admin/config 可管理）
    assert "dream" in sgme_config.CONFIG_SECTIONS
    assert sgme_config.SECTION_KEYS["dream"] == {
        "enabled", "schedule", "max_files", "ttl_mark", "archive_days", "report_dir",
    }


# ---------- 2. 表结构 ----------

def test_connect_memory_creates_dream_reports_table(tmp_path):
    """connect_memory 建 dream_reports 表（幂等，重复连接/迁移无副作用）。"""
    data_dir = tmp_path / "data"
    conn = db_mod.connect_memory(data_dir)
    try:
        tables = db_mod.list_tables(conn)
        assert "dream_reports" in tables
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(dream_reports)").fetchall()]
        for c in ("date", "path", "refined_count", "memory_count", "scene_count",
                  "error_count", "expired_count", "archived_count", "summary", "created_at"):
            assert c in cols, f"缺列 {c}"
        # 幂等：迁移函数重复调用 + 重复连接
        db_mod._migrate_dream_reports_table(conn)
        db_mod._migrate_dream_reports_table(conn)
    finally:
        db_mod.close(conn)
    conn2 = db_mod.connect_memory(data_dir)
    try:
        assert "dream_reports" in db_mod.list_tables(conn2)
    finally:
        db_mod.close(conn2)


# ---------- 3. 四步编排：漏提炼补提炼 + 日报产物 ----------

def test_run_dream_refines_stale_files(conns, cfg):
    """验收 3：模拟漏提炼（status=new 文件）→ 运行后被提炼 + 日报产出 + 落库。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-stale-1")

    summary = dream_mod.run_dream(mem_conn, session_conn, cfg)

    assert summary["status"] == "done"
    assert summary["refined_count"] == 1
    assert summary["error_count"] == 0
    assert summary["date"] == datetime.now().strftime("%Y%m%d")
    assert _raw_status(session_conn, "f-stale-1") == "refined"

    # 日报 MD 落盘（不入 git，隔离目录）
    report_path = summary["report_path"]
    md = Path(report_path).read_text(encoding="utf-8")
    assert f"# Dream 日报 {summary['date']}" in md
    assert "处理文件：1（成功 1 / 失败 0）" in md
    assert "TTL 过期标记：0" in md

    # dream_reports 落库
    assert _report_count(mem_conn) == 1
    row = dream_mod.get_report(mem_conn, summary["date"])
    assert row is not None
    assert row["refined_count"] == 1
    assert row["error_count"] == 0
    assert "提炼 1 文件" in row["summary"]
    assert row["content"] == md


def test_run_dream_same_day_upsert(conns, cfg):
    """同日重复运行 → dream_reports 按日一行 upsert（不产生重复行）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-1")
    dream_mod.run_dream(mem_conn, session_conn, cfg)
    dream_mod.run_dream(mem_conn, session_conn, cfg)
    assert _report_count(mem_conn) == 1


# ---------- 4. 生命周期：TTL 主动标记 + 冷归档 ----------

def test_run_dream_ttl_mark(conns, cfg):
    """验收 4a：超 TTL 的 active 动态维度记忆 → expired（rejected_at/reject_reason）。"""
    mem_conn, session_conn, _ = conns
    _seed_memory(mem_conn, memory_id="m-old-dynamic", ttl_days=1,
                 updated_at="2020-01-01T00:00:00Z")            # 超期 → 应标记
    _seed_memory(mem_conn, memory_id="m-static", ttl_days=None,
                 updated_at="2020-01-01T00:00:00Z")            # 静态维度 → 不标记
    _seed_memory(mem_conn, memory_id="m-rejected", ttl_days=1,
                 updated_at="2020-01-01T00:00:00Z", status="rejected")  # 非 active → 跳过
    _seed_memory(mem_conn, memory_id="m-fresh", ttl_days=1,
                 updated_at=_now_minus_hours(2))            # 未超期 → 不动

    summary = dream_mod.run_dream(mem_conn, session_conn, cfg)

    assert summary["expired_count"] == 1
    old = _memory_row(mem_conn, "m-old-dynamic")
    assert old["status"] == "expired"
    assert old["reject_reason"] == "dream_ttl_expired"
    assert old["rejected_at"] is not None
    assert _memory_row(mem_conn, "m-static")["status"] == "active"
    assert _memory_row(mem_conn, "m-rejected")["status"] == "rejected"
    assert _memory_row(mem_conn, "m-fresh")["status"] == "active"


def test_run_dream_ttl_mark_disabled(conns, cfg):
    """ttl_mark=false → 生命周期 A 跳过。"""
    mem_conn, session_conn, _ = conns
    _seed_memory(mem_conn, memory_id="m-old", ttl_days=1, updated_at="2020-01-01T00:00:00Z")
    cfg2 = {**cfg, "dream": {**cfg["dream"], "ttl_mark": False}}
    summary = dream_mod.run_dream(mem_conn, session_conn, cfg2)
    assert summary["expired_count"] == 0
    assert _memory_row(mem_conn, "m-old")["status"] == "active"


def test_run_dream_cold_archive(conns, cfg):
    """验收 4b：>90 天 refined 文件 → archived；未超期 / new 不动。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-old", status="refined",
                   started_at="2020-01-01T00:00:00Z")
    _seed_raw_file(session_conn, file_id="f-recent", status="refined",
                   started_at="2026-08-09T00:00:00Z")
    # new 文件（近期）：① 被抽取提炼为 refined，started_at 未超期 → ③ 不归档
    _seed_raw_file(session_conn, file_id="f-fresh-new", status="new",
                   started_at="2026-08-09T00:00:00Z")

    summary = dream_mod.run_dream(mem_conn, session_conn, cfg)

    assert summary["archived_count"] == 1
    assert _raw_status(session_conn, "f-old") == "archived"
    assert _raw_status(session_conn, "f-recent") == "refined"
    assert _raw_status(session_conn, "f-fresh-new") == "refined"


# ---------- 5. 失败预案：单文件失败继续 + 阶段失败标注 ----------

def test_run_dream_single_file_failure_continues(conns, cfg):
    """验收 5：单文件失败 → 其余继续；错误进日报 + dream_error 信号。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-broken", write_disk=False)  # 磁盘缺失 → L0 解析失败
    _seed_raw_file(session_conn, file_id="f-good")

    summary = dream_mod.run_dream(mem_conn, session_conn, cfg)

    assert summary["refined_count"] == 1
    assert summary["error_count"] == 1
    assert _raw_status(session_conn, "f-broken") == "error"
    assert _raw_status(session_conn, "f-good") == "refined"
    assert any(e["file_id"] == "f-broken" for e in summary["errors"])

    # 日报含错误条目 + dream_reports.error_count
    md = Path(summary["report_path"]).read_text(encoding="utf-8")
    assert "f-broken" in md
    row = dream_mod.get_report(mem_conn, summary["date"])
    assert row["error_count"] == 1

    # error 级事件进 signal_events（type='dream_error'）
    ev = mem_conn.execute(
        "SELECT * FROM signal_events WHERE type='dream_error' AND source='dream'"
    ).fetchone()
    assert ev is not None
    import json
    payload = json.loads(ev["payload"])
    assert payload["date"] == summary["date"]
    assert payload["error_count"] == 1


def test_run_dream_stage_failure_continues(conns, cfg):
    """阶段失败（memories 表缺失 → TTL 标记 SQL 异常）→ 该阶段中止，其余继续并标注。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-good")
    mem_conn.execute("DROP TABLE memories")
    mem_conn.commit()

    summary = dream_mod.run_dream(mem_conn, session_conn, cfg)

    assert summary["status"] == "done"
    assert summary["refined_count"] == 1            # ① 抽取不受影响
    assert summary["expired_count"] == 0            # ③A 阶段中止
    assert any("TTL 主动标记失败" in s for s in summary["stage_errors"])
    assert _report_count(mem_conn) == 1             # ④ 日报仍产出
    md = Path(summary["report_path"]).read_text(encoding="utf-8")
    assert "阶段异常" in md
    # 池总量统计失败 → 回退 0 不阻塞
    assert summary["total_memories"] == 0


# ---------- 6. API：手动触发 202 / 重复 409 / 日报查询 ----------

def test_trigger_async_202_and_reports_endpoints(client, conns, cfg):
    """验收 2：手动触发 202 异步 → 后台跑完 → 日报列表/详情可查。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-api")

    resp = client.post("/v1/admin/dream/trigger", headers=ADMIN_HEADERS)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["triggered"] == "async"
    assert body["status"] == "queued"

    assert _wait_until(lambda: _report_count(mem_conn) >= 1), "后台 Dream 未在超时内完成"
    assert _raw_status(session_conn, "f-api") == "refined"
    date_label = datetime.now().strftime("%Y%m%d")

    # 列表（date 倒序）
    resp = client.get("/v1/admin/dream/reports", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert body["reports"][0]["date"] == date_label

    # 单日详情（含 MD 正文）
    resp = client.get(f"/v1/admin/dream/reports/{date_label}", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    r = resp.json()["report"]
    assert r["date"] == date_label
    assert r["refined_count"] == 1
    assert "# Dream 日报" in r["content"]

    # 不存在日期 → 404
    resp = client.get("/v1/admin/dream/reports/20990101", headers=ADMIN_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"

    # 参数校验 → 400
    resp = client.get("/v1/admin/dream/reports?page=0", headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    resp = client.get("/v1/admin/dream/reports?limit=999", headers=ADMIN_HEADERS)
    assert resp.status_code == 400

    # Agent Key / 无 Key → 403
    resp = client.post("/v1/admin/dream/trigger", headers=AGENT_HEADERS)
    assert resp.status_code == 403
    resp = client.get("/v1/admin/dream/reports", headers=AGENT_HEADERS)
    assert resp.status_code == 403
    resp = client.get("/v1/admin/dream/reports", headers={})
    assert resp.status_code == 403


def test_trigger_conflict_409(client, conns, cfg):
    """验收 2：执行中重复触发 → 409 ERR_CONFLICT（防重入）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-c")

    # 先跑完一次（后台线程），确保锁空闲
    resp = client.post("/v1/admin/dream/trigger", headers=ADMIN_HEADERS)
    assert resp.status_code == 202
    assert _wait_until(lambda: _report_count(mem_conn) >= 1)

    # 占用执行锁模拟执行中 → 重复触发 409
    assert dream_mod.RUN_LOCK.acquire(blocking=False)
    try:
        resp = client.post("/v1/admin/dream/trigger", headers=ADMIN_HEADERS)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "ERR_CONFLICT"
    finally:
        dream_mod.RUN_LOCK.release()

    # 锁释放后恢复 202
    resp = client.post("/v1/admin/dream/trigger", headers=ADMIN_HEADERS)
    assert resp.status_code == 202
    assert _wait_until(lambda: _report_count(mem_conn) >= 1)


def test_reports_pagination(client, conns):
    """日报列表分页：date 倒序 + total。"""
    mem_conn, _, _ = conns
    for i, d in enumerate(["20260808", "20260809", "20260810"], start=1):
        mem_conn.execute(
            "INSERT INTO dream_reports (date, path, refined_count, memory_count,"
            " scene_count, error_count, expired_count, archived_count, summary, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d, f"data/reports/dream-{d}.md", i, 0, 0, 0, 0, 0, f"摘要 {d}",
             "2026-08-10T00:00:00Z"),
        )
    mem_conn.commit()

    resp = client.get("/v1/admin/dream/reports?page=1&limit=2", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [r["date"] for r in body["reports"]] == ["20260810", "20260809"]

    resp = client.get("/v1/admin/dream/reports?page=2&limit=2", headers=ADMIN_HEADERS)
    assert [r["date"] for r in resp.json()["reports"]] == ["20260808"]


# ---------- 7. 调度：定时自动执行 ----------

def test_scheduler_loop_fires_at_schedule(conns, cfg, monkeypatch, tmp_path):
    """验收 1：到点自动执行（定时器线程按 schedule 触发 run_dream → 日报生成）。

    v1.0 连接隔离：调度器线程自建独立连接（data_dir），写同一测试库文件；
    测试连接（conns）读可见性依赖提交（SQLite 文件级一致性）。
    """
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-sched")
    monkeypatch.setattr(dream_mod, "_seconds_until", lambda s: 0.05)
    stop = threading.Event()

    t = threading.Thread(
        target=dream_mod._scheduler_loop, args=(cfg, stop, tmp_path / "data"),
        daemon=True,
    )
    t.start()
    try:
        assert _wait_until(lambda: _report_count(mem_conn) >= 1, timeout=5), \
            "定时器未在超时内触发 Dream"
        assert _raw_status(session_conn, "f-sched") == "refined"
    finally:
        stop.set()
        t.join(timeout=2)
    assert not t.is_alive()


def test_scheduler_respects_disabled_and_empty_schedule(conns, cfg, monkeypatch, tmp_path):
    """enabled=false → 到点跳过；schedule 空 → 不自动只手动。"""
    mem_conn, session_conn, _ = conns

    # enabled=false
    cfg_disabled = {**cfg, "dream": {**cfg["dream"], "enabled": False}}
    monkeypatch.setattr(dream_mod, "_seconds_until", lambda s: 0.05)
    stop = threading.Event()
    t = threading.Thread(
        target=dream_mod._scheduler_loop,
        args=(cfg_disabled, stop, tmp_path / "data"), daemon=True,
    )
    t.start()
    try:
        time.sleep(0.3)
        assert _report_count(mem_conn) == 0
    finally:
        stop.set()
        t.join(timeout=2)

    # schedule 空串 = 不自动只手动（循环停在长眠分支，不执行）
    cfg_empty = {**cfg, "dream": {**cfg["dream"], "schedule": ""}}
    stop2 = threading.Event()
    t2 = threading.Thread(
        target=dream_mod._scheduler_loop,
        args=(cfg_empty, stop2, tmp_path / "data"), daemon=True,
    )
    t2.start()
    try:
        time.sleep(0.2)
        assert _report_count(mem_conn) == 0
    finally:
        stop2.set()
        t2.join(timeout=2)


def test_ensure_scheduler_idempotent(conns, cfg, tmp_path):
    """ensure_scheduler 幂等：首次启动返回 True，重复调用返回 False。

    前面的触发类测试已通过 trigger 端点拉起过全局定时器，先重置模块全局
    保证本用例与执行顺序无关。
    """
    with dream_mod._scheduler_lock:
        dream_mod._scheduler_thread = None
    assert dream_mod.ensure_scheduler(cfg, data_dir=tmp_path / "data") is True
    try:
        assert dream_mod.ensure_scheduler(cfg, data_dir=tmp_path / "data") is False
        assert dream_mod._scheduler_thread is not None
        assert dream_mod._scheduler_thread.is_alive()
        assert dream_mod._scheduler_thread.name == "sgme-dream-scheduler"
    finally:
        # 幂等测试用线程：置位停止并等待退出（防残留线程干扰其他用例）
        dream_mod.stop_scheduler(timeout=2.0)


def test_seconds_until():
    """下次 HH:MM 计算：正常格式 / 已过时刻顺延次日 / 非法格式回退。"""
    now = datetime.now()
    future = now.replace(second=0, microsecond=0)
    future = future.replace(minute=(now.minute + 2) % 60,
                            hour=(now.hour + (1 if now.minute + 2 >= 60 else 0)) % 24)
    secs = dream_mod._seconds_until(future.strftime("%H:%M"))
    assert 60 <= secs <= 86400

    # 当前时刻已过 → 顺延到明天同一时刻（不超过 24h）
    secs = dream_mod._seconds_until(now.strftime("%H:%M"))
    assert 0 < secs <= 86400

    assert dream_mod._seconds_until("not-a-time") == 3600.0


# ---------- 8. 其他集成 ----------

def test_config_api_includes_dream_section(client):
    """GET /v1/admin/config 含 dream 段（可写段白名单接线）。"""
    resp = client.get("/v1/admin/config", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert "dream" in resp.json()["config"]
    assert resp.json()["config"]["dream"]["schedule"] == "03:00"


def test_run_dream_rerun_after_crash_semantics(conns, cfg):
    """整体崩溃可重入：error 状态文件下次运行不再重复计为失败（幂等语义）。"""
    mem_conn, session_conn, _ = conns
    _seed_raw_file(session_conn, file_id="f-broken", write_disk=False)
    summary1 = dream_mod.run_dream(mem_conn, session_conn, cfg)
    assert summary1["error_count"] == 1

    # 第二次运行：status=error 的文件不在 status=new 队列中 → 不再处理
    summary2 = dream_mod.run_dream(mem_conn, session_conn, cfg)
    assert summary2["refined_count"] == 0
    assert summary2["error_count"] == 0
