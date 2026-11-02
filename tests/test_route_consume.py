# -*- coding: utf-8 -*-
"""tests/test_route_consume.py：D-67 route 分流消费管道测试（TDD）。

覆盖三件事：
1. 技能候选池的数据层与 operations 层——列表、转入、审核（幂等）；
2. 存量脚本 ``scripts/consume_skill_candidates.py``——dry-run / apply / 分批 / 重跑幂等；
3. 三处入口对齐——HTTP 管理端点、MCP 工具清单、operations 共用实现。

设计口径（与实现同步钉死）：
- 候选池是 skills.db 里的独立表 ``skill_candidates``，**不写 skills 主表**；
  因此不会污染技能检索，也不会被 sync_index 的 delete 分支清掉。
- 一条 route=skill_candidate 的记忆转入候选池即视为「已消费」；再次调用不重复插入。
- 高置信度（``confidence >= 0.8``）自动 accepted；低置信默认留在待消费列表，
  加 ``--include-low-confidence`` 才以 pending 收进池。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao, skill_candidates_dao
from sgme.mcp_server import ONBOARDING_TOOLS, bind_app_state, build_mcp_server
from sgme.operations import skill_candidates as sc
from sgme.server.app import create_app

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}

HIGH_CONTENT = (
    "部署脚本必须先备份 memory.db 再执行迁移，否则迁移失败后无法回滚到旧版本。"
)
LOW_CONTENT = "今天看了一下日志，感觉有点吵。"


# ---------- 夹具 ----------


@pytest.fixture
def dbs(tmp_path):
    """隔离的 memory.db + skills.db（含候选池表）。"""
    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    skills = db_mod.connect_skills(tmp_path / "data")
    yield mem, skills
    for c in (mem, sess, wiki, skills):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _add(
    mem,
    content: str,
    route: str = "skill_candidate",
    *,
    mtype: str | None = "经验",
    certainty: str | None = "confirmed",
    priority: int = 70,
    memory_id: str | None = None,
) -> str:
    """插入一条带 2.0 route 元数据的记忆。"""
    extra: dict = {}
    if route is not None:
        extra["route"] = route
    if mtype is not None:
        extra["type"] = mtype
    if certainty is not None:
        extra["certainty"] = certainty
    return memory_dao.insert_memory(
        mem,
        content=content,
        memory_type="fact",
        priority=priority,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["preferences"],
        prompt_version="test",
        memory_id=memory_id,
        extra=extra,
    )


# ---------- 置信度与草稿 ----------


def test_score_high_confidence_experience():
    """确认型经验 + 可执行措辞 + 具体对象 → 高置信。"""
    result = sc.score_candidate(
        {
            "content": HIGH_CONTENT,
            "type": "经验",
            "certainty": "confirmed",
            "priority": 70,
            "dimensions": ["preferences"],
        }
    )
    assert result["confidence"] >= 0.8
    assert result["high_confidence"] is True
    assert result["reasons"]


def test_score_low_confidence_vague_note():
    """模糊感想 / 非经验 / 无执行线索 → 低置信，留待审核。"""
    result = sc.score_candidate(
        {
            "content": LOW_CONTENT,
            "type": "想法",
            "certainty": "uncertain",
            "priority": 30,
            "dimensions": [],
        }
    )
    assert result["confidence"] < 0.8
    assert result["high_confidence"] is False


def test_build_skill_draft_is_deterministic_and_safe(dbs):
    """草稿名只由 memory_id 派生（ASCII 白名单），正文含来源但不含真实路径。"""
    mem, _ = dbs
    mid = _add(mem, HIGH_CONTENT, memory_id="0123456789abcdef")
    memory = memory_dao.get_memory(mem, mid)

    draft = sc.build_skill_draft(memory)
    again = sc.build_skill_draft(memory)

    assert draft == again, "同一条记忆的草稿必须确定性生成"
    assert draft["name"] == "exp-01234567"
    assert draft["content"].startswith("---\n")
    assert "description:" in draft["content"]
    assert mid in draft["content"]
    assert "D:\\" not in draft["content"] and "/home/" not in draft["content"]


# ---------- 候选池 DAO ----------


def test_candidate_dao_insert_is_idempotent(dbs):
    """同一 memory_id 重复写入不产生第二行。"""
    _, skills = dbs
    rec = {
        "memory_id": "m-1",
        "name": "exp-m-1",
        "description": "x",
        "content": "draft",
        "confidence": 0.9,
        "status": "accepted",
    }

    first, created1 = skill_candidates_dao.create_candidate(skills, rec)
    second, created2 = skill_candidates_dao.create_candidate(skills, rec)

    assert created1 is True and created2 is False
    assert first == second
    assert skill_candidates_dao.count_candidates(skills) == 1


# ---------- 待消费列表 ----------


def test_list_pending_excludes_consumed_and_other_routes(dbs):
    """只列 route=skill_candidate 且尚未进入候选池的记忆。"""
    mem, skills = dbs
    keep = _add(mem, HIGH_CONTENT + "A")
    adopted = _add(mem, HIGH_CONTENT + "B")
    _add(mem, "普通用户偏好", route="memory", mtype="偏好")
    result = sc.adopt_skill_candidate(mem, skills, adopted)
    assert result.ok

    page = sc.list_pending_route_memories(mem, skills, limit=50)

    assert page.ok
    ids = {item["memory_id"] for item in page.data["items"]}
    assert keep in ids
    assert adopted not in ids
    assert page.data["remaining"] == 1


def test_list_pending_filters_by_confidence(dbs):
    """min_confidence 只保留达线项。"""
    mem, skills = dbs
    high = _add(mem, HIGH_CONTENT)
    low = _add(mem, LOW_CONTENT, mtype="想法", certainty="uncertain", priority=30)

    page = sc.list_pending_route_memories(mem, skills, limit=50, min_confidence=0.8)

    ids = {item["memory_id"] for item in page.data["items"]}
    assert ids == {high}
    assert low not in ids


# ---------- 转入候选池 ----------


def test_adopt_high_confidence_marks_accepted(dbs):
    """高置信自动 accepted；低置信保持 pending。"""
    mem, skills = dbs
    high = _add(mem, HIGH_CONTENT)
    low = _add(mem, LOW_CONTENT, mtype="想法", certainty="uncertain", priority=30)

    r1 = sc.adopt_skill_candidate(mem, skills, high)
    r2 = sc.adopt_skill_candidate(mem, skills, low)

    assert r1.ok and r1.data["status"] == "accepted"
    assert r2.ok and r2.data["status"] == "pending"
    assert r1.data["created"] is True


def test_adopt_is_idempotent(dbs):
    """重复转入：不新增行、不覆盖既有审核状态。"""
    mem, skills = dbs
    mid = _add(mem, HIGH_CONTENT)

    first = sc.adopt_skill_candidate(mem, skills, mid)
    sc.review_skill_candidate(mem, skills, mid, "ignored", note="人工忽略")
    second = sc.adopt_skill_candidate(mem, skills, mid, status="accepted")

    assert first.data["candidate_id"] == second.data["candidate_id"]
    assert second.data["created"] is False
    assert second.data["status"] == "ignored", "转入不得把已审核状态打回"
    assert skill_candidates_dao.count_candidates(skills) == 1


def test_adopt_rejects_non_candidate_route(dbs):
    """route=memory 的记忆不得转入技能候选池。"""
    mem, skills = dbs
    mid = _add(mem, "用户偏好中文", route="memory", mtype="偏好")

    result = sc.adopt_skill_candidate(mem, skills, mid)

    assert result.ok is False
    assert result.error_code == "ERR_INVALID_ARGS"
    assert skill_candidates_dao.count_candidates(skills) == 0


def test_adopt_missing_memory_404(dbs):
    mem, skills = dbs
    result = sc.adopt_skill_candidate(mem, skills, "not-exist")
    assert result.ok is False
    assert result.error_code == "ERR_NOT_FOUND"


def test_duplicate_content_is_ignored_not_duplicated(dbs):
    """同内容只保留一条候选，后来的标记 ignored 并指向首条。"""
    mem, skills = dbs
    first = _add(mem, HIGH_CONTENT, memory_id="dup-first")
    second = _add(mem, HIGH_CONTENT, memory_id="dup-second")

    sc.adopt_skill_candidate(mem, skills, first)
    r2 = sc.adopt_skill_candidate(mem, skills, second)

    assert r2.ok
    assert r2.data["duplicate"] is True
    assert r2.data["status"] == "ignored"
    assert r2.data["duplicate_of"] == first
    assert skill_candidates_dao.count_candidates(skills) == 2


# ---------- 审核 / 忽略（幂等） ----------


def test_review_ignore_is_idempotent(dbs):
    """连续两次忽略：第二次 changed=False，状态与时间戳不抖。"""
    mem, skills = dbs
    mid = _add(mem, LOW_CONTENT, mtype="想法", certainty="uncertain", priority=30)
    sc.adopt_skill_candidate(mem, skills, mid)

    first = sc.review_skill_candidate(mem, skills, mid, "ignored", note="不通用")
    after_first = skill_candidates_dao.get_candidate(skills, mid)
    second = sc.review_skill_candidate(mem, skills, mid, "ignored")
    after_second = skill_candidates_dao.get_candidate(skills, mid)

    assert first.ok and first.data["changed"] is True
    assert second.ok and second.data["changed"] is False
    assert after_second["status"] == "ignored"
    assert after_second["updated_at"] == after_first["updated_at"]
    assert after_second["reviewed_at"] == after_first["reviewed_at"]


def test_review_auto_adopts_when_missing(dbs):
    """直接审核一条未转入的记忆：先建候选再落决策，仍幂等。"""
    mem, skills = dbs
    mid = _add(mem, HIGH_CONTENT)

    r = sc.review_skill_candidate(mem, skills, mid, "ignored", note="不需要")

    assert r.ok and r.data["created"] is True
    row = skill_candidates_dao.get_candidate(skills, mid)
    assert row is not None and row["status"] == "ignored"


def test_review_rejects_unknown_decision(dbs):
    mem, skills = dbs
    mid = _add(mem, HIGH_CONTENT)
    with pytest.raises(sc.InvalidArgs):
        sc.review_skill_candidate(mem, skills, mid, "done")


def test_list_pool_filters_status_and_confidence(dbs):
    mem, skills = dbs
    high = _add(mem, HIGH_CONTENT)
    low = _add(mem, LOW_CONTENT, mtype="想法", certainty="uncertain", priority=30)
    sc.adopt_skill_candidate(mem, skills, high)
    sc.adopt_skill_candidate(mem, skills, low)

    accepted = sc.list_skill_candidates(skills, status="accepted")
    pending = sc.list_skill_candidates(skills, status="pending")
    confident = sc.list_skill_candidates(skills, min_confidence=0.8)

    assert {x["memory_id"] for x in accepted.data["items"]} == {high}
    assert {x["memory_id"] for x in pending.data["items"]} == {low}
    assert {x["memory_id"] for x in confident.data["items"]} == {high}
    assert accepted.data["total"] == 1
    summary = sc.candidate_summary(skills)
    assert summary.ok
    assert summary.data["total"] == 2
    assert summary.data["by_status"] == {"accepted": 1, "pending": 1}
    assert summary.data["high_confidence"] == 1


# ---------- 存量脚本 ----------


def test_script_dry_run_writes_nothing(dbs, tmp_path):
    from scripts import consume_skill_candidates as script

    mem, skills = dbs
    for i in range(3):
        _add(mem, HIGH_CONTENT + str(i), memory_id=f"dry-{i}")

    stats = script.main(
        ["--dry-run", "--data-dir", str(tmp_path / "data"), "--batch-size", "2"]
    )

    assert stats["scanned"] == 3
    assert stats["high_confidence"] == 3
    assert stats["inserted"] == 0
    assert skill_candidates_dao.count_candidates(skills) == 0


def test_script_dry_run_does_not_create_skills_db(tmp_path):
    """dry-run 对数据目录零写入：skills.db 不存在时也不得创建它。"""
    from scripts import consume_skill_candidates as script

    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    _add(mem, HIGH_CONTENT, memory_id="no-write")
    mem.close()
    sess.close()
    wiki.close()
    assert not (tmp_path / "data" / "skills.db").exists()

    stats = script.main(["--dry-run", "--data-dir", str(tmp_path / "data")])

    assert stats["scanned"] == 1
    assert not (tmp_path / "data" / "skills.db").exists()


def test_script_apply_batches_and_is_rerunnable(dbs, tmp_path):
    from scripts import consume_skill_candidates as script

    mem, skills = dbs
    for i in range(25):
        mtype = "经验" if i % 2 == 0 else "想法"
        certainty = "confirmed" if i % 2 == 0 else "uncertain"
        _add(
            mem,
            (HIGH_CONTENT if i % 2 == 0 else LOW_CONTENT) + f" #{i}",
            mtype=mtype,
            certainty=certainty,
            priority=70 if i % 2 == 0 else 30,
            memory_id=f"batch-{i:03d}",
        )

    first = script.main(
        ["--apply", "--data-dir", str(tmp_path / "data"), "--batch-size", "7"]
    )
    second = script.main(
        ["--apply", "--data-dir", str(tmp_path / "data"), "--batch-size", "7"]
    )

    assert first["scanned"] == 25
    assert first["inserted"] == 13
    assert first["accepted"] == 13
    assert first["deferred"] == 12, "低置信默认留在待消费列表，不灌进候选池"
    assert skill_candidates_dao.count_candidates(skills) == 13
    assert second["scanned"] == 25
    assert second["inserted"] == 0
    assert second["skipped"] == 13
    assert second["deferred"] == 12
    assert skill_candidates_dao.count_candidates(skills) == 13


def test_script_can_include_low_confidence_as_pending(dbs, tmp_path):
    """显式 --include-low-confidence 时，低置信才以 pending 收进池。"""
    from scripts import consume_skill_candidates as script

    mem, skills = dbs
    _add(mem, HIGH_CONTENT, memory_id="mix-high")
    _add(
        mem,
        LOW_CONTENT,
        mtype="想法",
        certainty="uncertain",
        priority=30,
        memory_id="mix-low",
    )

    stats = script.main(
        [
            "--apply",
            "--data-dir",
            str(tmp_path / "data"),
            "--include-low-confidence",
        ]
    )

    assert stats["inserted"] == 2
    assert stats["accepted"] == 1
    assert stats["pending"] == 1
    assert skill_candidates_dao.count_candidates(skills) == 2


def test_script_limit(dbs, tmp_path):
    from scripts import consume_skill_candidates as script

    mem, skills = dbs
    for i in range(5):
        _add(mem, HIGH_CONTENT + str(i), memory_id=f"limit-{i}")

    stats = script.main(
        ["--apply", "--data-dir", str(tmp_path / "data"), "--limit", "2"]
    )

    assert stats["scanned"] == 2
    assert skill_candidates_dao.count_candidates(skills) == 2


# ---------- HTTP 管理端点 ----------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    cfg = sgme_config.load_config()
    cfg["skills"] = {"enabled": True, "source_dirs": [str(tmp_path / "skills_src")]}
    (tmp_path / "skills_src").mkdir()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg,
        mem_conn=mem,
        session_conn=sess,
        wiki_conn=wiki,
        data_dir=tmp_path / "data",
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    with TestClient(app) as c:
        c.mem = mem
        c.skills = app.state.skills_conn
        yield c
    for conn in (mem, sess, wiki, app.state.skills_conn):
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def test_http_requires_admin_key(client):
    assert client.get("/v1/admin/skill-candidates").status_code == 403
    assert client.get(
        "/v1/admin/skill-candidates", headers={"X-API-Key": AGENT_KEY}
    ).status_code == 403


def test_http_pending_adopt_review_flow(client):
    mid = _add(client.mem, HIGH_CONTENT, memory_id="http-mid")

    pending = client.get("/v1/admin/skill-candidates/pending", headers=ADMIN_HEADERS)
    assert pending.status_code == 200
    assert [x["memory_id"] for x in pending.json()["items"]] == [mid]

    adopted = client.post(
        "/v1/admin/skill-candidates/adopt",
        headers=ADMIN_HEADERS,
        json={"memory_id": mid},
    )
    assert adopted.status_code == 200
    assert adopted.json()["status"] == "accepted"

    again = client.post(
        "/v1/admin/skill-candidates/adopt",
        headers=ADMIN_HEADERS,
        json={"memory_id": mid},
    )
    assert again.status_code == 200
    assert again.json()["created"] is False

    reviewed = client.patch(
        f"/v1/admin/skill-candidates/{mid}",
        headers=ADMIN_HEADERS,
        json={"decision": "ignored", "note": "人工忽略"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "ignored"
    assert reviewed.json()["changed"] is True

    repeat = client.patch(
        f"/v1/admin/skill-candidates/{mid}",
        headers=ADMIN_HEADERS,
        json={"decision": "ignored"},
    )
    assert repeat.status_code == 200
    assert repeat.json()["changed"] is False


def test_http_adopt_unknown_memory_404(client):
    r = client.post(
        "/v1/admin/skill-candidates/adopt",
        headers=ADMIN_HEADERS,
        json={"memory_id": "nope"},
    )
    assert r.status_code == 404


# ---------- MCP 工具面 ----------


def test_mcp_candidate_tools_declared():
    """新增 MCP 工具必须同步进 onboarding 能力清单（防清单漂移）。"""
    expected = {
        "skill_candidate_pending",
        "skill_candidate_list",
        "skill_candidate_adopt",
        "skill_candidate_review",
    }
    declared = {t["name"] for t in ONBOARDING_TOOLS}
    assert expected <= declared


def test_mcp_candidate_tools_registered(tmp_path, monkeypatch):
    """工具真实注册（与 ONBOARDING_TOOLS 的集合由既有测试兜底）。"""
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    skills = db_mod.connect_skills(tmp_path / "data")
    bind_app_state(
        {
            "cfg": cfg,
            "mem_conn": mem,
            "session_conn": sess,
            "wiki_conn": wiki,
            "skills_conn": skills,
        }
    )
    server = build_mcp_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {
        "skill_candidate_pending",
        "skill_candidate_list",
        "skill_candidate_adopt",
        "skill_candidate_review",
    } <= names
    assert json.dumps(sorted(names), ensure_ascii=False)
    for conn in (mem, sess, wiki, skills):
        conn.close()
