"""tests/test_operations_backup.py：operations 层 backup 测试（0.8 T-8）。

参照 tests/test_operations_health.py 的 fixture 范式。覆盖：

1. 操作函数返回 OperationResult（backup_create / backup_list / backup_restore）
2. data 超集（HTTP 形态）字段完整且**顺序**正确
3. restore 快照不存在 → OperationResult(ok=False, ERR_NOT_FOUND)
4. **契约等价性**（最关键）：改造后端点响应 vs 改造前 routes_backup 冻结的
   字段集合逐字段一致（键序 + 取值 + 错误结构；改造前响应体已用冻结脚本
   在基线 9563646 上实证，见模块内契约常量注释）
5. 权限契约：Agent Key 调 backup 端点仍 403
6. restore 端点完成后 app.state 连接已交换为新连接（入口层职责）

⚠️ 连接生命周期：restore 会关闭 fixture 创建的旧连接并重开新连接——
restore 相关测试结束后必须显式关闭 app.state 中的新连接，
否则 Windows 下 tmp_path 清理会撞 SQLite 文件锁（WinError 32）。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.backup import manager
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations.backup import (
    backup_create as op_backup_create,
    backup_list as op_backup_list,
    backup_restore as op_backup_restore,
    http_payload,
)
from sgme.operations.errors import ERR_NOT_FOUND, OperationResult
from sgme.server.app import create_app

# ---------- 改造前冻结契约（基线 9563646 routes_backup.py 逐字段抄录，
# 任何变动即破坏性变更；键序 = 改造前响应体键序，勿调整） ----------

# POST /v1/admin/backup 与 /create 响应体顶层键序
CREATE_TOP_KEYS = ["snapshot_id", "level", "path", "created_at", "files", "push_remote"]
# GET /v1/admin/backup/list 响应体
LIST_TOP_KEYS = ["snapshots", "total"]
LIST_SNAPSHOT_KEYS = ["snapshot_id", "level", "path"]
# POST /v1/admin/backup/restore 响应体（_new_conns 不得出现——入口层私有传输字段）
RESTORE_TOP_KEYS = ["restored", "pre_restore_snapshot"]
RESTORE_RESTORED_KEYS = ["files", "snapshot_id"]
# 错误结构
ERROR_KEYS = ["error"]
ERROR_BODY_KEYS = ["code", "message"]


# ---------- fixtures（照抄 test_operations_health.py 范式） ----------

@pytest.fixture
def cfg(tmp_path):
    c = sgme_config.load_config()
    # 备份目录 / data_dir 全部隔离到 tmp_path：防污染项目 data/backups，
    # 防 restore 直连全局 data/ 撞 Gateway 的 memory.db-wal 锁（WinError 32）
    c["backup"] = {
        "dir": str(tmp_path / "backups"),
        "schedule": "0 2 * * *",
        "raw_cold_days": 90,
        "remote_dir": None,
    }
    c["paths"]["data_dir"] = str(tmp_path / "data")
    return c


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（monkeypatch sgme_config.RAW_DIR）。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    (rd / "session1.md").write_text("# backup ops test", encoding="utf-8")
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path；restore 测试中连接会被关闭，teardown 兜底）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    for conn in (mem_conn, session_conn, wiki_conn):
        try:
            conn.close()
        except Exception:
            pass


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
        data_dir=tmp_path / "data",
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture(autouse=True)
def _stop_backup_scheduler_after():
    """每个测试后停止 backup_scheduler 常驻线程（防跨文件连接泄漏）。"""
    yield
    from sgme.engine import backup_scheduler
    backup_scheduler.stop_scheduler(timeout=2.0)


@pytest.fixture
def client(app):
    return TestClient(app)


ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


def _close_conns(*conns: sqlite3.Connection) -> None:
    """关闭 restore 重开的新连接（防 Windows tmp_path 清理撞文件锁）。"""
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


# ---------- 1. 返回类型 ----------

def test_backup_create_returns_operation_result_ok(conns, cfg):
    """operations.backup_create() 返回 OperationResult 且 ok=True。"""
    # Arrange / Act
    mem_conn, session_conn, wiki_conn = conns
    res = op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full")

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert isinstance(res.data, dict)


def test_backup_list_returns_operation_result_ok(conns, cfg):
    """operations.backup_list() 返回 OperationResult 且 ok=True。"""
    # Arrange：先造一个快照，列表才有内容
    mem_conn, session_conn, wiki_conn = conns
    op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full")

    # Act
    res = op_backup_list(cfg)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.data["total"] == 1


def test_backup_restore_returns_operation_result_ok(conns, cfg):
    """operations.backup_restore() 返回 OperationResult 且 ok=True（含 _new_conns）。"""
    # Arrange：先造快照
    mem_conn, session_conn, wiki_conn = conns
    snap = op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full").data

    # Act
    res = op_backup_restore(
        cfg, mem_conn, session_conn, wiki_conn, snapshot_id=snap["snapshot_id"],
    )

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.data["pre_restore_snapshot"].startswith("pre_restore_")
    _close_conns(*res.data["_new_conns"])


# ---------- 2. data 超集字段完整（HTTP 形态，键序与改造前一致） ----------

def test_create_data_http_shape_complete(conns, cfg):
    """create 超集键序 = CREATE_TOP_KEYS，push_remote 未配置 remote_dir 时跳过。"""
    # Arrange / Act
    mem_conn, session_conn, wiki_conn = conns
    data = op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full").data

    # Assert：字段集合与顺序
    assert list(data.keys()) == CREATE_TOP_KEYS
    assert data["level"] == "full"
    assert data["snapshot_id"].startswith("full_")
    assert isinstance(data["path"], str)
    assert isinstance(data["created_at"], str)
    assert "memory.db" in data["files"]
    assert data["push_remote"] == {"ok": True, "skipped": True}


def test_list_data_http_shape_complete(conns, cfg):
    """list 超集键序 = LIST_TOP_KEYS；每条快照键序 = LIST_SNAPSHOT_KEYS。"""
    # Arrange：造两个快照（full 先、incremental 后 → 列表按名称降序，最新的在前）
    mem_conn, session_conn, wiki_conn = conns
    op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full")
    op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="incremental")

    # Act
    data = op_backup_list(cfg).data

    # Assert
    assert list(data.keys()) == LIST_TOP_KEYS
    assert data["total"] == 2
    assert len(data["snapshots"]) == 2
    for snap in data["snapshots"]:
        assert list(snap.keys()) == LIST_SNAPSHOT_KEYS
    # 名称降序（incremental 的创建时间晚于 full → 排在前面）
    names = [s["snapshot_id"] for s in data["snapshots"]]
    assert names == sorted(names, reverse=True)
    assert names[0].startswith("incremental_")
    assert names[1].startswith("full_")


def test_restore_data_http_shape_complete(conns, cfg, raw_dir):
    """restore 超集含 restored{files, snapshot_id} + pre_restore_snapshot + _new_conns。

    raw_dir 必须注入：快照含 raw/ 时 restore 才回填 raw/ 并计入 files
    （冻结契约实证：files == [memory.db, session.db, wiki.db, raw/]）。
    """
    # Arrange：先造快照
    mem_conn, session_conn, wiki_conn = conns
    snap = op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full").data

    # Act
    data = op_backup_restore(
        cfg, mem_conn, session_conn, wiki_conn, snapshot_id=snap["snapshot_id"],
    ).data

    # Assert
    assert list(data.keys()) == RESTORE_TOP_KEYS + ["_new_conns"]
    assert list(data["restored"].keys()) == RESTORE_RESTORED_KEYS
    assert data["restored"]["snapshot_id"] == snap["snapshot_id"]
    assert set(data["restored"]["files"]) >= {"memory.db", "session.db", "wiki.db", "raw/"}
    assert data["pre_restore_snapshot"].startswith("pre_restore_")
    # 投影后 _new_conns 必须被剔除（入口层私有传输字段不进响应）
    assert "_new_conns" not in http_payload(data)
    _close_conns(*data["_new_conns"])


# ---------- 3. restore 快照不存在 ----------

def test_restore_missing_snapshot_fails_not_found(conns, cfg):
    """快照不存在 → OperationResult(ok=False, ERR_NOT_FOUND)，文案含 snapshot_id。"""
    # Arrange / Act
    mem_conn, session_conn, wiki_conn = conns
    res = op_backup_restore(cfg, mem_conn, session_conn, wiki_conn, snapshot_id="no_such_snap")

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "快照不存在: no_such_snap"
    # 失败时旧连接不应被关闭（restore 未执行）
    assert mem_conn.execute("SELECT 1").fetchone() is not None


# ---------- 4. 契约等价性（最关键）：端点响应 vs 冻结字段集合 ----------

def test_http_create_contract_unchanged(client):
    """POST /v1/admin/backup/create：响应键序与取值 = 改造前冻结契约。"""
    # Act
    resp = client.post(
        "/v1/admin/backup/create", json={"level": "full"}, headers=ADMIN_HEADERS,
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == CREATE_TOP_KEYS
    assert body["level"] == "full"
    assert body["snapshot_id"].startswith("full_")
    assert body["push_remote"] == {"ok": True, "skipped": True}


def test_http_create_alt_path_contract_unchanged(client):
    """POST /v1/admin/backup（契约 §5 路径）：与 /create 同构，默认 level=incremental。"""
    # Act
    resp = client.post("/v1/admin/backup", json={}, headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == CREATE_TOP_KEYS
    assert body["level"] == "incremental"
    assert body["snapshot_id"].startswith("incremental_")


def test_http_list_contract_unchanged(client):
    """GET /v1/admin/backup/list：键序与取值 = 改造前冻结契约。"""
    # Arrange：造两个快照
    client.post("/v1/admin/backup/create", json={"level": "full"}, headers=ADMIN_HEADERS)
    client.post("/v1/admin/backup", json={}, headers=ADMIN_HEADERS)

    # Act
    resp = client.get("/v1/admin/backup/list", headers=ADMIN_HEADERS)

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == LIST_TOP_KEYS
    assert body["total"] == 2
    assert len(body["snapshots"]) == 2
    for snap in body["snapshots"]:
        assert list(snap.keys()) == LIST_SNAPSHOT_KEYS


def test_http_restore_contract_unchanged(client, app, raw_dir):
    """POST /v1/admin/backup/restore：响应 = {restored, pre_restore_snapshot}，
    不含 _new_conns；且 app.state 连接已交换为新连接。"""
    # Arrange：先经端点造快照
    snap_id = client.post(
        "/v1/admin/backup/create", json={"level": "full"}, headers=ADMIN_HEADERS,
    ).json()["snapshot_id"]
    old_mem_conn = app.state.mem_conn

    # Act
    resp = client.post(
        "/v1/admin/backup/restore", json={"snapshot_id": snap_id}, headers=ADMIN_HEADERS,
    )

    # Assert：响应体 = 冻结契约（_new_conns 不得出现）
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == RESTORE_TOP_KEYS
    assert list(body["restored"].keys()) == RESTORE_RESTORED_KEYS
    assert body["restored"]["snapshot_id"] == snap_id
    assert body["pre_restore_snapshot"].startswith("pre_restore_")
    assert set(body["restored"]["files"]) >= {"memory.db", "session.db", "wiki.db", "raw/"}

    # Assert：入口层职责——app.state 连接已交换（旧 conn 已被 restore 关闭）
    assert app.state.mem_conn is not old_mem_conn
    assert app.state.mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone() is not None

    # 清理新连接（防 Windows tmp_path 清理撞文件锁）
    _close_conns(app.state.mem_conn, app.state.session_conn, app.state.wiki_conn)


def test_http_restore_missing_contract_unchanged(client):
    """restore 快照不存在：404 + 错误结构逐字段 = 冻结契约（code/message）。"""
    # Act
    resp = client.post(
        "/v1/admin/backup/restore",
        json={"snapshot_id": "no_such_snap"},
        headers=ADMIN_HEADERS,
    )

    # Assert
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert list(body.keys()) == ERROR_KEYS
    assert list(body["error"].keys()) == ERROR_BODY_KEYS
    assert body["error"]["code"] == "ERR_NOT_FOUND"
    assert body["error"]["message"] == "快照不存在: no_such_snap"


# ---------- 5. 权限契约 ----------

def test_http_agent_key_still_forbidden(client):
    """Agent Key 调 backup 端点仍 403（require_admin_key 不变）。"""
    # Act
    resp = client.post(
        "/v1/admin/backup/create",
        json={"level": "full"},
        headers=AGENT_HEADERS,
    )

    # Assert
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


# ---------- 6. operations 直调与端点输出一致（同一批连接对照） ----------

def test_operations_and_http_agree_on_shared_fields(client, conns, cfg):
    """同一状态下，operations 直调 data 与端点响应体逐字段一致。"""
    # Arrange：端点先造一个快照
    client.post("/v1/admin/backup/create", json={"level": "full"}, headers=ADMIN_HEADERS)

    # Act：operations 直调（同一 cfg/连接）
    mem_conn, session_conn, wiki_conn = conns
    op_data = op_backup_create(cfg, mem_conn, session_conn, wiki_conn, level="full").data
    http_body = client.post(
        "/v1/admin/backup/create", json={"level": "full"}, headers=ADMIN_HEADERS,
    ).json()

    # Assert：共享字段逐字段一致（快照 id/时间戳各自生成，仅比较结构与稳定字段）
    assert list(http_body.keys()) == list(op_data.keys()) == CREATE_TOP_KEYS
    assert http_body["level"] == op_data["level"] == "full"
    assert http_body["push_remote"] == op_data["push_remote"] == {"ok": True, "skipped": True}
    # 端点列表与直调列表一致（同一备份目录）
    list_body = client.get("/v1/admin/backup/list", headers=ADMIN_HEADERS).json()
    assert list_body["total"] == len(op_backup_list(cfg).data["snapshots"]) == 3
