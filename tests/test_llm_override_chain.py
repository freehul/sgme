"""tests/test_llm_override_chain.py：refine.llm_override 生效修复（2026-11-06）。

背景：llm_override 此前只在 refine_file 内、且仅当文件带 agent_model 时应用 →
没有 agent_model 的文件里 sgme.yaml 的 llm_override 被静默忽略（B121 同类缺陷）。
修复后：
1. 无 agent_model 的文件也走 llm_override 动态链（L1）
2. pipeline.refine_one 把同一条动态链传给 L3（persist_memories）
3. llm_override 优先于 agent_model
4. apply_llm_override 幂等（已应用则原样返回）

全部零真实 LLM：extract_memories / persist_memories 被桩替换。
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod
from sgme.raw import store


MAGPIE = "magpie"
MAGPIE_MODEL = "zcode/GLM-5.3-Flash-Trial"


@pytest.fixture
def cfg():
    c = config.load_config()
    c.setdefault("llm", {}).setdefault("providers", {})[MAGPIE] = {
        "name": MAGPIE,
        "base_url": "http://127.0.0.1:13425/v1",
        "api_key_env": "MAGPIE_API_KEY",
        "provider_type": "openai_compat",
        "default_model": MAGPIE_MODEL,
        "context_window": 200000,
        "timeout_s": 60,
    }
    c["refine"]["llm_override"] = {
        "provider": MAGPIE, "model": MAGPIE_MODEL, "max_tokens": 16384,
    }
    return c


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


def _setup(raw_dir, session_conn, file_id="f-ov", agent_model=None):
    msgs = [
        {"timestamp": "2026-11-05T10:00:00Z", "role": "user", "content": "我用 Python 写 SGME"},
        {"timestamp": "2026-11-05T10:01:00Z", "role": "assistant", "content": "了解"},
    ]
    store.write_new_file(file_id=file_id, session_key=f"sess_{file_id}",
                         started_at="2026-11-05T10:00:00Z", agent_id="test",
                         first_messages=msgs)
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key=f"sess_{file_id}", started_at="2026-11-05T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    if agent_model:
        session_conn.execute(
            "update raw_files set agent_model=? where file_id=?", (agent_model, file_id))
        session_conn.commit()
    return file_id


def _head(llm_cfg):
    return (llm_cfg.get("chains", {}).get("refinement") or [{}])[0].get("provider")


def test_refine_file_applies_override_without_agent_model(raw_dir, mem_conn, session_conn, cfg, monkeypatch):
    fid = _setup(raw_dir, session_conn)
    captured = {}

    def fake_extract(conversations, dimensions, llm_cfg, **kwargs):
        captured["llm_cfg"] = llm_cfg
        return [], MAGPIE, {
            "stage": "l1_extraction", "version": "v019", "variant": None,
            "chunk_failures": 0, "chunk_total": 1, "l1_truncated_chunks": 0,
        }

    monkeypatch.setattr(refine_mod.l2_extraction, "extract_memories", fake_extract)
    res = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert res.status == "refined"
    assert _head(captured["llm_cfg"]) == MAGPIE
    # 原 cfg 不被修改（纯函数语义）
    assert _head(cfg["llm"]) == "agnes"


def test_refine_one_passes_override_chain_to_persist(mem_conn, session_conn, cfg, monkeypatch):
    captured = {}

    def fake_refine(file_id, mem, sess, cfg_):
        captured["refine_cfg"] = cfg_
        return refine_mod.RefineResult(file_id=file_id, status="refined",
                                       memories=[{"content": "x"}])

    def fake_persist(result, mem, cfg_, **kwargs):
        captured["persist_cfg"] = cfg_
        return {}

    monkeypatch.setattr(refine_mod, "refine_file", fake_refine)
    monkeypatch.setattr(pipeline_mod, "persist_memories", fake_persist)
    pipeline_mod.refine_one("f-ov", mem_conn, session_conn, cfg)
    assert _head(captured["refine_cfg"]["llm"]) == MAGPIE
    assert _head(captured["persist_cfg"]["llm"]) == MAGPIE


def test_override_precedence_over_agent_model(raw_dir, mem_conn, session_conn, cfg, monkeypatch):
    fid = _setup(raw_dir, session_conn, file_id="f-ov-prec", agent_model="deepseek/deepseek-v4-flash")
    captured = {}

    def fake_extract(conversations, dimensions, llm_cfg, **kwargs):
        captured["llm_cfg"] = llm_cfg
        return [], MAGPIE, {
            "stage": "l1_extraction", "version": "v019", "variant": None,
            "chunk_failures": 0, "chunk_total": 1, "l1_truncated_chunks": 0,
        }

    monkeypatch.setattr(refine_mod.l2_extraction, "extract_memories", fake_extract)
    refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert _head(captured["llm_cfg"]) == MAGPIE


def test_apply_llm_override_idempotent(cfg):
    once = refine_mod.apply_llm_override(cfg)
    assert once is not cfg
    twice = refine_mod.apply_llm_override(once)
    assert twice is once
    assert _head(twice["llm"]) == MAGPIE


def test_apply_llm_override_noop_without_override(cfg):
    cfg["refine"]["llm_override"] = {}
    assert refine_mod.apply_llm_override(cfg) is cfg
