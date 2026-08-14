"""ST-18 测试：项目替代联动（L1 supersedes 声明 → 旧主体记忆自动标记）。

背景（2026-08-11 AIXM 案例）：提炼出「X 已被 Y 替代」时，旧主体 X 的相关记忆
应自动标记（status='rejected'，数据保留可溯源），防过时记忆长期存活。

覆盖：
- L1 解析透传 supersedes（str / list / 缺省 / 非法类型）
- refine_file 归一化透传 supersedes（L0 会话 → L1 mock 输出）
- apply_supersession_linkage：声明替代 → 旧主体记忆 reject（原因含替代者 + 溯源链）
- 无声明 / 主体未命中 / 已 rejected / 本批新记忆 → 行为不变
- pipeline.persist_memories 接线：stats.supersession_rejected

mock LLM 用 httpx.MockTransport 注入固定 JSON 输出。
"""

from __future__ import annotations

import json

import httpx
import pytest

from sgme import config
from sgme.engine import l1, l15 as l15_mod, pipeline as pipeline_mod, refine as refine_mod
from sgme.raw import store
from sgme.data import db as db_mod, memory_dao, session_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


def _mock_llm_client(response_body: str) -> httpx.Client:
    """构造 mock httpx 客户端，返回固定 LLM 输出。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _insert_existing(mem_conn, content, dim_ids, **kw):
    """插入一条已存在的旧记忆，返回 memory_id。"""
    return memory_dao.insert_memory(
        mem_conn, content=content,
        memory_type=kw.get("memory_type", "persona"),
        priority=kw.get("priority", 60),
        time_velocity=kw.get("time_velocity", "static"),
        ttl_days=kw.get("ttl_days"),
        dimension_ids=dim_ids,
        created_at=kw.get("created_at", "2026-01-01T00:00:00Z"),
        updated_at=kw.get("updated_at", "2026-01-01T00:00:00Z"),
    )


# ---------- L1 解析透传 supersedes ----------

def test_l1_parse_passes_supersedes_string(cfg):
    """supersedes 为字符串 → 规整为单元素列表。"""
    text = json.dumps([{
        "content": "AIXM 已被 SGME 替代，项目移入 OLD/",
        "dimensions": ["项目"], "memory_type": "episodic",
        "priority": 80, "time_velocity": "dynamic",
        "source_message_ids": [3], "supersedes": "AIXM",
    }])
    memories = l1.parse_l1_output(text, cfg["dimensions"])
    assert memories[0]["supersedes"] == ["AIXM"]


def test_l1_parse_passes_supersedes_list(cfg):
    """supersedes 为数组 → 原样保留（多个旧主体）。"""
    text = json.dumps([{
        "content": "新引擎已替代旧引擎和旧方案",
        "dimensions": ["项目"], "memory_type": "episodic",
        "priority": 80, "time_velocity": "dynamic",
        "source_message_ids": [1], "supersedes": ["AIXM", "旧方案"],
    }])
    memories = l1.parse_l1_output(text, cfg["dimensions"])
    assert memories[0]["supersedes"] == ["AIXM", "旧方案"]


def test_l1_parse_supersedes_absent_defaults_empty(cfg):
    """无 supersedes 声明 → 空列表（既有行为不变）。"""
    text = json.dumps([{
        "content": "用户是独立开发者",
        "dimensions": ["身份"], "memory_type": "persona",
        "priority": 85, "time_velocity": "static",
        "source_message_ids": [1],
    }])
    memories = l1.parse_l1_output(text, cfg["dimensions"])
    assert memories[0]["supersedes"] == []


def test_l1_parse_supersedes_invalid_types(cfg):
    """supersedes 为非法类型/空白串 → 空列表。"""
    text = json.dumps([
        {"content": "记忆A", "dimensions": ["项目"], "memory_type": "persona",
         "priority": 60, "time_velocity": "static", "source_message_ids": [1],
         "supersedes": 123},
        {"content": "记忆B", "dimensions": ["项目"], "memory_type": "persona",
         "priority": 60, "time_velocity": "static", "source_message_ids": [1],
         "supersedes": "   "},
        {"content": "记忆C", "dimensions": ["项目"], "memory_type": "persona",
         "priority": 60, "time_velocity": "static", "source_message_ids": [1],
         "supersedes": ["AIXM", "", 7]},
    ])
    memories = l1.parse_l1_output(text, cfg["dimensions"])
    assert memories[0]["supersedes"] == []
    assert memories[1]["supersedes"] == []
    assert memories[2]["supersedes"] == ["AIXM"]


# ---------- refine_file 归一化透传 ----------

def test_refine_file_preserves_supersedes(raw_dir, mem_conn, session_conn, cfg):
    """refine_file 归一化重建不丢 supersedes（L0 会话 → L1 mock 输出）。"""
    fid = "f-supersession-refine"
    msgs = [
        {"timestamp": "2026-08-11T10:00:00Z", "role": "user",
         "content": "AIXM 项目不用了，已被 SGME 替代，旧项目移入 OLD/ 目录"},
        {"timestamp": "2026-08-11T10:01:00Z", "role": "assistant",
         "content": "了解，已更新项目状态"},
    ]
    store.write_new_file(
        file_id=fid, session_key="sess_supersede", started_at="2026-08-11T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=store.relative_path(fid),
        session_key="sess_supersede", started_at="2026-08-11T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(fid),
    )

    l1_body = json.dumps([{
        "content": "AIXM 已被 SGME 替代，AIXM 项目移入 OLD/ 目录",
        "dimensions": ["项目"], "memory_type": "episodic",
        "priority": 80, "time_velocity": "dynamic",
        "source_message_ids": [1], "supersedes": "AIXM",
    }])
    cli = _mock_llm_client(l1_body)

    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "refined"
    assert len(result.memories) == 1
    assert result.memories[0]["supersedes"] == ["AIXM"]


# ---------- apply_supersession_linkage ----------

def test_linkage_rejects_old_subject_memories(mem_conn):
    """声明替代 → 旧主体 X 相关记忆标记 rejected，原因含替代者与溯源链。"""
    old_id = _insert_existing(mem_conn, "AIXM 项目使用 Vue 3 开发", ["projects"])
    new_mem = {
        "memory_id": "new-1",
        "content": "AIXM 已被 SGME 替代，AIXM 项目移入 OLD/ 目录",
        "dimension_ids": ["projects"],
        "supersedes": ["AIXM"],
    }
    # 模拟 L1.5 落库后状态：新记忆已在库（memory_id 写回 dict）
    new_id = memory_dao.insert_memory(
        mem_conn, content=new_mem["content"], memory_type="episodic",
        priority=80, time_velocity="dynamic", ttl_days=None,
        dimension_ids=["projects"], created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    new_mem["memory_id"] = new_id

    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_mem])
    assert rejected == [old_id]
    old = memory_dao.get_memory(mem_conn, old_id)
    assert old["status"] == "rejected"
    # 原因含替代者 Y（新记忆内容）与溯源链（新记忆 memory_id）
    assert "SGME" in old["reject_reason"]
    assert new_id in old["reject_reason"]
    # 新记忆自身不受影响
    new_row = memory_dao.get_memory(mem_conn, new_id)
    assert new_row["status"] == "active"


def test_linkage_no_declaration_noop(mem_conn):
    """无 supersedes 声明 → 行为不变，旧记忆仍 active。"""
    old_id = _insert_existing(mem_conn, "AIXM 项目使用 Vue 3 开发", ["projects"])
    new_mem = {
        "memory_id": "new-1",
        "content": "今天修复了 AIXM 的一个 bug",
        "dimension_ids": ["projects"],
        # 无 supersedes
    }
    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_mem])
    assert rejected == []
    assert memory_dao.get_memory(mem_conn, old_id)["status"] == "active"


def test_linkage_subject_not_mentioned_noop(mem_conn):
    """声明的主体在旧记忆中无内容提及 → 不标记。"""
    old_id = _insert_existing(mem_conn, "SGME 记忆引擎使用 Python", ["projects"])
    new_mem = {
        "memory_id": "new-1",
        "content": "AIXM 已被 SGME 替代",
        "dimension_ids": ["projects"],
        "supersedes": ["AIXM"],
    }
    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_mem])
    assert rejected == []
    assert memory_dao.get_memory(mem_conn, old_id)["status"] == "active"


def test_linkage_excludes_batch_new_memories(mem_conn):
    """本批新记忆自身（内容必提及旧主体名）不被标记。"""
    new_a = {
        "memory_id": "new-a",
        "content": "AIXM 已被 SGME 替代，AIXM 项目移入 OLD/ 目录",
        "dimension_ids": ["projects"],
        "supersedes": ["AIXM"],
    }
    # 同批另一条新记忆也提及旧主体名（如历史记录），同样不标记
    new_b = {
        "memory_id": "new-b",
        "content": "AIXM 迁移期间整理了旧文档",
        "dimension_ids": ["projects"],
    }
    # 模拟 L1.5 落库后状态：两条新记忆都已在库（memory_id 写回 dict）
    for m in (new_a, new_b):
        m["memory_id"] = memory_dao.insert_memory(
            mem_conn, content=m["content"], memory_type="episodic",
            priority=80, time_velocity="dynamic", ttl_days=None,
            dimension_ids=["projects"], created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_a, new_b])
    assert rejected == []
    assert memory_dao.get_memory(mem_conn, new_a["memory_id"])["status"] == "active"
    assert memory_dao.get_memory(mem_conn, new_b["memory_id"])["status"] == "active"


def test_linkage_skips_already_rejected_and_expired(mem_conn):
    """已 rejected / 非 active 的记忆不再处理。"""
    old_id = _insert_existing(mem_conn, "AIXM 项目使用 Vue 3 开发", ["projects"])
    memory_dao.reject_memory(mem_conn, old_id, "用户纠错")
    new_mem = {
        "memory_id": "new-1",
        "content": "AIXM 已被 SGME 替代",
        "dimension_ids": ["projects"],
        "supersedes": ["AIXM"],
    }
    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_mem])
    assert rejected == []
    # 原因未被覆盖（保持用户纠错原因）
    assert memory_dao.get_memory(mem_conn, old_id)["reject_reason"] == "用户纠错"


def test_linkage_multiple_subjects_and_memories(mem_conn):
    """多个旧主体 + 多条旧记忆：全部命中并去重标记。"""
    old_a = _insert_existing(mem_conn, "AIXM 项目使用 Vue 3 开发", ["projects"])
    old_b = _insert_existing(mem_conn, "旧方案基于 Python 2", ["projects"])
    old_c = _insert_existing(mem_conn, "AIXM 的部署在 NAS 上", ["projects"])
    new_mem = {
        "memory_id": "new-1",
        "content": "新引擎已替代 AIXM 与旧方案",
        "dimension_ids": ["projects"],
        "supersedes": ["AIXM", "旧方案"],
    }
    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_mem])
    assert sorted(rejected) == sorted([old_a, old_b, old_c])
    for mid in (old_a, old_b, old_c):
        assert memory_dao.get_memory(mem_conn, mid)["status"] == "rejected"


def test_linkage_case_insensitive_and_short_subject_ignored(mem_conn):
    """主体匹配大小写不敏感；过短主体（<2 字符）防御性忽略。"""
    old_a = _insert_existing(mem_conn, "AIXM 的部署在 NAS 上", ["projects"])
    new_mem = {
        "memory_id": "new-1",
        "content": "AIXM 已被 SGME 替代",
        "dimension_ids": ["projects"],
        "supersedes": ["aixm"],  # 小写主体 → 命中大写内容
    }
    rejected = l15_mod.apply_supersession_linkage(mem_conn, [new_mem])
    assert rejected == [old_a]
    # 单字符主体忽略
    new_mem2 = {
        "memory_id": "new-2",
        "content": "X 方案被 Y 替代",
        "dimension_ids": ["projects"],
        "supersedes": ["X"],
    }
    assert l15_mod.apply_supersession_linkage(mem_conn, [new_mem2]) == []


# ---------- pipeline 接线 ----------

def _patch_l15_client(monkeypatch, cli):
    """patch l15_mod.resolve_conflicts 注入 mock client（同 test_e2e_v04 模式）。"""
    original_resolve = l15_mod.resolve_conflicts

    def patched_resolve(new_memories, mem_conn, cfg, client=None, **kwargs):
        return original_resolve(new_memories, mem_conn, cfg, client=cli, **kwargs)

    monkeypatch.setattr(l15_mod, "resolve_conflicts", patched_resolve)


def test_persist_memories_supersession_linkage(monkeypatch, mem_conn, cfg):
    """完整接线：L1.5 落库（mock store）→ 替代联动标记旧记忆 → stats 上报。"""
    old_id = _insert_existing(mem_conn, "AIXM 项目使用 Vue 3 开发", ["projects"])
    new_mem = {
        "content": "AIXM 已被 SGME 替代，AIXM 项目移入 OLD/ 目录",
        "memory_type": "episodic", "priority": 80, "time_velocity": "dynamic",
        "dimension_ids": ["projects"],
        "source_message_ids": [1], "file_id": "f1", "occurred_at": None,
        "supersedes": ["AIXM"],
    }
    result = refine_mod.RefineResult(file_id="f1", status="refined", memories=[new_mem])
    cli = _mock_llm_client(json.dumps(
        [{"new_memory_index": 0, "candidate_ids": [], "action": "store"}],
    ))
    _patch_l15_client(monkeypatch, cli)
    finalized = []
    monkeypatch.setattr(
        refine_mod, "finalize_refinement",
        lambda r, c, cfg, client=None: finalized.append(True),
    )

    stats = pipeline_mod.persist_memories(result, mem_conn, cfg)

    assert stats["stored"] == 1
    assert stats["supersession_rejected"] == 1
    assert finalized, "finalize_refinement 应被调用"
    # 旧记忆已标记，原因含替代者 SGME 与溯源链
    old = memory_dao.get_memory(mem_conn, old_id)
    assert old["status"] == "rejected"
    assert "SGME" in old["reject_reason"]
    assert new_mem["memory_id"] in old["reject_reason"]
    # 新记忆正常 active（替代者记忆不被误伤）
    assert memory_dao.get_memory(mem_conn, new_mem["memory_id"])["status"] == "active"


def test_persist_memories_no_supersedes_unchanged(monkeypatch, mem_conn, cfg):
    """无替代声明 → 旧记忆保持 active，stats 计数为 0（行为不变）。"""
    old_id = _insert_existing(mem_conn, "AIXM 项目使用 Vue 3 开发", ["projects"])
    new_mem = {
        "content": "今天修复了 AIXM 的一个 bug",
        "memory_type": "episodic", "priority": 60, "time_velocity": "dynamic",
        "dimension_ids": ["projects"],
        "source_message_ids": [1], "file_id": "f2", "occurred_at": None,
    }
    result = refine_mod.RefineResult(file_id="f2", status="refined", memories=[new_mem])
    cli = _mock_llm_client(json.dumps(
        [{"new_memory_index": 0, "candidate_ids": [], "action": "store"}],
    ))
    _patch_l15_client(monkeypatch, cli)
    monkeypatch.setattr(refine_mod, "finalize_refinement", lambda r, c, cfg, client=None: None)

    stats = pipeline_mod.persist_memories(result, mem_conn, cfg)

    assert stats["supersession_rejected"] == 0
    assert memory_dao.get_memory(mem_conn, old_id)["status"] == "active"
    assert memory_dao.get_memory(mem_conn, new_mem["memory_id"])["status"] == "active"
