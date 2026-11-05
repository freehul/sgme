"""tests/test_persona_fill.py：D-76 人设懒补全与写回（T-306）。

四步闭环：追问 → ①查角色档案 → ②命中直接用 → ③未命中生成+冲突检查+落盘 → ④回答。
覆盖验收六条：
① 命中不重复生成（零 LLM）
② 冲突时拒绝并返回「没提过」（不写回）
③ 落盘条目带 generated: true 与 keys
④ memory.db 无新增写入（隔离铁律：角色人生事实永不进记忆池）
⑤ 同一问题二次调用返回同一事实
⑥ 角色不存在 → ERR_NOT_FOUND
另加 HTTP 端点接线测试。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.operations import care as care_operation
from sgme.operations.errors import ERR_NOT_FOUND
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

FILL_ANSWER = "一所做外贸的小公司，订单时好时坏。\nKEYS: 第一份工作, 公司, 外贸"


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
def card_with_book(roles_dir):
    """带 character_book 的测试卡（一条非 constant 人生条目 + 一条 constant 习惯）。"""
    card = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "管家",
            "description": "测试卡",
            "character_book": {
                "name": "管家的人生",
                "entries": [
                    {
                        "keys": ["中学", "上学"],
                        "constant": False,
                        "insertion_order": 100,
                        "content": "一所普通中学，没什么特别的。",
                    },
                    {
                        "keys": ["茶"],
                        "constant": True,
                        "insertion_order": 130,
                        "content": "只喝不加糖的茶。",
                    },
                ],
            },
        },
    }
    roles_mod.save_role(roles_dir, "butler", card)
    return card


@pytest.fixture
def fake_llm(monkeypatch):
    """mock 提炼链：记录调用，返回可控文本。"""
    calls: list[str] = []

    def _install(text: str):
        def fake(cfg_llm, prompt, chain_name="refinement", client=None):
            calls.append(prompt)
            return text, "fake-provider", {}
        monkeypatch.setattr("sgme.llm.chain.call_with_fallback", fake)
        return calls

    return _install


# ---------- ① 命中不重复生成 ----------

def test_hit_skips_llm(roles_dir, card_with_book, cfg, fake_llm):
    calls = fake_llm("不该被调用的文本")

    res = care_operation.persona_fill("butler", "你中学在哪上的？", cfg)

    assert res.ok is True
    assert res.data["status"] == "hit"
    assert res.data["answer"] == "一所普通中学，没什么特别的。"
    assert res.data["generated"] is False
    assert calls == []  # 命中零 LLM，不重复生成


# ---------- ② 冲突时拒绝并返回「没提过」 ----------

def test_conflict_returns_unset_no_write(roles_dir, card_with_book, cfg, fake_llm):
    fake_llm("UNSET")

    res = care_operation.persona_fill("butler", "你第一份工作月薪多少？", cfg)

    assert res.ok is True
    assert res.data["status"] == "unset"
    assert "没提过" in res.data["answer"]
    card = roles_mod.get_role(roles_dir, "butler")
    entries = card["data"]["character_book"]["entries"]
    assert len(entries) == 2  # 未写回


# ---------- ③ 落盘条目带 generated: true 与 keys ----------

def test_filled_entry_persisted_with_generated_and_keys(roles_dir, card_with_book, cfg, fake_llm):
    fake_llm(FILL_ANSWER)

    res = care_operation.persona_fill("butler", "你第一份工作在哪？", cfg)

    assert res.ok is True
    assert res.data["status"] == "filled"
    card = roles_mod.get_role(roles_dir, "butler")
    entries = card["data"]["character_book"]["entries"]
    assert len(entries) == 3
    new = entries[-1]
    assert new["generated"] is True
    assert new["keys"] == ["第一份工作", "公司", "外贸"]
    assert new["constant"] is False
    assert new["content"] == "一所做外贸的小公司，订单时好时坏。"
    assert new["insertion_order"] == 140  # max(130)+10


# ---------- ④ memory.db 无新增写入（隔离铁律） ----------

def test_memory_db_untouched(tmp_path, roles_dir, card_with_book, cfg, fake_llm):
    from sgme.data import db as db_mod

    mem_conn, _, _ = db_mod.init_databases(tmp_path / "data")
    fake_llm(FILL_ANSWER)

    def db_bytes() -> dict:
        return {
            f.name: f.read_bytes()
            for f in sorted((tmp_path / "data").glob("*.db"))
        }

    before = db_bytes()
    res = care_operation.persona_fill("butler", "你第一份工作在哪？", cfg)
    after = db_bytes()

    assert res.ok is True and res.data["status"] == "filled"
    assert before == after  # 三库字节不变 = 零写入
    db_mod.close(mem_conn)


# ---------- ⑤ 同一问题二次调用返回同一事实 ----------

def test_same_question_same_fact_twice(roles_dir, card_with_book, cfg, fake_llm):
    calls = fake_llm(FILL_ANSWER)

    r1 = care_operation.persona_fill("butler", "你第一份工作在哪？", cfg)
    r2 = care_operation.persona_fill("butler", "你第一份工作在哪？", cfg)

    assert r1.data["status"] == "filled"
    assert r2.data["status"] == "hit"  # 第二次命中第一次写回的条目
    assert r2.data["answer"] == r1.data["answer"]
    assert r2.data["generated"] is True
    assert len(calls) == 1  # LLM 只被调一次


# ---------- ⑥ 角色不存在 → ERR_NOT_FOUND ----------

def test_role_not_found(roles_dir, cfg):
    res = care_operation.persona_fill("ghost", "你老家在哪？", cfg)

    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


# ---------- 附加：question 为空 → ERR_INVALID_ARGS ----------

def test_empty_question_rejected(roles_dir, card_with_book, cfg):
    res = care_operation.persona_fill("butler", "  ", cfg)
    assert res.ok is False


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


def test_http_persona_fill_endpoint(app, roles_dir, card_with_book, fake_llm):
    fake_llm(FILL_ANSWER)
    client = TestClient(app)

    r = client.post(
        "/v1/admin/roles/butler/persona-fill",
        json={"question": "你第一份工作在哪？"},
        headers=AGENT_HEADERS,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "filled"
    assert "外贸" in body["answer"]

# ---------- ⑦ 真实链路回归：概念标签 ≠ 问题原文（2026-11-05 生产实测发现） ----------

# 真实 LLM 产出的 keys 是「概念标签」，不是用户问句里的原文。
# 生产实测（companion 角色）：第一次写入 keys=普通乡镇小学/下雨天/操场泥/童年记忆，
# 第二次问「你小学在哪上的」时全部不命中（key 不在问句里字面出现）→ 重新生成，
# insertion_order 100 → 110，同一件事换个说法，用户看到「每次都在现编」。
REAL_SHAPE_ANSWER = (
    "上的是一所普通乡镇小学，最记得下雨天操场全是泥，鞋陷进去拔不出来。\n"
    "KEYS: 普通乡镇小学, 下雨天, 操场泥, 童年记忆"
)


def test_hit_when_keys_are_conceptual_labels(roles_dir, card_with_book, cfg, fake_llm):
    """keys 是概念标签时，二次追问仍须命中同一条（零重生成）。"""
    calls = fake_llm(REAL_SHAPE_ANSWER)

    r1 = care_operation.persona_fill(
        "butler", "你小学在哪上的，记忆最深刻的事是什么？", cfg
    )
    assert r1.ok is True and r1.data["status"] == "filled"

    # 人类第二次会换个问法，只留主干
    r2 = care_operation.persona_fill("butler", "你小学在哪上的？", cfg)

    assert r2.ok is True
    assert r2.data["status"] == "hit", "概念标签未被命中 → 每次都会重新编一段"
    assert r2.data["answer"] == r1.data["answer"]
    assert len(calls) == 1, "命中必须零 LLM；重建说明又生成了一遍"


def test_source_question_is_recorded_and_hits(roles_dir, card_with_book, cfg, fake_llm):
    """写回条目必须记下原问题；同一句复问走原问题精确匹配。"""
    calls = fake_llm(REAL_SHAPE_ANSWER)
    q = "你小学在哪上的，记忆最深刻的事是什么？"

    care_operation.persona_fill("butler", q, cfg)

    card = roles_mod.get_role(roles_dir, "butler")
    entry = card["data"]["character_book"]["entries"][-1]
    assert entry.get("source_questions") == [q]

    r2 = care_operation.persona_fill("butler", q, cfg)
    assert r2.data["status"] == "hit"
    assert len(calls) == 1


def test_no_false_hit_on_unrelated_question(roles_dir, card_with_book, cfg, fake_llm):
    """放宽命中不得误伤：无关问题仍走生成，不能把人生条目乱套。"""
    fake_llm(REAL_SHAPE_ANSWER)
    care_operation.persona_fill("butler", "你小学在哪上的？", cfg)

    fake_llm("UNSET")
    r = care_operation.persona_fill("butler", "你第一份工作月薪多少？", cfg)
    assert r.data["status"] == "unset"


def test_existing_entry_still_hits_without_source_questions(roles_dir, card_with_book, cfg, fake_llm):
    """存量条目没有 source_questions（老数据）也要能靠 keys 命中。"""
    calls = fake_llm("不该被调用")
    res = care_operation.persona_fill("butler", "你中学在哪上的？", cfg)
    assert res.data["status"] == "hit"
    assert calls == []

