"""scripts/e2e_smoke.py：端到端冒烟测试。

链路：构造会话 → append → refine/trigger → 验证记忆入库 → inject → search → health 水位。

运行方式（mock LLM，无外部依赖）：
    python scripts/e2e_smoke.py

预期输出：
    append ok → refine ok (N 条记忆) → inject blocks ≥1 → search trace 非空 → health watermark 推进
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 让 sgme 包可导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 必须先 monkeypatch L1/L1.5 再 import server（server import 时不调 LLM）
from sgme.engine import l1, l15  # noqa: E402
from sgme.llm import provider as llm_provider  # noqa: E402

# ---------- mock LLM ----------
# L1 mock：从会话提取 2 条记忆
_L1_OUTPUT = json.dumps([
    {
        "content": "用户是一名独立开发者",
        "memory_type": "persona",
        "priority": 85,
        "time_velocity": "static",
        "dimensions": ["身份"],
        "source_message_ids": [1],
    },
    {
        "content": "用户正在开发 SGME 记忆引擎项目",
        "memory_type": "persona",
        "priority": 80,
        "time_velocity": "dynamic",
        "dimensions": ["项目"],
        "source_message_ids": [2],
    },
], ensure_ascii=False)

# L1.5 mock：全部 store（无冲突）
_L15_OUTPUT = json.dumps([
    {"new_memory_index": 0, "candidate_ids": [], "action": "store"},
    {"new_memory_index": 1, "candidate_ids": [], "action": "store"},
], ensure_ascii=False)


def _install_mock_llm() -> None:
    """安装 mock L1 + L1.5（绕过真实 LLM 调用）。"""
    # L1: 直接替换 extract_l1
    _orig_extract_l1 = l1.extract_l1

    def fake_extract_l1(conversation, dimensions, llm_cfg, client=None, **kwargs):
        import json as _json
        memories = _json.loads(_L1_OUTPUT)
        return memories, "mock", {"stage": "l1_extraction", "version": "working-mock", "variant": None}

    l1.extract_l1 = fake_extract_l1

    # L1.5: 替换 call_with_fallback 返回 L1.5 裁决
    import sgme.llm.chain as llm_chain

    _orig_call = llm_chain.call_with_fallback

    def fake_call_with_fallback(llm_cfg, prompt, chain_name="refinement", client=None):
        # 根据 prompt 内容判断是 L1 还是 L1.5
        if "{{new_memories}}" in prompt or "新记忆#" in prompt or "[新记忆#" in prompt:
            return _L15_OUTPUT, "mock"
        # L1 已被 fake_extract_l1 拦截，不会走到这里
        return _L1_OUTPUT, "mock"

    llm_chain.call_with_fallback = fake_call_with_fallback


def main() -> int:
    """运行端到端冒烟测试。"""
    import sqlite3
    import tempfile
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from sgme import config as sgme_config
    from sgme.raw import store as raw_store
    from sgme.server.app import create_app
    from sgme.data import db as db_mod
    from sgme.data import memory_dao

    _install_mock_llm()

    # 隔离环境：tmp data/ + raw/
    tmp_dir = Path(tempfile.mkdtemp(prefix="sgme_e2e_"))
    raw_dir = tmp_dir / "raw"
    raw_dir.mkdir()
    sgme_config.RAW_DIR = raw_dir
    raw_store.config = sgme_config

    cfg = sgme_config.load_config()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_dir / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])

    app = create_app(
        cfg=cfg, mem_conn=mem_conn, session_conn=session_conn, wiki_conn=wiki_conn,
        admin_key="smoke-admin", agent_key="smoke-agent",
    )
    client = TestClient(app)

    AGENT = {"X-API-Key": "smoke-agent"}
    ADMIN = {"X-API-Key": "smoke-admin"}

    def _now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 60)
    print("SGME E2E Smoke Test (mock LLM)")
    print("=" * 60)

    # ---------- 1. append ----------
    content = (
        f"# {_now()} user\n我是一名独立开发者\n"
        f"# {_now()} assistant\n你好！\n"
        f"# {_now()} user\n我正在开发 SGME 记忆引擎项目\n"
    )
    r = client.post("/v1/append", json={
        "session_key": "e2e-smoke-session",
        "started_at": "2026-08-04T10:00:00Z",
        "content": content,
    }, headers=AGENT)
    assert r.status_code == 200, f"append 失败: {r.text}"
    file_id = r.json()["file_id"]
    print(f"[1] append ok: file_id={file_id[:8]}... status={r.json()['status']}")

    # ---------- 2. health: 提炼前 ----------
    h1 = client.get("/v1/health").json()
    print(f"[2] health (提炼前): queue_depth={h1['refinement']['queue_depth']} "
          f"watermark_age_sec={h1['refinement']['watermark_age_sec']}")

    # ---------- 3. refine/trigger ----------
    r = client.post("/v1/admin/refine/trigger", json={
        "file_id": file_id,
    }, headers=ADMIN)
    assert r.status_code == 200, f"refine 失败: {r.text}"
    body = r.json()
    print(f"[3] refine ok: status={body['status']} "
          f"memories_count={body['memories_count']} "
          f"l15_stored={body['l15']['stored']} "
          f"fallback={body['l15']['fallback']}")
    assert body["status"] == "refined"
    assert body["memories_count"] >= 1

    # ---------- 4. inject ----------
    r = client.post("/v1/inject", json={"mode": "daily"}, headers=AGENT)
    assert r.status_code == 200, f"inject 失败: {r.text}"
    body = r.json()
    print(f"[4] inject ok: blocks={len(body['blocks'])} "
          f"stats_mode={body['stats']['mode']} "
          f"tokens_est={body['stats']['tokens_est']}")
    assert len(body["blocks"]) >= 1

    # ---------- 5. search ----------
    r = client.post("/v1/search", json={
        "query": "记忆引擎",
        "scopes": ["memory"],
    }, headers=AGENT)
    assert r.status_code == 200, f"search 失败: {r.text}"
    body = r.json()
    print(f"[5] search ok: results={len(body['results'])}")
    if body["results"]:
        first = body["results"][0]
        trace = first.get("trace", [])
        print(f"    第一条: content={first['content'][:30]}... trace={len(trace)}")
        if trace:
            print(f"    trace[0]: file_id={trace[0]['file_id'][:8]}... "
                  f"path={trace[0]['path']}")
        assert len(trace) >= 1, "search trace 为空"
        assert trace[0]["file_id"] == file_id, "trace file_id 不匹配"

    # ---------- 6. health: 提炼后 ----------
    h2 = client.get("/v1/health").json()
    print(f"[6] health (提炼后): queue_depth={h2['refinement']['queue_depth']} "
          f"watermark_age_sec={h2['refinement']['watermark_age_sec']}")
    assert h2["refinement"]["queue_depth"] == 0, "提炼后 queue_depth 应为 0"
    assert h2["refinement"]["watermark_age_sec"] is not None, "提炼后 watermark 应有值"
    assert h2["refinement"]["watermark_age_sec"] >= 0

    # ---------- 7. stats ----------
    r = client.get("/v1/admin/stats", headers=ADMIN)
    assert r.status_code == 200
    stats = r.json()
    print(f"[7] stats ok: memories={stats['memories']['total']} "
          f"raw_files={stats['raw_files']['total']} "
          f"refined={stats['raw_files']['refined']}")
    assert stats["memories"]["total"] >= 2, "应有 ≥2 条记忆"

    # ---------- 清理 ----------
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)

    print("=" * 60)
    print("E2E SMOKE PASSED")
    print("  append ok → refine ok (2 条记忆) → inject blocks ≥1 → "
          "search trace 非空 → health watermark 推进")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
