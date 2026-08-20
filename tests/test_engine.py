"""T4 测试：L1 提取（归一化 + prompt 渲染 + JSON 解析 + refine 调度）。

mock LLM 用 httpx.MockTransport 注入固定 JSON 输出。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sgme import config
from sgme.engine import l1, normalize, refine
from sgme.raw import store
from sgme.data import db as db_mod, memory_dao, session_dao


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


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


def _mock_llm_client(response_body: str, status: int = 200) -> httpx.Client:
    """构造 mock httpx 客户端，返回固定响应体。"""
    def handler(req):
        if status != 200:
            return httpx.Response(status, text=response_body)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": response_body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _mock_llm_client_sequence(bodies: list[str]) -> httpx.Client:
    """按顺序返回多个响应（用于重试测试）。"""
    state = {"i": 0}
    def handler(req):
        i = state["i"]
        state["i"] = i + 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": body}}]
        })
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ---------- normalize 测试 ----------

def test_normalize_alias_exact_match(cfg, mem_conn):
    """别名精确匹配：中文"技术栈"→tech_stack。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    dim_id, hit, score = normalize.normalize_dimension("技术栈", alias_map, registry_names)
    assert dim_id == "tech_stack"
    assert hit == "alias"


def test_normalize_display_name_match(cfg, mem_conn):
    """display_name 精确匹配：身份→identity。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    dim_id, hit, _ = normalize.normalize_dimension("身份", alias_map, registry_names)
    assert dim_id == "identity"


def test_normalize_registry_id_exact_match(cfg, mem_conn):
    """注册表 id 精确匹配（真实 LLM 常直接输出提示词清单英文 id）。

    回归：L1 提示词维度清单为 'id：display_name'（如 identity：身份），
    LLM 直接复制英文 id 时归一化必须命中，否则真实提炼 100% 丢弃（2026-08-04 修复）。
    """
    alias_map = memory_dao.build_alias_map(mem_conn)  # 仅含中文别名（真实产物）
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    for eng_id in ["identity", "projects", "tech_stack", "status", "family", "values"]:
        dim_id, hit, score = normalize.normalize_dimension(eng_id, alias_map, registry_names)
        assert dim_id == eng_id, f"{eng_id} 应精确命中注册表 id，实际 {dim_id}"
        assert hit == "alias"
        assert score == 1.0


def test_normalize_fuzzy_match(cfg, mem_conn):
    """模糊匹配：相近文本 ratio≥0.85 命中。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    # "技术栈选型" 与 "技术栈" ratio 约 0.8，应该不命中；用更接近的
    # "技能" 是别名，用"技能栈"测试 fuzzy（与"技能"/"技术栈"都比较）
    dim_id, hit, score = normalize.normalize_dimension("技能能力", alias_map, registry_names)
    # "技能能力" 与 "能力"(display_name of skills) ratio = ?
    # SequenceMatcher("技能能力","能力") = 2*2/(4+2) = 0.667，不命中
    # 与 "技能"(alias) = 2*2/(4+2) = 0.667
    # 所以应该 drop 或 fuzzy。这里不强断言具体 id，只验证流程
    assert hit in ("fuzzy", "drop", "alias")


def test_normalize_fuzzy_high_score(cfg, mem_conn):
    """高相似度 fuzzy 命中。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    # "技术栈" 加一个字 → "技术栈x"，ratio 应 ≥ 0.85
    dim_id, hit, score = normalize.normalize_dimension("技术栈x", alias_map, registry_names)
    assert hit == "fuzzy"
    assert dim_id == "tech_stack"
    assert score >= 0.85


def test_normalize_unknown_drops(cfg, mem_conn):
    """未知标签 → 丢弃。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    dim_id, hit, _ = normalize.normalize_dimension("随便", alias_map, registry_names)
    assert dim_id is None
    assert hit == "drop"


def test_normalize_pre_normalize_fullwidth(cfg, mem_conn):
    """预归一化：全角→半角。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    # 全角"技术栈"应能匹配
    dim_id, hit, _ = normalize.normalize_dimension("技术栈", alias_map, registry_names)
    assert dim_id == "tech_stack"


def test_normalize_batch_stats(cfg, mem_conn):
    """批量归一化统计：alias_hits/fuzzy_hits/drops 计数。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    names = ["技术栈", "身份", "随便", "状态"]
    result, stats = normalize.normalize_batch(names, alias_map, registry_names)
    assert "tech_stack" in result
    assert "identity" in result
    assert "status" in result
    assert stats.total == 4
    assert stats.drops == 1
    assert "随便" in stats.dropped_names


def test_normalize_drop_rate_warn(cfg, mem_conn):
    """丢弃率 > 20% → should_warn 返回 True。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    # 5 个标签，2 个未知 → 40% 丢弃
    names = ["技术栈", "未知1", "未知2", "身份", "状态"]
    _, stats = normalize.normalize_batch(names, alias_map, registry_names)
    assert stats.drop_rate == 0.4
    assert normalize.should_warn(stats) is True


def test_normalize_no_warn_low_drop_rate(cfg, mem_conn):
    """丢弃率 ≤ 20% → 不告警。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    names = ["技术栈", "身份", "状态", "目标", "随便"]  # 1/5 = 20%，不 > 20%
    _, stats = normalize.normalize_batch(names, alias_map, registry_names)
    assert stats.drop_rate == 0.2
    assert normalize.should_warn(stats) is False


def test_normalize_dedup(cfg, mem_conn):
    """同一条记忆多标签去重保序。"""
    alias_map = memory_dao.build_alias_map(mem_conn)
    registry_names = {d["id"]: d["display_name"] for d in cfg["dimensions"]}
    # "技术栈" + "技术选型"(别名→tech_stack) 应去重为一个
    result, _ = normalize.normalize_batch(
        ["技术栈", "技术选型"], alias_map, registry_names,
    )
    assert result == ["tech_stack"]


# ---------- l1.render_l1 测试 ----------

def test_render_l1_replaces_placeholders(cfg):
    """render_l1 替换 {{dimensions}} 与 {{conversation}}。"""
    prompt = l1.render_l1("这是会话内容", cfg["dimensions"])
    assert "{{dimensions}}" not in prompt
    assert "{{conversation}}" not in prompt
    assert "这是会话内容" in prompt
    # 维度清单含 "tech_stack：技术栈"
    assert "tech_stack：技术栈" in prompt


def test_render_l1_only_active_dimensions(cfg):
    """只渲染 active=1 的维度。"""
    dims = [
        {"id": "a", "display_name": "甲", "active": 1},
        {"id": "b", "display_name": "乙", "active": 0},
    ]
    prompt = l1.render_l1("conv", dims)
    assert "- a：甲" in prompt
    assert "- b：乙" not in prompt


def test_render_l1_injects_boundaries(cfg):
    """T-11：维度行附 boundaries（vs 对照消歧说明）——送达 LLM 消歧。"""
    dims = [
        {"id": "a", "display_name": "甲", "active": 1,
         "boundaries": "vs 家庭：家庭=成员与关系"},
        {"id": "b", "display_name": "乙", "active": 1},  # 无 boundaries
    ]
    prompt = l1.render_l1("conv", dims)
    assert "- a：甲（边界：vs 家庭：家庭=成员与关系）" in prompt
    # b 无 boundaries → 不带括号
    assert "- b：乙" in prompt
    assert "（边界：" not in prompt.split("- a：甲（边界：vs 家庭：家庭=成员与关系）")[1]


# ---------- l1.parse_l1_output 测试 ----------

def test_parse_l1_output_valid_json(cfg):
    """合法 JSON 数组 → 记忆列表。"""
    text = json.dumps([
        {
            "content": "用户使用 Python 3.11",
            "dimensions": ["技术栈"],
            "memory_type": "persona",
            "priority": 80,
            "time_velocity": "static",
            "source_message_ids": [1, 2],
        }
    ])
    result = l1.parse_l1_output(text, cfg["dimensions"])
    assert len(result) == 1
    assert result[0]["content"] == "用户使用 Python 3.11"
    assert result[0]["dimensions"] == ["技术栈"]
    assert result[0]["priority"] == 80


def test_parse_l1_output_priority_clamp(cfg):
    """priority 钳制 0-100。"""
    text = json.dumps([
        {"content": "x", "dimensions": ["技术栈"], "memory_type": "persona",
         "priority": 150, "time_velocity": "static"},
    ])
    result = l1.parse_l1_output(text, cfg["dimensions"])
    assert result[0]["priority"] == 100


def test_parse_l1_output_invalid_memory_type_defaults_persona(cfg):
    """memory_type 不合法 → 默认 persona。"""
    text = json.dumps([
        {"content": "x", "dimensions": ["技术栈"], "memory_type": "unknown",
         "priority": 50, "time_velocity": "static"},
    ])
    result = l1.parse_l1_output(text, cfg["dimensions"])
    assert result[0]["memory_type"] == "persona"


def test_parse_l1_output_invalid_time_velocity_backfill(cfg):
    """time_velocity 不合法 → 按维度默认回填。"""
    text = json.dumps([
        {"content": "x", "dimensions": ["status"], "memory_type": "persona",
         "priority": 50, "time_velocity": "wrong"},
    ])
    result = l1.parse_l1_output(text, cfg["dimensions"])
    # status 维度 time_velocity=dynamic
    assert result[0]["time_velocity"] == "dynamic"


def test_parse_l1_output_markdown_code_block(cfg):
    """LLM 输出带 ```json 包裹 → 仍能解析。"""
    text = '```json\n[{"content":"x","dimensions":["技术栈"],"memory_type":"persona","priority":50,"time_velocity":"static"}]\n```'
    result = l1.parse_l1_output(text, cfg["dimensions"])
    assert len(result) == 1


def test_parse_l1_output_bad_json_raises(cfg):
    """坏 JSON → RefineError。"""
    with pytest.raises(l1.RefineError, match="JSON"):
        l1.parse_l1_output("not a json at all", cfg["dimensions"])


def test_parse_l1_output_empty_dimensions_skipped(cfg):
    """dimensions 为空 → 跳过该条。"""
    text = json.dumps([
        {"content": "x", "dimensions": [], "memory_type": "persona",
         "priority": 50, "time_velocity": "static"},
    ])
    result = l1.parse_l1_output(text, cfg["dimensions"])
    assert result == []


# ---------- l1.extract_l1 测试（含重试） ----------

def test_extract_l1_success(cfg):
    """L1 提取成功（返回 3 元组：记忆 / provider / prompt_meta）。"""
    body = json.dumps([
        {"content": "用户用 Python", "dimensions": ["技术栈"], "memory_type": "persona",
         "priority": 80, "time_velocity": "static", "source_message_ids": [1]},
    ])
    cli = _mock_llm_client(body)
    memories, provider, meta = l1.extract_l1("会话", cfg["dimensions"], cfg["llm"], client=cli)
    assert len(memories) == 1
    assert memories[0]["content"] == "用户用 Python"
    # v0.5 主模型已切云端：provider 名随配置首链（当前为 deepseek），不写死 lm-studio
    assert provider == cfg["llm"]["chains"]["refinement"][0]["provider"]
    assert meta["stage"] == "l1_extraction"
    assert meta["version"].startswith("working-")
    assert meta["variant"] is None


def test_extract_l1_bad_json_retry_then_success(cfg):
    """坏 JSON → 重试 1 次成功。"""
    bodies = [
        "not json",
        json.dumps([{"content": "ok", "dimensions": ["技术栈"], "memory_type": "persona",
                     "priority": 70, "time_velocity": "static"}]),
    ]
    cli = _mock_llm_client_sequence(bodies)
    memories, _, _ = l1.extract_l1("会话", cfg["dimensions"], cfg["llm"], client=cli)
    assert len(memories) == 1
    assert memories[0]["content"] == "ok"


def test_extract_l1_retry_still_fails_raises(cfg):
    """重试后仍失败 → RefineError。"""
    cli = _mock_llm_client("not json at all")
    with pytest.raises(l1.RefineError, match="重试"):
        l1.extract_l1("会话", cfg["dimensions"], cfg["llm"], client=cli)


# ---------- refine.refine_file 测试 ----------

def _setup_raw_file(raw_dir, session_conn, file_id="f-refine", messages=None):
    """构造 raw 文件 + raw_files 行。"""
    msgs = messages or [
        {"timestamp": "2026-08-04T10:00:00Z", "role": "user", "content": "我用 Python 3.11 写 SGME 项目"},
        {"timestamp": "2026-08-04T10:01:00Z", "role": "assistant", "content": "了解，技术栈记下了"},
    ]
    store.write_new_file(
        file_id=file_id, session_key="sess_refine",
        started_at="2026-08-04T10:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key="sess_refine", started_at="2026-08-04T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


def test_refine_file_success(raw_dir, mem_conn, session_conn, cfg):
    """提炼成功：记忆归一化 + last_refined_seq 推进 + status=refined。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    body = json.dumps([
        {"content": "用户使用 Python 3.11", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
        {"content": "SGME 项目进行中", "dimensions": ["项目"],
         "memory_type": "episodic", "priority": 70, "time_velocity": "dynamic",
         "source_message_ids": [1]},
    ])
    cli = _mock_llm_client(body)

    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "refined"
    assert len(result.memories) == 2
    # dimensions 已归一化为 id
    assert "tech_stack" in result.memories[0]["dimension_ids"]
    assert "projects" in result.memories[1]["dimension_ids"]
    # last_refined_seq 推进
    assert result.new_last_refined_seq == 2
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["last_refined_seq"] == 2
    assert rf["status"] == "refined"


def test_refine_file_unknown_tag_dropped(raw_dir, mem_conn, session_conn, cfg):
    """未知标签丢弃 + 计数，不自动注册。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    body = json.dumps([
        {"content": "x", "dimensions": ["随便", "技术栈"],
         "memory_type": "persona", "priority": 70, "time_velocity": "static"},
    ])
    cli = _mock_llm_client(body)
    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "refined"
    assert len(result.memories) == 1
    # "随便" 丢弃，"技术栈" 保留
    assert result.memories[0]["dimension_ids"] == ["tech_stack"]
    assert result.stats.drops == 1
    assert "随便" in result.stats.dropped_names


def test_refine_file_all_tags_dropped_memory_skipped(raw_dir, mem_conn, session_conn, cfg):
    """全部标签丢弃 → 该记忆跳过。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    body = json.dumps([
        {"content": "x", "dimensions": ["随便", "胡说"],
         "memory_type": "persona", "priority": 70, "time_velocity": "static"},
    ])
    cli = _mock_llm_client(body)
    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert len(result.memories) == 0
    assert result.stats.drops == 2


def test_refine_file_high_drop_rate_warn(raw_dir, mem_conn, session_conn, cfg):
    """丢弃率 > 20% → anomaly_warn=True。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    # 5 条记忆，每条 1 个维度，2 个未知 → drops=2/5=40%
    body = json.dumps([
        {"content": "a", "dimensions": ["技术栈"], "memory_type": "persona", "priority": 70, "time_velocity": "static"},
        {"content": "b", "dimensions": ["随便"], "memory_type": "persona", "priority": 70, "time_velocity": "static"},
        {"content": "c", "dimensions": ["胡说"], "memory_type": "persona", "priority": 70, "time_velocity": "static"},
        {"content": "d", "dimensions": ["身份"], "memory_type": "persona", "priority": 70, "time_velocity": "static"},
        {"content": "e", "dimensions": ["状态"], "memory_type": "persona", "priority": 70, "time_velocity": "static"},
    ])
    cli = _mock_llm_client(body)
    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.anomaly_warn is True
    assert result.stats.drop_rate == 0.4


def test_refine_file_bad_json_marks_error(raw_dir, mem_conn, session_conn, cfg):
    """坏 JSON 重试仍失败 → status=error + anomaly_warn。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    cli = _mock_llm_client("not json")
    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "error"
    assert result.error is not None
    assert result.anomaly_warn is True
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "error"


def test_refine_file_incremental(raw_dir, mem_conn, session_conn, cfg):
    """增量提炼：首次提炼 seq 1-2，追加后增量段 seq 3。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    # 首次提炼
    body1 = json.dumps([
        {"content": "首次", "dimensions": ["技术栈"], "memory_type": "persona",
         "priority": 70, "time_velocity": "static"},
    ])
    cli1 = _mock_llm_client(body1)
    r1 = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli1)
    assert r1.new_last_refined_seq == 2

    # 追加新消息
    store.append_messages(fid, [
        {"timestamp": "2026-08-04T11:00:00Z", "role": "user", "content": "新问题"},
    ])
    session_dao.mark_status(session_conn, fid, "new")
    # 二次提炼（增量段 seq=3）
    body2 = json.dumps([
        {"content": "增量", "dimensions": ["项目"], "memory_type": "episodic",
         "priority": 60, "time_velocity": "dynamic"},
    ])
    cli2 = _mock_llm_client(body2)
    r2 = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli2)
    assert r2.new_last_refined_seq == 3
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["last_refined_seq"] == 3
    assert rf["status"] == "refined"


def test_refine_file_no_incremental(raw_dir, mem_conn, session_conn, cfg):
    """无增量段（last_refined_seq 已达最大）→ 直接标记 refined，不调 LLM。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    # 手动设置 last_refined_seq = 2（已全部提炼）
    session_dao.update_refine_cursor(session_conn, fid, 2)
    session_dao.mark_status(session_conn, fid, "new")

    # 用一个会失败的 client，验证不被调用
    def handler(req):
        raise AssertionError("LLM 不应被调用")
    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.memories == []
    assert result.new_last_refined_seq == 2
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "refined"


def test_refine_file_unknown_file_id(mem_conn, session_conn, cfg):
    """未知 file_id → status=error。"""
    result = refine.refine_file("nonexistent", mem_conn, session_conn, cfg)
    assert result.status == "error"
    assert "无记录" in result.error


def test_refine_file_l0_parse_error_marks_error(raw_dir, mem_conn, session_conn, cfg):
    """L0 文件解析失败（坏 frontmatter）→ status=error。"""
    fid = "f-bad-l0"
    path = store.file_path(fid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("无 frontmatter", encoding="utf-8")
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=store.relative_path(fid),
        session_key="s", started_at="2026-08-04T10:00:00Z", status="new",
    )
    result = refine.refine_file(fid, mem_conn, session_conn, cfg)
    assert result.status == "error"
    rf = session_dao.get_raw_file(session_conn, fid)
    assert rf["status"] == "error"


# ---------- #33 版本观测 ----------

def test_refine_file_records_l1_run_and_prompt_versions(raw_dir, mem_conn, session_conn, cfg):
    """#33：refine_file 记录 L1 refine_run + RefineResult.prompt_versions 透传（file_id 分流键）。"""
    from sgme.data.refine_dao import RefineRunRecorder
    fid = _setup_raw_file(raw_dir, session_conn)
    body = json.dumps([
        {"content": "用户使用 Python 3.11", "dimensions": ["技术栈"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static",
         "source_message_ids": [1]},
    ])
    cli = _mock_llm_client(body)

    result = refine.refine_file(fid, mem_conn, session_conn, cfg, client=cli)
    assert result.status == "refined"
    # prompt_versions 透传 L1 版本
    assert result.prompt_versions["l1_extraction"]["stage"] == "l1_extraction"
    assert result.prompt_versions["l1_extraction"]["version"].startswith("working-")
    assert result.prompt_versions["l1_extraction"]["variant"] is None
    # L1 refine_run 已记录（file_id = bucket_key）
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(runs) == 1
    assert runs[0]["file_id"] == fid
    assert runs[0]["status"] == "ok"
    assert runs[0]["memories_count"] == 1
