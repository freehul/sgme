"""tests/test_routes_admin_browse.py：F1 浏览类五端点（0.8 T-15，契约 §5.3~§5.7）。

覆盖矩阵
--------
1. 分页：page/limit 生效、count/total/page/limit 自洽、翻页不重不漏
2. **limit 硬上限 200**：=200 放行，>200 → 400 ERR_INVALID_ARGS
3. **默认仅 active**：不传 status 时 rejected / expired / archived 一律不可见
4. 参数校验：page<1 / 非整数 / 未知 sort / status 枚举外 / 非法时间戳 → 400
5. 过滤与排序：dimension_id、sort=priority|occurred_at、order、since/until 作用于 sort 字段
6. TTL 过滤（ttl_filter=true 才生效）
7. notes / custom_flag 防御性处理：基线无列返回 null；**模拟 ST-14 加列后自动生效**
8. scenes：默认 active、heat 倒序、memories_count 聚合
9. refine_runs：stage/status 过滤、**不做 status 缺省过滤**（error/running 默认可见）
10. sessions：session_key 子串匹配、agent_id/status 过滤；单条原文 200 与 404
11. stats/detail：daily/weekly 归组、totals == 各项求和、stage 过滤
12. 鉴权：六个端点全部 require_admin_key（缺 Key → 403）

不改动任何既有测试；本文件全部使用 tmp_path 隔离的三库与 raw 目录。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, scene_dao
from sgme.raw import store as raw_store
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
ADMIN = {"X-API-Key": ADMIN_KEY}

#: 六个端点（鉴权用例遍历）
BROWSE_ENDPOINTS = [
    "/v1/admin/memories",
    "/v1/admin/scenes",
    "/v1/admin/refine_runs",
    "/v1/admin/sessions",
    "/v1/admin/sessions/any-file-id",
    "/v1/admin/stats/detail",
]


# ---------- fixtures（范式取自 tests/test_operations_health.py） ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（会话原文读取用）。"""
    rd = tmp_path / "raw"
    (rd / "sessions").mkdir(parents=True)
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


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
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（注入同一批连接）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    # 不挂载 MCP：本用例只测 HTTP 路由，避免额外副作用
    monkeypatch.setenv("SGME_MCP_DISABLED", "1")
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key="test-agent-key",
        bearer_token="",  # 显式禁用 Bearer
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _iso(days_ago: float = 0) -> str:
    """N 天前的 UTC ISO 时间戳（全库统一格式）。"""
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_memory_status(conn: sqlite3.Connection, memory_id: str, status: str) -> None:
    """直接改 memories.status（覆盖 active 之外的四态，绕开业务链路）。"""
    conn.execute("UPDATE memories SET status=? WHERE memory_id=?", (status, memory_id))
    conn.commit()


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    status: str,
    started_at: str,
    *,
    provider: str = "test-provider",
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 20,
    total_tokens: int | None = 120,
    memories_count: int = 3,
    error: str | None = None,
) -> None:
    """直插 refine_runs（需要精确控制 started_at，故不用 RefineRunRecorder）。"""
    conn.execute(
        """
        INSERT INTO refine_runs
          (run_id, file_id, stage, version, variant, provider, bucket_key,
           started_at, finished_at, memories_count, prompt_tokens,
           completion_tokens, total_tokens, action_counts, status, error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (run_id, f"file_{run_id}", stage, "v001", None, provider, "bk",
         started_at, started_at, memories_count, prompt_tokens,
         completion_tokens, total_tokens, '{"store": 3}', status, error),
    )
    conn.commit()


def _insert_raw(
    conn: sqlite3.Connection,
    file_id: str,
    session_key: str,
    agent_id: str,
    status: str,
    started_at: str,
    *,
    size: int = 100,
) -> None:
    """直插 raw_files（session.db）。"""
    conn.execute(
        """
        INSERT INTO raw_files
          (file_id, path, session_key, agent_id, started_at, ended_at,
           refined_at, last_refined_seq, status, size)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (file_id, f"raw/sessions/{file_id}.md", session_key, agent_id,
         started_at, None, None, 1, status, size),
    )
    conn.commit()


@pytest.fixture
def seeded(app, conns, raw_dir):
    """种入四类数据：memories / scenes / refine_runs / raw_files。

    依赖 ``app`` 以保证 create_app（含 FTS 初始化）先于写入完成。
    """
    mem_conn, session_conn, _ = conns
    dims = [d["id"] for d in memory_dao.list_dimensions(mem_conn)]
    assert len(dims) >= 2, "注册表维度不足，用例前提不成立"
    dim_a, dim_b = dims[0], dims[1]

    # --- memories：5 active + 1 rejected + 1 expired + 1 archived ---
    active_ids: list[str] = []
    for i in range(5):
        mid = memory_dao.insert_memory(
            mem_conn,
            content=f"活跃记忆 {i}：每周三家庭安排",
            memory_type="persona",
            priority=10 * (i + 1),
            time_velocity="static",
            ttl_days=None,
            dimension_ids=[dim_a] if i % 2 == 0 else [dim_a, dim_b],
            sources=[(f"20260804_0147_{i}:83", "session")],
            created_at=_iso(10 - i),
            updated_at=_iso(10 - i),
            occurred_at=_iso(20 - i),
        )
        active_ids.append(mid)

    rejected_id = memory_dao.insert_memory(
        mem_conn, content="判错记忆", memory_type="persona", priority=5,
        time_velocity="static", ttl_days=None, dimension_ids=[dim_a],
        created_at=_iso(3), updated_at=_iso(3),
    )
    memory_dao.reject_memory(mem_conn, rejected_id, "用户纠错")

    expired_id = memory_dao.insert_memory(
        mem_conn, content="过时记忆", memory_type="persona", priority=6,
        time_velocity="dynamic", ttl_days=None, dimension_ids=[dim_a],
        created_at=_iso(3), updated_at=_iso(3),
    )
    _set_memory_status(mem_conn, expired_id, "expired")

    archived_id = memory_dao.insert_memory(
        mem_conn, content="归档记忆", memory_type="persona", priority=7,
        time_velocity="static", ttl_days=None, dimension_ids=[dim_a],
        created_at=_iso(3), updated_at=_iso(3),
    )
    _set_memory_status(mem_conn, archived_id, "archived")

    # TTL 已过期的 active 记忆（ttl_days=1 但 30 天没更新）→ 仅 ttl_filter=true 时被滤掉
    ttl_stale_id = memory_dao.insert_memory(
        mem_conn, content="TTL 过期但状态 active", memory_type="persona", priority=8,
        time_velocity="dynamic", ttl_days=1, dimension_ids=[dim_b],
        created_at=_iso(30), updated_at=_iso(30),
    )
    active_ids.append(ttl_stale_id)

    # --- scenes：3 active（heat 各异）+ 1 rejected ---
    for idx, (sid, heat) in enumerate([("scene_a", 30), ("scene_b", 10), ("scene_c", 20)]):
        scene_dao.insert_scene(
            mem_conn, sid, f"标题 {sid}", f"叙事文档全文 {sid}",
            created_at=_iso(5 - idx), updated_at=_iso(5 - idx),
        )
        mem_conn.execute("UPDATE scenes SET heat=? WHERE scene_id=?", (heat, sid))
    mem_conn.commit()
    scene_dao.insert_scene(mem_conn, "scene_x", "标题 x", "被判错的场景")
    scene_dao.update_scene_status(mem_conn, "scene_x", "rejected")
    # scene_a 关联 2 条记忆，scene_b 关联 0 条
    scene_dao.add_memory_link(mem_conn, "scene_a", active_ids[0])
    scene_dao.add_memory_link(mem_conn, "scene_a", active_ids[1])

    # --- refine_runs：跨周（2026-08-09 周日 / 2026-08-10 周一）+ 多 stage 多状态 ---
    _insert_run(mem_conn, "run_ok_1", "l1_extraction", "ok", "2026-08-09T10:00:00Z")
    _insert_run(mem_conn, "run_ok_2", "l1_extraction", "ok", "2026-08-09T12:00:00Z")
    _insert_run(mem_conn, "run_err", "l1_extraction", "error", "2026-08-10T09:00:00Z",
                error="LLM 超时", prompt_tokens=None, completion_tokens=None,
                total_tokens=None, memories_count=0)
    _insert_run(mem_conn, "run_scene", "l2_scene", "ok", "2026-08-10T11:00:00Z")
    _insert_run(mem_conn, "run_running", "tier0_summary", "running", "2026-08-10T13:00:00Z")

    # --- raw_files + 磁盘原文 ---
    _insert_raw(session_conn, "f_hermes_1", "hermes-aaa", "hermes", "refined", _iso(2))
    _insert_raw(session_conn, "f_hermes_2", "hermes-bbb", "hermes", "new", _iso(1))
    _insert_raw(session_conn, "f_other", "scsm-ccc", "scsm", "archived", _iso(3))
    # 只给第一条写磁盘原文：用于验证「索引在但文件缺失 → 404」
    (raw_dir / "sessions" / "f_hermes_1.md").write_text(
        "# 2026-08-09T21:27:43Z user\n中文原文全文，验证 UTF-8 不乱码。\n",
        encoding="utf-8",
    )

    return {
        "dim_a": dim_a,
        "dim_b": dim_b,
        "active_ids": active_ids,
        "rejected_id": rejected_id,
        "expired_id": expired_id,
        "archived_id": archived_id,
        "ttl_stale_id": ttl_stale_id,
    }


# ============================================================
# §5.3 GET /v1/admin/memories
# ============================================================

def test_memories_default_only_active(client, seeded):
    """默认仅 active：rejected / expired / archived 一律不可见（契约 §5.3.2）。"""
    r = client.get("/v1/admin/memories", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()

    statuses = {item["status"] for item in body["items"]}
    assert statuses == {"active"}, f"默认查询混入非 active: {statuses}"

    ids = {item["memory_id"] for item in body["items"]}
    assert seeded["rejected_id"] not in ids
    assert seeded["expired_id"] not in ids
    assert seeded["archived_id"] not in ids
    # 6 条 active（5 条常规 + 1 条 TTL 过期但状态仍 active）
    assert body["total"] == 6
    assert body["count"] == 6


def test_memories_status_explicit_multi(client, seeded):
    """显式传 status 才能看见非 active；逗号分隔多值生效。"""
    r = client.get("/v1/admin/memories?status=rejected", headers=ADMIN)
    assert r.status_code == 200
    assert [i["memory_id"] for i in r.json()["items"]] == [seeded["rejected_id"]]

    r = client.get("/v1/admin/memories?status=active,rejected,expired", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["total"] == 8  # 6 active + 1 rejected + 1 expired
    assert {i["status"] for i in r.json()["items"]} == {"active", "rejected", "expired"}


def test_memories_pagination_is_stable(client, seeded):
    """分页自洽：count/total/page/limit 正确，翻页不重不漏。"""
    p1 = client.get("/v1/admin/memories?page=1&limit=2", headers=ADMIN).json()
    p2 = client.get("/v1/admin/memories?page=2&limit=2", headers=ADMIN).json()
    p3 = client.get("/v1/admin/memories?page=3&limit=2", headers=ADMIN).json()

    for page_no, body in ((1, p1), (2, p2), (3, p3)):
        assert body["page"] == page_no
        assert body["limit"] == 2
        assert body["total"] == 6
        assert body["count"] == len(body["items"]) == 2

    ids = [i["memory_id"] for i in p1["items"] + p2["items"] + p3["items"]]
    assert len(set(ids)) == 6, "翻页出现重复或遗漏"

    # 越界页返回空列表而非报错，total 仍是全量
    p9 = client.get("/v1/admin/memories?page=9&limit=2", headers=ADMIN).json()
    assert p9["items"] == [] and p9["count"] == 0 and p9["total"] == 6


def test_memories_limit_cap_200(client, seeded):
    """limit 硬上限 200：=200 放行，201 → 400 ERR_INVALID_ARGS（契约 §5.3.2/§5.3.4）。"""
    ok = client.get("/v1/admin/memories?limit=200", headers=ADMIN)
    assert ok.status_code == 200
    assert ok.json()["limit"] == 200

    over = client.get("/v1/admin/memories?limit=201", headers=ADMIN)
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "ERR_INVALID_ARGS"
    assert "200" in over.json()["error"]["message"]

    huge = client.get("/v1/admin/memories?limit=100000", headers=ADMIN)
    assert huge.status_code == 400


@pytest.mark.parametrize("query", [
    "page=0",
    "page=-1",
    "page=abc",
    "limit=0",
    "limit=-5",
    "limit=xyz",
    "sort=bogus_field",
    "order=sideways",
    "status=deleted",
    "status=active,deleted",
    "since=not-a-timestamp",
    "until=2026-13-45",
    "ttl_filter=maybe",
])
def test_memories_invalid_params_400(client, seeded, query):
    """参数非法一律 400 + 统一 error 信封（不是 FastAPI 的 422）。"""
    r = client.get(f"/v1/admin/memories?{query}", headers=ADMIN)
    assert r.status_code == 400, f"{query} 期望 400，实得 {r.status_code}"
    assert r.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_memories_dimension_filter(client, seeded):
    """dimension_id 过滤：收注册表 id，且不因多标签产生重复行。"""
    dim_b = seeded["dim_b"]
    r = client.get(f"/v1/admin/memories?dimension_id={dim_b}", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    ids = [i["memory_id"] for i in body["items"]]
    assert len(ids) == len(set(ids)), "维度过滤出现重复行（JOIN 未去重）"
    for item in body["items"]:
        assert dim_b in item["dimensions"]

    # 不存在的维度 → 空列表，不是 404
    empty = client.get("/v1/admin/memories?dimension_id=__nope__", headers=ADMIN)
    assert empty.status_code == 200
    assert empty.json()["total"] == 0 and empty.json()["items"] == []


def test_memories_multi_dimensions_filter(client, seeded):
    """dimensions 逗号分隔多维度过滤：AND 语义全部命中（2026-08-13）。"""
    dim_a = seeded["dim_a"]
    dim_b = seeded["dim_b"]
    # 单维度
    only_a = client.get(f"/v1/admin/memories?dimensions={dim_a}", headers=ADMIN).json()
    only_b = client.get(f"/v1/admin/memories?dimensions={dim_b}", headers=ADMIN).json()
    # 多维度 AND = 两者交集（每个勾选维度都必须命中）
    both = client.get(
        f"/v1/admin/memories?dimensions={dim_a},{dim_b}", headers=ADMIN
    ).json()
    both_ids = set(i["memory_id"] for i in both["items"])
    for item in both["items"]:
        assert dim_a in item["dimensions"] and dim_b in item["dimensions"], \
            "AND 语义失败：勾选维度未全部命中"
    # AND 结果 ≤ 任一单维度（交集不大于任一分量）
    assert both["total"] <= only_a["total"]
    assert both["total"] <= only_b["total"]
    # 无重复行
    assert len(both_ids) == len([i["memory_id"] for i in both["items"]])


def test_memories_sort_and_order(client, seeded):
    """sort=priority 与 order 生效。"""
    desc = client.get("/v1/admin/memories?sort=priority&order=desc", headers=ADMIN).json()
    asc = client.get("/v1/admin/memories?sort=priority&order=asc", headers=ADMIN).json()

    pri_desc = [i["priority"] for i in desc["items"]]
    pri_asc = [i["priority"] for i in asc["items"]]
    assert pri_desc == sorted(pri_desc, reverse=True)
    assert pri_asc == sorted(pri_asc)
    assert pri_desc == list(reversed(pri_asc))


def test_memories_since_until_apply_to_sort_field(client, seeded):
    """since/until 作用于 sort 指定的字段，而非固定 updated_at（契约 §5.3.2）。"""
    all_items = client.get(
        "/v1/admin/memories?sort=occurred_at&order=asc", headers=ADMIN
    ).json()["items"]
    assert len(all_items) >= 3
    boundary = all_items[2]["occurred_at"]

    filtered = client.get(
        f"/v1/admin/memories?sort=occurred_at&order=asc&since={boundary}", headers=ADMIN
    ).json()
    assert all(i["occurred_at"] >= boundary for i in filtered["items"])
    assert filtered["total"] == len([i for i in all_items if i["occurred_at"] >= boundary])

    until = client.get(
        f"/v1/admin/memories?sort=occurred_at&order=asc&until={boundary}", headers=ADMIN
    ).json()
    assert all(i["occurred_at"] <= boundary for i in until["items"])


def test_memories_ttl_filter(client, seeded):
    """ttl_filter 默认 false（浏览全部）；true 时滤掉 TTL 过期条目。"""
    default = client.get("/v1/admin/memories", headers=ADMIN).json()
    assert seeded["ttl_stale_id"] in {i["memory_id"] for i in default["items"]}

    filtered = client.get("/v1/admin/memories?ttl_filter=true", headers=ADMIN).json()
    assert seeded["ttl_stale_id"] not in {i["memory_id"] for i in filtered["items"]}
    assert filtered["total"] == default["total"] - 1


def test_memories_item_contract_shape(client, seeded):
    """条目字段集与契约 §5.3.3 一致；基线无 notes/custom_flag 列 → 返回 null。"""
    body = client.get("/v1/admin/memories?limit=1", headers=ADMIN).json()
    assert set(body.keys()) == {
        "items", "count", "total", "page", "limit", "generated_at",
    }
    item = body["items"][0]
    assert set(item.keys()) == {
        "memory_id", "content", "dimensions", "memory_type", "priority", "status",
        "created_at", "updated_at", "occurred_at", "notes", "custom_flag", "source_ref",
    }
    assert isinstance(item["dimensions"], list)
    assert item["source_ref"] is None or isinstance(item["source_ref"], str)
    # ST-14 尚未合并：两列在基线上不存在，必须降级为 null 而不是 500
    assert item["notes"] is None
    assert item["custom_flag"] is None


def test_memories_notes_columns_light_up_after_st14(client, conns, seeded):
    """防御性列探测的正向验证：ST-14 合并后列由迁移就绪，notes/custom_flag 自动生效。"""
    mem_conn, _, _ = conns
    # 列已由 _migrate_mem_idea_columns 就绪（ST-14 合并），无需手动 ALTER
    target = seeded["active_ids"][0]
    mem_conn.execute(
        "UPDATE memories SET notes=?, custom_flag=? WHERE memory_id=?",
        ('["灵感"]', "starred", target),
    )
    mem_conn.commit()

    items = client.get("/v1/admin/memories?limit=200", headers=ADMIN).json()["items"]
    hit = next(i for i in items if i["memory_id"] == target)
    assert hit["notes"] == '["灵感"]'
    assert hit["custom_flag"] == "starred"


# ============================================================
# §5.4 GET /v1/admin/scenes
# ============================================================

def test_scenes_default_active_and_heat_sort(client, seeded):
    """默认仅 active + 缺省按 heat 倒序（契约 §5.4.1）。"""
    body = client.get("/v1/admin/scenes", headers=ADMIN).json()
    assert body["total"] == 3
    assert {s["status"] for s in body["items"]} == {"active"}
    assert "scene_x" not in {s["scene_id"] for s in body["items"]}

    heats = [s["heat"] for s in body["items"]]
    assert heats == sorted(heats, reverse=True) == [30, 20, 10]


def test_scenes_item_shape_and_memories_count(client, seeded):
    """条目字段集与 memories_count 聚合正确（契约 §5.4.2）。

    related_memories 为 2026-08-18 T-55 后续有意新增（WebUI 场景详情展示关联记忆）。
    """
    body = client.get("/v1/admin/scenes", headers=ADMIN).json()
    by_id = {s["scene_id"]: s for s in body["items"]}
    assert set(by_id["scene_a"].keys()) == {
        "scene_id", "title", "content", "heat", "status",
        "memories_count", "created_at", "updated_at",
        "related_memories",
    }
    assert by_id["scene_a"]["memories_count"] == 2
    assert by_id["scene_b"]["memories_count"] == 0


def test_scenes_sort_status_and_limit_cap(client, seeded):
    """sort 白名单、status 显式过滤、limit 上限 200 同样生效。"""
    updated = client.get("/v1/admin/scenes?sort=updated_at&order=asc", headers=ADMIN).json()
    stamps = [s["updated_at"] for s in updated["items"]]
    assert stamps == sorted(stamps)

    rejected = client.get("/v1/admin/scenes?status=rejected", headers=ADMIN).json()
    assert [s["scene_id"] for s in rejected["items"]] == ["scene_x"]

    assert client.get("/v1/admin/scenes?limit=200", headers=ADMIN).status_code == 200
    over = client.get("/v1/admin/scenes?limit=201", headers=ADMIN)
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "ERR_INVALID_ARGS"
    assert client.get("/v1/admin/scenes?sort=heatxx", headers=ADMIN).status_code == 400


# ============================================================
# §5.5 GET /v1/admin/refine_runs
# ============================================================

def test_refine_runs_shows_all_statuses_by_default(client, seeded):
    """提炼监控**不做 status 缺省过滤**：error / running 默认可见。"""
    body = client.get("/v1/admin/refine_runs", headers=ADMIN).json()
    assert body["total"] == 5
    assert {r["status"] for r in body["items"]} == {"ok", "error", "running"}


def test_refine_runs_item_shape_and_filters(client, seeded):
    """条目字段集（契约 §5.5.2）+ stage/status 过滤。"""
    body = client.get("/v1/admin/refine_runs?limit=1", headers=ADMIN).json()
    assert set(body["items"][0].keys()) == {
        "run_id", "file_id", "stage", "version", "provider", "status", "error",
        "started_at", "finished_at", "memories_count", "action_counts",
        "prompt_tokens", "completion_tokens", "total_tokens",
    }
    # action_counts 按库内 JSON 字符串原样透传
    assert isinstance(body["items"][0]["action_counts"], str)

    stage_hits = client.get(
        "/v1/admin/refine_runs?stage=l1_extraction", headers=ADMIN
    ).json()
    assert stage_hits["total"] == 3
    assert {r["stage"] for r in stage_hits["items"]} == {"l1_extraction"}

    errors = client.get("/v1/admin/refine_runs?status=error", headers=ADMIN).json()
    assert [r["run_id"] for r in errors["items"]] == ["run_err"]
    assert errors["items"][0]["error"] == "LLM 超时"

    both = client.get(
        "/v1/admin/refine_runs?stage=l1_extraction&status=ok", headers=ADMIN
    ).json()
    assert both["total"] == 2


def test_refine_runs_time_window_and_ordering(client, seeded):
    """since/until 作用于 started_at；缺省 started_at DESC。"""
    body = client.get("/v1/admin/refine_runs", headers=ADMIN).json()
    stamps = [r["started_at"] for r in body["items"]]
    assert stamps == sorted(stamps, reverse=True)

    window = client.get(
        "/v1/admin/refine_runs?since=2026-08-10T00:00:00Z", headers=ADMIN
    ).json()
    assert window["total"] == 3
    assert all(r["started_at"] >= "2026-08-10T00:00:00Z" for r in window["items"])


def test_refine_runs_invalid_params_400(client, seeded):
    """未知 stage / status / limit 超限 → 400。"""
    for query in ("stage=l9_unknown", "status=weird", "limit=201", "page=0"):
        r = client.get(f"/v1/admin/refine_runs?{query}", headers=ADMIN)
        assert r.status_code == 400, query
        assert r.json()["error"]["code"] == "ERR_INVALID_ARGS"


# ============================================================
# §5.6 GET /v1/admin/sessions（+ 单条原文）
# ============================================================

def test_sessions_list_and_filters(client, seeded):
    """列表字段集（契约 §5.6.1）+ agent_id / status 过滤。"""
    body = client.get("/v1/admin/sessions", headers=ADMIN).json()
    assert body["total"] == 3
    assert set(body["items"][0].keys()) == {
        "file_id", "session_key", "agent_id", "status", "size",
        "started_at", "ended_at", "refined_at",
    }
    # 内部字段不外泄
    assert "path" not in body["items"][0]
    assert "content_hash" not in body["items"][0]

    hermes = client.get("/v1/admin/sessions?agent_id=hermes", headers=ADMIN).json()
    assert hermes["total"] == 2

    refined = client.get("/v1/admin/sessions?status=refined", headers=ADMIN).json()
    assert [s["file_id"] for s in refined["items"]] == ["f_hermes_1"]

    assert client.get("/v1/admin/sessions?status=bogus", headers=ADMIN).status_code == 400
    assert client.get("/v1/admin/sessions?limit=201", headers=ADMIN).status_code == 400


def test_sessions_session_key_substring_match(client, seeded):
    """session_key 是子串匹配（契约 §5.6.1），且 LIKE 元字符被转义。"""
    prefix = client.get("/v1/admin/sessions?session_key=hermes-", headers=ADMIN).json()
    assert prefix["total"] == 2

    middle = client.get("/v1/admin/sessions?session_key=aaa", headers=ADMIN).json()
    assert [s["file_id"] for s in middle["items"]] == ["f_hermes_1"]

    # '%' 被转义为字面量，不再是通配符 → 匹配不到任何行
    wildcard = client.get("/v1/admin/sessions?session_key=%25", headers=ADMIN).json()
    assert wildcard["total"] == 0


def test_session_detail_ok(client, seeded):
    """单条原文：返回 §4.7 同构四键，中文 UTF-8 不乱码。"""
    r = client.get("/v1/admin/sessions/f_hermes_1", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"file_id", "session_key", "agent_id", "content"}
    assert body["file_id"] == "f_hermes_1"
    assert body["session_key"] == "hermes-aaa"
    assert body["agent_id"] == "hermes"
    assert "中文原文全文，验证 UTF-8 不乱码。" in body["content"]


def test_session_detail_404(client, seeded):
    """file_id 不存在 / 磁盘原文缺失，均为 404 ERR_NOT_FOUND（契约 §5.6.2）。"""
    unknown = client.get("/v1/admin/sessions/does-not-exist", headers=ADMIN)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "ERR_NOT_FOUND"

    # f_hermes_2 有索引行但没写磁盘文件
    missing = client.get("/v1/admin/sessions/f_hermes_2", headers=ADMIN)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ERR_NOT_FOUND"


# ============================================================
# §5.7 GET /v1/admin/stats/detail
# ============================================================

def test_stats_detail_daily_grouping_and_totals(client, seeded):
    """daily 归组 + totals == 各项求和（契约 §5.7）。"""
    body = client.get("/v1/admin/stats/detail?period=daily", headers=ADMIN).json()
    assert set(body.keys()) == {"items", "totals", "generated_at"}

    assert set(body["items"][0].keys()) == {
        "period_key", "stage", "runs", "ok", "error",
        "prompt_tokens", "completion_tokens", "total_tokens", "memories_count",
    }
    # 时间升序，便于直接喂图表
    assert [i["period_key"] for i in body["items"]] == sorted(
        i["period_key"] for i in body["items"]
    )

    for field in ("runs", "ok", "error", "prompt_tokens",
                  "completion_tokens", "total_tokens", "memories_count"):
        assert body["totals"][field] == sum(i[field] for i in body["items"]), field

    assert body["totals"]["runs"] == 5
    assert body["totals"]["ok"] == 3
    assert body["totals"]["error"] == 1  # running 既不计 ok 也不计 error
    # 4 条有 token 的 run × 100/20/120；error 那条为 NULL → COALESCE 记 0
    assert body["totals"]["prompt_tokens"] == 400
    assert body["totals"]["completion_tokens"] == 80
    assert body["totals"]["total_tokens"] == 480


def test_stats_detail_daily_rows_split_by_stage(client, seeded):
    """无 stage 参数时按 (period_key, stage) 分行。"""
    items = client.get("/v1/admin/stats/detail?period=daily", headers=ADMIN).json()["items"]
    keyed = {(i["period_key"], i["stage"]): i for i in items}

    assert keyed[("2026-08-09", "l1_extraction")]["runs"] == 2
    assert keyed[("2026-08-09", "l1_extraction")]["ok"] == 2
    assert keyed[("2026-08-10", "l1_extraction")]["error"] == 1
    assert keyed[("2026-08-10", "l2_scene")]["runs"] == 1
    assert keyed[("2026-08-10", "tier0_summary")]["runs"] == 1


def test_stats_detail_weekly_buckets_to_monday(client, seeded):
    """weekly 归组到 ISO 周起（周一）：08-09 是周日 → 归 08-03。"""
    items = client.get("/v1/admin/stats/detail?period=weekly", headers=ADMIN).json()["items"]
    keys = {i["period_key"] for i in items}
    assert keys == {"2026-08-03", "2026-08-10"}

    week1 = [i for i in items if i["period_key"] == "2026-08-03"]
    assert sum(i["runs"] for i in week1) == 2  # 两条周日的 run


def test_stats_detail_monthly_and_stage_filter(client, seeded):
    """monthly 归到月首；stage 过滤后只剩该 stage 的行。"""
    monthly = client.get("/v1/admin/stats/detail?period=monthly", headers=ADMIN).json()
    assert {i["period_key"] for i in monthly["items"]} == {"2026-08-01"}
    assert monthly["totals"]["runs"] == 5

    staged = client.get(
        "/v1/admin/stats/detail?period=daily&stage=l1_extraction", headers=ADMIN
    ).json()
    assert {i["stage"] for i in staged["items"]} == {"l1_extraction"}
    assert staged["totals"]["runs"] == 3


def test_stats_detail_time_window_and_defaults(client, seeded):
    """from/to 作用于 started_at；period 缺省 weekly。"""
    default = client.get("/v1/admin/stats/detail", headers=ADMIN).json()
    weekly = client.get("/v1/admin/stats/detail?period=weekly", headers=ADMIN).json()
    assert [i["period_key"] for i in default["items"]] == [
        i["period_key"] for i in weekly["items"]
    ]

    windowed = client.get(
        "/v1/admin/stats/detail?period=daily&from=2026-08-10T00:00:00Z", headers=ADMIN
    ).json()
    assert {i["period_key"] for i in windowed["items"]} == {"2026-08-10"}
    assert windowed["totals"]["runs"] == 3


def test_stats_detail_invalid_params_400(client, seeded):
    """未知 period / stage / 非法时间戳 → 400。"""
    for query in ("period=hourly", "stage=nope", "from=garbage", "to=2026-99-99"):
        r = client.get(f"/v1/admin/stats/detail?{query}", headers=ADMIN)
        assert r.status_code == 400, query
        assert r.json()["error"]["code"] == "ERR_INVALID_ARGS"


# ============================================================
# 鉴权（六端点统一 require_admin_key）
# ============================================================

@pytest.mark.parametrize("path", BROWSE_ENDPOINTS)
def test_browse_endpoints_require_admin_key(client, seeded, path):
    """缺 Key / Agent Key 一律 403 ERR_FORBIDDEN，且不泄漏任何数据。"""
    no_key = client.get(path)
    assert no_key.status_code == 403
    assert no_key.json()["error"]["code"] == "ERR_FORBIDDEN"

    agent_key = client.get(path, headers={"X-API-Key": "test-agent-key"})
    assert agent_key.status_code == 403
    assert "items" not in agent_key.json()
