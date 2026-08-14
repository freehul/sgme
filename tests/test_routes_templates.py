"""T-16 测试：模板管理 HTTP 端点（契约 §5.8，Admin Key）。

覆盖四个端点的协议层行为：
- 鉴权：无 Key / Agent Key → 403（四个端点逐一验证）
- GET    /v1/admin/templates        → 200 列表 + limit/offset 分页
- POST   /v1/admin/templates        → 200 新建；重名 → 409 ERR_CONFLICT
- PUT    /v1/admin/templates/{name} → 200 保存；非法 YAML / 校验失败 / name 不一致 → 400
- DELETE /v1/admin/templates/{name} → 内置 400、不存在 404、自定义 200

隔离：``sgme.profile.template.TEMPLATES_DIR`` 指向 tmp_path，真实 templates/ 只读。
fixture 形状照 tests/test_routes_admin.py（同一 app 装配范式）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.profile import template as template_mod
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}

REAL_TEMPLATES_DIR = sgme_config.PROJECT_ROOT / "templates"
BUILTIN_NAMES = ("coding", "daily", "full", "work")


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
def no_bearer(monkeypatch):
    """清除 SGME_BEARER_TOKEN（create_app 有 setdefault 的进程级副作用，见 test_routes_admin）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)


@pytest.fixture
def tpl_dir(tmp_path, monkeypatch) -> Path:
    """隔离模板目录（复制 4 个内置模板），真实 templates/ 不被写入。"""
    d = tmp_path / "templates"
    d.mkdir()
    for n in BUILTIN_NAMES:
        shutil.copyfile(REAL_TEMPLATES_DIR / f"{n}.yaml", d / f"{n}.yaml")
    monkeypatch.setattr(template_mod, "TEMPLATES_DIR", d)
    return d


@pytest.fixture
def app(cfg, conns, no_bearer, tpl_dir, tmp_path):
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def _body(name: str = "custom", display: str = "自定义模式") -> dict:
    """结构合法的模板 body（Σ(limit)=8 → 240 tokens ≤ 700）。"""
    return {
        "name": name,
        "display_name": display,
        "memory_types": ["identity", "projects", "tasks"],
        "token_budget": 700,
        "sections": [
            {"title": "👤 身份", "query": {"dimensions": ["identity"], "priority_min": 70, "limit": 5}},
            {"title": "📁 项目", "query": {"dimensions": ["projects", "tasks"], "match": "any", "limit": 3}},
        ],
    }


# ---------- 鉴权（四端点） ----------

# (method, path, 是否带 JSON body)——httpx 的 get/delete 不接受 json 参数，故分开标注
_ENDPOINTS = [
    ("get", "/v1/admin/templates", False),
    ("post", "/v1/admin/templates", True),
    ("put", "/v1/admin/templates/custom", True),
    ("delete", "/v1/admin/templates/custom", False),
]


def _call(client, method: str, path: str, with_body: bool, headers: dict | None = None):
    """统一发起请求（get/delete 不带 body）。"""
    kwargs: dict = {}
    if headers is not None:
        kwargs["headers"] = headers
    if with_body:
        kwargs["json"] = _body()
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path,with_body", _ENDPOINTS)
def test_requires_admin_key(client, method, path, with_body):
    """无 X-API-Key → 403 ERR_FORBIDDEN。"""
    r = _call(client, method, path, with_body)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR_FORBIDDEN"


@pytest.mark.parametrize("method,path,with_body", _ENDPOINTS)
def test_agent_key_forbidden(client, method, path, with_body):
    """Agent Key（非 Admin）→ 403。"""
    r = _call(client, method, path, with_body, headers=AGENT_HEADERS)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR_FORBIDDEN"


# ---------- GET 列表 ----------

def test_list_templates_ok(client):
    """§5.8.1：200 + items/count/total/generated_at，item 含 content 原文。"""
    r = client.get("/v1/admin/templates", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()

    assert body["total"] == 4
    assert body["count"] == 4
    assert body["generated_at"].endswith("Z")
    assert [i["name"] for i in body["items"]] == sorted(BUILTIN_NAMES)

    daily = next(i for i in body["items"] if i["name"] == "daily")
    for key in ("name", "display_name", "memory_types", "token_budget", "sections", "content"):
        assert key in daily
    assert daily["display_name"] == "日常模式"
    assert daily["token_budget"] == 700
    assert yaml.safe_load(daily["content"])["name"] == "daily"


def test_list_templates_pagination(client):
    """limit/offset 生效（对齐 SCSM list_templates(limit, offset)）。"""
    r = client.get("/v1/admin/templates?limit=2&offset=2", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["total"] == 4
    assert [i["name"] for i in body["items"]] == ["full", "work"]


def test_list_templates_invalid_limit_400(client):
    """limit 越界 → 400 ERR_INVALID_ARGS（范围校验在 operations 层）。"""
    r = client.get("/v1/admin/templates?limit=0", headers=ADMIN_HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_INVALID_ARGS"


# ---------- POST 新建 ----------

def test_create_template_ok(client, tpl_dir):
    """§5.8.3：新建 → {created: true, name, restart_required}。"""
    r = client.post("/v1/admin/templates", json=_body("brandnew"), headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["name"] == "brandnew"
    assert body["restart_required"] is False
    assert (tpl_dir / "brandnew.yaml").exists()


def test_create_duplicate_returns_409(client, tpl_dir):
    """§5.8.3：重名 → 409 ERR_CONFLICT，既有文件不被覆盖。"""
    before = (tpl_dir / "daily.yaml").read_bytes()

    r = client.post("/v1/admin/templates", json=_body("daily"), headers=ADMIN_HEADERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ERR_CONFLICT"
    assert (tpl_dir / "daily.yaml").read_bytes() == before


def test_create_from_yaml_content(client, tpl_dir):
    """content（YAML 全文）作为写入源，原文落盘。"""
    content = (
        "# from content\n"
        "name: yamlmade\n"
        "display_name: YAML 建的\n"
        "memory_types: [identity]\n"
        "token_budget: 700\n"
        "sections:\n"
        "  - title: T\n"
        "    query:\n"
        "      dimensions: [identity]\n"
        "      limit: 4\n"
    )
    r = client.post("/v1/admin/templates", json={"content": content}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["name"] == "yamlmade"
    assert (tpl_dir / "yamlmade.yaml").read_text(encoding="utf-8").startswith("# from content")


# ---------- PUT 更新 ----------

def test_update_template_ok(client, tpl_dir):
    """§5.8.2：更新 → {saved: true, restart_required: false}，内容真实写盘。"""
    body = _body("daily", display="改过的日常")
    r = client.put("/v1/admin/templates/daily", json=body, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["saved"] is True
    assert r.json()["restart_required"] is False

    on_disk = yaml.safe_load((tpl_dir / "daily.yaml").read_text(encoding="utf-8"))
    assert on_disk["display_name"] == "改过的日常"


def test_update_invalid_yaml_400(client, tpl_dir):
    """非法 YAML → 400 ERR_INVALID_ARGS，message 带解析详情，且不落盘。"""
    r = client.put(
        "/v1/admin/templates/badyaml",
        json={"content": "name: badyaml\n  broken: [unclosed\n"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "ERR_INVALID_ARGS"
    assert "YAML 解析失败" in err["message"]
    assert not (tpl_dir / "badyaml.yaml").exists()


def test_update_validation_failure_400(client, tpl_dir):
    """维度越界（section.dimensions ⊄ memory_types）→ 400，message 带校验详情。"""
    body = _body("oob")
    body["memory_types"] = ["identity"]
    body["sections"] = [{"title": "T", "query": {"dimensions": ["projects"], "limit": 3}}]

    r = client.put("/v1/admin/templates/oob", json=body, headers=ADMIN_HEADERS)
    assert r.status_code == 400
    assert "模板校验失败" in r.json()["error"]["message"]
    assert not (tpl_dir / "oob.yaml").exists()


def test_update_token_budget_exceeded_400(client):
    """Σ(limit)×AVG > token_budget → 400。"""
    body = _body("overbudget")
    body["token_budget"] = 100
    r = client.put("/v1/admin/templates/overbudget", json=body, headers=ADMIN_HEADERS)
    assert r.status_code == 400
    assert "token 预算超限" in r.json()["error"]["message"]


def test_update_name_mismatch_400(client):
    """§5.8.2：body.name 必须与路径一致。"""
    r = client.put("/v1/admin/templates/pathname", json=_body("othername"), headers=ADMIN_HEADERS)
    assert r.status_code == 400
    assert "不一致" in r.json()["error"]["message"]


def test_update_path_traversal_400(client, tpl_dir):
    """安全：路径穿越模板名被拒，且不产生任何越界文件。

    状态码可接受：名称非法 400；starlette 路由先行拒绝 404；
    SPA catch-all（app.py 静态托管）令规范化后的无 PUT 路由路径回 405
    （路径穿越被拒即可，重点是不写越界文件）。
    """
    r = client.put("/v1/admin/templates/..%2Fevil", json=_body("evil"), headers=ADMIN_HEADERS)
    assert r.status_code in (400, 404, 405)
    assert sorted(p.stem for p in tpl_dir.glob("*.yaml")) == sorted(BUILTIN_NAMES)
    assert not (tpl_dir.parent / "evil.yaml").exists()


# ---------- DELETE ----------

@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_delete_builtin_400(client, tpl_dir, name):
    """§5.8.4：内置模板拒绝删除 → 400，文件仍在。"""
    r = client.delete(f"/v1/admin/templates/{name}", headers=ADMIN_HEADERS)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "ERR_INVALID_ARGS"
    assert "内置模板不可删" in err["message"]
    assert (tpl_dir / f"{name}.yaml").exists()


def test_delete_missing_404(client):
    """§5.8.4：不存在 → 404 ERR_NOT_FOUND。"""
    r = client.delete("/v1/admin/templates/nope", headers=ADMIN_HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_delete_custom_ok(client, tpl_dir):
    """§5.8.4：删除自定义模板 → 200 {deleted: true}。"""
    assert client.post("/v1/admin/templates", json=_body("temp"), headers=ADMIN_HEADERS).status_code == 200
    assert (tpl_dir / "temp.yaml").exists()

    r = client.delete("/v1/admin/templates/temp", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert not (tpl_dir / "temp.yaml").exists()


# ---------- 端到端 CRUD ----------

def test_full_crud_cycle(client, tpl_dir):
    """增 → 查（列表含新模板且 valid）→ 改 → 删 的完整闭环。"""
    assert client.post("/v1/admin/templates", json=_body("cycle"), headers=ADMIN_HEADERS).status_code == 200

    listed = client.get("/v1/admin/templates", headers=ADMIN_HEADERS).json()
    assert listed["total"] == 5
    entry = next(i for i in listed["items"] if i["name"] == "cycle")
    assert entry["valid"] is True
    assert entry["builtin"] is False

    updated = _body("cycle", display="改名了")
    assert client.put("/v1/admin/templates/cycle", json=updated, headers=ADMIN_HEADERS).status_code == 200
    again = client.get("/v1/admin/templates", headers=ADMIN_HEADERS).json()
    assert next(i for i in again["items"] if i["name"] == "cycle")["display_name"] == "改名了"

    assert client.delete("/v1/admin/templates/cycle", headers=ADMIN_HEADERS).status_code == 200
    final = client.get("/v1/admin/templates", headers=ADMIN_HEADERS).json()
    assert final["total"] == 4
    assert "cycle" not in [i["name"] for i in final["items"]]
