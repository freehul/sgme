"""tests/test_refine_pause.py：提炼总闸（refine.paused，2026-11-14）。

验收（对应用户需求「提炼操作增加暂停开关」）：
1. 配置层：DEFAULT_REFINE_CONFIG 有 paused；sgme.yaml 的 paused 参与合并；
   SECTION_KEYS 白名单包含 paused（否则 config 接口写进去会被静默丢弃）。
2. 执行层：refine.paused=true 时 refine_file 直接延后——不解析 L0、不调 LLM，
   文件保持 status=new、fail_count 仍为 0。
3. 批扫：batch_scan 整轮跳过（status=paused，scanned=0，不扫队列）。
4. 后台 worker：暂停期直接返回，不建连接、不扫队列（运行中的批次在循环内
   每文件复查，见 pipeline 循环体）。
5. append 联动：暂停期不起线程、不触发提炼。

背景：此前的两个开关（refine_on_append / batch_scan.enabled）只挡自动触发，
挡不住 POST /v1/admin/refine/trigger-async（dsh 适配器每 turn 结束无条件调一次），
所以停提炼只能重启 Gateway，重启后又被点着。
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.engine import batch_scan as batch_scan_mod
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod
from sgme.raw import store


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


def _setup(raw_dir, session_conn, file_id="f-pause"):
    msgs = [
        {"timestamp": "2026-11-14T10:00:00Z", "role": "user", "content": "我在给 SGME 加提炼总闸"},
        {"timestamp": "2026-11-14T10:01:00Z", "role": "assistant", "content": "了解"},
    ]
    session_key = f"sess_{file_id}"
    store.write_new_file(
        file_id=file_id, session_key=session_key,
        started_at="2026-11-14T10:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key=session_key, started_at="2026-11-14T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


def _paused(cfg):
    """返回一份 paused=true 的配置副本（不污染共享 cfg）。"""
    import copy

    clone = copy.deepcopy(cfg)
    clone.setdefault("refine", {})["paused"] = True
    return clone


# ---------------- 1. 配置层 ----------------

def test_default_config_has_paused_off():
    assert config.DEFAULT_REFINE_CONFIG["paused"] is False


def test_merge_refine_config_picks_up_paused():
    merged = config._merge_refine_config({"paused": True})
    assert merged["paused"] is True
    # 非布尔值不采信（防 "false" / 1 这类脏值把总闸误开）
    merged2 = config._merge_refine_config({"paused": "true"})
    assert merged2["paused"] is False


def test_section_keys_allow_paused():
    """白名单缺 paused 会让 config 接口的写入被静默丢弃（历史多次踩坑）。"""
    assert "paused" in config.SECTION_KEYS["refine"]
    assert config.filter_keys("refine", {"paused": True}) == {"paused": True}


def test_apply_section_sets_paused():
    cfg = {"refine": {"paused": False}}
    config.apply_section(cfg, "refine", {"paused": True})
    assert cfg["refine"]["paused"] is True


# ---------------- 2. 执行层：refine_file ----------------

def test_is_paused_reads_flag(cfg):
    assert refine_mod.is_paused(cfg) is False
    assert refine_mod.is_paused(_paused(cfg)) is True
    # 缺段 / 非法 cfg 不得抛异常
    assert refine_mod.is_paused({}) is False
    assert refine_mod.is_paused({"refine": None}) is False


def test_refine_file_defers_when_paused(raw_dir, mem_conn, session_conn, cfg, monkeypatch):
    fid = _setup(raw_dir, session_conn)

    def must_not_parse(*a, **k):
        raise AssertionError("暂停期不应解析 L0 文件")

    monkeypatch.setattr(refine_mod.store, "parse_file", must_not_parse)
    res = refine_mod.refine_file(fid, mem_conn, session_conn, _paused(cfg))

    assert res.status == "deferred" and res.deferred is True
    assert res.error == "refine_paused"
    assert res.memories == []
    row = session_dao.get_raw_file(session_conn, fid)
    assert row["status"] == "new"
    assert int(row.get("fail_count") or 0) == 0
    assert not row.get("last_error")


# ---------------- 3. 批扫 ----------------

def test_batch_scan_skips_whole_round_when_paused(mem_conn, session_conn, cfg, raw_dir):
    _setup(raw_dir, session_conn, file_id="f-pause-batch")

    def must_not_scan(*a, **k):
        raise AssertionError("暂停期不应扫描队列")

    from sgme.data import session_dao as sd

    orig = sd.list_by_status
    try:
        sd.list_by_status = must_not_scan  # type: ignore[assignment]
        out = batch_scan_mod.run_batch_scan(mem_conn, session_conn, _paused(cfg))
    finally:
        sd.list_by_status = orig  # type: ignore[assignment]

    assert out["status"] == "paused"
    assert out["scanned"] == 0 and out["refined"] == 0
    assert session_dao.get_raw_file(session_conn, "f-pause-batch")["status"] == "new"


# ---------------- 4. 后台 worker ----------------

def test_async_worker_returns_immediately_when_paused(cfg, monkeypatch):
    def must_not_init(*a, **k):
        raise AssertionError("暂停期不应建数据库连接")

    monkeypatch.setattr(db_mod, "init_databases", must_not_init)
    # 不抛异常即通过（入口检查发生在建连接之前）
    pipeline_mod.async_refine_worker(None, 50, None, _paused(cfg))


# ---------------- 5. append 联动 ----------------

def test_append_link_does_not_start_thread_when_paused(cfg, monkeypatch):
    import threading

    started = {"n": 0}
    orig_thread = threading.Thread

    class _Spy(orig_thread):  # type: ignore[misc, valid-type]
        def start(self):  # noqa: D102
            started["n"] += 1
            return super().start()

    monkeypatch.setattr(threading, "Thread", _Spy)
    paused_cfg = _paused(cfg)
    paused_cfg["refine"]["refine_on_append"] = True  # 即便入口开关开着，总闸优先
    pipeline_mod._maybe_refine_on_append(
        paused_cfg, None, None, "f-pause-append", new_messages=999,
    )
    assert started["n"] == 0

# ---------------- 6. 端点：pause / resume / 状态 ----------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    """构造带 admin 端点的 app（样板同 tests/test_config_v07.py）。"""
    from fastapi.testclient import TestClient

    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = config.load_config()
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    app = create_app(
        cfg=cfg, mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    yield TestClient(app)
    for conn in (mem, session, wiki):
        db_mod.close(conn)


def _h():
    return {"X-API-Key": "test-admin"}


def test_pause_endpoint_toggles_flag(api):
    r = api.post("/v1/admin/refine/pause", headers=_h())
    assert r.status_code == 200
    assert api.get("/v1/admin/refine/pause", headers=_h()).json()["paused"] is True

    r = api.post("/v1/admin/refine/resume", headers=_h())
    assert r.status_code == 200
    assert api.get("/v1/admin/refine/pause", headers=_h()).json()["paused"] is False


def test_pause_status_reports_queue_and_entry_switches(api):
    body = api.get("/v1/admin/refine/pause", headers=_h()).json()
    assert body["paused"] is False
    assert isinstance(body["queue_depth"], int)
    assert "refine_on_append" in body and "batch_scan_enabled" in body


def test_paused_is_visible_via_config_section(api):
    """config 段读路径也要看得到 paused（运维直接看 /v1/admin/config/refine）。"""
    api.post("/v1/admin/refine/pause", headers=_h())
    body = api.get("/v1/admin/config/refine", headers=_h()).json()
    assert body["config"]["paused"] is True


def test_health_reports_paused(api):
    api.post("/v1/admin/refine/pause", headers=_h())
    body = api.get("/v1/health").json()
    assert body["refinement"]["paused"] is True
