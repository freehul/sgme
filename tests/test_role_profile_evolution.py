"""tests/test_role_profile_evolution.py：活人化角色——基线层（sgme_profile）+ 演化层。

覆盖验收（设计文档 4.2，与前端契约对齐）：
① 基线层：extensions.sgme_profile 结构校验（5 键白名单 + personality_slots 六槽位）
② 基线层读写不丢：save → get 原样保留；与 sgme_care 等既有命名空间共存；
   内部读改写（越界记录等）不丢 profile
③ 演化层文件：load 缺文件 → 空结构；append 四种 kind
   （timeline/feelings/personality_changes/values_changes）；at 自动补齐；
   非法 kind/id/字段拒绝且不落盘
④ 操作层：GET 缺文件 200 空结构（不 404）；POST 追加；角色不存在 ERR_NOT_FOUND；
   参数非法 InvalidArgs
⑤ HTTP 两端点接线 + id 白名单（防路径穿越）拒绝 + 卡内 sgme_profile 保存读回
⑥ memory.db 零写入（隔离铁律：演化层只落 data/role_evolution/ 文件，不碰记忆池）
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.operations import care as care_op
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs
from sgme.server.app import create_app

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

NOW = "2026-11-02T08:00:00Z"


# ---------- 夹具与样例 ----------

def _profile() -> dict:
    """基线层五件套（前端契约样例结构，内容为虚构角色设定）。"""
    return {
        "biography": "在虚构的滨城长大，大学读文学，后来成为管家。",
        "values": "秩序之上，温柔其次；答应的事一定做到。",
        "hobbies": "擦银器、泡红茶、给多肉浇水。",
        "voice": "语速偏慢，低音区，句尾常带轻微停顿。",
        "personality_slots": {
            "语言风格": "短句为主，不堆敬语",
            "情绪反应": "先降温再回应",
            "价值观取舍": "守信 > 效率",
            "社交姿态": "低调随和",
            "冲突处理": "先承认再澄清",
            "亲密表达": "行动多于言语",
        },
    }


def _card_with_profile() -> dict:
    """带 sgme_profile + sgme_care 双命名空间的测试卡。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "管家",
            "description": "测试卡（基线层+演化层）",
            "extensions": {
                "sgme_care": {"greeting_templates": ["早。"]},
                "sgme_profile": _profile(),
            },
        },
    }


@pytest.fixture
def roles_dir(tmp_path, monkeypatch):
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    return rd


@pytest.fixture
def evolution_dir(tmp_path, monkeypatch):
    ed = tmp_path / "data" / "role_evolution"
    monkeypatch.setattr(sgme_config, "ROLE_EVOLUTION_DIR", ed)
    return ed


@pytest.fixture
def card(roles_dir):
    roles_mod.save_role(roles_dir, "butler", _card_with_profile())


# ---------- ① 基线层结构校验 ----------

def test_validate_profile_contract_shape_passes():
    assert roles_mod.validate_role_card(_card_with_profile()) == []


def test_validate_profile_unknown_key_rejected():
    card = _card_with_profile()
    card["data"]["extensions"]["sgme_profile"]["bio"] = "多余键"
    errors = roles_mod.validate_role_card(card)
    assert any("sgme_profile" in e and "bio" in e for e in errors)


def test_validate_profile_unknown_slot_rejected():
    card = _card_with_profile()
    card["data"]["extensions"]["sgme_profile"]["personality_slots"]["幽默感"] = ""
    errors = roles_mod.validate_role_card(card)
    assert any("personality_slots" in e and "幽默感" in e for e in errors)


def test_validate_profile_wrong_type_rejected():
    card = _card_with_profile()
    card["data"]["extensions"]["sgme_profile"]["biography"] = {"x": 1}
    errors = roles_mod.validate_role_card(card)
    assert any("biography" in e for e in errors)


# ---------- ② 基线层读写不丢 ----------

def test_profile_roundtrip_not_lost(roles_dir, card):
    got = roles_mod.get_role(roles_dir, "butler")
    assert got["data"]["extensions"]["sgme_profile"] == _profile()


def test_profile_survives_internal_rewrite(roles_dir, card):
    """内部读改写（越界记录路径）不得丢掉 sgme_profile / sgme_care。"""
    roles_mod.append_violation(roles_dir, "butler", "service_tone", "很高兴为您服务", now=NOW)
    got = roles_mod.get_role(roles_dir, "butler")
    assert got["data"]["extensions"]["sgme_profile"] == _profile()
    assert got["data"]["extensions"]["sgme_care"]["greeting_templates"] == ["早。"]
    assert len(got["data"]["extensions"]["sgme_violations"]["entries"]) == 1


def test_card_without_profile_still_valid(roles_dir):
    """无 profile 老卡不受影响（既有行为零回归）。"""
    card = {"spec": "chara_card_v2", "spec_version": "2.0",
            "data": {"name": "旧卡", "description": "老结构"}}
    assert roles_mod.validate_role_card(card) == []
    roles_mod.save_role(roles_dir, "legacy", card)
    assert roles_mod.get_role(roles_dir, "legacy")["data"]["name"] == "旧卡"


# ---------- ③ 演化层文件 ----------

def test_load_evolution_missing_file_empty(roles_dir, evolution_dir):
    evo = roles_mod.load_evolution("butler", evolution_dir)
    assert evo == {"timeline": [], "feelings": [], "personality_changes": [],
                   "values_changes": []}


def test_append_timeline(roles_dir, evolution_dir):
    entry = roles_mod.append_evolution(
        "butler", "timeline", {"event": "第一次一起吃火锅", "source": "chat"},
        evolution_dir, now=NOW)
    assert entry == {"at": NOW, "event": "第一次一起吃火锅", "source": "chat"}
    evo = roles_mod.load_evolution("butler", evolution_dir)
    assert evo["timeline"] == [entry]
    assert evo["feelings"] == []


def test_append_feelings_at_autostamped(roles_dir, evolution_dir):
    entry = roles_mod.append_evolution(
        "butler", "feelings", {"thought": "有点开心", "trigger": "他说了晚安"},
        evolution_dir)
    assert entry["thought"] == "有点开心" and entry["trigger"] == "他说了晚安"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["at"])


def test_append_personality_changes(roles_dir, evolution_dir):
    entry = roles_mod.append_evolution(
        "butler", "personality_changes",
        {"slot": "情绪反应", "from": "遇事先急", "to": "先降温再回应", "reason": "长期被温和对待"},
        evolution_dir, now=NOW)
    assert entry["slot"] == "情绪反应" and entry["to"] == "先降温再回应"


def test_append_values_changes(roles_dir, evolution_dir):
    entry = roles_mod.append_evolution(
        "butler", "values_changes", {"change": "更看重陪伴而非效率", "reason": "一次停电夜谈"},
        evolution_dir, now=NOW)
    assert entry["change"] == "更看重陪伴而非效率"


def test_append_multiple_accumulate(roles_dir, evolution_dir):
    roles_mod.append_evolution("butler", "timeline", {"event": "a"}, evolution_dir, now=NOW)
    roles_mod.append_evolution("butler", "timeline", {"event": "b"}, evolution_dir, now=NOW)
    roles_mod.append_evolution("butler", "feelings", {"thought": "c"}, evolution_dir, now=NOW)
    evo = roles_mod.load_evolution("butler", evolution_dir)
    assert [e["event"] for e in evo["timeline"]] == ["a", "b"]
    assert len(evo["feelings"]) == 1
    assert evo["personality_changes"] == [] and evo["values_changes"] == []


def test_append_unknown_kind_rejected(roles_dir, evolution_dir):
    with pytest.raises(ValueError, match="kind"):
        roles_mod.append_evolution("butler", "mood", {"thought": "x"}, evolution_dir, now=NOW)
    # 拒绝即不落盘
    assert roles_mod.load_evolution("butler", evolution_dir)["timeline"] == []


@pytest.mark.parametrize("kind,fields", [
    ("timeline", {}),                               # 缺 event
    ("feelings", {"thought": "   "}),               # 空白 thought
    ("personality_changes", {"slot": "情绪反应"}),   # 缺 to
    ("values_changes", {}),                         # 缺 change
    ("timeline", {"event": "x", "mood": "多余字段"}),
    ("feelings", {"thought": "x", "event": "串味字段"}),
])
def test_append_field_validation_rejected(roles_dir, evolution_dir, kind, fields):
    with pytest.raises(ValueError):
        roles_mod.append_evolution("butler", kind, fields, evolution_dir, now=NOW)


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "BUTLER", "", ".hidden"])
def test_append_invalid_role_id_rejected(roles_dir, evolution_dir, bad_id):
    with pytest.raises(ValueError, match="id"):
        roles_mod.append_evolution(bad_id, "timeline", {"event": "x"}, evolution_dir, now=NOW)
    # 防路径穿越：目标目录外不得产生文件
    assert not (evolution_dir.parent / "evil.json").exists()


def test_load_evolution_corrupt_file_raises(roles_dir, evolution_dir):
    evolution_dir.mkdir(parents=True, exist_ok=True)
    (evolution_dir / "butler.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="解析失败"):
        roles_mod.load_evolution("butler", evolution_dir)


# ---------- ④ 操作层 ----------

def test_get_evolution_missing_file_empty(roles_dir, card, evolution_dir):
    res = care_op.get_evolution("butler")
    assert res.ok is True
    assert res.data["role_id"] == "butler"
    assert res.data["timeline"] == [] and res.data["feelings"] == []
    assert res.data["personality_changes"] == [] and res.data["values_changes"] == []


def test_post_evolution_appends(roles_dir, card, evolution_dir):
    res = care_op.post_evolution(
        "butler", {"kind": "timeline", "event": "第一次对话", "source": "http"})
    assert res.ok is True
    assert res.data["entry"]["event"] == "第一次对话"
    assert res.data["entry"]["at"]
    evo = roles_mod.load_evolution("butler", evolution_dir)
    assert len(evo["timeline"]) == 1


def test_post_evolution_role_not_found(roles_dir, evolution_dir):
    res = care_op.post_evolution("ghost", {"kind": "timeline", "event": "x"})
    assert res.ok is False and res.error_code == ERR_NOT_FOUND


def test_post_evolution_invalid_kind(roles_dir, card, evolution_dir):
    with pytest.raises(InvalidArgs):
        care_op.post_evolution("butler", {"kind": "nope", "event": "x"})


def test_post_evolution_missing_required_field(roles_dir, card, evolution_dir):
    with pytest.raises(InvalidArgs):
        care_op.post_evolution("butler", {"kind": "timeline"})


def test_invalid_role_id_rejected(roles_dir, card, evolution_dir):
    with pytest.raises(InvalidArgs):
        care_op.get_evolution("../evil")
    with pytest.raises(InvalidArgs):
        care_op.post_evolution("../evil", {"kind": "timeline", "event": "x"})


# ---------- ⑤ HTTP 端点接线 ----------

@pytest.fixture
def conns(tmp_path):
    from sgme.data import db as db_mod

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "dbdata")
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, tmp_path, monkeypatch):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    monkeypatch.setattr(sgme_config, "ROLE_EVOLUTION_DIR", tmp_path / "data" / "role_evolution")
    cfg = sgme_config.load_config()
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


def test_http_evolution_endpoints(app, roles_dir, evolution_dir):
    client = TestClient(app)
    roles_mod.save_role(roles_dir, "butler", _card_with_profile())

    # GET：演化文件不存在 → 200 空结构（契约：不 404）
    r = client.get("/v1/admin/roles/butler/evolution", headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timeline"] == [] and body["feelings"] == []
    assert body["personality_changes"] == [] and body["values_changes"] == []

    # POST：追加 timeline / feelings 各一条
    r = client.post(
        "/v1/admin/roles/butler/evolution",
        json={"kind": "timeline", "event": "首次对话", "source": "http"},
        headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["event"] == "首次对话"
    r = client.post(
        "/v1/admin/roles/butler/evolution",
        json={"kind": "feelings", "thought": "有点开心", "trigger": "被记住了生日"},
        headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text

    # GET：两条都在
    body = client.get("/v1/admin/roles/butler/evolution", headers=AGENT_HEADERS).json()
    assert [e["event"] for e in body["timeline"]] == ["首次对话"]
    assert len(body["feelings"]) == 1

    # 非法 kind → 400；缺必填字段 → 400
    r = client.post("/v1/admin/roles/butler/evolution",
                    json={"kind": "nope", "event": "x"}, headers=AGENT_HEADERS)
    assert r.status_code == 400
    r = client.post("/v1/admin/roles/butler/evolution",
                    json={"kind": "timeline"}, headers=AGENT_HEADERS)
    assert r.status_code == 400

    # 角色不存在：POST → 404；GET → 仍 200 空结构（契约）
    r = client.post("/v1/admin/roles/ghost/evolution",
                    json={"kind": "timeline", "event": "x"}, headers=AGENT_HEADERS)
    assert r.status_code == 404
    r = client.get("/v1/admin/roles/ghost/evolution", headers=AGENT_HEADERS)
    assert r.status_code == 200
    assert r.json()["timeline"] == []

    # id 白名单（防路径穿越）→ 400
    r = client.post("/v1/admin/roles/BUTLER/evolution",
                    json={"kind": "timeline", "event": "x"}, headers=AGENT_HEADERS)
    assert r.status_code == 400


def test_http_profile_save_roundtrip(app, roles_dir):
    """保存角色卡时 sgme_profile 原样读写不丢（HTTP 路径）。"""
    client = TestClient(app)
    r = client.post(
        "/v1/admin/roles/butler",
        json={"data": {"name": "管家", "description": "测试",
                       "extensions": {"sgme_profile": _profile()}}},
        headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    got = client.get("/v1/admin/roles/butler", headers=AGENT_HEADERS).json()
    assert got["role"]["data"]["extensions"]["sgme_profile"] == _profile()


def test_http_evolution_auth_required(app):
    client = TestClient(app)
    r = client.get("/v1/admin/roles/butler/evolution")
    assert r.status_code in (401, 403)


# ---------- ⑥ memory.db 零写入（隔离铁律） ----------

def test_evolution_memory_db_untouched(tmp_path, roles_dir, card, evolution_dir):
    from sgme.data import db as db_mod

    mem_conn, _, _ = db_mod.init_databases(tmp_path / "dbdata2")

    def db_bytes() -> dict:
        return {f.name: f.read_bytes() for f in sorted((tmp_path / "dbdata2").glob("*.db"))}

    before = db_bytes()
    roles_mod.append_evolution("butler", "timeline", {"event": "x"}, evolution_dir, now=NOW)
    res = care_op.post_evolution("butler", {"kind": "feelings", "thought": "y"})
    after = db_bytes()

    assert res.ok is True
    assert before == after
    db_mod.close(mem_conn)
