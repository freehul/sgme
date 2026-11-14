"""tests/test_skill_candidate_promote.py：T3 技能候选晋级落成（三缺口补齐 2026-11-14）。

验收（依据 docs/plans/任务清单-三缺口补齐-20261114.md T3）：
1. accepted → promoted：skills 主表有该行 + hub 真源落盘 + 候选行 status=promoted
2. 落成失败（put_skill 抛异常）→ 候选行保持 accepted、返回 ok=False
3. promoted 重复调用幂等
4. pending / ignored 调 promote → 拒绝
5. dry-run 不写库
6. hub 未提供 → 明确失败（不降级成「只改状态」）
7. 批量：单条失败不中断；HTTP 端点默认 dry-run
"""

from __future__ import annotations

import json

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao, skills_dao
from sgme.operations import skill_candidates as sc_ops
from sgme.skills_hub import SkillsHub, parse_skills_hub_config


def _skill_md(name: str, content: str = "做法：先补 /v1 前缀再请求。") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: 测试候选技能\n"
        "category: testing\n"
        f"tags: {json.dumps(['testing', 'skill-candidate'], ensure_ascii=False)}\n"
        "version: 0.1.0\n"
        "pattern: manual\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{content}\n"
    )


def _insert_candidate(skills, memory_id, *, status="accepted", name="exp-test0001", content=None):
    now = "2026-11-14T00:00:00Z"
    skills.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, category,"
        " tags, confidence, status, source, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, name, "测试候选技能", content or _skill_md(name), "testing",
         json.dumps(["testing"]), 0.9, status, "memory_route", now, now),
    )
    skills.commit()


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path, cfg):
    mem = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    skills = db_mod.connect_skills(tmp_path)
    yield mem, skills
    db_mod.close(mem)
    db_mod.close(skills)


@pytest.fixture
def hub(tmp_path):
    c = parse_skills_hub_config({"skills_hub": {"enabled": True, "path": str(tmp_path / "hub")}})
    return SkillsHub(c)


# ---------- 单条 review：promoted 两阶段 ----------

def test_accepted_promote_writes_library(conns, hub):
    mem, skills = conns
    _insert_candidate(skills, "m-p1")
    r = sc_ops.review_skill_candidate(mem, skills, "m-p1", "promoted", hub=hub, reviewer="lead")
    assert r.ok, f"晋级应成功: {r.message}"
    assert r.data["library_written"] is True
    # ① 真源落盘
    skill_file = hub.root / "exp-test0001" / "SKILL.md"
    assert skill_file.is_file()
    assert "测试候选技能" in skill_file.read_text(encoding="utf-8")
    # ② sqlite skills 主表有该行
    rows = skills.execute(
        "SELECT name, source, content FROM skills WHERE name='exp-test0001'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["source"] == "candidate_promote"
    assert "先补 /v1 前缀" in rows[0]["content"]
    # ③ 候选行 promoted + 审核留痕
    row = skills.execute(
        "SELECT status, reviewer FROM skill_candidates WHERE memory_id='m-p1'"
    ).fetchone()
    assert row["status"] == "promoted" and row["reviewer"] == "lead"


def test_promote_failure_keeps_accepted(conns, hub, monkeypatch):
    mem, skills = conns
    _insert_candidate(skills, "m-f1")

    def boom(name, content):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(hub, "put_skill", boom)
    r = sc_ops.review_skill_candidate(mem, skills, "m-f1", "promoted", hub=hub)
    assert not r.ok and r.error_code == "ERR_INTERNAL"
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-f1'"
    ).fetchone()
    assert row["status"] == "accepted"  # 落成失败绝不标 promoted
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


def test_promote_is_idempotent(conns, hub):
    mem, skills = conns
    _insert_candidate(skills, "m-idem")
    first = sc_ops.review_skill_candidate(mem, skills, "m-idem", "promoted", hub=hub)
    assert first.ok and first.data["changed"] is True
    # 重复调用走同状态幂等分支（不重复落成、不再要求 hub）
    second = sc_ops.review_skill_candidate(mem, skills, "m-idem", "promoted", hub=None)
    assert second.ok and second.data["changed"] is False
    assert skills.execute("SELECT COUNT(*) FROM skills WHERE name='exp-test0001'").fetchone()[0] == 1


def test_pending_and_ignored_rejected(conns, hub):
    mem, skills = conns
    _insert_candidate(skills, "m-pend", status="pending")
    _insert_candidate(skills, "m-ign", status="ignored")
    rp = sc_ops.review_skill_candidate(mem, skills, "m-pend", "promoted", hub=hub)
    ri = sc_ops.review_skill_candidate(mem, skills, "m-ign", "promoted", hub=hub)
    assert not rp.ok and rp.error_code == "ERR_INVALID_ARGS"
    assert not ri.ok and ri.error_code == "ERR_INVALID_ARGS"
    rows = skills.execute(
        "SELECT memory_id, status FROM skill_candidates WHERE memory_id IN ('m-pend','m-ign')"
    ).fetchall()
    assert {r["memory_id"]: r["status"] for r in rows} == {"m-pend": "pending", "m-ign": "ignored"}


def test_promote_without_hub_fails_not_degrades(conns):
    """hub 未提供 → 明确失败（不降级成「只改状态」的假晋级）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-nohub")
    r = sc_ops.review_skill_candidate(mem, skills, "m-nohub", "promoted", hub=None)
    assert not r.ok and r.error_code == "ERR_INVALID_ARGS"
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-nohub'"
    ).fetchone()
    assert row["status"] == "accepted"
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


# ---------- 批量入口 ----------

def test_batch_dry_run_writes_nothing(conns, hub):
    mem, skills = conns
    for i in range(3):
        _insert_candidate(skills, f"m-d{i}", name=f"exp-dry{i:04d}")
    r = sc_ops.promote_skill_candidates(mem, skills, hub, limit=10, apply=False)
    assert r.ok
    data = r.data
    assert data["apply"] is False
    assert data["promoted"] == 0 and data["failed"] == 0
    assert len(data["items"]) == 3 and all(it["would_promote"] for it in data["items"])
    # 零写入
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    statuses = skills.execute(
        "SELECT DISTINCT status FROM skill_candidates"
    ).fetchall()
    assert [s["status"] for s in statuses] == ["accepted"]
    assert not (hub.root / "exp-dry0000").exists()


def test_batch_apply_single_failure_does_not_stop(conns, hub):
    mem, skills = conns
    _insert_candidate(skills, "m-a1", name="exp-ok0001")
    _insert_candidate(skills, "m-a2", name="bad/name")  # put_skill 校验非法名 → 单条失败
    _insert_candidate(skills, "m-a3", name="exp-ok0003")
    r = sc_ops.promote_skill_candidates(mem, skills, hub, limit=10, apply=True, reviewer="lead")
    assert r.ok
    data = r.data
    assert data["apply"] is True
    assert data["promoted"] == 2 and data["failed"] == 1
    by_mid = {it["memory_id"]: it for it in data["items"]}
    assert by_mid["m-a1"]["promoted"] is True
    assert by_mid["m-a2"]["promoted"] is False and by_mid["m-a2"]["error"]
    assert by_mid["m-a3"]["promoted"] is True
    # 失败那条保持 accepted，成功两条 promoted
    statuses = dict(
        skills.execute(
            "SELECT memory_id, status FROM skill_candidates WHERE memory_id IN ('m-a1','m-a2','m-a3')"
        ).fetchall()
    )
    assert statuses == {"m-a1": "promoted", "m-a2": "accepted", "m-a3": "promoted"}


# ---------- HTTP 批量端点（默认 dry-run） ----------

@pytest.fixture
def api(tmp_path, cfg, conns, monkeypatch):
    from fastapi.testclient import TestClient
    from sgme.server.app import create_app

    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    c = dict(cfg)
    # 显式启用技能模块 + hub 指向 tmp（不依赖本机真实配置；data_dir 必须显式传，
    # 否则 create_app 自建的 skills.db 会落到全局 DATA_DIR——见 app.py 注释）
    c["skills"] = {"enabled": True, "source_dirs": [str(tmp_path / "skills_src")]}
    c["skills_hub"] = {"enabled": True, "path": str(tmp_path / "hub")}
    mem, skills = conns
    session = db_mod.connect_session(tmp_path)
    wiki = db_mod.connect_wiki(tmp_path)
    app = create_app(
        cfg=c, mem_conn=mem, session_conn=session, wiki_conn=wiki,
        data_dir=tmp_path,
        admin_key="test-admin", agent_key="test-agent",
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    yield TestClient(app), skills, (tmp_path / "hub")
    db_mod.close(session)
    db_mod.close(wiki)


def _admin_headers():
    return {"X-API-Key": "test-admin"}


def test_http_promote_default_dry_run(api, conns):
    client, skills, hub_root = api
    _insert_candidate(skills, "m-http1", name="exp-http0001")
    resp = client.post("/v1/admin/skill-candidates/promote", json={"limit": 10},
                       headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["apply"] is False and data["promoted"] == 0
    assert [it["name"] for it in data["items"]] == ["exp-http0001"]
    assert not (hub_root / "exp-http0001").exists()
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


def test_http_promote_apply_writes_library(api):
    client, skills, hub_root = api
    _insert_candidate(skills, "m-http2", name="exp-http0002")
    resp = client.post("/v1/admin/skill-candidates/promote",
                       json={"limit": 10, "apply": True, "reviewer": "lead"},
                       headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["apply"] is True and data["promoted"] == 1 and data["failed"] == 0
    assert (hub_root / "exp-http0002" / "SKILL.md").is_file()
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='exp-http0002'"
    ).fetchone()[0] == 1
