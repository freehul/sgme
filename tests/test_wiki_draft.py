"""tests/test_wiki_draft.py：wiki 草稿态与转正（v0.3 S4，2026-11-14）。

验收（对应 D5 拍板「自动写入先落草稿态」）：
1. 建页可指定 status=draft；列表按 status=draft 可筛选
2. **检索默认不返回草稿**（FTS / LIKE 两条路都只认 active——这是草稿态的核心价值）
3. 转正：draft → active 后立刻可被检索命中
4. 幂等：已 active 重复转正返回 promoted=false
5. superseded 历史页不许转正（会打乱取代链）→ 400
6. 页面不存在 → 404

设计依据：docs/plans/设计-v0.3-三平面分流定稿与实施契约.md §3.2
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config
from sgme.data import db as db_mod
from sgme.data import wiki_dao
from sgme.server.app import create_app


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    mem = db_mod.connect_memory(tmp_path / "data")
    session = db_mod.connect_session(tmp_path / "data")
    wiki = db_mod.connect_wiki(tmp_path / "data")
    app = create_app(
        cfg=config.load_config(), mem_conn=mem, session_conn=session, wiki_conn=wiki,
        admin_key="test-admin", agent_key="test-agent",
        bearer_token="", agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    yield TestClient(app), wiki
    for conn in (mem, session, wiki):
        db_mod.close(conn)


def _h():
    return {"X-API-Key": "test-agent"}


def _create(client, title, content, status=None):
    body = {"title": title, "content": content, "source_type": "text"}
    if status:
        body["status"] = status
    return client.post("/v1/wiki/pages", json=body, headers=_h())


def test_create_draft_and_list(api):
    client, _ = api
    r = _create(client, "草稿页甲", "这是一段还没评审的知识内容 zetaunique", status="draft")
    assert r.status_code == 200
    pid = r.json()["page_id"]

    r = client.get("/v1/wiki/pages?status=draft", headers=_h())
    assert r.status_code == 200
    ids = [p["page_id"] for p in r.json()["pages"]]
    assert pid in ids


def test_draft_not_searchable_until_promoted(api):
    client, _ = api
    r = _create(client, "草稿页乙", "独特词 gammaunique 只在草稿里出现", status="draft")
    pid = r.json()["page_id"]

    hit = client.get("/v1/wiki/search?q=gammaunique", headers=_h())
    assert hit.status_code == 200
    assert [p["page_id"] for p in hit.json()["results"]] == []

    # 转正后可检索
    pr = client.post(f"/v1/wiki/pages/{pid}/promote", json={"reviewer": "leo"}, headers=_h())
    assert pr.status_code == 200
    assert pr.json()["promoted"] is True and pr.json()["status"] == "active"

    hit2 = client.get("/v1/wiki/search?q=gammaunique", headers=_h())
    assert [p["page_id"] for p in hit2.json()["results"]] == [pid]


def test_promote_is_idempotent(api):
    client, _ = api
    pid = _create(client, "草稿页丙", "内容 deltaunique", status="draft").json()["page_id"]
    assert client.post(f"/v1/wiki/pages/{pid}/promote", headers=_h()).json()["promoted"] is True
    again = client.post(f"/v1/wiki/pages/{pid}/promote", headers=_h())
    assert again.status_code == 200
    assert again.json()["promoted"] is False


def test_superseded_cannot_promote(api):
    client, wiki = api
    pid = _create(client, "历史页", "旧内容 epsilonunique").json()["page_id"]
    wiki_dao.mark_superseded(wiki, pid, supersedes_by=pid)
    r = client.post(f"/v1/wiki/pages/{pid}/promote", headers=_h())
    assert r.status_code >= 400


def test_promote_missing_page(api):
    client, _ = api
    r = client.post("/v1/wiki/pages/nope/promote", headers=_h())
    assert r.status_code == 404


def test_dao_promote_only_from_draft(api):
    client, wiki = api
    pid = _create(client, "直接发布的页", "正常内容 zetaunique2").json()["page_id"]
    # 建的时候没给 status → 默认 active，转正应返回 False
    assert wiki_dao.promote_page(wiki, pid) is False
