"""T10 测试：L2 场景聚合（render / parse / aggregate 三动作 / 阈值预警 / refine 集成）。

mock LLM 用 httpx.MockTransport 注入固定 JSON 输出。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from sgme import config
from sgme.engine import l2, refine
from sgme.raw import store
from sgme.data import db as db_mod, memory_dao, scene_dao, session_dao


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
    """构造 mock httpx 客户端，返回固定 L2 动作 JSON。"""
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_client_sequence(bodies: list[str]) -> httpx.Client:
    """按顺序返回多个响应（L1 + L2 串联测试用）。"""
    state = {"i": 0}
    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _make_memory(content="x", dim_ids=("tech_stack",), memory_id=None, **kw):
    """构造一条已落库语义的 memory dict。"""
    return {
        "memory_id": memory_id or str(uuid.uuid4()),
        "content": content,
        "dimension_ids": list(dim_ids),
        "memory_type": kw.get("memory_type", "persona"),
        "priority": kw.get("priority", 70),
        "time_velocity": kw.get("time_velocity", "static"),
    }


# ---------- render_l2 ----------

def test_render_l2_replaces_placeholders(cfg):
    """render_l2 替换 {{new_memories}} / {{existing_scenes}} / {{max_scenes}}。"""
    mem = _make_memory(content="用户用 Python", dim_ids=["tech_stack"])
    scenes = [{"scene_id": "s1", "title": "工作", "content": "工作场景正文" * 50}]
    prompt = l2.render_l2([mem], scenes, cfg)
    assert "{{new_memories}}" not in prompt
    assert "{{existing_scenes}}" not in prompt
    assert "{{max_scenes}}" not in prompt
    assert "用户用 Python" in prompt
    assert "s1" in prompt
    # max_scenes 来自 cfg['l2']['max_scenes']
    assert str(cfg["l2"]["max_scenes"]) in prompt


# ---------- parse_l2_output ----------

def test_parse_l2_output_valid():
    """合法 JSON 数组 → 动作列表。"""
    text = json.dumps([
        {"action": "create", "target_scene_id": "s-new",
         "merged_content": "# 新\n正文", "reason": "新主题"},
    ])
    actions = l2.parse_l2_output(text)
    assert len(actions) == 1
    assert actions[0]["action"] == "create"
    assert actions[0]["target_scene_id"] == "s-new"
    assert actions[0]["merged_content"] == "# 新\n正文"


def test_parse_l2_output_markdown_block():
    """```json 包裹 → 仍能解析。"""
    text = '```json\n[{"action":"create","target_scene_id":"s1","merged_content":"# x","reason":"r"}]\n```'
    actions = l2.parse_l2_output(text)
    assert len(actions) == 1
    assert actions[0]["action"] == "create"


def test_parse_l2_output_bad_json_raises():
    """坏 JSON → L2Error。"""
    with pytest.raises(l2.L2Error):
        l2.parse_l2_output("not json at all")


def test_parse_l2_output_invalid_action_raises():
    """非法 action → L2Error。"""
    text = json.dumps([
        {"action": "delete", "target_scene_id": "s1",
         "merged_content": "# x", "reason": "r"},
    ])
    with pytest.raises(l2.L2Error):
        l2.parse_l2_output(text)


# ---------- aggregate: create ----------

def test_aggregate_create_new_scene(mem_conn, cfg):
    """create：scenes 表出现新行 heat=1 + scene_memories 关联。"""
    mem = _make_memory(content="用户开始学习 Rust", dim_ids=["tech_stack"])
    new_sid = str(uuid.uuid4())
    body = json.dumps([
        {"action": "create", "target_scene_id": new_sid,
         "merged_content": "# Rust 学习\n用户开始学 Rust", "reason": "新主题"},
    ])
    cli = _mock_llm_client(body)
    result = l2.aggregate([mem], mem_conn, cfg, client=cli)
    # create 场景 id 由系统生成（不信 LLM 编造的假 uuid）
    assert len(result.created) == 1
    created_sid = result.created[0]
    assert created_sid != new_sid  # 系统生成，非 LLM 提供的 id
    scene = scene_dao.get_scene(mem_conn, created_sid)
    assert scene is not None
    assert scene["heat"] == 1
    assert scene["status"] == "active"
    assert "Rust" in scene["content"]
    # scene_memories 关联
    linked = scene_dao.list_memories_for_scene(mem_conn, created_sid)
    assert mem["memory_id"] in linked


# ---------- aggregate: update ----------

def test_aggregate_update_existing_scene(mem_conn, cfg):
    """update：heat+1 + scene_versions 出现旧版本归档。"""
    sid = str(uuid.uuid4())
    scene_dao.insert_scene(
        mem_conn, scene_id=sid, title="工作",
        content="# 工作\n旧正文", created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-01T00:00:00Z",
    )
    mem = _make_memory(content="用户换了新项目", dim_ids=["projects"])
    body = json.dumps([
        {"action": "update", "target_scene_id": sid,
         "merged_content": "# 工作\n新正文含新项目", "reason": "补充进展"},
    ])
    cli = _mock_llm_client(body)
    result = l2.aggregate([mem], mem_conn, cfg, client=cli)
    assert sid in result.updated
    scene = scene_dao.get_scene(mem_conn, sid)
    assert scene["heat"] == 2  # 1 + 1
    assert "新正文含新项目" in scene["content"]
    # scene_versions 归档旧内容
    versions = scene_dao.list_scene_versions(mem_conn, sid)
    assert len(versions) == 1
    assert "旧正文" in versions[0]["content"]
    assert versions[0]["reason"] == "update"


# ---------- aggregate: merge ----------

def test_aggregate_merge_archives_old(mem_conn, cfg):
    """merge：旧行 status=archived + 新行 heat=sum+1。"""
    sid1 = str(uuid.uuid4())
    sid2 = str(uuid.uuid4())
    scene_dao.insert_scene(
        mem_conn, scene_id=sid1, title="A",
        content="# A\nA 正文", created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-01T00:00:00Z",
    )
    # 手动把 sid1 heat 拉到 2
    scene_dao.update_scene_content(
        mem_conn, scene_id=sid1, content="# A\nA 正文", heat_increment=1,
    )
    scene_dao.insert_scene(
        mem_conn, scene_id=sid2, title="B",
        content="# B\nB 正文", created_at="2026-08-02T00:00:00Z",
        updated_at="2026-08-02T00:00:00Z",
    )
    # sid2 heat 拉到 3（默认 1，再 +2）
    scene_dao.update_scene_content(
        mem_conn, scene_id=sid2, content="# B\nB 正文", heat_increment=2,
    )

    new_sid = str(uuid.uuid4())
    mem = _make_memory(content="A 和 B 是同一主题", dim_ids=["projects"])
    body = json.dumps([
        {"action": "merge", "target_scene_id": new_sid,
         "merged_content": "# 合并场景\nA+B 合并正文",
         "merged_from": [sid1, sid2], "reason": "主题重合"},
    ])
    cli = _mock_llm_client(body)
    result = l2.aggregate([mem], mem_conn, cfg, client=cli)
    # merge 新场景 id 由系统生成
    assert len(result.merged) == 1
    merged_sid = result.merged[0]
    assert merged_sid != new_sid
    assert sid1 in result.archived
    assert sid2 in result.archived
    # 旧行 archived
    assert scene_dao.get_scene(mem_conn, sid1)["status"] == "archived"
    assert scene_dao.get_scene(mem_conn, sid2)["status"] == "archived"
    # 新行 heat = sum(2+3) + 1 = 6
    new_scene = scene_dao.get_scene(mem_conn, merged_sid)
    assert new_scene["heat"] == 6
    assert new_scene["status"] == "active"


# ---------- aggregate: 空输入 ----------

def test_aggregate_empty_memories_returns_empty(mem_conn, cfg):
    """空输入 → 空结果。"""
    result = l2.aggregate([], mem_conn, cfg)
    assert result.created == []
    assert result.updated == []
    assert result.merged == []
    assert result.archived == []
    assert result.error is None
    assert result.anomaly_warn is False


# ---------- aggregate: LLM 失败 ----------

def test_aggregate_llm_failure_marks_error(mem_conn, cfg):
    """LLM 全挂 → error + anomaly_warn。"""
    def handler(req):
        raise httpx.ConnectError("connection refused")
    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    mem = _make_memory(content="x", dim_ids=["tech_stack"])
    result = l2.aggregate([mem], mem_conn, cfg, client=cli)
    assert result.error is not None
    assert result.anomaly_warn is True
    assert result.created == []


# ---------- aggregate: 坏 JSON ----------

def test_aggregate_bad_json_marks_error(mem_conn, cfg):
    """LLM 返回坏 JSON → error + anomaly_warn（不重试）。"""
    mem = _make_memory(content="x", dim_ids=["tech_stack"])
    cli = _mock_llm_client("not json at all")
    result = l2.aggregate([mem], mem_conn, cfg, client=cli)
    assert result.error is not None
    assert result.anomaly_warn is True
    assert result.created == []


# ---------- #33 版本观测 ----------

def test_aggregate_records_refine_run(mem_conn, cfg):
    """#33：aggregate 每记忆批记 refine_run（action 分布 + version）+ prompt_meta 透传。"""
    from sgme.data.refine_dao import RefineRunRecorder
    mem = _make_memory(content="用户开始学习 Rust", dim_ids=["tech_stack"])
    body = json.dumps([
        {"action": "create", "target_scene_id": "s-new",
         "merged_content": "# Rust 学习\n用户开始学 Rust", "reason": "新主题"},
    ])
    cli = _mock_llm_client(body)
    result = l2.aggregate([mem], mem_conn, cfg, client=cli)
    assert len(result.created) == 1
    # prompt_meta
    assert result.prompt_meta is not None
    assert result.prompt_meta["stage"] == "l2_scene"
    assert result.prompt_meta["version"].startswith("working-")
    # refine_run
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l2_scene")
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["version"].startswith("working-")
    assert json.loads(runs[0]["action_counts"]) == {"create": 1}


# ---------- check_scene_threshold ----------

def test_check_scene_threshold_yellow(mem_conn, cfg):
    """场景数 >= yellow 阈值 → ('yellow', N)。"""
    cfg["l2"]["warn_thresholds"] = {"yellow": 2, "orange": 3, "red": 4}
    for i in range(2):
        scene_dao.insert_scene(
            mem_conn, scene_id=str(uuid.uuid4()),
            title=f"s{i}", content=f"# s{i}",
        )
    level, count = l2.check_scene_threshold(mem_conn, cfg)
    assert level == "yellow"
    assert count == 2


def test_check_scene_threshold_red(mem_conn, cfg):
    """场景数 >= red 阈值 → ('red', N)。"""
    cfg["l2"]["warn_thresholds"] = {"yellow": 2, "orange": 3, "red": 4}
    for i in range(4):
        scene_dao.insert_scene(
            mem_conn, scene_id=str(uuid.uuid4()),
            title=f"s{i}", content=f"# s{i}",
        )
    level, count = l2.check_scene_threshold(mem_conn, cfg)
    assert level == "red"
    assert count == 4


def test_check_scene_threshold_under_limit(mem_conn, cfg):
    """未达 yellow 阈值 → (None, N)。"""
    cfg["l2"]["warn_thresholds"] = {"yellow": 5, "orange": 10, "red": 20}
    scene_dao.insert_scene(
        mem_conn, scene_id=str(uuid.uuid4()),
        title="only", content="# only",
    )
    level, count = l2.check_scene_threshold(mem_conn, cfg)
    assert level is None
    assert count == 1


# ---------- refine 集成 ----------

def test_refine_file_triggers_l2(raw_dir, mem_conn, session_conn, cfg):
    """refine_file 完成后 wiki.db 出现 active 场景行（L1 + L2 串联）。"""
    fid = "f-l2-integration"
    msgs = [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user",
         "content": "我开始用 Rust 写新项目"},
        {"timestamp": "2026-08-04T10:01:00Z", "role": "assistant",
         "content": "了解，记下了"},
    ]
    store.write_new_file(
        file_id=fid, session_key="sess_l2", started_at="2026-08-04T10:00:00Z",
        agent_id="test", first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=store.relative_path(fid),
        session_key="sess_l2", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(fid),
    )

    l1_body = json.dumps([
        {"content": "用户开始用 Rust 写新项目", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
    ])
    new_sid = str(uuid.uuid4())
    l2_body = json.dumps([
        {"action": "create", "target_scene_id": new_sid,
         "merged_content": "# Rust 项目\n用户开始用 Rust 写新项目",
         "reason": "新主题"},
    ])
    cli = _mock_llm_client_sequence([l1_body, l2_body])

    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "refined"
    assert len(result.memories) == 1
    # L1.5 落库（模拟 routes_admin：写回 memory_id）后调 finalize_refinement 触发 L2
    for m in result.memories:
        mid = memory_dao.insert_memory(
            mem_conn,
            content=m["content"],
            memory_type=m.get("memory_type", "persona"),
            priority=m.get("priority", 50),
            time_velocity=m.get("time_velocity", "static"),
            ttl_days=None,
            dimension_ids=m.get("dimension_ids", []),
            sources=[(fid, "session")],
        )
        m["memory_id"] = mid
    refine.finalize_refinement(result, mem_conn, cfg, client=cli)
    # wiki.db 出现 active 场景（id 由系统生成）
    scenes = scene_dao.list_active_scenes(mem_conn, limit=10)
    assert len(scenes) >= 1
    scene = scenes[0]
    assert scene["status"] == "active"
    assert scene["heat"] == 1
    assert "Rust" in scene["content"]
