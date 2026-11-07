# -*- coding: utf-8 -*-
"""tests/test_zcode_agent_pipeline.py：ZCode agent 任务包 → 导入 → 落库（零 LLM）。

2026-11-07：外部网关（magpie / CLI 桥）调 API 会被体验桶风控封，改为「ZCode 客户端里
的 agent 自己推理、脚本只做打包与落库」。本测试钉住这条链路的契约：
1. zcode_stage 能把待提炼文件渲染成「模型该看到的完整提示词」+ manifest；
2. zcode_import 能校验 agent 产出的 JSON、归一化、落库，并推进 raw_files 游标。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "raw" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("SGME_HOME", str(tmp_path))
    monkeypatch.setenv("SGME_L2_SCHEMA", "2.0")
    monkeypatch.delenv("SGME_CONFIG_PATH", raising=False)
    return tmp_path


def _seed_file(home):
    from sgme.data import db as db_mod, session_dao
    from sgme.raw import store as raw_store

    mem, ses, _ = db_mod.init_databases(home / "data")
    fid = "test-file-1"
    msgs = [
        {"timestamp": "2026-11-06T10:00:00Z", "role": "user", "content": "我在调显存，4080S 上跑 4B 模型"},
        {"timestamp": "2026-11-06T10:01:00Z", "role": "assistant", "content": "先看显存占用再调 batch"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess-test", started_at="2026-11-06T10:00:00Z",
        agent_id="test", source_type="session", first_messages=msgs, metadata=None,
    )
    session_dao.insert_raw_file(
        ses, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess-test", started_at="2026-11-06T10:00:00Z",
        status="new", size=raw_store.file_size(fid),
    )
    mem.close()
    ses.close()
    return fid


def test_stage_generates_prompt_and_manifest(home):
    from scripts import zcode_stage

    _seed_file(home)
    rc = zcode_stage.main(["--home", str(home), "--out", str(home / "staging")])
    assert rc == 0
    manifest = json.loads((home / "staging" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_tasks"] == 1
    task = manifest["tasks"][0]
    prompt = (home / "staging" / task["prompt_path"]).read_text(encoding="utf-8")
    assert "[msg#1]" in prompt and "我在调显存" in prompt
    assert "输出" in prompt or "JSON" in prompt


def test_import_persists_memories_and_advances_cursor(home):
    from scripts import zcode_import, zcode_stage

    _seed_file(home)
    staging = home / "staging"
    zcode_stage.main(["--home", str(home), "--out", str(staging)])
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    out = staging / manifest["tasks"][0]["out_path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([
        {"content": "用户有 4080S 显卡", "memory_type": "fact",
         "priority": 50, "time_velocity": "static", "dimensions": ["tech_stack"]},
    ], ensure_ascii=False), encoding="utf-8")

    rc = zcode_import.main(["--home", str(home), "--staging", str(staging)])
    assert rc == 0
    import sqlite3

    m = sqlite3.connect(str(home / "data" / "memory.db"))
    assert m.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert m.execute("SELECT content FROM memories").fetchone()[0] == "用户有 4080S 显卡"

    s = sqlite3.connect(str(home / "data" / "session.db"))
    status, seq = s.execute("SELECT status, last_refined_seq FROM raw_files").fetchone()
    assert status == "refined"
    assert seq == 2

    # 幂等：再跑一次不重复落库
    zcode_import.main(["--home", str(home), "--staging", str(staging)])
    assert m.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_empty_after_prune_is_skipped_and_refined(home):
    """纯 tool 消息（剪枝后无正文）：不产任务，import 直接标 refined。"""
    from sgme.data import db as db_mod, session_dao
    from sgme.raw import store as raw_store
    from scripts import zcode_import, zcode_stage

    mem, ses, _ = db_mod.init_databases(home / "data")
    fid = "tool-only-1"
    msgs = [
        {"timestamp": "2026-11-06T10:00:00Z", "role": "tool", "content": "some tool dump", "tool_name": "bash"},
        {"timestamp": "2026-11-06T10:01:00Z", "role": "tool", "content": "another dump", "tool_name": "read"},
    ]
    raw_store.write_new_file(
        file_id=fid, session_key="sess-tool", started_at="2026-11-06T10:00:00Z",
        agent_id="test", source_type="session", first_messages=msgs, metadata=None,
    )
    session_dao.insert_raw_file(
        ses, file_id=fid, path=raw_store.relative_path(fid),
        session_key="sess-tool", started_at="2026-11-06T10:00:00Z",
        status="new", size=raw_store.file_size(fid),
    )
    mem.close()
    ses.close()

    staging = home / "staging"
    zcode_stage.main(["--home", str(home), "--out", str(staging)])
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_tasks"] == 0
    assert manifest["empty_files"] == [fid]

    rc = zcode_import.main(["--home", str(home), "--staging", str(staging)])
    assert rc == 0
    import sqlite3

    s = sqlite3.connect(str(home / "data" / "session.db"))
    status = s.execute("SELECT status FROM raw_files WHERE file_id=?", (fid,)).fetchone()[0]
    assert status == "refined"
