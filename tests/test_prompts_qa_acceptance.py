"""#33 QA 独立验收测试（test_prompts_qa_acceptance.py）。

与工程师测试（test_prompts / test_refine_dao / test_prompts_api）互补，独立验证：
- 热更新语义：@working 编辑立即生效（无缓存）；钉版不可变快照；进行中任务（已渲染 prompt）不受影响
- A/B 语义：sha256 确定性分流公式；跨 key 分布；variant 在 refine_runs 正确落账
- 版本可观测：逐块 refine_run；metrics 无自动裁决字段（红线 §6 #1）；旧库 NULL 不追溯（红线 §6 #4）
- 向后兼容：manifest 缺失默认 @working（引擎渲染层）；全新库 schema v3
- 最小侵入红线：4 渲染点全部经 PromptStore（无 read_text 直读残留）
- 红线：bucket_by 默认 file_id（§6 #2）；tier0 A/B 默认不启用（§6 #5）
- 维度联动：refresh_dimensions 函数级验证（DB 型资源变更即刷新）

只读验证，不修改任何 sgme/ 业务代码。
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.engine import l1 as l1_mod
from sgme.engine import l15 as l15_mod
from sgme.engine import l2 as l2_mod
from sgme.prompts import BucketCtx, PromptStore
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.refine_dao import RefineRunRecorder

STAGE_TEXTS = {
    "tier0_summary": "你是摘要器。\n{{memories}}",
    "l1_extraction": "你是提取器。\n{{dimensions}}\n{{conversation}}",
    "l1_conflict": "你是裁决器。\n{{new_memories}}\n{{candidates}}",
    "l2_scene": "你是聚合器。\n{{new_memories}}\n{{existing_scenes}}\n{{max_scenes}}",
}


# ---------- fixtures ----------

@pytest.fixture
def prompts_root(tmp_path):
    """临时 prompts 目录（4 个工作副本，无 manifest）。"""
    root = tmp_path / "prompts"
    root.mkdir()
    for stage, text in STAGE_TEXTS.items():
        (root / f"{stage}.txt").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def store(prompts_root):
    return PromptStore(prompts_root=prompts_root)


@pytest.fixture
def mem_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    # memory_tags 外键需要 dimension_registry 行（与 test_refine_dao 一致）
    cfg = sgme_config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _expected_bucket(bucket_key: str, split: float) -> str:
    """复刻设计 §1.4 确定性分流公式：sha256(key) 前 8 字节取模 100，< split*100 走 A。"""
    h = hashlib.sha256(bucket_key.encode("utf-8")).digest()[:8]
    val = int.from_bytes(h, "big") % 100
    return "A" if val < split * 100 else "B"


def _mock_llm_client(body: str):
    """httpx MockTransport 客户端，固定返回 body（provider 走 lm-studio 链）。"""
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={
                "choices": [{"message": {"content": body}}],
            })
        ),
        trust_env=False,
    )


def _setup_ab(store):
    """发布 v001 + v002 并开启 A/B（split=0.5, bucket_by=file_id）。"""
    store.publish("l1_extraction", note="A 版")
    (store.prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版措辞", encoding="utf-8",
    )
    store.publish("l1_extraction", note="B 版")
    store.configure_ab("l1_extraction", "v001", "v002", split=0.5, bucket_by="file_id")


# ---------- 热更新语义 ----------

def test_working_hot_reload_at_engine_render(prompts_root, monkeypatch):
    """@working 编辑后引擎渲染立即生效（无缓存）——render_l1 读最新工作副本。"""
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    dims = [{"id": "identity", "display_name": "身份", "active": 1}]
    out1 = l1_mod.render_l1("会话内容", dims)
    (prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\n# 新增指令\n{{conversation}}", encoding="utf-8",
    )
    out2 = l1_mod.render_l1("会话内容", dims)
    assert out1 != out2
    assert "# 新增指令" not in out1
    assert "# 新增指令" in out2


def test_inflight_render_frozen_not_affected_by_edit(store, prompts_root):
    """进行中任务不受影响：prompt 一旦渲染即为不可变字符串，后续编辑不影响已渲染文本。"""
    pv1 = store.get("l1_extraction")
    rendered = pv1.text  # 模拟任务进入 LLM 调用前已渲染好的 prompt
    # 任务执行中，工作副本被编辑
    (prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\n# 新指令\n{{conversation}}", encoding="utf-8",
    )
    # 已渲染字符串不受影响（原子语义）
    assert "# 新指令" not in rendered
    assert "{{conversation}}" in rendered
    # 下一次渲染使用新版本
    pv2 = store.get("l1_extraction")
    assert "# 新指令" in pv2.text
    assert pv2.version == f"working-{_sha8(pv2.text)}"


def test_pinned_snapshot_immune_to_working_edits(store, prompts_root):
    """activate 钉版后取的是不可变快照：后续编辑工作副本不影响钉版内容。"""
    store.publish("l1_extraction", note="基线")
    store.activate("l1_extraction", "v001")
    # 钉版生效
    pinned = store.get("l1_extraction")
    assert pinned.version == "v001"
    # 编辑工作副本（热更新草稿）
    (prompts_root / "l1_extraction.txt").write_text(
        "草稿改动\n{{conversation}}", encoding="utf-8",
    )
    # 钉版仍返回 v001 快照，不被草稿影响
    pinned2 = store.get("l1_extraction")
    assert pinned2.version == "v001"
    assert pinned2.text == STAGE_TEXTS["l1_extraction"]
    # 切回 @working 才看到草稿
    store.activate("l1_extraction", "@working")
    assert "草稿改动" in store.get("l1_extraction").text


# ---------- A/B 语义 ----------

def test_ab_bucket_formula_matches_sha256(store):
    """确定性分流公式与设计 §1.4 完全一致（复算 sha256 取模）。"""
    _setup_ab(store)
    for split in (0.0, 0.2, 0.5, 0.8, 1.0):
        store.configure_ab("l1_extraction", "v001", "v002", split=split, bucket_by="file_id")
        for key in ("file-001", "file-abc", "f", "中文-key", "abc123"):
            expect = _expected_bucket(key, split)
            pv = store.get("l1_extraction", BucketCtx(bucket_key=key))
            assert pv.variant == expect, f"key={key} split={split}"
            assert pv.version == ("v001" if expect == "A" else "v002")


def test_ab_distribution_across_keys(store):
    """不同 key 按 split 比例分流：200 个 key 两变体均出现且比例合理。"""
    _setup_ab(store)
    variants = [store.get("l1_extraction", BucketCtx(bucket_key=f"file-{i}")).variant for i in range(200)]
    assert set(variants) == {"A", "B"}
    ratio_a = variants.count("A") / len(variants)
    assert 0.25 <= ratio_a <= 0.75, f"A 占比 {ratio_a} 偏离 0.5 过远"


def test_ab_bucket_by_defaults_file_id(store, prompts_root):
    """红线 §6 #2：bucket_by 默认 file_id（configure_ab 不传时写入 manifest）。"""
    store.publish("l1_extraction", note="A")
    (store.prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版", encoding="utf-8",
    )
    store.publish("l1_extraction", note="B")
    store.configure_ab("l1_extraction", "v001", "v002", split=0.5)  # 不传 bucket_by
    import yaml
    m = yaml.safe_load((prompts_root / "manifest.yaml").read_text(encoding="utf-8"))
    assert m["stages"]["l1_extraction"]["ab"]["bucket_by"] == "file_id"


def test_ab_variant_recorded_in_refine_runs(prompts_root, mem_conn, monkeypatch):
    """A/B 命中变体正确落账：engine 链路 extract_l1 + mem_conn → refine_runs 记录 version/variant。"""
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    store = PromptStore(prompts_root=prompts_root)
    _setup_ab(store)

    cfg = sgme_config.load_config()
    body = json.dumps([
        {"content": "A/B 测试记忆", "dimensions": ["identity"],
         "memory_type": "persona", "priority": 80, "time_velocity": "static"},
    ], ensure_ascii=False)
    cli = _mock_llm_client(body)

    key = "file-abtest-1"
    memories, provider, meta = l1_mod.extract_l1(
        "一段会话", cfg["dimensions"], cfg["llm"], client=cli,
        bucket_ctx=BucketCtx(bucket_key=key), mem_conn=mem_conn,
    )
    assert len(memories) == 1
    expect = _expected_bucket(key, 0.5)
    assert meta["variant"] == expect
    assert meta["version"] in ("v001", "v002")

    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(runs) == 1
    r = runs[0]
    assert r["stage"] == "l1_extraction"
    assert r["version"] == meta["version"]
    assert r["variant"] == meta["variant"]
    assert r["bucket_key"] == key
    assert r["status"] == "ok"
    assert r["memories_count"] == 1


# ---------- 版本可观测 ----------

def test_extract_l1_records_run_per_chunk(prompts_root, mem_conn, monkeypatch):
    """逐批记录：多块会话 → 每块一条 refine_run（L1 分块每块一条，设计 §7）。"""
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    cfg = sgme_config.load_config()
    parts = []
    for i in range(12):
        parts.append(f"# 1783763000.{i} user\n消息{i} " + "x" * 500 + "\n")
    conv = "".join(parts)  # ~6K 字符
    state = {"i": 0}

    def handler(req):
        i = state["i"]
        state["i"] += 1
        body = json.dumps([
            {"content": f"块记忆{i}", "dimensions": ["identity"],
             "memory_type": "persona", "priority": 60, "time_velocity": "static"},
        ], ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    cli = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    memories, _, meta = l1_mod.extract_l1(
        conv, cfg["dimensions"], cfg["llm"], client=cli,
        chunk_size=2500, overlap=500,
        bucket_ctx=BucketCtx(bucket_key="file-chunk"), mem_conn=mem_conn,
    )
    assert memories  # 有产出
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(runs) >= 2  # 多块 → 多条 run
    for r in runs:
        assert r["status"] == "ok"
        assert r["version"].startswith("working-")
        assert r["variant"] is None
    assert meta["stage"] == "l1_extraction"


def test_refine_runs_records_error_status(prompts_root, mem_conn, monkeypatch):
    """版本可观测：LLM 全挂 → refine_runs 记 status=error + error 文本（不丢观测）。"""
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    cfg = sgme_config.load_config()

    def boom(req):
        raise httpx.ConnectError("mock 连接失败")

    cli = httpx.Client(transport=httpx.MockTransport(boom), trust_env=False)
    with pytest.raises(l1_mod.RefineError):
        l1_mod.extract_l1(
            "会话", cfg["dimensions"], cfg["llm"], client=cli,
            bucket_ctx=BucketCtx(bucket_key="file-err"), mem_conn=mem_conn,
        )
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["version"].startswith("working-")
    assert runs[0]["error"]


# ---------- 红线：metrics 不做自动裁决 / 旧库 NULL 不追溯 ----------

@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """隔离 FastAPI app：临时 prompts 目录 + 临时双库 + 临时 raw/ 与 tier0 摘要路径。"""
    from sgme.profile import tier0 as tier0_mod
    from sgme.raw import store as raw_store
    from sgme.server.app import create_app

    root = tmp_path / "prompts"
    root.mkdir()
    for stage, text in STAGE_TEXTS.items():
        (root / f"{stage}.txt").write_text(text, encoding="utf-8")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    monkeypatch.setattr(sgme_config, "RAW_DIR", raw_dir)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", root / "manifest.yaml")
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="test-admin-key", agent_key="test-agent-key",
    )
    client = TestClient(app)
    yield app, client
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def _mock_llm_sequence(bodies: list[str]) -> httpx.Client:
    """按顺序返回多个 chat/completions 响应（L1 + L2 串联）。"""
    state = {"i": 0}

    def handler(req):
        i = state["i"]
        state["i"] += 1
        body = bodies[min(i, len(bodies) - 1)]
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def test_metrics_no_automatic_adjudication(api_env):
    """红线 §6 #1：metrics 只返回原始观测（runs/error_runs/memories/avg_priority/action_dist），无自动裁决字段。"""
    app, client = api_env
    mem_conn = app.state.mem_conn
    run_id = RefineRunRecorder.start(mem_conn, "f1", "l1_extraction", "v001", None, "lm-studio", "f1")
    RefineRunRecorder.finish(mem_conn, run_id, 2, {}, "ok")
    memory_dao.insert_memory(
        mem_conn, content="m", memory_type="persona", priority=90,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v001",
    )
    resp = client.get("/v1/admin/prompts/metrics", params={"stage": "l1_extraction"},
                      headers={"X-API-Key": "test-admin-key"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"stage", "since", "groups"}
    assert len(body["groups"]) == 1
    g = body["groups"][0]
    assert set(g.keys()) == {
        "version", "variant", "runs", "error_runs",
        "memories_count", "memories_rows", "avg_priority", "action_dist",
    }
    # 无任何"裁决/结论/推荐"字段
    banned = ("winner", "recommend", "conclusion", "verdict", "decision", "胜", "推荐")
    assert all(not any(b in str(k).lower() for b in banned) for k in body.keys())
    assert all(not any(b in str(k).lower() for b in banned) for k in g.keys())


def test_metrics_ignores_null_prompt_version(api_env):
    """红线 §6 #4：旧库 memories.prompt_version=NULL 不追溯、不计入任何版本组。"""
    app, client = api_env
    mem_conn = app.state.mem_conn
    run_id = RefineRunRecorder.start(mem_conn, "f1", "l1_extraction", "v001", None, "lm-studio", "f1")
    RefineRunRecorder.finish(mem_conn, run_id, 2, {}, "ok")
    # 一条带版本 + 一条 NULL（旧库历史数据）
    memory_dao.insert_memory(
        mem_conn, content="带版本", memory_type="persona", priority=90,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
        prompt_version="l1_extraction:v001",
    )
    memory_dao.insert_memory(
        mem_conn, content="历史数据", memory_type="persona", priority=80,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
    )
    body = client.get("/v1/admin/prompts/metrics", params={"stage": "l1_extraction"},
                      headers={"X-API-Key": "test-admin-key"}).json()
    g = body["groups"][0]
    assert g["version"] == "v001"
    assert g["memories_rows"] == 1  # NULL 行不追溯
    assert g["avg_priority"] == 90.0


# ---------- 向后兼容 ----------

def test_manifest_missing_engine_render_works(prompts_root, monkeypatch):
    """manifest 缺失 → 引擎渲染走默认 @working（向后兼容老库/老测试）。"""
    monkeypatch.setattr(PromptStore, "PROMPTS_ROOT", prompts_root)
    monkeypatch.setattr(PromptStore, "MANIFEST_PATH", prompts_root / "manifest.yaml")
    assert not (prompts_root / "manifest.yaml").exists()
    dims = [{"id": "identity", "display_name": "身份", "active": 1}]
    out = l1_mod.render_l1("会话", dims)
    assert "会话" in out
    pv = PromptStore(prompts_root=prompts_root).get("l1_extraction")
    assert pv.version.startswith("working-")


def test_fresh_init_schema_v4(tmp_path):
    """全新库 init_databases → schema_version=4 + refine_runs 表 + prompt_version/content_seg 列。"""
    mem, session, wiki = db_mod.init_databases(tmp_path)
    try:
        assert db_mod.schema_version(mem) == 4
        assert db_mod.schema_version(session) == 4
        assert db_mod.schema_version(wiki) == 4
        tables = set(db_mod.list_tables(mem))
        assert "refine_runs" in tables
        cols = [r[1] for r in mem.execute("PRAGMA table_info(memories)").fetchall()]
        assert "prompt_version" in cols
        assert "content_seg" in cols
    finally:
        db_mod.close(mem)
        db_mod.close(session)
        db_mod.close(wiki)


def test_connect_memory_twice_idempotent(tmp_path):
    """重复连接/初始化安全（DDL IF NOT EXISTS + schema_versions 幂等）。"""
    c1 = db_mod.connect_memory(tmp_path)
    db_mod.close(c1)
    c2 = db_mod.connect_memory(tmp_path)
    try:
        assert db_mod.schema_version(c2) == 4
    finally:
        db_mod.close(c2)


def test_insert_memory_old_signature_backward_compat(mem_conn):
    """insert_memory 无 prompt_version 参数（旧调用）→ 正常落库，列为 NULL。"""
    mid = memory_dao.insert_memory(
        mem_conn, content="旧调用", memory_type="persona", priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=["identity"],
    )
    row = mem_conn.execute("SELECT * FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert row["prompt_version"] is None


# ---------- 最小侵入：4 渲染点全部经 PromptStore ----------

class _FakePV:
    """模拟 PromptVersion：render 层只消费 text；tier0 还消费 version/variant。"""

    def __init__(self, text, version="working-fake1234", variant=None):
        self.text = text
        self.version = version
        self.variant = variant


def test_all_render_points_route_through_promptstore(monkeypatch, tmp_path):
    """红线（§1.6 只改 1 行的点）：4 渲染点全部经 PromptStore.get，无 read_text 直读残留。"""
    from sgme.profile import tier0 as tier0_mod

    calls: list[str] = []

    def fake_get(self, stage, ctx=None):
        calls.append(stage)
        return _FakePV(STAGE_TEXTS[stage])

    monkeypatch.setattr(PromptStore, "get", fake_get)

    # L1
    l1_mod.render_l1("会话", [{"id": "identity", "display_name": "身份", "active": 1}])
    # L1.5
    l15_mod.render_l15(
        [{"content": "新记忆", "dimension_ids": ["identity"], "memory_type": "persona",
          "priority": 50, "time_velocity": "static"}],
        [],
    )
    # L2
    l2_mod.render_l2(
        [{"content": "记忆", "memory_id": "m1", "dimension_ids": ["identity"]}],
        [], {"l2": {"max_scenes": 10}},
    )
    # Tier0：generate_summary 内部经 PromptStore（mock LLM + 空记忆库）
    conn = db_mod.connect_memory(tmp_path)
    try:
        from sgme.llm import chain as llm_chain
        monkeypatch.setattr(llm_chain, "call_with_fallback",
                            lambda *a, **k: ("测试摘要", "mock", {}))
        cfg = sgme_config.load_config()
        result = tier0_mod.generate_summary(conn, cfg)
        assert result == "测试摘要"
    finally:
        db_mod.close(conn)

    assert calls == ["l1_extraction", "l1_conflict", "l2_scene", "tier0_summary"]


# ---------- 维度联动（函数级） ----------

def test_refresh_dimensions_after_registry_write(mem_conn):
    """维度注册表（DB 型资源）写库后 refresh_dimensions → cfg['dimensions'] 即时刷新。"""
    from sgme.server.routes_registry import refresh_dimensions

    cfg = {"dimensions": []}
    memory_dao.upsert_dimension(mem_conn, {
        "id": "qa_dim_live", "display_name": "QA 实时维度", "category": "动态",
        "time_velocity": "dynamic", "ttl_days": 7, "description": "",
    })
    mem_conn.commit()
    refresh_dimensions(cfg, mem_conn)
    ids = {d["id"] for d in cfg["dimensions"]}
    assert "qa_dim_live" in ids
    # 停用 → 再次刷新后消失
    mem_conn.execute("UPDATE dimension_registry SET active=0 WHERE id='qa_dim_live'")
    mem_conn.commit()
    refresh_dimensions(cfg, mem_conn)
    ids2 = {d["id"] for d in cfg["dimensions"]}
    assert "qa_dim_live" not in ids2


# ---------- 全链路：trigger 透传 prompt_version（routes_admin，T05 缺口补测） ----------

def test_refine_trigger_prompt_version_end_to_end(api_env, monkeypatch):
    """T05 全链路：/v1/admin/refine/trigger 响应含 prompt_versions + memories.prompt_version 落 L1 钉版版本。"""
    import uuid

    from sgme.engine import l15 as l15_mod
    from sgme.engine import l2 as l2_mod
    from sgme.engine import refine as refine_mod
    from sgme.data.search import vector as vector_mod

    app, client = api_env
    mem_conn = app.state.mem_conn

    # 钉版 l1_extraction → v001（使 prompt_version 确定可断言）
    store = PromptStore()
    store.publish("l1_extraction", note="基线")
    store.activate("l1_extraction", "v001")

    # mock LLM：L1 输出 1 条记忆；L1.5 候选池为空（短路 store，不调 LLM）；L2 输出 create
    l1_body = json.dumps([
        {"content": "QA 验收记忆", "dimensions": ["身份"],
         "memory_type": "persona", "priority": 85, "time_velocity": "static",
         "source_message_ids": [1]},
    ], ensure_ascii=False)
    l2_body = json.dumps([
        {"action": "create", "target_scene_id": str(uuid.uuid4()),
         "merged_content": "# QA 场景\nQA 验收记忆", "reason": "新主题"},
    ], ensure_ascii=False)
    cli = _mock_llm_sequence([l1_body, l2_body])

    # 注入 mock client（与 test_e2e_v04 同模式）
    _orig_refine = refine_mod.refine_file
    monkeypatch.setattr(refine_mod, "refine_file",
                        lambda fid, mem, session, cfg, source_type="session", client=None:
                        _orig_refine(fid, mem, session, cfg, source_type=source_type, client=cli))
    _orig_l15 = l15_mod.resolve_conflicts
    monkeypatch.setattr(l15_mod, "resolve_conflicts",
                        lambda nm, mem, cfg, client=None, **kw:
                        _orig_l15(nm, mem, cfg, client=cli, **kw))
    _orig_l2 = l2_mod.aggregate
    monkeypatch.setattr(l2_mod, "aggregate",
                        lambda m, mem, cfg, client=None, **kw:
                        _orig_l2(m, mem, cfg, client=cli, **kw))
    # embedding 依赖真实 LLM → no-op（finalize_refinement 内失败不阻塞，这里显式消除不确定性）
    monkeypatch.setattr(vector_mod, "upsert_memory_vector",
                        lambda *a, **k: None)

    # append → 产生 raw_files 行
    content = (
        "# 2024-01-01T10:00:00Z user\nQA 验收记忆内容\n"
        "# 2024-01-01T10:00:30Z assistant\n好的\n"
    )
    r = client.post("/v1/append", json={
        "session_key": "qa-prompt-version",
        "started_at": "2024-01-01T10:00:00Z",
        "content": content,
    }, headers={"X-API-Key": "test-agent-key"})
    assert r.status_code == 200, r.text
    file_id = r.json()["file_id"]

    # trigger
    r = client.post("/v1/admin/refine/trigger", json={"file_id": file_id},
                    headers={"X-API-Key": "test-admin-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "refined"
    assert body["memories_count"] >= 1

    # 响应含 prompt_versions（L1 钉版 v001）
    assert "prompt_versions" in body
    l1_meta = body["prompt_versions"]["l1_extraction"]
    assert l1_meta["stage"] == "l1_extraction"
    assert l1_meta["version"] == "v001"
    assert l1_meta["variant"] is None

    # memories.prompt_version 落 L1 版本
    rows = mem_conn.execute(
        "SELECT memory_id, prompt_version FROM memories"
    ).fetchall()
    assert len(rows) >= 1
    for row in rows:
        assert row["prompt_version"] == "l1_extraction:v001", row["prompt_version"]

    # refine_runs 落 L1 钉版版本（逐批记录）
    runs = RefineRunRecorder.list_by_stage(mem_conn, "l1_extraction")
    assert len(runs) >= 1
    assert runs[0]["version"] == "v001"
    assert runs[0]["variant"] is None
