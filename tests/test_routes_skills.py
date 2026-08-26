"""tests/test_routes_skills.py：技能仓库 CRUD 端点测试。

覆盖：
1. ``GET /v1/admin/skills`` 返回列表 / 基础信息（enabled/mode/path）
2. ``PUT /v1/admin/skills/{name}`` 写入技能
3. ``GET /v1/admin/skills/{name}`` 读取技能全文
4. ``DELETE /v1/admin/skills/{name}`` 删除技能（幂等）
5. 鉴权：agent key → 403
6. skills_hub 未启用 → 400
7. 健壮性：名字非法 / 内容为空 → 400；不存在 → 404

零真实网络：全部走本地临时目录，不触发 git/远端。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}
ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    from sgme import config as _c
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(_c, "RAW_DIR", raw)
    return raw


@pytest.fixture
def app(tmp_path, monkeypatch, raw_dir):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    cfg = sgme_config.load_config()
    # 开启技能仓库（map 模式指向临时目录）
    cfg["skills_hub"] = {
        "enabled": True,
        "mode": "map",
        "path": str(tmp_path / "skills"),
    }

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 鉴权 ----------

def test_skills_requires_admin(app, client):
    resp = client.get("/v1/admin/skills", headers=AGENT_HEADERS)
    assert resp.status_code == 403


# ---------- 列表 / 基础信息 ----------

def test_skills_list_empty(app, client):
    resp = client.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["mode"] == "map"
    assert data["total"] == 0
    assert data["skills"] == []


# ---------- 写入 / 读取 / 删除 ----------

def test_skills_crud_flow(app, client):
    # 写入
    resp = client.put(
        "/v1/admin/skills/my-skill",
        headers=ADMIN_HEADERS,
        json={"content": "# 我的技能\n\n你好，世界。"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "my-skill"

    # 列表出现
    resp = client.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    data = resp.json()
    assert data["total"] == 1
    assert data["skills"] == ["my-skill"]

    # 读取原文
    resp = client.get("/v1/admin/skills/my-skill", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert "你好，世界。" in resp.json()["content"]

    # 删除
    resp = client.delete("/v1/admin/skills/my-skill", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # 删除后列表为空
    resp = client.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    assert resp.json()["total"] == 0


def test_skills_delete_idempotent(app, client):
    resp = client.delete("/v1/admin/skills/ghost", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False


# ---------- 健壮性 ----------

def test_skills_get_missing_returns_404(app, client):
    resp = client.get("/v1/admin/skills/ghost", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


def test_skills_put_invalid_name(app, client):
    # 含空格的技能名不过白名单（字母/数字/下划线/中划线/点）
    resp = client.put(
        "/v1/admin/skills/bad name",
        headers=ADMIN_HEADERS,
        json={"content": "x"},
    )
    assert resp.status_code == 400


def test_skills_put_empty_content(app, client):
    resp = client.put(
        "/v1/admin/skills/ok",
        headers=ADMIN_HEADERS,
        json={"content": "   "},
    )
    assert resp.status_code == 400


# ---------- 未启用 ----------

def test_skills_disabled_returns_400(app_disabled, client_disabled):
    resp = client_disabled.get("/v1/admin/skills", headers=ADMIN_HEADERS)
    assert resp.status_code == 400


@pytest.fixture
def app_disabled(tmp_path, monkeypatch, raw_dir):
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    cfg = sgme_config.load_config()
    cfg["skills_hub"] = {"enabled": False}

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def client_disabled(app_disabled):
    return TestClient(app_disabled)


# =====================================================================
# ST-36 M2：四级披露**读侧**端点（sgme/server/routes_skills.py）
#
# 与上方 /v1/admin/skills（skills_hub 写仓管理，admin key）不同：
# 读侧挂 /v1/skills*（agent key），数据源 = sgme.skills 索引
# （cfg["skills"].source_dirs git 工作区 ∪ wiki skill 标记页）。
# skills.enabled=false → 整个 router 不挂载（404，镜像 care 模块开关方式）。
# =====================================================================

def _make_skill_tree(tmp_path):
    """造双技能 git 源目录（alpha 带 frontmatter+uses+两节正文；beta 无 frontmatter）。"""
    d = tmp_path / "skills_tree"
    (d / "alpha").mkdir(parents=True)
    (d / "alpha" / "SKILL.md").write_text(
        "---\n"
        "name: alpha\n"
        "description: 技能A简介——NAS 部署流水线\n"
        "version: 1.2.0\n"
        "category: deploy\n"
        "tags: [skill, deploy]\n"
        "uses:\n"
        "  - beta\n"
        "---\n"
        "# Alpha 总纲\n"
        "docker compose up -d\n"
        "\n"
        "## 步骤\n"
        "先 build 再 up\n"
        "\n"
        "## 踩坑\n"
        "端口冲突先查 netstat\n",
        encoding="utf-8",
    )
    (d / "beta").mkdir()
    (d / "beta" / "SKILL.md").write_text("# Beta 无frontmatter", encoding="utf-8")
    return d


def _make_wiki_conn_skill_page():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT,"
        " content TEXT, category TEXT, tags TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO wiki_pages VALUES ('w1','skill:wiki-skill','# Wiki 技能 NAS',"
        "'skill/common','[\"skill\"]','active')"
    )
    return conn


@pytest.fixture
def read_app(tmp_path, monkeypatch, raw_dir):
    from fastapi.testclient import TestClient

    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    cfg = sgme_config.load_config()
    cfg["skills"] = {
        "enabled": True,
        "source_dirs": [str(_make_skill_tree(tmp_path))],
        "budget": 40,
    }
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # 插入一条 wiki skill 标记页（读侧双源之一：git ∪ wiki）
    from sgme.data import wiki_dao

    wiki_dao.insert_page(wiki_conn, page_id="w1", title="skill:wiki-skill",
                         content="# Wiki 技能 NAS 部署", category="skill/common",
                         tags=["skill"], status="active")

    app = create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )
    yield app, tmp_path
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def read_client(read_app):
    _, _ = read_app
    from fastapi.testclient import TestClient

    app, _tmp = read_app
    return TestClient(app)


class TestSkillsReadEndpoints:
    def test_list_l0(self, read_app, read_client):
        resp = read_client.get("/v1/skills", headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        names = {s["name"] for s in data["skills"]}
        assert {"alpha", "beta", "wiki-skill"} <= names
        alpha = next(s for s in data["skills"] if s["name"] == "alpha")
        assert alpha["description"].startswith("技能A简介")
        assert alpha["category"] == "deploy"

    def test_list_requires_agent_key(self, read_client):
        assert read_client.get("/v1/skills").status_code in (401, 403)

    def test_digest_l1(self, read_client):
        resp = read_client.get("/v1/skills/alpha/digest", headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["version"] == "1.2.0"
        assert d["uses"] == ["beta"]
        assert any("踩坑" in s for s in d["sections"])
        assert d["sha256"]

    def test_digest_missing_404(self, read_client):
        resp = read_client.get("/v1/skills/ghost/digest", headers=AGENT_HEADERS)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ERR_NOT_FOUND"

    def test_get_l2_full(self, read_client):
        resp = read_client.get("/v1/skills/alpha", headers=AGENT_HEADERS)
        assert resp.status_code == 200
        assert "netstat" in resp.json()["content"]

    def test_get_l2_section(self, read_client):
        resp = read_client.get("/v1/skills/alpha",
                               params={"section": "踩坑"}, headers=AGENT_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert "netstat" in body["content"]
        assert "docker compose" not in body["content"]

    def test_get_unknown_section_404(self, read_client):
        resp = read_client.get("/v1/skills/alpha",
                               params={"section": "不存在"}, headers=AGENT_HEADERS)
        assert resp.status_code == 404

    def test_materialize_l3_real_file(self, read_app, read_client, tmp_path):
        import hashlib
        from pathlib import Path

        app, _ = read_app
        dest = tmp_path / "agent_ws"
        src_bytes = None
        # 从索引配置找到源文件字节做保真对照
        tree = app.state.cfg["skills"]["source_dirs"][0]
        src_bytes = (Path(tree) / "alpha" / "SKILL.md").read_bytes()

        resp = read_client.post("/v1/skills/alpha/materialize",
                                json={"dest_dir": str(dest)}, headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        out = Path(data["path"])
        assert out.is_file() and out.name == "SKILL.md" and out.parent.name == "alpha"
        # 字节保真（L3 铁律：不走 LLM 转写）
        assert out.read_bytes() == src_bytes
        expect_sha = hashlib.sha256(src_bytes).hexdigest()
        assert data["sha256"] == expect_sha
        # 幂等：重复物化同路径覆盖，sha 不变
        resp2 = read_client.post("/v1/skills/alpha/materialize",
                                 json={"dest_dir": str(dest)}, headers=AGENT_HEADERS)
        assert resp2.json()["sha256"] == expect_sha

    def test_materialize_requires_dest_dir(self, read_client):
        resp = read_client.post("/v1/skills/alpha/materialize",
                                json={}, headers=AGENT_HEADERS)
        assert resp.status_code in (400, 422)

    def test_search_via_unified_endpoint(self, read_client):
        """统一搜索 scopes=["skills"] 经 HTTP 端到端可用。"""
        resp = read_client.post("/v1/search",
                                json={"query": "NAS 部署", "scopes": ["skills"]},
                                headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        hits = [r for r in resp.json()["results"] if r["source"] == "skills"]
        assert hits and hits[0]["name"] == "alpha"


class TestSkillsReadDisabled:
    """skills.enabled=false → 读侧路由不挂载（镜像 care 扩展模块开关语义）。"""

    @pytest.fixture
    def disabled_read_app(self, tmp_path, monkeypatch, raw_dir):
        from sgme.data import db as db_mod
        from sgme.data import memory_dao
        from sgme.server.app import create_app

        monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
        monkeypatch.setenv("SGME_HOME", str(tmp_path))
        cfg = sgme_config.load_config()
        cfg["skills"] = {"enabled": False}
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data2")
        memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
        app = create_app(
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key",
            agent_key="test-agent-key",
            bearer_token="",
            agent_store_path=tmp_path / "agent_keys2.json",
        )
        yield app
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)

    def test_endpoints_404_when_disabled(self, disabled_read_app):
        client = TestClient(disabled_read_app)
        assert client.get("/v1/skills", headers=AGENT_HEADERS).status_code == 404
        assert client.get("/v1/skills/alpha/digest", headers=AGENT_HEADERS).status_code == 404
        assert client.get("/v1/skills/alpha", headers=AGENT_HEADERS).status_code == 404
        assert client.post("/v1/skills/alpha/materialize",
                           json={"dest_dir": "x"}, headers=AGENT_HEADERS).status_code == 404