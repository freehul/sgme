# -*- coding: utf-8 -*-
"""tests/test_persona_gate.py：D-48 人格沉淀四条件闸门回归。

四条件（设计 §3.8 / D-48）：
① 多轮 ≥3 会话（按 file_id 去重）
② 跨天 ≥2（按 occurred_at 日期去重）
③ 多场景 ≥2（COUNT(DISTINCT scene_key)，排除 unknown）
④ 来源可信且已发生（derivation ∈ fact/reconstructed 且 modality == done）

覆盖：
- four_condition_report 的四项计数与失败原因；
- legacy 字符串证据（1.x refine:<file_id>）不能证明 ②③ → 不通过；
- 不可信/未发生证据被剔除（不让推测/计划凑数）；
- persona_block 只注入通过闸门的特质；gate.enabled=false 可回退旧口径；
- 抽取器写结构化证据（会话/日期/场景/来源/模态）且同记忆去重；
- operations 注入主路径真实挂上「性格参考」块。
"""
from __future__ import annotations

from types import SimpleNamespace

from sgme import config as sgme_config
from sgme.data import db
from sgme.data import memory_dao
from sgme.data import persona_dao
from sgme.engine import persona_extract
from sgme.profile import persona_block
from sgme.operations import inject as inject_ops


def _ev(file_id="f1", day="2026-10-01", scene="identity:coding",
        derivation="fact", modality="done", memory_id="m1", hour="10"):
    return {
        "memory_id": memory_id,
        "file_id": file_id,
        "occurred_at": f"{day}T{hour}:00:00Z",
        "scene_key": scene,
        "derivation": derivation,
        "modality": modality,
    }


def _trait(refs, confidence=0.6, evidence_count=None):
    return {
        "trait_id": "t1",
        "dimension": "decision_style",
        "value": "成本敏感",
        "confidence": confidence,
        "evidence_count": len(refs) if evidence_count is None else evidence_count,
        "evidence_refs": refs,
        "scene_context": "general",
        "status": "active",
    }


# ---------- 闸门矩阵 ----------


def test_gate_passes_when_all_four_conditions_met():
    refs = [
        _ev("f1", "2026-10-01", "identity:coding", memory_id="m1"),
        _ev("f2", "2026-10-01", "family:desktop", memory_id="m2"),
        _ev("f3", "2026-10-02", "preferences:mobile", memory_id="m3"),
    ]
    report = persona_dao.four_condition_report(_trait(refs))
    assert report["passed"] is True
    assert (report["sessions"], report["days"], report["scenes"]) == (3, 2, 3)


def test_gate_fails_on_each_condition():
    # 只有 2 个会话
    r = persona_dao.four_condition_report(_trait([
        _ev("f1", "2026-10-01", "identity:coding", memory_id="m1"),
        _ev("f2", "2026-10-02", "family:desktop", memory_id="m2"),
    ]))
    assert not r["passed"] and any("会话" in x for x in r["reasons"])
    # 同一天
    r = persona_dao.four_condition_report(_trait([
        _ev("f1", "2026-10-01", "identity:coding", memory_id="m1"),
        _ev("f2", "2026-10-01", "family:desktop", memory_id="m2"),
        _ev("f3", "2026-10-01", "preferences:mobile", memory_id="m3"),
    ]))
    assert not r["passed"] and any("跨天" in x for x in r["reasons"])
    # 只有一个真实场景（unknown 不算）
    r = persona_dao.four_condition_report(_trait([
        _ev("f1", "2026-10-01", "identity:coding", memory_id="m1"),
        _ev("f2", "2026-10-02", "unknown", memory_id="m2"),
        _ev("f3", "2026-10-02", "identity:coding", memory_id="m3"),
    ]))
    assert not r["passed"] and any("多场景" in x for x in r["reasons"])


def test_gate_excludes_uncredible_or_unhappened_evidence():
    refs = [
        _ev("f1", "2026-10-01", "identity:coding", memory_id="m1"),
        _ev("f2", "2026-10-02", "family:desktop", derivation="inferred", memory_id="m2"),
        _ev("f3", "2026-10-03", "preferences:mobile", modality="planned", memory_id="m3"),
    ]
    r = persona_dao.four_condition_report(_trait(refs))
    assert r["credible"] == 1 and not r["passed"]
    assert any("会话" in x for x in r["reasons"])


def test_legacy_string_evidence_cannot_pass():
    r = persona_dao.four_condition_report(_trait(["refine:f1", "refine:f2", "refine:f3"]))
    assert not r["passed"]
    assert r["evidence"] == 0


def test_upsert_trait_dedupes_same_memory():
    conn = db.connect_memory.__wrapped__ if False else None  # 占位防 lint，不使用
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE persona_traits (trait_id TEXT PRIMARY KEY, dimension TEXT NOT NULL, "
        "value TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.0, "
        "evidence_count INTEGER NOT NULL DEFAULT 0, evidence_refs TEXT, "
        "scene_context TEXT NOT NULL DEFAULT 'general', status TEXT NOT NULL DEFAULT 'active', "
        "superseded_by TEXT, source TEXT NOT NULL DEFAULT 'rule', created_at TEXT, updated_at TEXT);"
    )
    ref = _ev(memory_id="m-same")
    t1 = persona_dao.upsert_trait(conn, "d", "v", evidence_ref=ref)
    t2 = persona_dao.upsert_trait(conn, "d", "v", evidence_ref=ref)
    assert t2["evidence_count"] == 1, "同一记忆不得重复计证"
    assert len(t2["evidence_refs"]) == 1
    conn.close()


# ---------- 注入块与抽取器 ----------


def _seed_memory(conn, mid, content, *, day, scene, derivation="fact", modality="done"):
    memory_dao.insert_memory(
        conn, content,
        memory_type="fact", priority=50, time_velocity="static", ttl_days=None,
        dimension_ids=["identity"], agent_tag="dsh", memory_id=mid,
        occurred_at=f"{day}T10:00:00Z",
        extra={"derivation": derivation, "modality": modality, "scene_key": scene},
    )


def test_persona_block_applies_gate_and_config_switch(tmp_path):
    conn = db.connect_memory(tmp_path)
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry (id, display_name, category, time_velocity, created_at) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    # 通过闸门的特质
    for i, (day, scene) in enumerate(
        [("2026-10-01", "identity:coding"), ("2026-10-02", "identity:desktop")]
    ):
        for j in range(2 if i else 1):
            persona_dao.upsert_trait(
                conn, "decision_style", "成本敏感",
                evidence_ref=_ev(f"f{i}{j}", day, scene, memory_id=f"m{i}{j}"),
            )
    block = persona_block.build_persona_block(conn, gate={"enabled": True})
    assert block is not None
    assert block["block"]["title"] == "性格参考"
    # 关掉闸门 + 降低证据门槛后，只有计数门槛即可（回退旧口径）
    legacy_conn = db.connect_memory(tmp_path / "second")
    legacy_conn.execute(
        "INSERT OR IGNORE INTO dimension_registry (id, display_name, category, time_velocity, created_at) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z')"
    )
    for k in range(3):
        persona_dao.upsert_trait(legacy_conn, "decision_style", "成本敏感",
                                 evidence_ref=f"refine:f{k}")
    assert persona_block.build_persona_block(
        legacy_conn, gate={"enabled": True}
    ) is None, "legacy 字符串证据不能过闸门"
    assert persona_block.build_persona_block(
        legacy_conn, gate={"enabled": False}
    ) is not None, "关闸门回退旧计数口径"
    conn.close()
    legacy_conn.close()


def test_extractor_records_structured_evidence(tmp_path):
    conn = db.connect_memory(tmp_path)
    conn.execute(
        "INSERT OR IGNORE INTO dimension_registry (id, display_name, category, time_velocity, created_at) "
        "VALUES ('identity','身份','静态','static','2026-10-01T00:00:00Z')"
    )
    conn.commit()
    _seed_memory(conn, "m1", "这次成本太高，账单要省", day="2026-10-01", scene="identity:coding")
    conn.commit()
    result = SimpleNamespace(
        file_id="f1",
        memories=[{"memory_id": "m1", "content": "这次成本太高，账单要省"}],
    )
    stats = persona_extract.extract_and_store(result, conn, {"persona": {"enabled": True}})
    assert stats["evidence_added"] >= 1
    rows = persona_dao.list_traits(conn, scene_context="general", min_confidence=0.0, limit=10)
    refs = [r for r in rows[0]["evidence_refs"] if isinstance(r, dict)]
    assert refs and refs[0]["scene_key"] == "identity:coding"
    assert refs[0]["occurred_at"].startswith("2026-10-01")
    assert refs[0]["derivation"] == "fact" and refs[0]["modality"] == "done"
    conn.close()


def test_operations_inject_attaches_persona_block(tmp_path):
    conn = db.connect_memory(tmp_path)
    cfg = sgme_config.load_config()
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    for i, (day, scene) in enumerate(
        [("2026-10-01", "identity:coding"), ("2026-10-02", "identity:desktop")]
    ):
        for j in range(2 if i else 1):
            persona_dao.upsert_trait(
                conn, "decision_style", "成本敏感",
                evidence_ref=_ev(f"f{i}{j}", day, scene, memory_id=f"m{i}{j}"),
            )
    conn.commit()
    cfg["persona"] = sgme_config._merge_persona_config({"gate": {"enabled": True}})
    res = inject_ops.inject(conn, cfg, custom_filter={"dimensions": ["identity"]})
    assert res.ok, res
    titles = [b.get("title") for b in res.data["blocks"]]
    assert "性格参考" in titles
    assert res.data["stats"]["persona_present"] is True
    conn.close()
