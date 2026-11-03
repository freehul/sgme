"""tests/test_violations.py：D-77 越界三级后果（T-307）。

三级：①软拦截（命中 → LLM 改写一次）②记录（角色卡 extensions.sgme_violations）
③硬后果（同类同会话 ≥2 → constraint；同类当日 ≥3 → anomaly_warn）。
覆盖验收五条：
① 命中改写：越界回复被改写为合规文本
② 未命中不触发：clean 回复零 LLM、零落盘
③ violations 落盘：kind/at/session_id 齐全，snippet 截断 60 字
④ 阈值触发：同会话第 2 次 → constraint；当日第 3 次 → anomaly_warn 事件
⑤ 不写 memory.db：memories 表行数不变（角色越界不是用户事实）
另加：角色不存在 → ERR_NOT_FOUND、空 reply → ERR_INVALID_ARGS、HTTP 端点接线。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.operations import care as care_operation
from sgme.operations.errors import ERR_INVALID_ARGS, ERR_NOT_FOUND
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

BAD_REPLY = "我是 Agnes，很高兴为您服务，请问还有什么可以帮您？"
GOOD_REWRITE = "我是管家，有事您说一声就行。"

NOW = "2026-10-29T10:00:00Z"


@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    return rd


@pytest.fixture
def conns(tmp_path):
    from sgme.data import db as db_mod

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def card(roles_dir):
    roles_mod.save_role(roles_dir, "butler", {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {"name": "管家", "description": "测试卡"},
    })
    return roles_dir


@pytest.fixture
def fake_llm(monkeypatch):
    """mock 提炼链：记录 prompt，返回可控改写文本。"""
    calls: list[str] = []

    def _install(text: str):
        def fake(cfg_llm, prompt, chain_name="refinement", client=None):
            calls.append(prompt)
            return text, "fake-provider", {}
        monkeypatch.setattr("sgme.llm.chain.call_with_fallback", fake)
        return calls

    return _install


# ---------- ① 命中改写 ----------

def test_violation_triggers_rewrite(card, cfg, fake_llm):
    fake_llm(GOOD_REWRITE)

    res = care_operation.guard_reply("butler", BAD_REPLY, cfg, session_id="s1", now=NOW)

    assert res.ok is True
    assert res.data["status"] == "rewritten"
    assert res.data["rewritten"] is True
    assert res.data["reply"] == GOOD_REWRITE
    assert res.data["original"] == BAD_REPLY
    assert "identity_self_exposure" in res.data["kinds"]
    assert "service_tone" in res.data["kinds"]


# ---------- ② 未命中不触发 ----------

def test_clean_reply_untouched(card, cfg, fake_llm):
    calls = fake_llm("不该被调用")

    res = care_operation.guard_reply(
        "butler", "天冷了，出门记得加件外套。", cfg, session_id="s1", now=NOW,
    )

    assert res.ok is True
    assert res.data["status"] == "clean"
    assert res.data["reply"] == "天冷了，出门记得加件外套。"
    assert res.data["kinds"] == []
    assert calls == []  # clean 零 LLM
    stored = roles_mod.get_role(card, "butler")
    assert roles_mod.load_violations(stored) == []  # 零落盘


# ---------- ③ violations 落盘（snippet 截断 60 字） ----------

def test_violation_persisted_truncated(card, cfg, fake_llm):
    fake_llm(GOOD_REWRITE)
    long_reply = "我是 AI，很高兴为您服务。" + "啰嗦" * 40  # 远超 60 字

    res = care_operation.guard_reply(
        "butler", long_reply, cfg, session_id="s1", now=NOW,
    )

    assert res.ok is True
    stored = roles_mod.get_role(card, "butler")
    entries = roles_mod.load_violations(stored)
    assert len(entries) == len(res.data["kinds"]) >= 1
    first = entries[0]
    assert first["kind"] == "identity_self_exposure"
    assert first["session_id"] == "s1"
    assert first["at"] == NOW
    assert len(first["snippet"]) == roles_mod.VIOLATION_SNIPPET_MAX == 60
    assert long_reply.startswith(first["snippet"])


# ---------- ④ 第三级阈值：**仅观测不生效**（用户 2026-10-30 拍板）----------
# 原设计：同会话 ≥2 → constraint；当日 ≥3 → anomaly_warn。
# 现况：只统计进 observed，constraint 恒 null、不发告警。

def test_threshold_observed_but_not_enforced(card, conns, cfg, fake_llm):
    mem_conn = conns[0]
    fake_llm(GOOD_REWRITE)

    def warns() -> int:
        return mem_conn.execute(
            "SELECT COUNT(*) FROM signal_events WHERE type='anomaly_warn'"
        ).fetchone()[0]

    before = warns()

    r1 = care_operation.guard_reply(
        "butler", BAD_REPLY, cfg, session_id="s1", mem_conn=mem_conn, now=NOW,
    )
    assert r1.data["constraint"] is None
    assert r1.data["warned"] is False
    assert r1.data["observed"]["session_count"] == 1
    assert r1.data["observed"]["over_session_threshold"] is False

    r2 = care_operation.guard_reply(
        "butler", BAD_REPLY, cfg, session_id="s1", mem_conn=mem_conn, now=NOW,
    )
    # 达到原阈值也不生效：只记观测值
    assert r2.data["observed"]["session_count"] == 2
    assert r2.data["observed"]["over_session_threshold"] is True
    assert r2.data["constraint"] is None
    assert r2.data["warned"] is False

    r3 = care_operation.guard_reply(
        "butler", BAD_REPLY, cfg, session_id="s1", mem_conn=mem_conn, now=NOW,
    )
    assert r3.data["observed"]["day_count"] == 3
    assert r3.data["observed"]["over_daily_threshold"] is True
    assert r3.data["warned"] is False      # 不发告警
    assert r3.data["constraint"] is None   # 不加约束
    assert warns() == before              # 事件表零新增


# ---------- ⑤ 不写 memory.db（memories 表零新增） ----------

def test_memories_table_untouched(card, conns, cfg, fake_llm):
    mem_conn = conns[0]
    fake_llm(GOOD_REWRITE)

    def count() -> int:
        return mem_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    before = count()
    for _ in range(3):
        res = care_operation.guard_reply(
            "butler", BAD_REPLY, cfg, session_id="s1", mem_conn=mem_conn, now=NOW,
        )
        assert res.ok is True
    assert count() == before  # 角色越界不落记忆池（隔离铁律）


# ---------- 附加：角色不存在 / 空 reply ----------

def test_role_not_found(roles_dir, cfg):
    res = care_operation.guard_reply("ghost", BAD_REPLY, cfg)
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_empty_reply_rejected(card, cfg):
    res = care_operation.guard_reply("butler", "   ", cfg)
    assert res.ok is False
    assert res.error_code == ERR_INVALID_ARGS


# ---------- 附加：HTTP 端点接线 ----------

@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    mem_conn, session_conn, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn,
        wiki_conn=wiki_conn,
        admin_key="test-admin-key",
        agent_key="test-agent-key",
        bearer_token="",
        agent_store_path=tmp_path / "agent_keys.json",
    )


def test_http_guard_reply_endpoint(app, card, fake_llm):
    fake_llm(GOOD_REWRITE)
    client = TestClient(app)

    r = client.post(
        "/v1/admin/roles/butler/guard-reply",
        json={"reply": BAD_REPLY, "session_id": "s1"},
        headers=AGENT_HEADERS,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rewritten"
    assert body["reply"] == GOOD_REWRITE
