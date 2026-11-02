"""tests/test_skills_db_first_read.py：技能读路径「库优先、盘回退」（2.0 全存库）。

背景（本文件的回归动机）
------------------------
``skills.db`` 已存全部技能正文，但 ``skill_digest`` / ``skill_get`` /
``materialize`` / ``package_skill`` 曾每次调 ``_load_records`` **扫 source_dirs**——
库与磁盘一旦不同步（容器重建、API 写盘后未重建索引），``skill_get`` 返回的
与检索/包里的是**两个版本**。

验收口径
--------
构造「库里 A、磁盘 B」：
- 传 ``skills_conn`` → digest/get/search 一律返回库内 A（与 list/search 同源）；
- 传 ``skills_conn=None``（含旧调用签名）→ 回退扫盘返回磁盘 B（向后兼容，
  冷启动/空库路径不回归）；
- 只在库里、未落盘的技能：库路径可读/可物化/可打包，纯扫盘路径 404；
- HTTP 与 MCP 两套入口传的也是同一个 ``skills_conn``（行为一致）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from sgme.data import db as db_mod
from sgme.data import skills_dao
from sgme.operations.errors import ERR_NOT_FOUND
from sgme.operations.skills import (
    materialize,
    package_skill,
    search_skills,
    skill_digest,
    skill_get,
)

# 磁盘版（B）：frontmatter + 正文
DISK_BODY = "# Alpha 磁盘标题\n磁盘正文：这是**磁盘**版本。\n"
DISK_MD = (
    "---\n"
    "name: alpha\n"
    "description: 磁盘版简介\n"
    "version: 9.9.9\n"
    "category: disk\n"
    "tags: [skill, disk]\n"
    "uses: [beta]\n"
    "---\n"
    + DISK_BODY
)
# 库内版（A）：skills.content 只存正文（frontmatter 拆成列），且入库前已 strip
DB_BODY = "# Alpha 库内标题\n库内正文：这是**库内**版本。"
# 只在库里、磁盘上没有的技能
DB_ONLY_BODY = "# 只存库的技能\n库里新建、尚未落盘的技能。"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _db_record(name: str, body: str, **meta) -> dict:
    """构造 skills_dao.upsert_skill 的写入字典（默认值对齐 indexer 产物）。"""
    body = body.strip()  # indexer/SkillRecord 口径：正文入库前 strip
    return {
        "name": name,
        "sha256": meta.pop("sha256", _sha(body)),
        "description": meta.pop("description", ""),
        "tags": meta.pop("tags", ["skill"]),
        "category": meta.pop("category", None),
        "version": meta.pop("version", None),
        "pattern": meta.pop("pattern", None),
        "source": meta.pop("source", "git"),
        "origin_path": meta.pop("origin_path", None),
        "content": body,
        "uses": meta.pop("uses", []),
        "uses_strength": meta.pop("uses_strength", {}),
    }


# ---------- fixtures ----------


@pytest.fixture
def skill_tree(tmp_path: Path) -> Path:
    """磁盘版本 B：<root>/alpha/SKILL.md。"""
    d = tmp_path / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(DISK_MD, encoding="utf-8")
    return tmp_path / "skills"


@pytest.fixture
def cfg(skill_tree: Path) -> dict:
    return {"skills": {"enabled": True, "source_dirs": [str(skill_tree)], "budget": 40}}


@pytest.fixture
def conn(tmp_path: Path, skill_tree: Path):
    """库版本 A：alpha（含 uses 边）+ 只在库里的 db-only。

    alpha 的 origin_path 指向磁盘文件（真实同步产物就是这样写的）——用于验证
    「记录解析库优先，但 SKILL.md 字节仍取原文件」不破坏 L3 字节保真。
    """
    c = db_mod.connect_skills(tmp_path)
    skills_dao.upsert_skill(
        c,
        _db_record(
            "alpha", DB_BODY, description="库内简介", category="db", version="1.0.0",
            origin_path=str(skill_tree / "alpha" / "SKILL.md"),
        ),
    )
    skills_dao.upsert_skill(c, _db_record("db-only", DB_ONLY_BODY, description="只存库"))
    # uses 出边（扫盘路径来自 frontmatter.uses；库路径来自 skill_uses）
    skills_dao.replace_uses(c, "alpha", ["beta"], strengths={"beta": "strong"})
    c.commit()
    yield c
    c.close()


# ---------- L1/L2：库优先、盘回退 ----------


class TestByNameReadsPreferDb:
    def test_skill_get_returns_db_body(self, cfg, conn):
        data = skill_get(cfg, None, name="alpha", skills_conn=conn).data
        assert "库内正文" in data["content"]
        assert "磁盘正文" not in data["content"]
        assert data["sha256"] == _sha(DB_BODY)

    def test_skill_get_without_conn_returns_disk(self, cfg, conn):
        """兼容：不传 skills_conn（旧签名）与显式 None 都回退扫盘。"""
        legacy = skill_get(cfg, None, name="alpha").data
        explicit_none = skill_get(cfg, None, name="alpha", skills_conn=None).data
        assert "磁盘正文" in legacy["content"]
        assert "库内正文" not in legacy["content"]
        assert explicit_none["content"] == legacy["content"]

    def test_skill_get_section_from_db(self, cfg, conn):
        data = skill_get(cfg, None, name="alpha", section="Alpha 库内标题",
                         skills_conn=conn).data
        assert "库内正文" in data["content"]
        assert "磁盘" not in data["content"]

    def test_digest_prefers_db(self, cfg, conn):
        d = skill_digest(cfg, None, name="alpha", skills_conn=conn).data
        assert d["description"] == "库内简介"
        assert d["version"] == "1.0.0"
        assert d["category"] == "db"
        # uses 来自 skill_uses 出边（不是回退扫盘的 frontmatter）
        assert d["uses"] == ["beta"]
        assert any("库内标题" in s for s in d["sections"])
        assert d["sha256"] == _sha(DB_BODY)

    def test_digest_without_conn_returns_disk(self, cfg, conn):
        d = skill_digest(cfg, None, name="alpha").data
        assert d["description"] == "磁盘版简介"
        assert d["version"] == "9.9.9"
        assert d["category"] == "disk"
        assert d["uses"] == ["beta"]

    def test_db_only_skill_readable_via_db(self, cfg, conn):
        data = skill_get(cfg, None, name="db-only", skills_conn=conn).data
        assert data["content"] == DB_ONLY_BODY.strip()
        # 纯扫盘路径没有这个技能 → 证明上面的内容确实来自库
        assert skill_get(cfg, None, name="db-only").ok is False

    def test_dirty_row_falls_back_to_disk(self, tmp_path, cfg):
        """正文缺失（脏行）宁可不命中 → 回退扫盘，而不是返回空技能。"""
        d = tmp_path / "dbdir"
        d.mkdir()
        c = db_mod.connect_skills(d)
        try:
            skills_dao.upsert_skill(c, _db_record("alpha", "", description="脏行"))
            c.commit()
            data = skill_get(cfg, None, name="alpha", skills_conn=c).data
            assert "磁盘正文" in data["content"]
        finally:
            c.close()


# ---------- 检索：库优先（含向量/名直达命中的元数据补齐） ----------


class TestSearchPrefersDb:
    def test_search_metadata_from_db(self, cfg, conn, monkeypatch):
        from sgme.skills import vectors as vectors_mod

        # 向量路在离线测试环境不可用：强制降级纯 FTS（不影响库优先判定）
        monkeypatch.setattr(vectors_mod, "embed_texts", lambda texts, cfg: {})

        hits = search_skills("alpha", cfg, None, limit=5, skills_conn=conn)
        top = next(h for h in hits if h["name"] == "alpha")
        assert top["description"] == "库内简介"
        assert top["category"] == "db"

        disk_hits = search_skills("alpha", cfg, None, limit=5)
        disk_top = next(h for h in disk_hits if h["name"] == "alpha")
        assert disk_top["description"] == "磁盘版简介"
        assert disk_top["category"] == "disk"


# ---------- L3 物化：记录解析库优先 ----------


class TestMaterializeDbFirst:
    def test_db_only_skill_materializes_from_db(self, cfg, conn, tmp_path):
        data = materialize(
            cfg, None, name="db-only", dest_dir=str(tmp_path / "out"), skills_conn=conn
        ).data
        out = Path(data["path"])
        assert out.read_text(encoding="utf-8") == DB_ONLY_BODY.strip()
        assert data["sha256"] == _sha(DB_ONLY_BODY.strip())
        # 纯扫盘路径找不到 → 404（证明上面的记录来自库）
        res = materialize(cfg, None, name="db-only", dest_dir=str(tmp_path / "out2"))
        assert res.ok is False and res.error_code == ERR_NOT_FOUND

    def test_disk_skill_still_writes_source_bytes(self, cfg, conn, tmp_path):
        """库里有记录也不破坏 L3 字节保真：源文件在 → 写原文件字节（含 frontmatter）。

        库里 ``content`` 只存正文，按正文写盘会丢 frontmatter；materialize 的
        铁律是「原文件字节」，故字节源仍优先 origin_path，库只负责记录解析。
        """
        data = materialize(
            cfg, None, name="alpha", dest_dir=str(tmp_path / "out"), skills_conn=conn
        ).data
        assert Path(data["path"]).read_text(encoding="utf-8") == DISK_MD


# ---------- 完整包：记录解析库优先 ----------


class TestPackageDbFirst:
    def test_db_only_skill_packages_from_db(self, cfg, conn):
        data = package_skill(cfg, None, name="db-only", skills_conn=conn).data
        assert data["count"] == 1
        assert data["members"][0]["path"] == "SKILL.md"
        assert data["members"][0]["data"] == DB_ONLY_BODY.strip().encode("utf-8")
        # 纯扫盘路径找不到 → 404
        res = package_skill(cfg, None, name="db-only")
        assert res.ok is False and res.error_code == ERR_NOT_FOUND


# ---------- 入口层：MCP 与 HTTP 传同一个 skills_conn ----------


def _mcp_call(server, name: str, args: dict) -> dict:
    raw = asyncio.run(server.call_tool(name, args))
    results, _meta = raw if isinstance(raw, tuple) else (raw, None)
    text = "\n".join(c.text for c in results if getattr(c, "text", None))
    return json.loads(text)


class TestEntrypointsPassSkillsConn:
    def test_mcp_tools_prefer_db(self, cfg, conn):
        from sgme.mcp_server import _app_state, bind_app_state, build_mcp_server

        saved = dict(_app_state)
        bind_app_state({
            "cfg": cfg, "mem_conn": None, "session_conn": None,
            "wiki_conn": None, "skills_conn": conn,
        })
        try:
            server = build_mcp_server()
            got = _mcp_call(server, "skill_get", {"name": "alpha"})
            assert "库内正文" in got["content"]
            digest = _mcp_call(server, "skill_digest", {"name": "alpha"})
            assert digest["description"] == "库内简介"
        finally:
            _app_state.clear()
            _app_state.update(saved)

    def test_http_endpoints_prefer_db(self, tmp_path, cfg, conn, monkeypatch):
        from fastapi.testclient import TestClient

        from sgme import config as sgme_config
        from sgme.data import memory_dao
        from sgme.server.app import create_app
        from sgme.skills import vectors as vectors_mod

        monkeypatch.setattr(vectors_mod, "embed_texts", lambda texts, cfg: {})
        raw = tmp_path / "raw"
        raw.mkdir()
        monkeypatch.setattr(sgme_config, "RAW_DIR", raw)
        monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
        monkeypatch.setenv("SGME_HOME", str(tmp_path))

        full_cfg = sgme_config.load_config()
        full_cfg["skills"] = dict(cfg["skills"])
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
        memory_dao.import_registry(mem_conn, full_cfg["dimensions"], full_cfg["aliases"])
        app = create_app(
            cfg=full_cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key",
            agent_key="test-agent-key",
            bearer_token="",
            agent_store_path=tmp_path / "agent_keys.json",
        )
        # create_app 自建了一个空 skills.db（后台同步未跑）；换成预置「库 A」的连接，
        # 正是要验证入口层确实把 app.state.skills_conn 透传给了 operation。
        own_skills_conn = app.state.skills_conn
        app.state.skills_conn = conn
        headers = {"X-API-Key": "test-agent-key"}
        try:
            client = TestClient(app)
            got = client.get("/v1/skills/alpha", headers=headers)
            assert got.status_code == 200, got.text
            assert "库内正文" in got.json()["content"]
            assert "磁盘正文" not in got.json()["content"]

            digest = client.get("/v1/skills/alpha/digest", headers=headers)
            assert digest.status_code == 200, digest.text
            assert digest.json()["description"] == "库内简介"

            hits = client.get("/v1/skills/search", params={"q": "alpha"}, headers=headers)
            assert hits.status_code == 200, hits.text
            top = next(it for it in hits.json()["items"] if it["name"] == "alpha")
            assert top["description"] == "库内简介"

            mat = client.post(
                "/v1/skills/db-only/materialize",
                json={"dest_dir": str(tmp_path / "http_out")},
                headers=headers,
            )
            assert mat.status_code == 200, mat.text
            assert Path(mat.json()["path"]).read_text(encoding="utf-8") == DB_ONLY_BODY.strip()
        finally:
            if own_skills_conn is not None:
                own_skills_conn.close()
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
            db_mod.close(wiki_conn)
