"""tests/test_demands.py：需求池 demands 测试（0.8 ST-15）。

覆盖：
1. 建表迁移：demands 表 + 4 个索引就绪；`_migrate_demands_table` 重复调用幂等；
   刻意无外键（project_id / origin_idea_id 可存任意值）
2. DAO：CRUD（insert/get/update/delete）、白名单防注入、分页 + total、
   LIKE 通配符转义、origin_idea_id 反查
3. API 鉴权：无 Key / Agent Key → 403
4. API CRUD：新建（默认值）、详情、404、编辑、PATCH 改 status 被拒
5. **四态流转全路径**：pending→planned→partial→done（resolved_at 落值）、
   done→pending 回退（resolved_at 清空）、done→done 幂等、非法状态 400
6. 分页：limit 上限 200、page/limit 边界 → 400
7. 过滤与排序：status 多值 / project_id / q 子串 / since-until / sort=priority
8. project_meta 软校验：表未就绪放行；表就绪后未知 project_id → 400
9. 升格链路：origin_idea_id 只存不校验（跨模块耦合留集成阶段）

fixture 范式参照 tests/test_operations_health.py 与 tests/test_routes_admin.py。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import demand_dao
from sgme.data import memory_dao
from sgme.operations import demand as demand_ops
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}

BASE = "/v1/admin/demands"


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """隔离的 memory.db / session.db / wiki.db。"""
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
def no_bearer(monkeypatch):
    """清除 SGME_BEARER_TOKEN（create_app 的 setdefault 是进程级副作用）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)


@pytest.fixture
def app(cfg, conns, no_bearer, tmp_path):
    mem, session, wiki = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem,
        session_conn=session,
        wiki_conn=wiki,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def tick(monkeypatch):
    """可控时钟：每次取时间戳前进 1 秒（库内时间戳精度到秒，真实时钟无法验证递增）。"""
    state = {"n": 0}

    def fake_now() -> str:
        state["n"] += 1
        return f"2026-08-09T10:00:{state['n']:02d}Z"

    monkeypatch.setattr(demand_ops, "_now_iso", fake_now)
    return state


def _create(client: TestClient, **body) -> dict:
    """POST 新建需求并断言 200，返回响应体。"""
    body.setdefault("title", "需求标题")
    resp = client.post(BASE, json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed(conn: sqlite3.Connection, n: int, **overrides) -> list[str]:
    """直接经 DAO 灌 n 条需求（分页/排序用例的数据准备）。"""
    ids: list[str] = []
    for i in range(n):
        did = demand_dao.new_demand_id()
        demand_dao.insert_demand(
            conn,
            demand_id=did,
            title=overrides.get("title", f"需求-{i:02d}"),
            content=overrides.get("content", f"内容-{i:02d}"),
            status=overrides.get("status", "pending"),
            priority=overrides.get("priority", i),
            project_id=overrides.get("project_id"),
            origin_idea_id=overrides.get("origin_idea_id"),
            source_ref=overrides.get("source_ref"),
            created_at=f"2026-08-01T00:00:{i:02d}Z",
            updated_at=f"2026-08-02T00:00:{i:02d}Z",
        )
        ids.append(did)
    return ids


def _make_project_meta(conn: sqlite3.Connection) -> None:
    """伪造 ST-16 的 project_meta 表（本 worktree 无此表，用于验证软校验自动生效）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_meta (
          project_id TEXT PRIMARY KEY, name TEXT, path TEXT, git_repo TEXT,
          last_active_at TEXT, milestone TEXT, created_at TEXT, updated_at TEXT)
        """
    )


def _seed_project(conn: sqlite3.Connection, project_id: str = "SGME") -> None:
    """预置 project_meta 项目记录（ST-16 合并后软校验就绪，测试需真实项目放行）。"""
    _make_project_meta(conn)
    conn.execute(
        "INSERT OR REPLACE INTO project_meta "
        "(project_id, name, path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, project_id, f"D:/Projects/{project_id}",
         "2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z"),
    )
    conn.commit()


# ==================== 1. 建表迁移 ====================

def test_demands_table_and_indexes_created(mem_conn):
    """connect_memory 后 demands 表与 4 个索引就绪。"""
    assert "demands" in db_mod.list_tables(mem_conn)

    cols = {r[1] for r in mem_conn.execute("PRAGMA table_info(demands)").fetchall()}
    assert cols == {
        "demand_id", "title", "content", "status", "priority", "project_id",
        "origin_idea_id", "source_ref", "created_at", "updated_at", "resolved_at",
    }

    idx = {
        r[0] for r in mem_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='demands'"
        ).fetchall()
    }
    for name in (
        "idx_demands_status_priority",
        "idx_demands_updated",
        "idx_demands_project",
        "idx_demands_origin_idea",
    ):
        assert name in idx, f"缺索引 {name}（现有: {sorted(idx)}）"


def test_migrate_demands_table_is_idempotent(mem_conn):
    """重复调用迁移不报错，且不丢数据（CREATE ... IF NOT EXISTS）。"""
    db_mod._migrate_demands_table(mem_conn)
    did = _seed(mem_conn, 1)[0]

    for _ in range(3):
        db_mod._migrate_demands_table(mem_conn)

    assert demand_dao.get_demand(mem_conn, did) is not None
    assert demand_dao.count_demands(mem_conn) == 1


def test_connect_memory_twice_is_idempotent(tmp_path, cfg):
    """同一 data_dir 二次 connect_memory（模拟服务重启）不报错，数据保留。"""
    conn1 = db_mod.connect_memory(tmp_path / "data")
    did = _seed(conn1, 1)[0]
    db_mod.close(conn1)

    conn2 = db_mod.connect_memory(tmp_path / "data")
    try:
        assert "demands" in db_mod.list_tables(conn2)
        assert demand_dao.get_demand(conn2, did) is not None
    finally:
        db_mod.close(conn2)


def test_demands_has_no_foreign_keys(mem_conn):
    """刻意无外键：project_id / origin_idea_id 指向不存在的行也能写入。

    project_meta 由 ST-16 并行创建，本 worktree 无该表；若加外键则 DML 直接崩。
    """
    fks = mem_conn.execute("PRAGMA foreign_key_list(demands)").fetchall()
    assert fks == [], f"demands 不应有外键约束: {fks}"

    # 外键开关确实是开着的（证明"无外键"是 DDL 的选择，不是 PRAGMA 没生效）
    assert mem_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    did = demand_dao.new_demand_id()
    demand_dao.insert_demand(
        mem_conn, demand_id=did, title="悬空引用",
        project_id="NOT_EXIST_PROJECT", origin_idea_id="NOT_EXIST_MEMORY",
    )
    row = demand_dao.get_demand(mem_conn, did)
    assert row["project_id"] == "NOT_EXIST_PROJECT"
    assert row["origin_idea_id"] == "NOT_EXIST_MEMORY"


# ==================== 2. DAO ====================

def test_dao_crud_roundtrip(mem_conn):
    """insert → get → update_fields → update_status → delete 全链路。"""
    did = demand_dao.new_demand_id()
    demand_dao.insert_demand(
        mem_conn, demand_id=did, title="做一个记忆系统", content="宽泛需求",
        status="pending", priority=80, source_ref="20260804_014703_cc9bb4:83",
        created_at="2026-08-01T00:00:00Z", updated_at="2026-08-01T00:00:00Z",
    )

    row = demand_dao.get_demand(mem_conn, did)
    assert row["title"] == "做一个记忆系统"
    assert row["priority"] == 80
    assert row["status"] == "pending"
    assert row["resolved_at"] is None

    assert demand_dao.update_demand_fields(
        mem_conn, did, fields={"title": "改名", "project_id": "SGME"},
        updated_at="2026-08-03T00:00:00Z",
    ) is True
    row = demand_dao.get_demand(mem_conn, did)
    assert row["title"] == "改名"
    assert row["project_id"] == "SGME"
    assert row["updated_at"] == "2026-08-03T00:00:00Z"

    assert demand_dao.update_demand_status(
        mem_conn, did, status="done", resolved_at="2026-08-04T00:00:00Z",
        updated_at="2026-08-04T00:00:00Z",
    ) is True
    assert demand_dao.get_demand(mem_conn, did)["resolved_at"] == "2026-08-04T00:00:00Z"

    assert demand_dao.count_demands(mem_conn, status="done") == 1
    assert demand_dao.delete_demand(mem_conn, did) is True
    assert demand_dao.get_demand(mem_conn, did) is None
    assert demand_dao.delete_demand(mem_conn, did) is False


def test_dao_update_missing_row_returns_false(mem_conn):
    """更新不存在的行返回 False（不抛异常，由上层翻 404）。"""
    assert demand_dao.update_demand_fields(mem_conn, "nope", fields={"title": "x"}) is False
    assert demand_dao.update_demand_status(mem_conn, "nope", status="done") is False


def test_dao_rejects_non_whitelisted_column(mem_conn):
    """可写列白名单：status / 任意列名一律 ValueError（防注入 + 防绕过状态端点）。"""
    did = _seed(mem_conn, 1)[0]
    with pytest.raises(ValueError):
        demand_dao.update_demand_fields(mem_conn, did, fields={"status": "done"})
    with pytest.raises(ValueError):
        demand_dao.update_demand_fields(mem_conn, did, fields={"title=1; DROP": "x"})
    with pytest.raises(ValueError):
        demand_dao.update_demand_fields(mem_conn, did, fields={})


def test_dao_rejects_bad_sort_and_order(mem_conn):
    """ORDER BY 白名单兜底（防拼接注入）。"""
    with pytest.raises(ValueError):
        demand_dao.list_demands(mem_conn, sort="priority; DROP TABLE demands")
    with pytest.raises(ValueError):
        demand_dao.list_demands(mem_conn, order="up")
    with pytest.raises(ValueError):
        demand_dao.list_demands(mem_conn, page=0)


def test_dao_pagination_returns_total(mem_conn):
    """分页返回 (当前页, 命中总数)；翻页不重不漏。"""
    _seed(mem_conn, 7)
    page1, total = demand_dao.list_demands(mem_conn, sort="priority", order="asc", page=1, limit=3)
    page2, _ = demand_dao.list_demands(mem_conn, sort="priority", order="asc", page=2, limit=3)
    page3, _ = demand_dao.list_demands(mem_conn, sort="priority", order="asc", page=3, limit=3)

    assert total == 7
    assert [len(page1), len(page2), len(page3)] == [3, 3, 1]
    seen = [r["demand_id"] for r in page1 + page2 + page3]
    assert len(set(seen)) == 7
    assert [r["priority"] for r in page1] == [0, 1, 2]


def test_dao_like_wildcards_are_escaped(mem_conn):
    """q 中的 % / _ 按字面量匹配，不退化成通配。"""
    _seed(mem_conn, 1, title="进度 100%")
    _seed(mem_conn, 1, title="毫不相干")

    hit, total = demand_dao.list_demands(mem_conn, q="100%")
    assert total == 1 and hit[0]["title"] == "进度 100%"

    miss, total_miss = demand_dao.list_demands(mem_conn, q="%相干%")
    assert total_miss == 0 and miss == []


def test_dao_list_by_origin_idea(mem_conn):
    """按升格来源创意反查（ST-14 溯源链）。"""
    _seed(mem_conn, 2, origin_idea_id="idea-1")
    _seed(mem_conn, 1, origin_idea_id="idea-2")
    assert len(demand_dao.list_demands_by_origin_idea(mem_conn, "idea-1")) == 2
    assert len(demand_dao.list_demands_by_origin_idea(mem_conn, "idea-9")) == 0


def test_dao_project_meta_probe(mem_conn):
    """project_meta 已随 ST-16 合并就绪 → available=True；按行判定存在性。"""
    # ST-16 合并后 project_meta 表随 connect_memory 建表就绪，软校验自动生效
    assert demand_dao.project_meta_available(mem_conn) is True
    assert demand_dao.project_exists(mem_conn, "SGME") is False

    _make_project_meta(mem_conn)
    assert demand_dao.project_meta_available(mem_conn) is True
    assert demand_dao.project_exists(mem_conn, "SGME") is False

    mem_conn.execute(
        "INSERT INTO project_meta (project_id, name, path, created_at, updated_at) "
        "VALUES ('SGME','SGME','D:/Projects/SGME', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')"
    )
    mem_conn.commit()
    assert demand_dao.project_exists(mem_conn, "SGME") is True


# ==================== 3. 鉴权 ====================

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", BASE),
        ("post", BASE),
        ("get", f"{BASE}/whatever"),
        ("patch", f"{BASE}/whatever"),
        ("put", f"{BASE}/whatever/status"),
    ],
)
def test_demands_without_api_key_returns_403(client, method, path):
    """无 X-API-Key → 403 ERR_FORBIDDEN（全部端点）。"""
    resp = getattr(client, method)(path)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", BASE),
        ("post", BASE),
        ("put", f"{BASE}/whatever/status"),
    ],
)
def test_demands_with_agent_key_returns_403(client, method, path):
    """Agent Key（非管理员）→ 403。"""
    resp = getattr(client, method)(path, headers=AGENT_HEADERS)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


# ==================== 4. 新建 / 详情 / 编辑 ====================

def test_create_demand_defaults(client):
    """新建默认：status=pending、priority=50、resolved_at=null，含中文 status_label。"""
    body = _create(client, title="  我想要一个记忆系统  ", content="宽泛需求，无标准无时限")
    assert body["title"] == "我想要一个记忆系统"      # 首尾空白已裁剪
    assert body["status"] == "pending"
    assert body["status_label"] == "未立项"
    assert body["priority"] == 50
    assert body["project_id"] is None
    assert body["origin_idea_id"] is None
    assert body["resolved_at"] is None
    assert body["created_at"] == body["updated_at"]
    assert body["warnings"] == []
    assert body["demand_id"]


def test_create_demand_full_fields_and_detail(client, mem_conn):
    """全字段新建 + GET 详情逐字段一致。"""
    _seed_project(mem_conn)
    created = _create(
        client, title="WebUI", content="记忆浏览界面", status="planned",
        priority=90, project_id="SGME", origin_idea_id="mem-abc",
        source_ref="20260804_014703_cc9bb4:83",
    )
    got = client.get(f"{BASE}/{created['demand_id']}", headers=ADMIN_HEADERS)
    assert got.status_code == 200, got.text
    detail = got.json()

    for key in (
        "demand_id", "title", "content", "status", "status_label", "priority",
        "project_id", "origin_idea_id", "source_ref", "created_at", "updated_at",
        "resolved_at",
    ):
        assert detail[key] == created[key], key
    assert detail["status_label"] == "已立项"
    assert "warnings" not in detail  # 详情是纯投影，不带动作态元信息


def test_create_demand_done_sets_resolved_at(client):
    """建时即 done（历史需求补录）也落 resolved_at，维持状态不变式。"""
    body = _create(client, title="历史需求", status="done")
    assert body["status_label"] == "已解决"
    assert body["resolved_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {},                                  # 缺 title
        {"title": "   "},                    # title 空白
        {"title": "x", "status": "未立项"},   # 中文状态非法（只收英文枚举）
        {"title": "x", "status": "unknown"},
        {"title": "x", "priority": 101},
        {"title": "x", "priority": -1},
        {"title": "x", "priority": "high"},
        {"title": "x", "priority": True},    # bool 不是合法整数
        {"title": "x", "typo_field": 1},     # 未知字段不静默吞
    ],
)
def test_create_demand_invalid_payload_returns_400(client, payload):
    """非法入参一律 400 ERR_INVALID_ARGS（而非 FastAPI 默认 422）。"""
    resp = client.post(BASE, json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_get_unknown_demand_returns_404(client):
    """详情 404 ERR_NOT_FOUND。"""
    resp = client.get(f"{BASE}/no-such-id", headers=ADMIN_HEADERS)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_patch_demand_updates_only_given_fields(client, tick):
    """PATCH 只改传入字段，其余保持；updated_at 刷新，created_at 不变。"""
    created = _create(client, title="旧标题", content="旧内容", priority=10)
    did = created["demand_id"]

    resp = client.patch(
        f"{BASE}/{did}", json={"title": "新标题", "priority": 77}, headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "新标题"
    assert body["priority"] == 77
    assert body["content"] == "旧内容"                 # 未传 → 保持
    assert body["created_at"] == created["created_at"]
    assert body["updated_at"] > created["updated_at"]
    assert body["updated_fields"] == ["priority", "title"]


def test_patch_demand_can_unbind_project(client, mem_conn):
    """显式传 project_id: null = 解绑；未传该键则保持原值。"""
    _seed_project(mem_conn)
    did = _create(client, title="x", project_id="SGME")["demand_id"]

    keep = client.patch(f"{BASE}/{did}", json={"title": "y"}, headers=ADMIN_HEADERS).json()
    assert keep["project_id"] == "SGME"

    unbind = client.patch(
        f"{BASE}/{did}", json={"project_id": None}, headers=ADMIN_HEADERS,
    ).json()
    assert unbind["project_id"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "done"},        # status 必须走 PUT /status
        {},                        # 无可改字段
        {"priority": 200},
        {"title": ""},
        {"resolved_at": "2026-01-01T00:00:00Z"},  # 只读派生字段
    ],
)
def test_patch_demand_invalid_returns_400(client, payload):
    did = _create(client, title="x")["demand_id"]
    resp = client.patch(f"{BASE}/{did}", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_patch_unknown_demand_returns_404(client):
    resp = client.patch(f"{BASE}/no-such-id", json={"title": "x"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


# ==================== 5. 四态流转（核心验收点） ====================

def _put_status(client: TestClient, did: str, status: str):
    return client.put(f"{BASE}/{did}/status", json={"status": status}, headers=ADMIN_HEADERS)


def test_status_full_path_pending_to_done(client, tick, mem_conn):
    """全路径 pending → planned → partial → done；done 落 resolved_at。"""
    _seed_project(mem_conn)
    created = _create(client, title="记忆系统", project_id="SGME")
    did = created["demand_id"]
    assert created["status"] == "pending"

    expected_labels = {
        "planned": "已立项", "partial": "部分解决", "done": "已解决",
    }
    previous = "pending"
    last_updated = created["updated_at"]

    for status in ("planned", "partial", "done"):
        resp = _put_status(client, did, status)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == status
        assert body["status_label"] == expected_labels[status]
        assert body["previous_status"] == previous
        assert body["changed"] is True
        assert body["updated_at"] > last_updated       # 每次流转刷新 updated_at
        # 只有 done 有 resolved_at
        assert (body["resolved_at"] is not None) == (status == "done")
        previous, last_updated = status, body["updated_at"]

    detail = client.get(f"{BASE}/{did}", headers=ADMIN_HEADERS).json()
    assert detail["status"] == "done"
    assert detail["resolved_at"] is not None


def test_status_rollback_from_done_clears_resolved_at(client, tick):
    """判断题②：转出 done 清空 resolved_at（维持 resolved_at ⟺ status=done 不变式）。"""
    did = _create(client, title="会被重开的需求")["demand_id"]
    done = _put_status(client, did, "done").json()
    assert done["resolved_at"] is not None

    reopened = _put_status(client, did, "partial").json()
    assert reopened["status"] == "partial"
    assert reopened["resolved_at"] is None
    assert reopened["previous_status"] == "done"

    # 再次解决 → 重新落值
    again = _put_status(client, did, "done").json()
    assert again["resolved_at"] is not None


def test_status_allows_arbitrary_direction(client, tick):
    """判断题④：不限制流转方向，done → pending 直接回退合法。"""
    did = _create(client, title="任意流转")["demand_id"]
    _put_status(client, did, "done")

    back = _put_status(client, did, "pending")
    assert back.status_code == 200, back.text
    body = back.json()
    assert body["status"] == "pending"
    assert body["previous_status"] == "done"
    assert body["resolved_at"] is None


def test_status_same_value_is_idempotent(client, tick):
    """done → done 幂等：保留首次解决时刻，changed=False，updated_at 仍刷新。"""
    did = _create(client, title="幂等")["demand_id"]
    first = _put_status(client, did, "done").json()
    second = _put_status(client, did, "done").json()

    assert second["resolved_at"] == first["resolved_at"]  # 不刷新首次解决时刻
    assert second["changed"] is False
    assert second["updated_at"] > first["updated_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unknown"},
        {"status": "已解决"},      # 中文展示名不是入参
        {"status": ""},
        {"status": None},
        {},                        # 缺 status
        {"status": "done", "resolved_at": "2026-01-01T00:00:00Z"},  # 不接受额外字段
    ],
)
def test_status_invalid_returns_400(client, payload):
    """非法状态 / 缺参 → 400 ERR_INVALID_ARGS。"""
    did = _create(client, title="x")["demand_id"]
    resp = client.put(f"{BASE}/{did}/status", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_status_unknown_demand_returns_404(client):
    resp = _put_status(client, "no-such-id", "done")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_planned_without_project_warns_but_succeeds(client, mem_conn):
    """判断题③：转 planned 不强制 project_id，只回非阻断 warning。"""
    _seed_project(mem_conn)
    did = _create(client, title="先拍板后建项目")["demand_id"]
    body = _put_status(client, did, "planned").json()
    assert body["status"] == "planned"
    assert body["warnings"] == ["planned_without_project"]

    # 补上 project_id 后 warning 消失
    patched = client.patch(
        f"{BASE}/{did}", json={"project_id": "SGME"}, headers=ADMIN_HEADERS,
    ).json()
    assert patched["warnings"] == []


# ==================== 6. 分页 ====================

def test_list_pagination_envelope(client, mem_conn):
    """分页信封 {items, count, total, page, limit, generated_at}。"""
    _seed(mem_conn, 5)
    body = client.get(f"{BASE}?page=2&limit=2&sort=priority&order=asc", headers=ADMIN_HEADERS).json()

    assert set(body) == {"items", "count", "total", "page", "limit", "generated_at"}
    assert body["total"] == 5
    assert body["count"] == 2
    assert body["page"] == 2
    assert body["limit"] == 2
    assert [i["priority"] for i in body["items"]] == [2, 3]
    assert body["generated_at"]


def test_list_defaults(client, mem_conn):
    """缺省 page=1 / limit=50 / sort=updated_at desc；四态全展示（不默认过滤）。"""
    _seed(mem_conn, 3, status="done")
    _seed(mem_conn, 2, status="pending")
    body = client.get(BASE, headers=ADMIN_HEADERS).json()

    assert body["page"] == 1
    assert body["limit"] == 50
    assert body["total"] == 5
    ts = [i["updated_at"] for i in body["items"]]
    assert ts == sorted(ts, reverse=True)


def test_list_limit_upper_bound(client, mem_conn):
    """limit=200 放行；limit=201 → 400（硬上限，不静默截断）。"""
    _seed(mem_conn, 1)
    ok = client.get(f"{BASE}?limit=200", headers=ADMIN_HEADERS)
    assert ok.status_code == 200, ok.text
    assert ok.json()["limit"] == 200

    over = client.get(f"{BASE}?limit=201", headers=ADMIN_HEADERS)
    assert over.status_code == 400, over.text
    assert over.json()["error"]["code"] == "ERR_INVALID_ARGS"


@pytest.mark.parametrize(
    "qs",
    [
        "page=0", "page=-1", "page=abc", "page=1.5",
        "limit=0", "limit=-5", "limit=abc", "limit=1000",
        "sort=nope", "sort=title", "order=up",
        "status=archived", "status=done,bogus",
        "since=not-a-time", "until=2026-13-45",
    ],
)
def test_list_invalid_params_return_400(client, qs):
    """非法查询参数 → 400 ERR_INVALID_ARGS。"""
    resp = client.get(f"{BASE}?{qs}", headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_list_empty_pool(client):
    """空池子 → items=[]，count=total=0（不是 404）。"""
    body = client.get(BASE, headers=ADMIN_HEADERS).json()
    assert body["items"] == []
    assert body["count"] == 0
    assert body["total"] == 0


# ==================== 7. 过滤与排序 ====================

def test_list_status_multi_value_filter(client, mem_conn):
    """status 逗号多值过滤。"""
    _seed(mem_conn, 2, status="pending")
    _seed(mem_conn, 3, status="planned")
    _seed(mem_conn, 1, status="done")

    only_done = client.get(f"{BASE}?status=done", headers=ADMIN_HEADERS).json()
    assert only_done["total"] == 1

    two = client.get(f"{BASE}?status=pending,planned", headers=ADMIN_HEADERS).json()
    assert two["total"] == 5
    assert {i["status"] for i in two["items"]} == {"pending", "planned"}

    # 重复值去重不影响结果
    dup = client.get(f"{BASE}?status=done,done", headers=ADMIN_HEADERS).json()
    assert dup["total"] == 1


def test_list_project_and_q_filter(client, mem_conn):
    """project_id 精确匹配 + q 标题/内容子串。"""
    _seed(mem_conn, 2, project_id="SGME", title="SGME 需求")
    _seed(mem_conn, 1, project_id="HERMES", title="Hermes 需求", content="限流")

    by_project = client.get(f"{BASE}?project_id=SGME", headers=ADMIN_HEADERS).json()
    assert by_project["total"] == 2

    by_title = client.get(f"{BASE}?q=Hermes", headers=ADMIN_HEADERS).json()
    assert by_title["total"] == 1

    by_content = client.get(f"{BASE}?q=限流", headers=ADMIN_HEADERS).json()
    assert by_content["total"] == 1

    combined = client.get(f"{BASE}?project_id=SGME&q=限流", headers=ADMIN_HEADERS).json()
    assert combined["total"] == 0


def test_list_time_range_filter(client, mem_conn):
    """since/until 作用于 sort 时间列（默认 updated_at，闭区间）。"""
    _seed(mem_conn, 5)  # updated_at = 2026-08-02T00:00:00Z .. :04Z

    hit = client.get(
        f"{BASE}?since=2026-08-02T00:00:01Z&until=2026-08-02T00:00:03Z",
        headers=ADMIN_HEADERS,
    ).json()
    assert hit["total"] == 3

    none = client.get(f"{BASE}?since=2027-01-01T00:00:00Z", headers=ADMIN_HEADERS).json()
    assert none["total"] == 0

    # 作用列随 sort 切换到 created_at
    by_created = client.get(
        f"{BASE}?sort=created_at&since=2026-08-01T00:00:03Z", headers=ADMIN_HEADERS,
    ).json()
    assert by_created["total"] == 2


def test_list_sort_by_priority(client, mem_conn):
    """sort=priority 高优先在前（默认 desc）。"""
    _seed(mem_conn, 4)
    body = client.get(f"{BASE}?sort=priority", headers=ADMIN_HEADERS).json()
    assert [i["priority"] for i in body["items"]] == [3, 2, 1, 0]

    asc = client.get(f"{BASE}?sort=priority&order=asc", headers=ADMIN_HEADERS).json()
    assert [i["priority"] for i in asc["items"]] == [0, 1, 2, 3]


# ==================== 8. project_meta 软校验 ====================

def test_project_id_accepted_when_project_meta_absent(client, mem_conn):
    """project_meta 未就绪：任意 project_id 放行（不校验，无 warning）。"""
    body = _create(client, title="未注册项目", project_id="ANY_PROJECT")
    assert body["project_id"] == "ANY_PROJECT"
    assert "unknown_project" not in str(body.get("warnings", []))


def test_project_id_validated_when_project_meta_present(client, mem_conn):
    """project_meta 就绪后（2026-08-13 语义：待办池化）：未知 project_id → warning 不阻断，已知 → 无 warning。"""
    _make_project_meta(mem_conn)
    mem_conn.execute(
        "INSERT INTO project_meta (project_id, name, path, created_at, updated_at) "
        "VALUES ('SGME','SGME','D:/Projects/SGME', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')"
    )
    mem_conn.commit()

    # 未知项目：创建成功 + warning（不再 400）
    ghost = _create(client, title="ghost 标记", project_id="GHOST")
    assert ghost["project_id"] == "GHOST"
    assert any("未登记" in w for w in ghost["warnings"])

    # 已知项目：创建成功 + 无 warning
    good = _create(client, title="x", project_id="SGME")
    assert good["project_id"] == "SGME"
    assert not any("未登记" in w for w in good["warnings"])

    # PATCH 改到未知项目：同样 warning 不阻断
    did = good["demand_id"]
    patch_r = client.patch(
        f"{BASE}/{did}", json={"project_id": "GHOST"}, headers=ADMIN_HEADERS
    )
    assert patch_r.status_code == 200, patch_r.text
    assert any("未登记" in w for w in patch_r.json()["warnings"])


# ==================== 9. 升格链路（ST-14 接口约定） ====================

def test_origin_idea_id_stored_without_validation(client, mem_conn):
    """origin_idea_id 只存不校验：memories 里没有该 id 也照常建（跨模块耦合留集成阶段）。"""
    body = _create(client, title="由创意升格而来", origin_idea_id="mem-not-exist-yet")
    assert body["origin_idea_id"] == "mem-not-exist-yet"

    # DAO 反查可闭合溯源链
    linked = demand_dao.list_demands_by_origin_idea(mem_conn, "mem-not-exist-yet")
    assert [d["demand_id"] for d in linked] == [body["demand_id"]]


def test_origin_idea_id_not_editable_by_patch(client):
    """origin_idea_id 是溯源事实，不在 PATCH 白名单内 → 400。"""
    did = _create(client, title="x", origin_idea_id="mem-1")["demand_id"]
    resp = client.patch(f"{BASE}/{did}", json={"origin_idea_id": "mem-2"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_demands_does_not_touch_memories_table(client, mem_conn):
    """边界：需求池写入不改 memories（创意侧 custom_flag 归 ST-14）。"""
    before = mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    _create(client, title="升格", origin_idea_id="mem-1")
    after = mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert before == after
