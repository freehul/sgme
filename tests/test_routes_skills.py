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
    # 治理版 skills 模块 source_dirs 显式置空：钉住「旧 hub 兼容回退」契约。
    # 不置空会读到资源包默认 /app/cache/skills/（B150 起随包分发，非空），
    # 写侧被治理版接管（门禁 400 / 删除不存在 404），下方旧契约用例全崩
    # （2026-09-07 实锤；T-153）。
    cfg["skills"] = {"enabled": False, "source_dirs": []}

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


# ---------- 治理版写侧主路径（skills.source_dirs 已配置，B114 后主路径；T-153） ----------

def _gov_git_init(d):
    """治理版 store 落盘后要在目录内 commit，测试目录需自带 git 仓。"""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=str(d), check=True)
    subprocess.run(["git", "config", "user.email", "test@sgme.local"], cwd=str(d), check=True)
    subprocess.run(["git", "config", "user.name", "SGME Test"], cwd=str(d), check=True)


def _gov_cfg(app, tmp_path):
    """给当前 app 挂一个临时 git 技能仓，返回该目录。"""
    hub = tmp_path / "gov_hub"
    hub.mkdir()
    _gov_git_init(hub)
    app.state.cfg["skills"] = {"enabled": True, "source_dirs": [str(hub)], "budget": 40}
    return hub


def test_governed_put_rejects_bare_body_with_violations(app, client, tmp_path):
    """治理版接管（source_dirs 非空）：无 frontmatter 裸正文 → 400 + violations 清单。"""
    _gov_cfg(app, tmp_path)
    resp = client.put(
        "/v1/admin/skills/my-skill",
        headers=ADMIN_HEADERS,
        json={"content": "# 我的技能\n\n你好，世界。"},
    )
    assert resp.status_code == 400
    violations = resp.json()["error"]["details"]["violations"]
    assert any("description" in v for v in violations)


def test_governed_put_and_delete_roundtrip(app, client, tmp_path):
    """治理版主路径：合规 SKILL.md 写入 200 → 落盘 + commit；删除不存在 → 404。"""
    hub = _gov_cfg(app, tmp_path)
    content = (
        "---\nname: my-skill\ndescription: 测试技能——治理版写入链路\n"
        "category: test\n---\n\n# 我的技能\n\n你好，世界。\n"
    )
    resp = client.put("/v1/admin/skills/my-skill", headers=ADMIN_HEADERS,
                      json={"content": content})
    assert resp.status_code == 200, resp.text
    assert (hub / "my-skill" / "SKILL.md").is_file()
    # 治理版契约：删除不存在的技能 → 404（区别于旧 hub 兼容回退的幂等 200）
    resp = client.delete("/v1/admin/skills/ghost", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


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
    # 注：2026-08-28 起 wiki 桥接已移除，技能仅来自 source_dirs（git）；
    # 此处 wiki 页不再贡献技能索引（下方断言 wiki-skill 不应出现）。
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
        assert {"alpha", "beta"} <= names
        # 2026-08-28：wiki 桥接已移除，wiki 页不应出现在技能索引
        assert "wiki-skill" not in names
        alpha = next(s for s in data["skills"] if s["name"] == "alpha")
        assert alpha["description"].startswith("技能A简介")
        assert alpha["category"] == "deploy"

    def test_list_requires_agent_key(self, read_client):
        assert read_client.get("/v1/skills").status_code in (401, 403)

    # ---------- T-163 对齐补齐：技能检索 HTTP 端点（与 MCP skill_search 同实现） ----------

    def test_search_endpoint(self, read_client):
        resp = read_client.get("/v1/skills/search", params={"q": "alpha"},
                               headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["query"] == "alpha"
        assert data["count"] == len(data["items"])
        # alpha 名称命中；字段结构（name/score/source）
        hit = next((s for s in data["items"] if s["name"] == "alpha"), None)
        assert hit is not None, f"items={data['items']}"
        assert {"name", "score", "source"} <= set(hit)

    def test_search_not_swallowed_by_name_route(self, read_client):
        """/v1/skills/search 须注册在 /{name} 之前（否则被当成技能名 404）。"""
        resp = read_client.get("/v1/skills/search", params={"q": "beta"},
                               headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        assert "items" in resp.json()

    def test_search_requires_agent_key(self, read_client):
        resp = read_client.get("/v1/skills/search", params={"q": "alpha"})
        assert resp.status_code in (401, 403)

    def test_search_missing_query(self, read_client):
        # q 必填（min_length=1）→ 422 统一错误结构
        resp = read_client.get("/v1/skills/search", headers=AGENT_HEADERS)
        assert resp.status_code == 422

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
        # T-232：成功响应标注落盘侧（产物在服务端文件系统）
        assert data["landing_side"] == "server"
        # 幂等：重复物化同路径覆盖，sha 不变
        resp2 = read_client.post("/v1/skills/alpha/materialize",
                                 json={"dest_dir": str(dest)}, headers=AGENT_HEADERS)
        assert resp2.json()["sha256"] == expect_sha

    def test_materialize_requires_dest_dir(self, read_client):
        resp = read_client.post("/v1/skills/alpha/materialize",
                                json={}, headers=AGENT_HEADERS)
        assert resp.status_code in (400, 422)

    def test_materialize_crossmachine_drive_path_400(self, read_client, monkeypatch):
        """T-232：非 Windows 服务端收到盘符 dest → 400 ERR_INVALID_ARGS（不再假成功）。"""
        monkeypatch.setattr("sgme.operations.skills._server_os", lambda: "posix")
        resp = read_client.post("/v1/skills/alpha/materialize",
                                json={"dest_dir": "C:/t232-probe"}, headers=AGENT_HEADERS)
        assert resp.status_code == 400, resp.text
        err = resp.json()["error"]
        assert err["code"] == "ERR_INVALID_ARGS"
        assert "服务端" in err["message"] and "skill_get" in err["message"]

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
        # POST materialize：SPA catch-all（GET /{full_path:path}，ui/dist 存在时注册）
        # 会通配命中该路径但方法不符 → Starlette 返回 405 而非 404。
        # 语义上「禁用=不可用」已由 GET 三连 404 验证；POST 接受 404/405 两种形态
        # （主仓有 ui/dist 构建产物 → 405；干净 checkout 无 dist → 404）。
        st = client.post("/v1/skills/alpha/materialize",
                         json={"dest_dir": "x"}, headers=AGENT_HEADERS).status_code
        assert st in (404, 405)

# ---------- T-217：/v1/admin/skills/reindex 的 rebuild_fts 请求体 ----------


class TestSkillsReindexEndpoint:
    """reindex 运维兜底端点（T-215 引入；T-217 加 rebuild_fts 请求体 + 缺行自检）。"""

    @pytest.fixture
    def reindex_client(self, tmp_path, monkeypatch, raw_dir):
        from sgme.data import db as db_mod
        from sgme.data import memory_dao
        from sgme.server.app import create_app

        monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
        monkeypatch.setenv("SGME_HOME", str(tmp_path))
        cfg = sgme_config.load_config()
        cfg["skills"] = {
            "enabled": True,
            "source_dirs": [str(tmp_path / "skills_src")],
            "budget": 40,
        }
        (tmp_path / "skills_src" / "alpha").mkdir(parents=True)
        (tmp_path / "skills_src" / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: 测试技能 NAS 部署\n---\n# Alpha\n正文\n",
            encoding="utf-8",
        )
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
        memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
        app = create_app(
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            data_dir=tmp_path / "data",
            admin_key="test-admin-key",
            agent_key="test-agent-key",
            agent_store_path=tmp_path / "agent_keys.json",
        )
        yield TestClient(app)
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)

    def test_reindex_no_body_still_ok(self, reindex_client):
        """不带请求体（旧调用姿势）保持 200，且响应带 T-217 新字段。"""
        r = reindex_client.post("/v1/admin/skills/reindex", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["inserted"] == 1
        assert body["fts_rebuilt"] is False
        assert "pending_embed" in body  # embed=False 也如实上报（修复假 0）

    def test_reindex_rebuild_fts_flag(self, reindex_client):
        r = reindex_client.post(
            "/v1/admin/skills/reindex", headers=ADMIN_HEADERS, json={"rebuild_fts": True}
        )
        assert r.status_code == 200
        assert r.json()["fts_rebuilt"] is True

    def test_reindex_rejects_agent_key(self, reindex_client):
        r = reindex_client.post("/v1/admin/skills/reindex", headers=AGENT_HEADERS)
        assert r.status_code == 403


# =====================================================================
# T-281：跨机包通道（GET /v1/skills/{name}/package + GET /v1/skills/{name}/files/{relpath}）
#
# 与 materialize 同源同过滤（operations.iter_package_files）；归档根 MANIFEST.sha256，
# 归档内全部为服务端生成的相对路径（无宿主路径）。
# =====================================================================


def _fill_pkg_tree(tree) -> None:
    """给 alpha 铺随附文件：顶层 install.py / 深层中文路径 / 1 MiB 大文件 / junk / 空目录。"""
    from pathlib import Path

    root = Path(tree) / "alpha"
    (root / "install.py").write_text("# install\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "sgme_client.py").write_text("print('client')\n", encoding="utf-8")
    deep = root / "corpus" / "金匮要略"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "序.md").write_text("# 序\n\n金匮要略第一。\n", encoding="utf-8")
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "big.bin").write_bytes(bytes(range(256)) * 4096)  # 1 MiB 可复现样本
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "x.pyc").write_bytes(b"junk")
    (root / "debug.log").write_text("noise\n", encoding="utf-8")
    (root / "empty").mkdir(exist_ok=True)


@pytest.fixture
def pkg_client(tmp_path, monkeypatch, raw_dir):
    """读侧 app（agent key）+ 富随附文件树；三连接全传非 None（防静默切真实库）。"""
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.server.app import create_app

    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    monkeypatch.setenv("SGME_HOME", str(tmp_path))

    tree = _make_skill_tree(tmp_path)
    _fill_pkg_tree(tree)
    cfg = sgme_config.load_config()
    cfg["skills"] = {"enabled": True, "source_dirs": [str(tree)], "budget": 40}

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
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
    yield TestClient(app), app, tree
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _read_tar_gz(blob: bytes):
    """解析 tar.gz：返回 (成员名列表, {成员名: 原始字节})。"""
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        files = {
            m.name: tf.extractfile(m).read()
            for m in tf.getmembers()
            if m.isfile()
        }
        return tf.getnames(), files


def _parse_manifest(text: str) -> dict:
    """canonical manifest → {relpath: sha256}（校验行格式）。"""
    out = {}
    for line in text.split("\n")[:-1]:
        assert len(line) > 66 and line[64:66] == "  ", f"manifest 行格式非法: {line!r}"
        out[line[66:]] = line[:64]
    return out


class TestSkillPackageEndpoint:
    def test_download_bytes_headers_and_manifest(self, pkg_client):
        """包下载：字节保真 + X-Skill-* 头 + 归档结构（MANIFEST 平级、无宿主路径）。"""
        import hashlib
        from pathlib import Path

        client, _app, tree = pkg_client
        src = Path(tree) / "alpha"
        resp = client.get("/v1/skills/alpha/package", headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/gzip"
        assert resp.headers["content-disposition"] == 'attachment; filename="alpha.tar.gz"'
        sha = resp.headers["X-Skill-Sha256"]
        n_files = int(resp.headers["X-Skill-Files"])

        names, files = _read_tar_gz(resp.content)
        assert names[0] == "MANIFEST.sha256"
        manifest_blob = files.pop("MANIFEST.sha256")
        expected = {
            "alpha/SKILL.md", "alpha/install.py", "alpha/scripts/sgme_client.py",
            "alpha/corpus/金匮要略/序.md", "alpha/assets/big.bin",
        }
        assert set(files) == expected, set(files) ^ expected
        # 字节保真：逐文件与服务端源文件 sha256 一致（含 1 MiB 大文件与中文路径）
        for arc, blob in files.items():
            rel = arc.split("/", 1)[1]
            assert blob == (src / rel).read_bytes(), rel
            if rel == "assets/big.bin":
                assert len(blob) == 1024 * 1024
        # junk / 空目录不入包
        assert not any("__pycache__" in n or n.endswith(".log") for n in files)
        assert not any(n.endswith("/empty") or "empty/" in n for n in files)

        manifest_text = manifest_blob.decode("utf-8")
        manifest = _parse_manifest(manifest_text)
        assert set(manifest) == {n.split("/", 1)[1] for n in files}
        assert len(manifest) == n_files  # X-Skill-Files = 清单行数（不含 MANIFEST）
        assert sha == hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
        for rel, digest in manifest.items():
            assert hashlib.sha256(files[f"alpha/{rel}"]).hexdigest() == digest, rel
        assert resp.headers["ETag"] == f'"{sha}"'

    def test_etag_304_roundtrip(self, pkg_client):
        """ETag = 聚合哈希；If-None-Match 命中 → 304，且不重复记一次下载。"""
        client, app, _tree = pkg_client
        first = client.get("/v1/skills/alpha/package", headers=AGENT_HEADERS)
        assert first.status_code == 200
        etag = first.headers["ETag"]
        second = client.get(
            "/v1/skills/alpha/package",
            headers={**AGENT_HEADERS, "If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["X-Skill-Sha256"] == first.headers["X-Skill-Sha256"]
        calls = app.state.mem_conn.execute(
            "SELECT calls FROM skill_usage_daily WHERE layer='package' AND skill='alpha'"
        ).fetchone()
        assert calls["calls"] == 1  # 304 不计下载

    def test_format_zip_reserved_400(self, pkg_client):
        """zip 为保留参数 → 400 引导文案（当前唯一正式格式 = tar.gz）。"""
        client, _app, _tree = pkg_client
        resp = client.get("/v1/skills/alpha/package?format=zip", headers=AGENT_HEADERS)
        assert resp.status_code == 400, resp.text
        err = resp.json()["error"]
        assert err["code"] == "ERR_INVALID_ARGS"
        assert "tar.gz" in err["message"] and "zip" in err["message"]
        assert client.get(
            "/v1/skills/alpha/package?format=rar", headers=AGENT_HEADERS
        ).status_code == 400

    def test_unknown_skill_404_and_auth(self, pkg_client):
        client, _app, _tree = pkg_client
        assert client.get("/v1/skills/ghost/package", headers=AGENT_HEADERS).status_code == 404
        assert client.get("/v1/skills/ghost/package").status_code in (401, 403)
        assert client.get("/v1/skills/ghost/files/SKILL.md").status_code in (401, 403)

    def test_quota_exceeded_400(self, pkg_client):
        """超配 → 400 明确报错（skills.package_quota.* 独立键）。"""
        client, app, _tree = pkg_client
        app.state.cfg["skills"]["package_quota"] = {"max_total_bytes": 2048}
        resp = client.get("/v1/skills/alpha/package", headers=AGENT_HEADERS)
        assert resp.status_code == 400, resp.text
        msg = resp.json()["error"]["message"]
        assert "超限" in msg and "package_quota.max_total_bytes" in msg

    def test_builtin_protocol_single_file_package(self, pkg_client):
        """内置《技能检索协议》：对齐 skill_get 提供单文件包（消除新不一致）。"""
        client, _app, _tree = pkg_client
        resp = client.get("/v1/skills/skill-registry-protocol/package", headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        assert int(resp.headers["X-Skill-Files"]) == 1
        names, files = _read_tar_gz(resp.content)
        assert set(names) == {"MANIFEST.sha256", "skill-registry-protocol/SKILL.md"}
        assert files["MANIFEST.sha256"].count(b"\n") == 1

    def test_package_matches_materialize_landing(self, pkg_client, tmp_path):
        """等价：包内逐文件字节 == materialize 落盘逐文件字节（HTTP 端到端对照）。"""
        import io
        import tarfile
        from pathlib import Path

        client, _app, _tree = pkg_client
        pkg = client.get("/v1/skills/alpha/package", headers=AGENT_HEADERS)
        assert pkg.status_code == 200

        unzipped = tmp_path / "unzipped"
        unzipped.mkdir()
        with tarfile.open(fileobj=io.BytesIO(pkg.content), mode="r:gz") as tf:
            tf.extractall(unzipped, filter="data")

        landed = tmp_path / "landed"
        mat = client.post(
            "/v1/skills/alpha/materialize",
            json={"dest_dir": str(landed)},
            headers=AGENT_HEADERS,
        )
        assert mat.status_code == 200, mat.text

        def _hashes(base: Path) -> dict:
            import hashlib

            return {
                p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(base.rglob("*"))
                if p.is_file()
            }

        assert _hashes(unzipped / "alpha") == _hashes(landed / "alpha")

    def test_package_records_usage_layer(self, pkg_client):
        """遥测：包下载 note=format=tar.gz;files=N；单文件读 note=file;<扩展名>。"""
        client, app, _tree = pkg_client
        resp = client.get("/v1/skills/alpha/package", headers=AGENT_HEADERS)
        assert resp.status_code == 200
        n_files = int(resp.headers["X-Skill-Files"])
        rows = [dict(r) for r in app.state.mem_conn.execute("SELECT * FROM skill_usage_daily")]
        assert len(rows) == 1
        assert (rows[0]["layer"], rows[0]["skill"], rows[0]["caller"]) == (
            "package", "alpha", "default",
        )
        assert rows[0]["note"] == f"format=tar.gz;files={n_files}"

        assert client.get(
            "/v1/skills/alpha/files/install.py", headers=AGENT_HEADERS
        ).status_code == 200
        rows2 = [dict(r) for r in app.state.mem_conn.execute("SELECT * FROM skill_usage_daily")]
        assert len(rows2) == 1 and rows2[0]["calls"] == 2  # 同组聚合成一行
        assert rows2[0]["note"] == "file;.py"  # 只记扩展名，不记真实路径


class TestSkillFileEndpoint:
    def test_raw_bytes_and_sha_header(self, pkg_client):
        import hashlib
        from pathlib import Path

        client, _app, tree = pkg_client
        src = (Path(tree) / "alpha" / "install.py").read_bytes()
        resp = client.get("/v1/skills/alpha/files/install.py", headers=AGENT_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["X-File-Sha256"] == hashlib.sha256(src).hexdigest()
        assert resp.content == src

    def test_chinese_deep_member_and_big_file(self, pkg_client):
        from pathlib import Path

        client, _app, tree = pkg_client
        cn = client.get(
            "/v1/skills/alpha/files/corpus/金匮要略/序.md", headers=AGENT_HEADERS
        )
        assert cn.status_code == 200, cn.text
        assert cn.content == (Path(tree) / "alpha" / "corpus" / "金匮要略" / "序.md").read_bytes()
        big = client.get("/v1/skills/alpha/files/assets/big.bin", headers=AGENT_HEADERS)
        assert big.status_code == 200 and len(big.content) == 1024 * 1024

    def test_junk_and_nonmember_404(self, pkg_client):
        client, _app, _tree = pkg_client
        for rel in (
            "__pycache__/x.pyc",
            "debug.log",
            "scripts/nope.py",
            "empty",
            "SKILL.md.bak",
        ):
            resp = client.get(f"/v1/skills/alpha/files/{rel}", headers=AGENT_HEADERS)
            assert resp.status_code == 404, (rel, resp.status_code, resp.text[:120])
        # 成员不存在 ≠ 技能不存在：文案区分
        resp = client.get("/v1/skills/alpha/files/scripts/nope.py", headers=AGENT_HEADERS)
        assert "包成员不存在" in resp.json()["error"]["message"]
        # 技能不存在 → 404
        assert client.get(
            "/v1/skills/ghost/files/SKILL.md", headers=AGENT_HEADERS
        ).status_code == 404

    def test_shape_invalid_400(self, pkg_client):
        """形态非法（绝对 / 盘符 / ..）→ 400（URL 编码形态绕过框架归一化）。"""
        client, _app, _tree = pkg_client
        for encoded in ("..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "C%3A%2Fwindows%2Fx"):
            resp = client.get(f"/v1/skills/alpha/files/{encoded}", headers=AGENT_HEADERS)
            assert resp.status_code == 400, (encoded, resp.status_code, resp.text[:120])
            assert resp.json()["error"]["code"] == "ERR_INVALID_ARGS"


class TestSkillPackageDisabled:
    def test_package_endpoints_404_when_disabled(self, tmp_path, monkeypatch, raw_dir):
        from sgme.data import db as db_mod
        from sgme.data import memory_dao
        from sgme.server.app import create_app

        monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
        monkeypatch.setenv("SGME_HOME", str(tmp_path))
        cfg = sgme_config.load_config()
        cfg["skills"] = {"enabled": False}
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data3")
        memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
        app = create_app(
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key",
            agent_key="test-agent-key",
            bearer_token="",
            agent_store_path=tmp_path / "agent_keys3.json",
        )
        try:
            client = TestClient(app)
            assert client.get(
                "/v1/skills/alpha/package", headers=AGENT_HEADERS
            ).status_code == 404
            assert client.get(
                "/v1/skills/alpha/files/SKILL.md", headers=AGENT_HEADERS
            ).status_code == 404
        finally:
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
            db_mod.close(wiki_conn)
