"""tests/test_skill_candidate_promote.py：T3 技能候选晋级落成（三缺口补齐 2026-11-14）。

验收（依据 docs/plans/任务清单-三缺口补齐-20261114.md T3）：
1. accepted → promoted：skills 主表有该行 + hub 真源落盘 + 候选行 status=promoted
2. 落成失败（put_skill 抛异常）→ 候选行保持 accepted、返回 ok=False
3. promoted 重复调用幂等
4. pending / ignored 调 promote → 拒绝
5. dry-run 不写库
6. hub 未提供 → 明确失败（不降级成「只改状态」）
7. 批量：单条失败不中断；HTTP 端点默认 dry-run

T7（2026-11-15，任务清单-技能自动命名）补充验收：
8. 占位名（exp- 前缀）promote → 拒绝（候选尚未命名）
9. suggested_name 存在 → 作为最终名落成
10. 入参 name 覆盖 suggested_name
11. 落成成功后最终名写回候选行 name（单一真相源）；SKILL.md frontmatter 同步

T11（2026-11-15，任务清单-候选三分类）补充验收：
12. suggested_kind=wiki → 拒绝，文案含知识库通道提示
13. suggested_kind=discard → 拒绝，提示标 ignored
14. kind=skill → 落成后 skills.category == suggested_domain（不再用记忆维度）
15. 无 suggested_domain → category='other'
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


def _insert_candidate(
    skills, memory_id, *, status="accepted", name="test-skill-0001",
    content=None, suggested_name=None, suggested_kind=None, suggested_domain=None,
):
    now = "2026-11-14T00:00:00Z"
    skills.execute(
        "INSERT INTO skill_candidates (memory_id, name, description, content, category,"
        " tags, confidence, status, source, suggested_name, suggested_kind,"
        " suggested_domain, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, name, "测试候选技能", content or _skill_md(name), "testing",
         json.dumps(["testing"]), 0.9, status, "memory_route", suggested_name,
         suggested_kind, suggested_domain, now, now),
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
    # ① 真源落盘（T7：SKILL.md frontmatter 的 name 与最终名一致）
    skill_file = hub.root / "test-skill-0001" / "SKILL.md"
    assert skill_file.is_file()
    assert "测试候选技能" in skill_file.read_text(encoding="utf-8")
    assert "name: test-skill-0001" in skill_file.read_text(encoding="utf-8").split("\n---")[0]
    # ② sqlite skills 主表有该行
    rows = skills.execute(
        "SELECT name, source, content FROM skills WHERE name='test-skill-0001'"
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
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='test-skill-0001'"
    ).fetchone()[0] == 1


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
        _insert_candidate(skills, f"m-d{i}", name=f"test-dry-{i:03d}")
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
    assert not (hub.root / "test-dry-000").exists()


def test_batch_apply_single_failure_does_not_stop(conns, hub):
    mem, skills = conns
    _insert_candidate(skills, "m-a1", name="test-ok-0001")
    _insert_candidate(skills, "m-a2", name="bad/name")  # put_skill 校验非法名 → 单条失败
    _insert_candidate(skills, "m-a3", name="test-ok-0003")
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
    _insert_candidate(skills, "m-http1", name="test-http-0001")
    resp = client.post("/v1/admin/skill-candidates/promote", json={"limit": 10},
                       headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["apply"] is False and data["promoted"] == 0
    assert [it["name"] for it in data["items"]] == ["test-http-0001"]
    assert not (hub_root / "test-http-0001").exists()
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


def test_http_promote_apply_writes_library(api):
    client, skills, hub_root = api
    _insert_candidate(skills, "m-http2", name="test-http-0002")
    resp = client.post("/v1/admin/skill-candidates/promote",
                       json={"limit": 10, "apply": True, "reviewer": "lead"},
                       headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["apply"] is True and data["promoted"] == 1 and data["failed"] == 0
    assert (hub_root / "test-http-0002" / "SKILL.md").is_file()
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='test-http-0002'"
    ).fetchone()[0] == 1


# ---------- T7（2026-11-15）：promote 集成命名 ----------

def test_exp_placeholder_name_rejected(conns, hub):
    """占位名（exp- 前缀）且无 suggested_name → 拒绝，候选保持 accepted。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t7a", name="exp-000b20d9")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t7a", "promoted", hub=hub)
    assert not r.ok and r.error_code == "ERR_INVALID_ARGS"
    assert "先跑命名流程" in r.message
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-t7a'"
    ).fetchone()
    assert row["status"] == "accepted"
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    assert not (hub.root / "exp-000b20d9").exists()


def test_suggested_name_used_on_promote(conns, hub):
    """suggested_name 存在 → 作为最终名落成，并写回候选行 name（单一真相源）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t7b", name="exp-000b20d9",
                      suggested_name="lmstudio-int8-quant-load-fail")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t7b", "promoted", hub=hub)
    assert r.ok, f"晋级应成功: {r.message}"
    assert r.data["name"] == "lmstudio-int8-quant-load-fail"
    # 落成用最终名
    assert (hub.root / "lmstudio-int8-quant-load-fail" / "SKILL.md").is_file()
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='lmstudio-int8-quant-load-fail'"
    ).fetchone()[0] == 1
    # 写回候选行 name + 状态 promoted
    row = skills.execute(
        "SELECT name, status FROM skill_candidates WHERE memory_id='m-t7b'"
    ).fetchone()
    assert row["name"] == "lmstudio-int8-quant-load-fail"
    assert row["status"] == "promoted"


def test_explicit_name_overrides_suggested(conns, hub):
    """入参 name 优先于 suggested_name。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t7c", name="exp-000b20d9",
                      suggested_name="suggested-fallback-name")
    r = sc_ops.review_skill_candidate(
        mem, skills, "m-t7c", "promoted", hub=hub, name="explicit-param-name",
    )
    assert r.ok and r.data["name"] == "explicit-param-name"
    assert (hub.root / "explicit-param-name" / "SKILL.md").is_file()
    row = skills.execute(
        "SELECT name, status FROM skill_candidates WHERE memory_id='m-t7c'"
    ).fetchone()
    assert row["name"] == "explicit-param-name" and row["status"] == "promoted"
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='suggested-fallback-name'"
    ).fetchone()[0] == 0


def test_promoted_skill_md_frontmatter_renamed(conns, hub):
    """落成的 SKILL.md frontmatter name 行 = 最终名（不再落占位名）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t7d", name="exp-000b20d9",
                      suggested_name="frontmatter-check-name")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t7d", "promoted", hub=hub)
    assert r.ok, r.message
    text = (hub.root / "frontmatter-check-name" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("\n---")[0]
    assert "name: frontmatter-check-name" in frontmatter
    assert "exp-000b20d9" not in frontmatter


def test_explicit_exp_name_also_rejected(conns, hub):
    """入参 name 传占位名同样拒绝（闸门看最终名，不看来路）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t7e", name="exp-000b20d9",
                      suggested_name="good-suggested-name")
    r = sc_ops.review_skill_candidate(
        mem, skills, "m-t7e", "promoted", hub=hub, name="exp-badparam1",
    )
    assert not r.ok and r.error_code == "ERR_INVALID_ARGS"
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-t7e'"
    ).fetchone()
    assert row["status"] == "accepted"


# ---------- T11（2026-11-15）：promote 接入 kind + domain ----------

def test_wiki_kind_rejected_with_channel_hint(conns, hub):
    """kind=wiki → 拒绝 promote，提示走知识库通道。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t11a", name="exp-000b20d9",
                      suggested_name="wiki-kind-skill-name",
                      suggested_kind="wiki", suggested_domain="ai")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t11a", "promoted", hub=hub)
    assert not r.ok and r.error_code == "ERR_INVALID_ARGS"
    assert "wiki" in r.message and "知识库" in r.message
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-t11a'"
    ).fetchone()
    assert row["status"] == "accepted"
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


def test_discard_kind_rejected(conns, hub):
    """kind=discard → 拒绝，提示改标 ignored。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t11b", name="exp-000b20d9",
                      suggested_name="discard-kind-skill-name",
                      suggested_kind="discard", suggested_domain="tool")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t11b", "promoted", hub=hub)
    assert not r.ok and r.error_code == "ERR_INVALID_ARGS"
    assert "ignored" in r.message
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-t11b'"
    ).fetchone()
    assert row["status"] == "accepted"
    assert skills.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


def test_skill_kind_promotes_with_domain_category(conns, hub):
    """kind=skill → 落成，skills.category = suggested_domain（不再用记忆维度）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t11c", name="exp-000b20d9",
                      suggested_name="domain-category-skill",
                      suggested_kind="skill", suggested_domain="ai")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t11c", "promoted", hub=hub)
    assert r.ok, r.message
    row = skills.execute(
        "SELECT category FROM skills WHERE name='domain-category-skill'"
    ).fetchone()
    assert row["category"] == "ai"  # 不是夹具里的记忆维度 testing
    # SKILL.md frontmatter category 同步为领域
    text = (hub.root / "domain-category-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "category: ai" in text.split("\n---")[0]


def test_missing_domain_falls_back_to_other(conns, hub):
    """无 suggested_domain → category='other'（不阻断）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t11d", name="exp-000b20d9",
                      suggested_name="no-domain-skill",
                      suggested_kind="skill", suggested_domain=None)
    r = sc_ops.review_skill_candidate(mem, skills, "m-t11d", "promoted", hub=hub)
    assert r.ok, r.message
    row = skills.execute(
        "SELECT category FROM skills WHERE name='no-domain-skill'"
    ).fetchone()
    assert row["category"] == "other"


def test_unclassified_kind_still_promotes(conns, hub):
    """向后兼容：未分类（kind=NULL）但已命名的候选仍可 promote（T7 行为不变）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t11e", name="exp-000b20d9",
                      suggested_name="unclassified-skill", suggested_kind=None)
    r = sc_ops.review_skill_candidate(mem, skills, "m-t11e", "promoted", hub=hub)
    assert r.ok, r.message
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='unclassified-skill'"
    ).fetchone()[0] == 1


# ---------- T-401（2026-11-17）：promote 撞名检查 ----------

def test_promote_name_conflict_rejected_no_overwrite(conns, hub):
    """主表已有同名且内容不同的技能 → promote 拒绝；主表内容不被覆盖、真源不落盘。"""
    mem, skills = conns
    from sgme.data import skills_dao
    skills_dao.upsert_skill(skills, {
        "name": "conflicted-skill", "sha256": "a" * 64,
        "description": "已有旧技能", "category": "ai", "content": "旧内容：不要覆盖我。",
        "source": "manual", "origin_path": "",
    })
    skills.commit()
    old_sha = skills_dao.get_skill(skills, "conflicted-skill")["sha256"]
    _insert_candidate(skills, "m-t401a", name="exp-000c401a",
                      suggested_name="conflicted-skill",
                      suggested_kind="skill", suggested_domain="ai")
    r = sc_ops.review_skill_candidate(mem, skills, "m-t401a", "promoted", hub=hub)
    assert not r.ok, "撞名 promote 应被拒绝"
    assert "同名技能" in r.message and "拒绝覆盖" in r.message
    # 主表技能未被覆盖（sha 不变）
    assert skills_dao.get_skill(skills, "conflicted-skill")["sha256"] == old_sha
    # 候选保持 accepted（T3 硬要求：绝不出现标 promoted 但未落成）
    row = skills.execute(
        "SELECT status FROM skill_candidates WHERE memory_id='m-t401a'"
    ).fetchone()
    assert row["status"] == "accepted"
    # hub 真源目录没有落盘
    assert not (hub.root / "conflicted-skill").exists()


def test_promote_same_content_retry_is_idempotent(conns, hub):
    """同一候选 promote 成功后状态回退（模拟 update_status 中途失败）再重试 →
    主表同名且 sha256 与本次写入一致 → 放行（幂等自愈，不误伤）。"""
    mem, skills = conns
    _insert_candidate(skills, "m-t401b", name="exp-000c401b",
                      suggested_name="retry-skill",
                      suggested_kind="skill", suggested_domain="ai")
    r1 = sc_ops.review_skill_candidate(mem, skills, "m-t401b", "promoted", hub=hub)
    assert r1.ok, r1.message
    # 模拟落成后标状态失败：候选行回退 accepted
    skills.execute(
        "UPDATE skill_candidates SET status='accepted', name='exp-000c401b'"
        " WHERE memory_id='m-t401b'"
    )
    skills.commit()
    r2 = sc_ops.review_skill_candidate(mem, skills, "m-t401b", "promoted", hub=hub)
    assert r2.ok, f"同内容重试应放行（幂等自愈）: {r2.message}"
    assert skills.execute(
        "SELECT COUNT(*) FROM skills WHERE name='retry-skill'"
    ).fetchone()[0] == 1
    row = skills.execute(
        "SELECT status, name FROM skill_candidates WHERE memory_id='m-t401b'"
    ).fetchone()
    assert row["status"] == "promoted" and row["name"] == "retry-skill"
