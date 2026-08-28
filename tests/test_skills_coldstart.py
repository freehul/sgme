# -*- coding: utf-8 -*-
"""tests/test_skills_coldstart.py：T-106 M5 冷启动包端点测试。"""
from __future__ import annotations

import os
import subprocess

import pytest


@pytest.fixture()
def cs_env(tmp_path, monkeypatch):
    """冷启动测试环境：git 工作区 3 技能（1 auto + 2 manual）+ wiki 手册页。"""
    import sqlite3
    import sys

    repo = tmp_path / "skills"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)

    def put(name, pattern):
        d = repo / name
        d.mkdir(parents=True)
        fm = (f"---\nname: {name}\nversion: 0.1.0\npattern: {pattern}\n"
              f"category: test\ndescription: {name} 的描述文本\n---\n")
        (d / "SKILL.md").write_text(fm + f"\n# {name}\n\n正文内容\n", encoding="utf-8")

    put("hot-skill-a", "auto")
    put("hot-skill-b", "auto")
    put("cold-skill-c", "manual")

    db = tmp_path / "wiki.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT,"
        " content TEXT, category TEXT, tags TEXT, source_type TEXT, source_url TEXT,"
        " source_file TEXT, ingested_at TEXT, updated_at TEXT, content_seg TEXT,"
        " description TEXT, description_seg TEXT, author TEXT,"
        " status TEXT DEFAULT 'active', supersedes TEXT)"
    )
    conn.execute(
        "INSERT INTO wiki_pages (page_id, title, content, category, tags, status)"
        " VALUES ('m1','SGME操作手册','# SGME 操作手册\n\nappend 用法',"
        "'guide','[\"onboarding\"]','active')"
    )
    conn.commit()
    wiki_conn = sqlite3.connect(str(db))
    wiki_conn.row_factory = sqlite3.Row

    cfg = {"skills": {"enabled": True, "source_dirs": [str(repo)], "budget": 2}}
    return cfg, wiki_conn


class TestColdstartOperation:
    def test_returns_index_hotset_manual(self, cs_env):
        from sgme.operations.skills import cold_start

        cfg, wiki_conn = cs_env
        r = cold_start(cfg, wiki_conn)
        assert r.ok
        d = r.data
        # 单一注入：仅协议 skill（与 source_dirs 的 3 个测试技能无关）
        assert d["index"]["total"] == 1
        assert d["index"]["items"][0]["name"] == "skill-registry-protocol"
        # 热集恒定空（按需检索范式，无热常驻）
        assert d["hotset"] == []
        # 操作手册
        assert d["manual"] is not None
        assert "SGME操作手册" in d["manual"]["title"]

    def test_manual_missing_returns_none_not_error(self, cs_env, tmp_path):
        import sqlite3

        from sgme.operations.skills import cold_start

        cfg, _ = cs_env
        empty = sqlite3.connect(str(tmp_path / "empty.db"))
        empty.execute(
            "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT,"
            " content TEXT, category TEXT, tags TEXT, source_type TEXT, source_url TEXT,"
            " source_file TEXT, ingested_at TEXT, updated_at TEXT, content_seg TEXT,"
            " description TEXT, description_seg TEXT, author TEXT,"
            " status TEXT DEFAULT 'active', supersedes TEXT)"
        )
        empty.commit()
        r = cold_start(cfg, empty)
        assert r.ok and r.data["manual"] is None


class TestColdstartRoute:
    def test_route_200_full_shape(self, tmp_path, monkeypatch, cs_env):
        import sgme.config as sgme_config
        from fastapi.testclient import TestClient

        cfg, _ = cs_env
        monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
        monkeypatch.setenv("SGME_HOME", str(tmp_path))

        from sgme.data import db as db_mod
        from sgme.server.app import create_app

        # create_app 内部会补齐 dimensions/aliases 等缺省段（load_config 全量）
        full_cfg = dict(sgme_config.load_config())
        full_cfg["skills"] = cfg.get("skills")

        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
        # 把手册页写进真实 schema 的 wiki 库（fixture 里那个库只给 operations 层用）
        wiki_conn.execute(
            "INSERT INTO wiki_pages (page_id, title, content, category, tags, status)"
            " VALUES ('m1','SGME操作手册','# SGME 操作手册\n\nappend 用法',"
            "'guide','[\"onboarding\"]','active')"
        )
        wiki_conn.commit()
        app = create_app(
            cfg=full_cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key",
            agent_key="test-agent-key",
            agent_store_path=tmp_path / "agent_keys.json",
        )
        c = TestClient(app)
        resp = c.get("/v1/skills/coldstart", headers={"X-API-Key": "test-agent-key"})
        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["index"]["total"] == 1
        assert d["index"]["items"][0]["name"] == "skill-registry-protocol"
        assert len(d["hotset"]) == 0
        assert "SGME操作手册" in (d["manual"] or {}).get("title", "")
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)
