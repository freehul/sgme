"""tests/test_operations_registry.py：operations 层 registry 链路测试（0.8 T-8）。

覆盖：
1. 操作函数返回 OperationResult（成功 ok=True / 业务失败 ok=False + 错误码）
2. data 的 HTTP 形态字段完整（对照改造前逐字段冻结的键序）
3. **契约等价性**（最关键）：/v1/admin/registry 各端点经 operations 包装后，
   响应字段集合/键序/取值与改造前 tests/test_registry_api.py 冻结的行为逐条一致
   （错误码 400/404、幂等 upsert、active_only 过滤、鉴权 403）
4. 副作用保留：#33 refresh_dimensions——写库后 cfg['dimensions'] 即时刷新
   （新增维度注入 / 停用维度退出）
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs, OperationResult
from sgme.operations.registry import (
    DIMENSION_FIELD_KEYS,
    http_payload,
    registry_create_alias,
    registry_create_dim,
    registry_delete_alias,
    registry_get,
    registry_list,
    registry_update_dim,
)
from sgme.server.app import create_app

# ---------- 改造前冻结契约（test_registry_api.py 逐字段抄录，任何变动即破坏性变更） ----------

# 端点响应顶层键（键序即响应体键序）
LIST_TOP_KEYS = ["total", "dimensions"]
GET_TOP_KEYS = ["dimension"]
CREATE_TOP_KEYS = ["status", "dimension"]
UPDATE_TOP_KEYS = ["status", "dimension_id", "updates"]
ALIAS_TOP_KEYS = ["status", "alias", "dimension_id"]
DELETE_TOP_KEYS = ["status", "alias", "deleted"]
# 维度对象字段（dimension_registry 全列 + aliases，键序 = 表列序 + aliases）
# POST /dimensions 回显的 dimension 是归一化入参（不含 active/created_at）
CREATE_DIMENSION_FIELD_KEYS = [
    "id", "display_name", "category", "time_velocity", "ttl_days", "description",
    "boundaries",  # T-11：create 回显含 boundaries（入参可选，缺失为 None）
]

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


# ---------- fixtures（范式照抄 test_operations_health.py） ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）+ 注册表导入。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
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


# ---------- 工具 ----------

def _new_dim(**overrides) -> dict:
    """构造合法的维度入参（键集与 DimensionCreateRequest 一致）。"""
    dim = {
        "id": "ops_test_dim",
        "display_name": "运维测试维度",
        "category": "偏好",
        "time_velocity": "static",
        "ttl_days": None,
        "description": "operations 层测试新增",
    }
    dim.update(overrides)
    return dim


def _dim_ids(data: dict) -> list[str]:
    return [d["id"] for d in data["dimensions"]]


# ---------- 1. 操作函数返回 OperationResult ----------

def test_registry_list_returns_operation_result_ok(conns):
    """registry_list 返回 OperationResult 且 ok=True，total 与改造前一致（15）。"""
    mem_conn, _, _ = conns
    res = registry_list(mem_conn)
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert res.data["total"] == len(sgme_config.load_config()["dimensions"])


def test_registry_get_returns_operation_result_ok(conns):
    """registry_get 已知维度 → ok=True，dimension 含 aliases。"""
    mem_conn, _, _ = conns
    res = registry_get(mem_conn, "identity")
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.data["dimension"]["id"] == "identity"
    assert res.data["dimension"]["display_name"] == "身份"
    assert "身份" in res.data["dimension"]["aliases"]


def test_registry_get_unknown_dimension_fails_not_found(conns):
    """registry_get 未知维度 → ok=False + ERR_NOT_FOUND。"""
    mem_conn, _, _ = conns
    res = registry_get(mem_conn, "nonexistent")
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.message == "维度不存在: nonexistent"


def test_registry_create_dim_returns_operation_result_ok(conns, cfg):
    """registry_create_dim → ok=True，dimension 为归一化入参回显（6 键）。"""
    mem_conn, _, _ = conns
    res = registry_create_dim(mem_conn, cfg, dim=_new_dim())
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.data["status"] == "ok"
    assert list(res.data["dimension"].keys()) == CREATE_DIMENSION_FIELD_KEYS
    assert res.data["dimension"]["id"] == "ops_test_dim"


def test_registry_create_dim_invalid_id_raises_invalid_args(conns, cfg):
    """非法维度 id（非 snake_case）→ 抛 InvalidArgs（→ HTTP 400）。"""
    mem_conn, _, _ = conns
    with pytest.raises(InvalidArgs):
        registry_create_dim(mem_conn, cfg, dim=_new_dim(id="Bad Dim!"))


@pytest.mark.parametrize("bad_category", ["乱码", "STATIC"])
def test_registry_create_dim_invalid_category_raises_invalid_args(conns, cfg, bad_category):
    """category 不在白名单 → 抛 InvalidArgs。"""
    mem_conn, _, _ = conns
    with pytest.raises(InvalidArgs):
        registry_create_dim(mem_conn, cfg, dim=_new_dim(category=bad_category))


def test_registry_create_dim_invalid_velocity_raises_invalid_args(conns, cfg):
    """time_velocity 不在白名单 → 抛 InvalidArgs。"""
    mem_conn, _, _ = conns
    with pytest.raises(InvalidArgs):
        registry_create_dim(mem_conn, cfg, dim=_new_dim(time_velocity="weekly"))


def test_registry_update_dim_returns_operation_result_ok(conns, cfg):
    """registry_update_dim → ok=True，updates 入参回显。"""
    mem_conn, _, _ = conns
    res = registry_update_dim(mem_conn, cfg, "identity", updates={"display_name": "身份信息"})
    assert res.ok is True
    assert res.data["status"] == "ok"
    assert res.data["dimension_id"] == "identity"
    assert res.data["updates"] == {"display_name": "身份信息"}


def test_registry_update_dim_unknown_fails_not_found(conns):
    """registry_update_dim 未知维度 → ok=False + ERR_NOT_FOUND。"""
    mem_conn, _, _ = conns
    res = registry_update_dim(mem_conn, cfg, "nonexistent", updates={"active": False})
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_registry_create_alias_returns_operation_result_ok(conns, cfg):
    """registry_create_alias → ok=True，alias/dimension_id 回显。"""
    mem_conn, _, _ = conns
    res = registry_create_alias(mem_conn, cfg, alias="技术选型", dimension_id="tech_stack")
    assert res.ok is True
    assert res.data == {"status": "ok", "alias": "技术选型", "dimension_id": "tech_stack"}


def test_registry_create_alias_unknown_dim_fails_not_found(conns):
    """registry_create_alias 目标维度不存在 → ok=False + ERR_NOT_FOUND。"""
    mem_conn, _, _ = conns
    res = registry_create_alias(mem_conn, cfg, alias="孤儿别名", dimension_id="nonexistent")
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_registry_delete_alias_returns_operation_result_ok(conns, cfg):
    """registry_delete_alias → ok=True，deleted=1。"""
    mem_conn, _, _ = conns
    registry_create_alias(mem_conn, cfg, alias="临时别名", dimension_id="identity")
    res = registry_delete_alias(mem_conn, cfg, "临时别名")
    assert res.ok is True
    assert res.data == {"status": "ok", "alias": "临时别名", "deleted": 1}


def test_registry_delete_alias_unknown_fails_not_found(conns, cfg):
    """registry_delete_alias 别名不存在 → ok=False + ERR_NOT_FOUND。"""
    mem_conn, _, _ = conns
    res = registry_delete_alias(mem_conn, cfg, "不存在的别名")
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


# ---------- 2. HTTP 形态字段完整（http_payload 投影） ----------

def test_list_http_shape_complete(conns):
    """registry_list 的 data 经 http_payload 后：顶层键序 + 维度字段键序冻结。"""
    mem_conn, _, _ = conns
    data = registry_list(mem_conn).data
    body = http_payload(data)
    assert list(body.keys()) == LIST_TOP_KEYS
    assert len(body["dimensions"]) == body["total"] == len(sgme_config.load_config()["dimensions"])
    for d in body["dimensions"]:
        assert list(d.keys()) == DIMENSION_FIELD_KEYS


def test_get_http_shape_complete(conns):
    """registry_get 的 data 经 http_payload 后：dimension 字段键序冻结。"""
    mem_conn, _, _ = conns
    body = http_payload(registry_get(mem_conn, "identity").data)
    assert list(body.keys()) == GET_TOP_KEYS
    assert list(body["dimension"].keys()) == DIMENSION_FIELD_KEYS


def test_create_http_shape_complete(conns, cfg):
    """registry_create_dim 的 data 经 http_payload 后：status + 6 键 dimension。"""
    mem_conn, _, _ = conns
    body = http_payload(registry_create_dim(mem_conn, cfg, dim=_new_dim()).data)
    assert list(body.keys()) == CREATE_TOP_KEYS
    assert list(body["dimension"].keys()) == CREATE_DIMENSION_FIELD_KEYS


def test_update_http_shape_complete(conns, cfg):
    """registry_update_dim 的 data 经 http_payload 后：status/dimension_id/updates。"""
    mem_conn, _, _ = conns
    body = http_payload(
        registry_update_dim(mem_conn, cfg, "identity", updates={"active": False}).data
    )
    assert list(body.keys()) == UPDATE_TOP_KEYS
    assert body["status"] == "ok"
    assert body["dimension_id"] == "identity"
    assert body["updates"] == {"active": False}


def test_alias_http_shape_complete(conns, cfg):
    """registry_create_alias / registry_delete_alias 的 data 投影键序冻结。"""
    mem_conn, _, _ = conns
    create_body = http_payload(
        registry_create_alias(mem_conn, cfg, alias="技术选型", dimension_id="tech_stack").data
    )
    assert list(create_body.keys()) == ALIAS_TOP_KEYS
    delete_body = http_payload(registry_delete_alias(mem_conn, cfg, "技术选型").data)
    assert list(delete_body.keys()) == DELETE_TOP_KEYS


# ---------- 3. 契约等价性（最关键）：端点响应与改造前逐条一致 ----------

def test_list_endpoint_contract_unchanged(client):
    """GET /v1/admin/registry：total/维度集合/别名与改造前逐字段一致。"""
    resp = client.get("/v1/admin/registry", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == LIST_TOP_KEYS
    assert body["total"] == len(sgme_config.load_config()["dimensions"])
    dims = {d["id"] for d in body["dimensions"]}
    assert {"identity", "family", "goals", "tech_stack"} <= dims
    identity = next(d for d in body["dimensions"] if d["id"] == "identity")
    assert list(identity.keys()) == DIMENSION_FIELD_KEYS
    assert "身份" in identity["aliases"]


def test_get_endpoint_contract_unchanged(client):
    """GET /v1/admin/registry/{dim_id}：单维度详情与改造前一致。"""
    resp = client.get("/v1/admin/registry/identity", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert list(body.keys()) == GET_TOP_KEYS
    assert body["dimension"]["id"] == "identity"
    assert body["dimension"]["display_name"] == "身份"


def test_get_unknown_endpoint_contract_404(client):
    """GET 未知维度 → 404（错误码 ERR_NOT_FOUND）。"""
    resp = client.get("/v1/admin/registry/nonexistent", headers=ADMIN_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_create_endpoint_contract_unchanged(client):
    """POST /v1/admin/registry/dimensions：新增 + 幂等 + 出现在列表（改造前逐条一致）。"""
    payload = {
        "id": "testdim",
        "display_name": "测试维度",
        "category": "偏好",
        "time_velocity": "static",
        "description": "审计测试新增",
    }
    resp = client.post("/v1/admin/registry/dimensions", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == CREATE_TOP_KEYS
    assert body["status"] == "ok"
    assert list(body["dimension"].keys()) == CREATE_DIMENSION_FIELD_KEYS
    # 幂等：重复提交不报错
    resp2 = client.post("/v1/admin/registry/dimensions", json=payload, headers=ADMIN_HEADERS)
    assert resp2.status_code == 200
    # 出现在列表
    resp3 = client.get("/v1/admin/registry", headers=ADMIN_HEADERS)
    assert "testdim" in [d["id"] for d in resp3.json()["dimensions"]]


def test_create_invalid_id_endpoint_contract_400(client):
    """POST 非法维度 id（非 snake_case）→ 400（错误码 ERR_INVALID_ARGS）。"""
    resp = client.post("/v1/admin/registry/dimensions", json={
        "id": "Bad Dim!", "display_name": "坏维度",
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


def test_update_endpoint_contract_unchanged(client):
    """PUT 停用维度：默认列表不含、active_only=false 含（改造前逐条一致）。"""
    client.post("/v1/admin/registry/dimensions", json={
        "id": "tempdim", "display_name": "临时维度", "category": "动态",
    }, headers=ADMIN_HEADERS)
    resp = client.put("/v1/admin/registry/dimensions/tempdim", json={"active": False},
                      headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert list(resp.json().keys()) == UPDATE_TOP_KEYS
    resp_list = client.get("/v1/admin/registry", headers=ADMIN_HEADERS)
    assert "tempdim" not in [d["id"] for d in resp_list.json()["dimensions"]]
    resp_all = client.get("/v1/admin/registry?active_only=false", headers=ADMIN_HEADERS)
    assert "tempdim" in [d["id"] for d in resp_all.json()["dimensions"]]


def test_alias_create_endpoint_contract_unchanged(client):
    """POST /v1/admin/registry/aliases：别名出现在维度详情（改造前逐条一致）。"""
    resp = client.post("/v1/admin/registry/aliases", json={
        "alias": "技术选型", "dimension_id": "tech_stack",
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert list(resp.json().keys()) == ALIAS_TOP_KEYS
    resp2 = client.get("/v1/admin/registry/tech_stack", headers=ADMIN_HEADERS)
    assert "技术选型" in resp2.json()["dimension"]["aliases"]


def test_alias_delete_endpoint_contract_unchanged(client):
    """DELETE /v1/admin/registry/aliases/{alias}：删除后详情不含（改造前逐条一致）。"""
    client.post("/v1/admin/registry/aliases", json={
        "alias": "临时别名", "dimension_id": "identity",
    }, headers=ADMIN_HEADERS)
    resp = client.delete("/v1/admin/registry/aliases/临时别名", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert list(resp.json().keys()) == DELETE_TOP_KEYS
    assert resp.json()["deleted"] == 1
    resp2 = client.get("/v1/admin/registry/identity", headers=ADMIN_HEADERS)
    assert "临时别名" not in resp2.json()["dimension"]["aliases"]


def test_requires_admin_contract_unchanged(client):
    """Agent Key 调 registry → 403（改造前逐条一致）。"""
    for method, path, kwargs in [
        ("get", "/v1/admin/registry", {}),
        ("post", "/v1/admin/registry/dimensions", {"json": {"id": "x", "display_name": "x"}}),
    ]:
        resp = getattr(client, method)(path, headers=AGENT_HEADERS, **kwargs)
        assert resp.status_code == 403


def test_operation_and_endpoint_agree(client, conns, cfg):
    """同一状态下，operations 直调 + http_payload 与端点响应逐字段一致。"""
    mem_conn, _, _ = conns
    # 直调路径
    op_body = http_payload(registry_list(mem_conn).data)
    # 端点路径
    http_body = client.get("/v1/admin/registry", headers=ADMIN_HEADERS).json()
    # 逐字段一致（键序 + 取值）
    assert list(http_body.keys()) == list(op_body.keys()) == LIST_TOP_KEYS
    assert http_body["total"] == op_body["total"]
    assert [list(d.keys()) for d in http_body["dimensions"]] \
        == [list(d.keys()) for d in op_body["dimensions"]]
    assert http_body == op_body


# ---------- 4. 副作用保留：#33 refresh_dimensions ----------

def test_create_dim_refreshes_cfg_dimensions(conns, cfg):
    """写库后 cfg['dimensions'] 即时刷新：新增维度注入。"""
    mem_conn, _, _ = conns
    assert "ops_test_dim" not in _dim_ids(cfg)
    res = registry_create_dim(mem_conn, cfg, dim=_new_dim())
    assert res.ok is True
    assert "ops_test_dim" in _dim_ids(cfg)


def test_deactivate_dim_refreshes_cfg_dimensions(conns, cfg):
    """停用维度 → refresh 后 cfg['dimensions'] 不再含它（注入退出）。"""
    mem_conn, _, _ = conns
    registry_create_dim(mem_conn, cfg, dim=_new_dim(id="ops_tmp_dim"))
    assert "ops_tmp_dim" in _dim_ids(cfg)
    res = registry_update_dim(mem_conn, cfg, "ops_tmp_dim", updates={"active": False})
    assert res.ok is True
    assert "ops_tmp_dim" not in _dim_ids(cfg)
