"""tests/test_operations_prompts.py：operations 层 prompts 链路测试（0.8 T-8）。

覆盖（参照 tests/test_operations_health.py 的 fixture 范式）：
1. 五个操作函数（prompts_list/publish/activate/ab/metrics）返回 OperationResult(ok=True)
2. data 的 HTTP 形态字段完整（与改造前 routes_prompts 响应逐字段一致）
3. **契约等价性（最关键）**：改造后端点响应 == operations 直调结果 == 既有测试
   （test_prompts_api / test_prompts_qa_acceptance）冻结的字段集合，逐字段一致
4. 错误路径：未知 stage / 缺占位符 / 版本不存在 / ab 缺 a/b / ab 引用非法 →
   operations 抛 InvalidArgs，端点 400 ERR_INVALID_ARGS（与改造前同码同文案）
5. 副作用保留：publish 落盘版本文件；activate/ab 写回 manifest 并被 list 反映
6. 鉴权不变：非 Admin Key → 403
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.refine_dao import RefineRunRecorder
from sgme.operations.errors import InvalidArgs, OperationResult
from sgme.operations.prompts import (
    prompts_ab,
    prompts_activate,
    prompts_list,
    prompts_metrics,
    prompts_publish,
)
from sgme.prompts import PromptManifestError, PromptStore
from sgme.raw import store as raw_store
from sgme.server.app import create_app

# ---------- v0.6/#33 冻结契约（改造前逐字段抄录，任何变动即破坏性变更） ----------

# GET /v1/admin/prompts：顶层仅 stages；每条 stage 四字段（构造顺序）
LIST_TOP_KEYS = ["stages"]
LIST_STAGE_KEYS = ["stage", "active", "ab", "versions"]
# 版本元数据（VersionInfo dataclass 字段序）
VERSION_INFO_KEYS = ["version", "file", "sha256", "created_at", "note"]
# POST /publish：{"status": "ok", "version": VersionInfo}
PUBLISH_TOP_KEYS = ["status", "version"]
# POST /activate
ACTIVATE_TOP_KEYS = ["status", "stage", "active"]
# POST /ab：enabled=true 回显 a/b/split/bucket_by；false 时不回显
AB_ENABLED_KEYS = ["status", "stage", "ab_enabled", "a", "b", "split", "bucket_by"]
AB_DISABLED_KEYS = ["status", "stage", "ab_enabled"]
# GET /metrics（RefineRunRecorder.summarize 契约，test_prompts_qa_acceptance 冻结）
METRICS_TOP_KEYS = ["stage", "since", "groups"]
# 组内键序按 RefineRunRecorder.summarize 实际构造顺序冻结（memories_rows/avg_priority
# 在分组后补挂，故排在 action_dist 之后——与 test_prompts_qa_acceptance 的集合断言等价，
# 此处更严格：键序也锁定）
METRICS_GROUP_KEYS = [
    "version", "variant", "runs", "error_runs",
    "memories_count", "action_dist", "memories_rows", "avg_priority",
]

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

STAGE_TEXTS = {
    "tier0_summary": "你是摘要器。\n{{memories}}",
    "l1_extraction": "你是提取器。\n{{dimensions}}\n{{conversation}}",
    "l1_conflict": "你是裁决器。\n{{new_memories}}\n{{candidates}}",
    "l2_scene": "你是聚合器。\n{{new_memories}}\n{{existing_scenes}}\n{{max_scenes}}",
}


# ---------- fixtures ----------

@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 目录（与 test_prompts_api 一致）。"""
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    return rd


@pytest.fixture
def prompts_root(tmp_path):
    """隔离 prompts 目录（4 个工作副本，无 manifest → 默认全 @working）。"""
    root = tmp_path / "prompts"
    root.mkdir()
    for stage, text in STAGE_TEXTS.items():
        (root / f"{stage}.txt").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def ops_env(monkeypatch, prompts_root):
    """直调 operations 的隔离环境：PromptStore 类属性指向临时目录（无需起 app）。

    操作函数默认构造 PromptStore() 时读取类属性，故 monkeypatch 后
    prompts_list()/prompts_publish()/... 直接落在 prompts_root 上。
    """
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    return prompts_root


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir, prompts_root):
    """隔离 FastAPI 应用（与 test_prompts_api 同款 fixture，契约等价对照用）。"""
    from sgme.profile import tier0 as tier0_mod
    from sgme.server.app import create_app

    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _seed_metrics(mem_conn) -> None:
    """构造 2 个 run（v001/None 与 v002/A）+ 1 条带 prompt_version 的记忆（同 test_prompts_api）。"""
    r1 = RefineRunRecorder.start(mem_conn, "f1", "l1_extraction", "v001", None, "lm-studio", "f1")
    RefineRunRecorder.finish(mem_conn, r1, 2, {}, "ok")
    r2 = RefineRunRecorder.start(mem_conn, "f2", "l1_extraction", "v002", "A", "deepseek", "f2")
    RefineRunRecorder.finish(mem_conn, r2, 1, {"store": 1}, "ok")
    memory_dao.insert_memory(
        mem_conn, content="m", memory_type="persona", priority=90,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v001",
    )


def _publish_twice_for_ab(client) -> None:
    """发布 v001 + v002（工作副本改一次），供 A/B 测试用。"""
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    (PromptStore.PROMPTS_ROOT / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版", encoding="utf-8",
    )
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)


# ---------- 1. 返回类型 ----------

def test_prompts_list_returns_operation_result_ok(ops_env):
    """prompts_list() 返回 OperationResult 且 ok=True。"""
    res = prompts_list()
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert isinstance(res.data, dict)


def test_prompts_publish_returns_operation_result_ok(ops_env):
    """prompts_publish() 返回 OperationResult 且 ok=True。"""
    res = prompts_publish("l1_extraction", note="基线")
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None


def test_prompts_activate_returns_operation_result_ok(ops_env):
    """prompts_activate() 返回 OperationResult 且 ok=True。"""
    prompts_publish("l1_extraction")
    res = prompts_activate("l1_extraction", "v001")
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None


def test_prompts_ab_returns_operation_result_ok(ops_env):
    """prompts_ab() 返回 OperationResult 且 ok=True（启用与关闭两分支）。"""
    prompts_publish("l1_extraction")
    (PromptStore.PROMPTS_ROOT / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版", encoding="utf-8",
    )
    prompts_publish("l1_extraction")
    for kwargs in ({"a": "v001", "b": "v002", "split": 0.3}, {"enabled": False}):
        res = prompts_ab("l1_extraction", **kwargs)
        assert isinstance(res, OperationResult)
        assert res.ok is True


def test_prompts_metrics_returns_operation_result_ok(app):
    """prompts_metrics() 返回 OperationResult 且 ok=True。"""
    _seed_metrics(app.state.mem_conn)
    res = prompts_metrics(app.state.mem_conn, "l1_extraction")
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None


# ---------- 2. HTTP 形态字段完整 ----------

def test_list_data_shape_default_working(ops_env):
    """默认 @working：4 stage，每段字段齐全，active=@working，versions 空。"""
    data = prompts_list().data
    assert list(data.keys()) == LIST_TOP_KEYS
    assert len(data["stages"]) == 4
    for stage in data["stages"]:
        assert list(stage.keys()) == LIST_STAGE_KEYS
    l1 = next(s for s in data["stages"] if s["stage"] == "l1_extraction")
    assert l1["active"] == "@working"
    assert l1["ab"] == {"enabled": False}
    assert l1["versions"] == []


def test_publish_data_shape_complete(ops_env):
    """publish 后 data 含 status + version（VersionInfo 五字段）。"""
    data = prompts_publish("l1_extraction", note="基线").data
    assert list(data.keys()) == PUBLISH_TOP_KEYS
    assert data["status"] == "ok"
    assert list(data["version"].keys()) == VERSION_INFO_KEYS
    assert data["version"]["version"] == "v001"
    assert data["version"]["file"] == "versions/l1_extraction/v001.txt"
    assert data["version"]["sha256"]
    assert data["version"]["created_at"]
    assert data["version"]["note"] == "基线"


def test_activate_data_shape_complete(ops_env):
    """activate 后 data 含 status/stage/active。"""
    prompts_publish("l1_extraction")
    data = prompts_activate("l1_extraction", "v001").data
    assert list(data.keys()) == ACTIVATE_TOP_KEYS
    assert data == {"status": "ok", "stage": "l1_extraction", "active": "v001"}


def test_ab_data_shape_enabled_and_disabled(ops_env):
    """ab 启用回显 a/b/split/bucket_by；关闭只回 status/stage/ab_enabled。"""
    prompts_publish("l1_extraction")
    (PromptStore.PROMPTS_ROOT / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版", encoding="utf-8",
    )
    prompts_publish("l1_extraction")

    enabled = prompts_ab("l1_extraction", "v001", "v002", split=0.3).data
    assert list(enabled.keys()) == AB_ENABLED_KEYS
    assert enabled["status"] == "ok"
    assert enabled["ab_enabled"] is True
    assert enabled["a"] == "v001"
    assert enabled["b"] == "v002"
    assert enabled["split"] == 0.3
    assert enabled["bucket_by"] == "file_id"

    disabled = prompts_ab("l1_extraction", enabled=False).data
    assert list(disabled.keys()) == AB_DISABLED_KEYS
    assert disabled["ab_enabled"] is False
    assert "a" not in disabled and "b" not in disabled


def test_metrics_data_shape_complete(app):
    """metrics 按 (version, variant) 分组返回 runs/memories/avg_priority/action_dist。"""
    _seed_metrics(app.state.mem_conn)
    data = prompts_metrics(app.state.mem_conn, "l1_extraction").data
    assert list(data.keys()) == METRICS_TOP_KEYS
    assert data["stage"] == "l1_extraction"
    assert data["since"] is None
    by_key = {g["version"] + (g["variant"] or ""): g for g in data["groups"]}
    for g in data["groups"]:
        assert list(g.keys()) == METRICS_GROUP_KEYS
    assert by_key["v001"]["runs"] == 1
    assert by_key["v001"]["memories_count"] == 2
    assert by_key["v001"]["memories_rows"] == 1
    assert by_key["v001"]["avg_priority"] == 90.0
    assert by_key["v001"]["action_dist"] == {}
    assert by_key["v002A"]["runs"] == 1
    assert by_key["v002A"]["action_dist"] == {"store": 1}
    assert by_key["v002A"]["avg_priority"] is None


def test_ops_accept_explicit_store(prompts_root):
    """store 显式注入（指向临时目录）与默认构造等价，作为测试/复用注入点。"""
    store = PromptStore(prompts_root=prompts_root)
    data = prompts_publish("l1_extraction", note="注入", store=store).data
    assert data["version"]["version"] == "v001"
    assert (prompts_root / "versions" / "l1_extraction" / "v001.txt").exists()


# ---------- 3. 契约等价性（最关键） ----------

def test_http_list_contract_unchanged(client):
    """GET /v1/admin/prompts：字段集合与顺序仍与改造前一致，且 == operations 直调结果。"""
    # 状态构造全走端点（与既有测试同路径）：publish ×2 → activate v001 → ab 0.3
    _publish_twice_for_ab(client)
    client.post("/v1/admin/prompts/activate",
                json={"stage": "l1_extraction", "version_ref": "v001"}, headers=ADMIN_HEADERS)
    client.post("/v1/admin/prompts/ab",
                json={"stage": "l1_extraction", "a": "v001", "b": "v002", "split": 0.3},
                headers=ADMIN_HEADERS)

    resp = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == LIST_TOP_KEYS
    assert len(body["stages"]) == 4
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert list(l1.keys()) == LIST_STAGE_KEYS
    assert l1["active"] == "v001"
    assert l1["ab"]["enabled"] is True
    assert l1["ab"]["split"] == 0.3
    assert set(l1["ab"].keys()) == {"enabled", "a", "b", "split", "bucket_by"}
    assert list(l1["versions"][0].keys()) == VERSION_INFO_KEYS
    assert l1["versions"][0]["version"] == "v001"

    # 等价性：同一状态下 operations 直调结果与端点响应逐字节一致
    assert prompts_list().data == body


def test_http_publish_contract_unchanged(client):
    """POST /publish：响应 == 冻结字段集合；operations 直调结构一致 + 版本递增。"""
    resp = client.post("/v1/admin/prompts/publish",
                       json={"stage": "l1_extraction", "note": "基线"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == PUBLISH_TOP_KEYS
    assert body["status"] == "ok"
    assert list(body["version"].keys()) == VERSION_INFO_KEYS
    assert body["version"]["version"] == "v001"
    assert body["version"]["note"] == "基线"

    # operations 直调：同一字段结构，版本继续递增（v002）
    op_data = prompts_publish("l1_extraction", note="再发").data
    assert list(op_data.keys()) == PUBLISH_TOP_KEYS
    assert list(op_data["version"].keys()) == VERSION_INFO_KEYS
    assert op_data["version"]["version"] == "v002"


def test_http_activate_contract_unchanged(client):
    """POST /activate：响应 == 冻结字段集合；@working 与钉版两分支。"""
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    resp = client.post("/v1/admin/prompts/activate",
                       json={"stage": "l1_extraction", "version_ref": "v001"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == ACTIVATE_TOP_KEYS
    assert body == {"status": "ok", "stage": "l1_extraction", "active": "v001"}

    op_data = prompts_activate("l1_extraction", "@working").data
    assert list(op_data.keys()) == ACTIVATE_TOP_KEYS
    assert op_data["active"] == "@working"


def test_http_ab_contract_unchanged(client):
    """POST /ab：启用/关闭两分支响应字段 == 冻结集合；operations 直调结构一致。"""
    _publish_twice_for_ab(client)

    resp = client.post("/v1/admin/prompts/ab",
                       json={"stage": "l1_extraction", "a": "v001", "b": "v002", "split": 0.3},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == AB_ENABLED_KEYS
    assert body == {"status": "ok", "stage": "l1_extraction", "ab_enabled": True,
                    "a": "v001", "b": "v002", "split": 0.3, "bucket_by": "file_id"}

    op_data = prompts_ab("l1_extraction", "v001", "v002", split=0.7).data
    assert list(op_data.keys()) == AB_ENABLED_KEYS
    assert op_data["split"] == 0.7

    resp2 = client.post("/v1/admin/prompts/ab",
                        json={"stage": "l1_extraction", "enabled": False},
                        headers=ADMIN_HEADERS)
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert list(body2.keys()) == AB_DISABLED_KEYS
    assert body2 == {"status": "ok", "stage": "l1_extraction", "ab_enabled": False}

    op_data2 = prompts_ab("l1_extraction", enabled=False).data
    assert list(op_data2.keys()) == AB_DISABLED_KEYS
    assert op_data2["ab_enabled"] is False


def test_http_metrics_contract_unchanged(app, client):
    """GET /metrics：响应字段 == QA 验收冻结集合，且 == operations 直调结果。"""
    _seed_metrics(app.state.mem_conn)
    resp = client.get("/v1/admin/prompts/metrics", params={"stage": "l1_extraction"},
                      headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == METRICS_TOP_KEYS
    assert set(body.keys()) == {"stage", "since", "groups"}  # test_prompts_qa_acceptance 冻结
    assert len(body["groups"]) == 2
    for g in body["groups"]:
        assert list(g.keys()) == METRICS_GROUP_KEYS
        assert set(g.keys()) == set(METRICS_GROUP_KEYS)
    # 无自动裁决字段（红线 §6 #1）
    banned = ("winner", "recommend", "conclusion", "verdict", "decision", "胜", "推荐")
    assert all(not any(b in str(k).lower() for b in banned) for k in body.keys())
    assert all(not any(b in str(k).lower() for b in banned) for g in body["groups"] for k in g.keys())

    # 等价性：同一状态下 operations 直调结果与端点响应逐字节一致
    assert prompts_metrics(app.state.mem_conn, "l1_extraction").data == body


def test_list_broken_manifest_matches_historical_failure(app, prompts_root):
    """manifest 损坏（active 指向不存在文件）→ 与改造前同行为：PromptManifestError 上抛 → 500。

    注意：原 routes_prompts.list_prompts 的 try/except 只包 stage_config，而
    list_versions 先抛——坏 manifest 从未被"容错"过（stage 级容错仅对
    stage_config 单点异常生效）。抽取后行为必须一致：不吞异常，交给入口层
    全局异常处理器 → 500 ERR_INTERNAL。
    """
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    import yaml
    m = yaml.safe_load((prompts_root / "manifest.yaml").read_text(encoding="utf-8"))
    m["stages"]["l1_extraction"]["active"] = "versions/l1_extraction/v999.txt"
    (prompts_root / "manifest.yaml").write_text(
        yaml.safe_dump(m, allow_unicode=True), encoding="utf-8",
    )
    # operations 直调：与旧路由同源异常（PromptManifestError，不经 InvalidArgs）
    with pytest.raises(PromptManifestError):
        prompts_list()
    # 端点：全局异常处理器兜底 → 500
    resp = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "ERR_INTERNAL"


# ---------- 4. 错误路径 ----------

def test_publish_unknown_stage_invalid_args(ops_env):
    """未知 stage → operations 抛 InvalidArgs；端点 400 ERR_INVALID_ARGS。"""
    with pytest.raises(InvalidArgs, match="未知 stage"):
        prompts_publish("no_such")


def test_publish_unknown_stage_400(client):
    resp = client.post("/v1/admin/prompts/publish", json={"stage": "no_such"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_publish_missing_placeholder_400(client, prompts_root):
    """工作副本缺必备占位符 → 400（PromptStore 语义校验）。"""
    (prompts_root / "l1_extraction.txt").write_text("缺占位符", encoding="utf-8")
    resp = client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_activate_missing_version_invalid_args(ops_env):
    """激活不存在的版本 → operations 抛 InvalidArgs。"""
    with pytest.raises(InvalidArgs, match="版本文件不存在"):
        prompts_activate("l1_extraction", "v999")


def test_activate_missing_version_400(client):
    client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    resp = client.post("/v1/admin/prompts/activate",
                       json={"stage": "l1_extraction", "version_ref": "v999"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_ab_missing_a_b_400(client):
    """enabled=true 且 a/b 缺失 → 400（同改造前文案）。"""
    resp = client.post("/v1/admin/prompts/ab",
                       json={"stage": "l1_extraction"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"
    assert resp.json()["error"]["message"] == "enabled=true 时 a/b 必填"


def test_ab_invalid_ref_invalid_args(ops_env):
    """ab 引用不存在的版本 → operations 抛 InvalidArgs。"""
    prompts_publish("l1_extraction")
    with pytest.raises(InvalidArgs, match="ab 文件不存在"):
        prompts_ab("l1_extraction", "v001", "v999", split=0.5)


def test_ab_invalid_ref_400(client):
    _publish_twice_for_ab(client)
    resp = client.post("/v1/admin/prompts/ab",
                       json={"stage": "l1_extraction", "a": "v001", "b": "v999", "split": 0.5},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_metrics_unknown_stage_invalid_args(app):
    """未知 stage → operations 抛 InvalidArgs（校验先于 DB 访问）。"""
    with pytest.raises(InvalidArgs, match="未知 stage"):
        prompts_metrics(app.state.mem_conn, "bad")


def test_metrics_unknown_stage_400(client):
    resp = client.get("/v1/admin/prompts/metrics", params={"stage": "bad"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


# ---------- 5. 副作用保留 ----------

def test_publish_writes_version_file(client, prompts_root):
    """publish 落盘版本文件（原子写产物，无临时残留）。"""
    resp = client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                       headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    ver_dir = prompts_root / "versions" / "l1_extraction"
    assert (ver_dir / "v001.txt").read_text(encoding="utf-8") == STAGE_TEXTS["l1_extraction"]
    assert [p.name for p in ver_dir.iterdir()] == ["v001.txt"]


def test_activate_and_ab_persist_to_manifest(client):
    """activate/ab 写回 manifest 并被 list 反映（状态在端点间可见）。"""
    _publish_twice_for_ab(client)
    client.post("/v1/admin/prompts/activate",
                json={"stage": "l1_extraction", "version_ref": "v001"}, headers=ADMIN_HEADERS)
    client.post("/v1/admin/prompts/ab",
                json={"stage": "l1_extraction", "a": "v001", "b": "v002", "split": 0.3,
                      "bucket_by": "memory_id"},
                headers=ADMIN_HEADERS)
    body = client.get("/v1/admin/prompts", headers=ADMIN_HEADERS).json()
    l1 = next(s for s in body["stages"] if s["stage"] == "l1_extraction")
    assert l1["active"] == "v001"
    assert l1["ab"]["enabled"] is True
    assert l1["ab"]["split"] == 0.3
    assert l1["ab"]["bucket_by"] == "memory_id"


# ---------- 6. 鉴权 ----------

def test_prompts_endpoints_still_require_admin(client):
    """Agent Key / 无 Key → 403（鉴权仍属入口层职责，未被抽取破坏）。"""
    assert client.get("/v1/admin/prompts", headers=AGENT_HEADERS).status_code == 403
    assert client.get("/v1/admin/prompts").status_code == 403
    assert client.post("/v1/admin/prompts/publish", json={"stage": "l1_extraction"},
                       headers=AGENT_HEADERS).status_code == 403
    assert client.get("/v1/admin/prompts/metrics", params={"stage": "l1_extraction"}).status_code == 403
