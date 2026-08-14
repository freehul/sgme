"""tests/test_care.py：Care Engine 角色层测试（ST-25 / T-35）。

覆盖：
1. 角色卡校验（CC V2 兼容子集）：合法卡通过、缺必填/多余键/非法扩展拒绝
2. 文件 CRUD：save/list/get/archive（原件永不删——归档不移出 .archive 即视为保留）
3. persona 物化：渲染提示词、落盘/读取/备份轮转（保留 3 份）
4. HTTP 端点：列表/详情/upsert/归档/persona 读取（TestClient 全链路）
5. persona 生成：mock LLM 链路 → 物化成功；LLM 不可用 → ERR_INTERNAL
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.care import roles as roles_mod
from sgme.operations.errors import ERR_CONFLICT, ERR_INTERNAL, ERR_NOT_FOUND, OperationResult
from sgme.server.app import create_app
from sgme.data import db as db_mod
from sgme.data import memory_dao

AGENT_HEADERS = {"X-API-Key": "test-agent-key"}


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def roles_dir(tmp_path, monkeypatch):
    """隔离 roles/ 目录。"""
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    return rd


@pytest.fixture
def persona_dir(tmp_path, monkeypatch):
    """隔离 persona 目录。"""
    pd = tmp_path / "personas"
    pd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", pd)
    return pd


@pytest.fixture
def conns(tmp_path):
    """隔离三库连接。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def app(conns, cfg, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（roles/persona 目录随 tmp_path 隔离）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    rd = tmp_path / "roles"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "ROLES_DIR", rd)
    pd = tmp_path / "personas"
    pd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "PERSONA_DIR", pd)
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


@pytest.fixture
def client(app):
    return TestClient(app)


def _valid_card(name: str = "管家") -> dict:
    """构造一张合法角色卡。"""
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": f"{name}的测试角色",
            "personality": "严谨体贴",
            "extensions": {"sgme_care": {"frequency": {"max_daily": 5}}},
        },
    }


# ---------- 1. 角色卡校验 ----------

def test_validate_valid_card_passes():
    assert roles_mod.validate_role_card(_valid_card()) == []


def test_validate_missing_required_rejected():
    errors = roles_mod.validate_role_card({"data": {"personality": "x"}})
    assert any("name" in e for e in errors)
    assert any("description" in e for e in errors)


def test_validate_unknown_keys_rejected():
    errors = roles_mod.validate_role_card(
        {"data": {"name": "n", "description": "d", "avatar_url": "http://x"}}
    )
    assert any("avatar_url" in e for e in errors)


def test_validate_care_extension_whitelist():
    bad = _valid_card()
    bad["data"]["extensions"]["sgme_care"]["evil_key"] = 1
    errors = roles_mod.validate_role_card(bad)
    assert any("evil_key" in e for e in errors)


def test_validate_top_level_whitelist():
    bad = _valid_card()
    bad["extra"] = 1
    errors = roles_mod.validate_role_card(bad)
    assert any("extra" in e for e in errors)


# ---------- 2. 文件 CRUD ----------

def test_save_and_get_role(roles_dir):
    fp = roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))
    assert fp.exists()
    card = roles_mod.get_role(roles_dir, "butler")
    assert card["data"]["name"] == "管家"
    assert card["role_id"] == "butler"


def test_list_roles_light_fields(roles_dir):
    roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))
    roles_mod.save_role(roles_dir, "companion", _valid_card("伴侣"))
    items = roles_mod.list_roles(roles_dir)
    assert len(items) == 2
    names = {i["role_id"] for i in items}
    assert names == {"butler", "companion"}
    # 轻量字段：不含 personality 等正文
    assert "personality" not in items[0]


def test_upsert_refreshes_updated_at(roles_dir):
    roles_mod.save_role(roles_dir, "butler", _valid_card())
    first = roles_mod.get_role(roles_dir, "butler")["data"]["updated_at"]
    roles_mod.save_role(roles_dir, "butler", _valid_card())
    second = roles_mod.get_role(roles_dir, "butler")["data"]["updated_at"]
    assert second >= first


def test_archive_moves_to_hidden_dir(roles_dir):
    roles_mod.save_role(roles_dir, "butler", _valid_card())
    assert roles_mod.archive_role(roles_dir, "butler") is True
    # 原件保留（.archive/ 内），列表不再出现
    assert not roles_mod.get_role(roles_dir, "butler")
    assert (roles_dir / ".archive" / "butler.json").exists()
    assert roles_mod.archive_role(roles_dir, "missing") is False


def test_invalid_role_id_rejected(roles_dir):
    with pytest.raises(ValueError):
        roles_mod.save_role(roles_dir, "../evil", _valid_card())


# ---------- 3. persona 物化 ----------

def test_render_persona_prompt_has_four_layers():
    prompt = roles_mod.render_persona_prompt("管家", "用户素材", 2000)
    assert "L1 基础锚点" in prompt
    assert "L2 兴趣图谱" in prompt
    assert "L3 交互协议" in prompt
    assert "L4 认知内核" in prompt
    assert "管家" in prompt


def test_persona_save_load_and_backup_rotation(persona_dir):
    fp1 = roles_mod.save_persona("butler", "# persona v1", persona_dir)
    assert fp1.exists()
    roles_mod.save_persona("butler", "# persona v2", persona_dir)
    roles_mod.save_persona("butler", "# persona v3", persona_dir)
    roles_mod.save_persona("butler", "# persona v4", persona_dir)
    # 备份轮转保留 3 份
    assert (persona_dir / "butler.md.bak1").exists()
    assert (persona_dir / "butler.md.bak2").exists()
    assert (persona_dir / "butler.md.bak3").exists()
    assert not (persona_dir / "butler.md.bak4").exists()
    assert roles_mod.load_persona("butler", persona_dir) == "# persona v4\n"


def test_load_persona_missing_returns_none(persona_dir):
    assert roles_mod.load_persona("ghost", persona_dir) is None


# ---------- 4. HTTP 端点 ----------

def test_http_roles_crud_flow(client):
    # upsert
    r = client.post("/v1/admin/roles/butler", json={"data": {"name": "管家", "description": "测试"}},
                    headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    # list（run_operation 返回裸 data）
    r = client.get("/v1/admin/roles", headers=AGENT_HEADERS)
    assert r.status_code == 200
    assert r.json()["total"] == 1
    # get
    r = client.get("/v1/admin/roles/butler", headers=AGENT_HEADERS)
    assert r.json()["role"]["data"]["name"] == "管家"
    # persona 未生成 → 404
    r = client.get("/v1/admin/roles/butler/persona", headers=AGENT_HEADERS)
    assert r.status_code == 404
    # archive
    r = client.delete("/v1/admin/roles/butler", headers=AGENT_HEADERS)
    assert r.status_code == 200
    r = client.get("/v1/admin/roles/butler", headers=AGENT_HEADERS)
    assert r.status_code == 404


def test_http_upsert_invalid_card_rejected(client):
    r = client.post("/v1/admin/roles/bad", json={"data": {"personality": "缺必填"}},
                    headers=AGENT_HEADERS)
    assert r.status_code == 500  # run_operation 内 ValueError → ERR_INTERNAL
    assert "校验失败" in r.json()["error"]["message"]


# ---------- 5. persona 生成（mock LLM 链） ----------

def test_generate_persona_success(conns, cfg, persona_dir, roles_dir, monkeypatch):
    """mock LLM 链 → persona 物化成功。"""
    from sgme.llm import chain as llm_chain

    roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))
    monkeypatch.setattr(
        llm_chain, "call_with_fallback",
        lambda *a, **k: ("# 管家视角 persona\n用户是独立开发者。", "mock", {}),
    )
    mem_conn, _, _ = conns
    from sgme.operations.care import generate_persona

    res = generate_persona("butler", mem_conn, cfg)
    assert res.ok is True
    assert (persona_dir / "butler.md").exists()
    assert "管家视角" in roles_mod.load_persona("butler", persona_dir)


def test_generate_persona_profile_from_memory(conns, cfg, persona_dir, roles_dir, monkeypatch):
    """画像素材来自记忆池静态维度。"""
    from sgme.llm import chain as llm_chain

    roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))
    mem_conn, _, _ = conns
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    memory_dao.insert_memory(
        mem_conn, content="用户是独立开发者，深耕 SGME", memory_type="persona",
        priority=90, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"],
    )
    captured = {}

    def _fake_chain(llm_cfg, prompt, **kw):
        captured["prompt"] = prompt
        return ("persona", "mock", {})

    monkeypatch.setattr(llm_chain, "call_with_fallback", _fake_chain)
    from sgme.operations.care import generate_persona

    res = generate_persona("butler", mem_conn, cfg)
    assert res.ok is True
    assert "独立开发者" in captured["prompt"]  # 素材进了提示词


def test_generate_persona_llm_unavailable(conns, cfg, persona_dir, roles_dir, monkeypatch):
    """LLM 全链不可用 → ERR_INTERNAL（不降级直存）。"""
    from sgme.llm import chain as llm_chain
    from sgme.llm.provider import LLMUnavailable

    roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))

    def _boom(*a, **k):
        raise LLMUnavailable("mock down")

    monkeypatch.setattr(llm_chain, "call_with_fallback", _boom)
    mem_conn, _, _ = conns
    from sgme.operations.care import generate_persona

    res = generate_persona("butler", mem_conn, cfg)
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert not (persona_dir / "butler.md").exists()


def test_generate_persona_role_missing(conns, cfg, roles_dir):
    """角色不存在 → ERR_NOT_FOUND。"""
    from sgme.operations.care import generate_persona

    mem_conn, _, _ = conns
    res = generate_persona("ghost", mem_conn, cfg)
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


# ---------- 6. 关怀信号（T-36） ----------

def _seed_memories(mem_conn: sqlite3.Connection, cfg: dict, *, stale_tasks: int = 0,
                   mood: int = 0, focus_today: int = 0) -> None:
    """灌入推导用测试记忆（可指定各类型数量）。"""
    import datetime as _dt

    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(stale_tasks):
        memory_dao.insert_memory(
            mem_conn, content=f"待办事项 {i}（无进展）", memory_type="task",
            priority=80, time_velocity="dynamic", ttl_days=30,
            dimension_ids=["tasks"], updated_at=old, occurred_at=old,
        )
    for i in range(mood):
        memory_dao.insert_memory(
            mem_conn, content=f"今天感觉很疲惫 {i}", memory_type="status",
            priority=80, time_velocity="dynamic", ttl_days=7,
            dimension_ids=["status"],
        )
    for i in range(focus_today):
        memory_dao.insert_memory(
            mem_conn, content=f"专注工作 {i}", memory_type="focus",
            priority=75, time_velocity="dynamic", ttl_days=30,
            dimension_ids=["focus"], updated_at=today, occurred_at=today,
        )


def test_scan_generates_all_signal_types(conns, cfg):
    """四类信号全部可推导（待办/情绪/过劳/每日）。"""
    from sgme.care import signals as signals_mod

    mem_conn, _, _ = conns
    _seed_memories(mem_conn, cfg, stale_tasks=2, mood=1, focus_today=6)

    stats = signals_mod.scan_care_signals(mem_conn, cfg)

    assert stats["care_todo_due"] == 2
    assert stats["care_mood"] == 1
    assert stats["care_overwork"] == 1
    assert stats["care_daily"] == 1


def test_scan_idempotent_no_duplicates(conns, cfg):
    """幂等：重复扫描不产生重复事件（uuid5 确定性去重）。"""
    from sgme.care import signals as signals_mod

    mem_conn, _, _ = conns
    _seed_memories(mem_conn, cfg, stale_tasks=1)

    first = signals_mod.scan_care_signals(mem_conn, cfg)
    second = signals_mod.scan_care_signals(mem_conn, cfg)

    assert first["care_todo_due"] == 1
    assert second["care_todo_due"] == 0  # 已存在，去重跳过
    total = mem_conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE type LIKE 'care_%'"
    ).fetchone()[0]
    assert total == 2  # 1 todo_due + 1 daily


def test_scan_mood_keyword_config(conns, cfg):
    """情绪关键词可配置（care.mood_keywords 覆盖默认）。"""
    from sgme.care import signals as signals_mod

    mem_conn, _, _ = conns
    _seed_memories(mem_conn, cfg, mood=1)  # 内容含"疲惫"（默认词命中）
    cfg2 = dict(cfg)
    cfg2["care"] = {"mood_keywords": ["不存在之词"]}
    stats = signals_mod.scan_care_signals(mem_conn, cfg2)
    assert stats["care_mood"] == 0


def test_scan_mood_technical_context_excluded(conns, cfg):
    """假阳性防护：技术语境（bug/测试/竞态）中的情绪词不算情绪信号。"""
    from sgme.care import signals as signals_mod

    mem_conn, _, _ = conns
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # 技术记忆：含"崩溃"但属 bug 排查语境 → 应排除
    memory_dao.insert_memory(
        mem_conn, content="test_dream 崩溃确认为偶发竞态，已修复", memory_type="status",
        priority=80, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"],
    )
    stats = signals_mod.scan_care_signals(mem_conn, cfg)
    assert stats["care_mood"] == 0


def test_scan_mood_weak_tech_words_do_not_kill_real_mood(conns, cfg):
    """边界：弱技术词（测试/修复）不误杀真情绪——「测试：今天心情低落」应命中。"""
    from sgme.care import signals as signals_mod

    mem_conn, _, _ = conns
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    memory_dao.insert_memory(
        mem_conn, content="测试：今天心情很低落，想休息", memory_type="status",
        priority=80, time_velocity="dynamic", ttl_days=7,
        dimension_ids=["status"],
    )
    stats = signals_mod.scan_care_signals(mem_conn, cfg)
    assert stats["care_mood"] == 1


def test_scan_mood_emotional_hits_even_with_some_tech_words(conns, cfg):
    """真情绪仍命中：技术词没出现时照常触发。"""
    from sgme.care import signals as signals_mod

    mem_conn, _, _ = conns
    _seed_memories(mem_conn, cfg, mood=1)
    stats = signals_mod.scan_care_signals(mem_conn, cfg)
    assert stats["care_mood"] == 1


def test_list_and_consume_signals(conns, cfg):
    """列表过滤 + 消费标记。"""
    from sgme.care import signals as signals_mod
    from sgme.operations.care import consume_signal, list_signals

    mem_conn, _, _ = conns
    _seed_memories(mem_conn, cfg, stale_tasks=1)
    signals_mod.scan_care_signals(mem_conn, cfg)

    # 未消费过滤
    res = list_signals(mem_conn, unconsumed_only=True)
    assert res.ok is True
    assert res.data["total"] == 2  # todo_due + daily
    # 类型过滤
    res = list_signals(mem_conn, signal_type="care_daily")
    assert res.data["total"] == 1
    eid = res.data["signals"][0]["event_id"]
    # 消费（原子认领：谁消费谁标记）
    res = consume_signal(mem_conn, eid)
    assert res.ok is True
    res = consume_signal(mem_conn, eid)  # 已被消费 → 原子抢失败 → 409
    assert res.ok is False
    assert res.error_code == ERR_CONFLICT
    # 不存在 → 同样认领失败（原子 UPDATE rowcount=0，统一 409）
    res = consume_signal(mem_conn, "no-such-event")
    assert res.ok is False
    assert res.error_code == ERR_CONFLICT
    # 消费后不再出现在未消费列表
    res = list_signals(mem_conn, unconsumed_only=True)
    assert all(s["event_id"] != eid for s in res.data["signals"])


def test_http_care_signals_flow(client, conns, cfg):
    """HTTP 全链路：scan → list → consume。"""
    mem_conn, _, _ = conns
    _seed_memories(mem_conn, cfg, stale_tasks=1)

    r = client.post("/v1/admin/care/scan", headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["scan"]["care_todo_due"] == 1

    r = client.get("/v1/admin/care/signals?unconsumed_only=true", headers=AGENT_HEADERS)
    assert r.status_code == 200
    assert r.json()["total"] == 2

    eid = r.json()["signals"][0]["event_id"]
    r = client.post(f"/v1/admin/care/signals/{eid}/consume", headers=AGENT_HEADERS)
    assert r.status_code == 200

    r = client.get("/v1/admin/care/signals?unconsumed_only=true", headers=AGENT_HEADERS)
    assert all(s["event_id"] != eid for s in r.json()["signals"])


# ---------- 7. 角色注入装配（T-37） ----------

def _butler_card_with_sysprompt() -> dict:
    """带 system_prompt（含 {{original}}）与关怀策略的管家卡。"""
    card = _valid_card("管家")
    card["data"]["system_prompt"] = "你是{{char}}，管家。{{original}}"
    card["data"]["extensions"] = {
        "sgme_care": {"greeting_templates": ["早安，主人"], "frequency": {"max_daily": 3}}
    }
    return card


def test_assemble_without_persona(conns, cfg, roles_dir):
    """装配：无 persona → system_prompt（{{original}} 已替换）+ care_policy。"""
    from sgme.operations.care import assemble

    roles_mod.save_role(roles_dir, "butler", _butler_card_with_sysprompt())
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg)

    assert res.ok is True
    d = res.data
    assert d["role_name"] == "管家"
    assert "{{original}}" not in d["system_prompt"]  # 已替换
    assert "专属沟通角色" in d["system_prompt"]
    assert d["persona"] is None  # 未生成
    assert d["care_policy"]["frequency"]["max_daily"] == 3
    assert d["profile_blocks"] == []  # 未请求画像


def test_assemble_with_persona(conns, cfg, roles_dir, persona_dir):
    """装配：persona 已物化 → 包含全文。"""
    from sgme.operations.care import assemble

    roles_mod.save_role(roles_dir, "butler", _butler_card_with_sysprompt())
    roles_mod.save_persona("butler", "# 管家 persona 内容", persona_dir)
    mem_conn, _, _ = conns

    res = assemble("butler", mem_conn, cfg)

    assert res.ok is True
    assert "管家 persona" in res.data["persona"]


def test_assemble_with_inject_mode(conns, cfg, roles_dir):
    """装配 + inject_mode=daily → profile_blocks 附带用户画像（零物化）。"""
    from sgme.operations.care import assemble

    roles_mod.save_role(roles_dir, "butler", _butler_card_with_sysprompt())
    mem_conn, _, _ = conns
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    # daily 模板第一 section 查 [identity, family]，match=all → 需同时命中
    memory_dao.insert_memory(
        mem_conn, content="用户是独立开发者", memory_type="persona",
        priority=90, time_velocity="static", ttl_days=None,
        dimension_ids=["identity", "family"],
    )

    res = assemble("butler", mem_conn, cfg, inject_mode="daily")

    assert res.ok is True
    assert len(res.data["profile_blocks"]) >= 1
    # 画像块内容包含刚插入的记忆
    joined = str(res.data["profile_blocks"])
    assert "独立开发者" in joined


def test_assemble_role_missing(conns, cfg, roles_dir):
    """角色不存在 → ERR_NOT_FOUND。"""
    from sgme.operations.care import assemble

    mem_conn, _, _ = conns
    res = assemble("ghost", mem_conn, cfg)
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_http_assemble_endpoint(client, roles_dir, persona_dir):
    """HTTP 装配端点（无画像）：system_prompt + care_policy。"""
    roles_mod.save_role(roles_dir, "butler", _butler_card_with_sysprompt())
    r = client.get("/v1/admin/roles/butler/assemble", headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role_name"] == "管家"
    assert body["care_policy"]["greeting_templates"] == ["早安，主人"]
    assert "{{original}}" not in body["system_prompt"]


# ---------- 8. 当前角色（T-40） ----------

def test_active_role_default_none(conns, cfg, tmp_path, monkeypatch):
    """未设置 → role_id=None。"""
    from sgme.operations.care import get_active_role

    monkeypatch.setattr(sgme_config, "DATA_DIR", tmp_path)
    res = get_active_role()
    assert res.ok is True
    assert res.data["role_id"] is None


def test_set_and_get_active_role(conns, cfg, roles_dir, tmp_path, monkeypatch):
    """设置后读取一致（换皮不换芯）。"""
    from sgme.operations.care import get_active_role, set_active_role

    monkeypatch.setattr(sgme_config, "DATA_DIR", tmp_path)
    roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))

    res = set_active_role("butler")
    assert res.ok is True
    assert res.data["status"] == "active"

    res = get_active_role()
    assert res.data["role_id"] == "butler"
    # 状态文件落盘
    assert (tmp_path / "care" / "active_role.json").exists()


def test_set_active_role_missing(conns, cfg, roles_dir, tmp_path, monkeypatch):
    """角色不存在 → ERR_NOT_FOUND。"""
    from sgme.operations.care import set_active_role

    monkeypatch.setattr(sgme_config, "DATA_DIR", tmp_path)
    res = set_active_role("ghost")
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_http_active_role_flow(client, roles_dir, tmp_path, monkeypatch):
    """HTTP 全链路：GET（None）→ PUT → GET（butler）。"""
    monkeypatch.setattr(sgme_config, "DATA_DIR", tmp_path)
    roles_mod.save_role(roles_dir, "butler", _valid_card("管家"))

    r = client.get("/v1/admin/care/active-role", headers=AGENT_HEADERS)
    assert r.status_code == 200
    assert r.json()["role_id"] is None

    r = client.put("/v1/admin/care/active-role", json={"role_id": "butler"},
                   headers=AGENT_HEADERS)
    assert r.status_code == 200, r.text

    r = client.get("/v1/admin/care/active-role", headers=AGENT_HEADERS)
    assert r.json()["role_id"] == "butler"

    r = client.put("/v1/admin/care/active-role", json={"role_id": "ghost"},
                   headers=AGENT_HEADERS)
    assert r.status_code == 404
