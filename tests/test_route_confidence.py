"""tests/test_route_confidence.py：分流置信度契约（v0.3 S6，2026-11-14）。

验收：
1. route_confidence 缺省 1.0（v023 及更早没有该字段，不许因缺字段整批进 inbox）
2. 非法值 / 越界值归一
3. 低置信（<0.6）不进候选池，改写入 routing_inbox
4. 高置信照常进候选池
5. v024 提示词文件存在且含 route_confidence 锚点；candidate 版本不参与 active 判定（T-411）
"""

from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao, routing_inbox_dao
from sgme.engine import l2_extract as l2e
from sgme.engine import routing as routing_mod


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def conns(tmp_path):
    mem = db_mod.connect_memory(tmp_path)
    c = config.load_config()
    memory_dao.import_registry(mem, c["dimensions"], c["aliases"])
    skills = db_mod.connect_skills(tmp_path)
    yield mem, skills
    mem.close()
    skills.close()


def _enabled(cfg):
    import copy

    c = copy.deepcopy(cfg)
    c.setdefault("refine", {})["routing_enabled"] = True
    return c


def _add_memory(mem, mid, route="skill_candidate", content="调 LM Studio 要补 /v1 前缀"):
    now = "2026-11-14T00:00:00Z"
    mem.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity,"
        " created_at, updated_at, status, route)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, content, "fact", 50, "slow", now, now, "active", route),
    )
    mem.commit()


# ---------------- 归一 ----------------

def test_confidence_defaults_to_high():
    """缺字段 → 1.0（v023 兼容，绝不能默认 0）。"""
    assert l2e._route_confidence(None) == 1.0
    assert l2e._route_confidence("") == 1.0


def test_confidence_clamps_and_rejects_garbage():
    assert l2e._route_confidence(0.42) == 0.42
    assert l2e._route_confidence(-1) == 0.0
    assert l2e._route_confidence(9) == 1.0
    assert l2e._route_confidence("abc") == 1.0
    assert l2e._route_confidence("0.3") == 0.3


# ---------------- 分发语义 ----------------

def test_low_confidence_goes_to_inbox(conns, cfg):
    mem, skills = conns
    _add_memory(mem, "m-low")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-low", "route": "skill_candidate",
          "route_confidence": 0.35, "route_reason": "拿不准是偏好还是步骤",
          "content": "调 LM Studio 要补 /v1 前缀"}],
        mem, _enabled(cfg),
    )
    assert out["inbox"] == 1 and out["adopted"] == 0
    rows = routing_inbox_dao.list_items(mem, status="pending")
    assert len(rows) == 1
    assert rows[0]["source_ref"] == "m-low"
    assert rows[0]["route_guess"] == "skill_candidate"
    assert abs(float(rows[0]["confidence"]) - 0.35) < 1e-6
    # 没有进候选池
    assert skills.execute("SELECT COUNT(*) FROM skill_candidates").fetchone()[0] == 0


def test_high_confidence_still_adopts(conns, cfg):
    mem, skills = conns
    _add_memory(mem, "m-high")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-high", "route": "skill_candidate", "route_confidence": 0.92}],
        mem, _enabled(cfg),
    )
    assert out["adopted"] == 1 and out["inbox"] == 0
    assert skills.execute("SELECT COUNT(*) FROM skill_candidates").fetchone()[0] == 1


def test_missing_confidence_treated_as_high(conns, cfg):
    """v023 产出的记忆没有置信度字段 → 仍按原有路径进候选池（不回归）。"""
    mem, skills = conns
    _add_memory(mem, "m-legacy")
    out = routing_mod.dispatch_after_persist(
        [{"memory_id": "m-legacy", "route": "skill_candidate"}], mem, _enabled(cfg),
    )
    assert out["adopted"] == 1 and out["inbox"] == 0


# ---------------- 提示词资产 ----------------

def test_v024_prompt_registered_but_not_active(tmp_path):
    """v024 契约锚点仍在；candidate 版本不参与 active 判定（T-411 机制断言）。

    T-430：原断言钉死「真实 manifest 的 active == v023」，v026 切换后语义过期。
    改为在 tmp_path 造 manifest（不读真实 manifest）：放入 approved + candidate
    各一版，active 指向 approved，断言 candidate 不改变 active 判定。
    """
    import hashlib

    import yaml
    from pathlib import Path

    from sgme.prompts.manager import PromptStore

    root = Path(__file__).resolve().parents[1] / "sgme" / "resources" / "prompts"
    v024 = root / "versions" / "l1_extraction" / "v024.txt"
    assert v024.exists(), "v024 提示词文件缺失"
    text = v024.read_text(encoding="utf-8")
    assert "route_confidence" in text and "route_reason" in text

    # 临时提示词库：approved(v001) + candidate(v002)，active 只认 approved
    stage = "l1_extraction"
    work = "你是提取器。\n{{dimensions}}\n{{conversation}}"
    fake = tmp_path / "prompts"
    (fake / "versions" / stage).mkdir(parents=True)
    (fake / f"{stage}.txt").write_text(work, encoding="utf-8")
    (fake / "versions" / stage / "v001.txt").write_text(work, encoding="utf-8")
    (fake / "versions" / stage / "v002.txt").write_text(work + "\n候选版", encoding="utf-8")

    def _entry(ver: str, status: str) -> dict:
        path = fake / "versions" / stage / f"{ver}.txt"
        return {
            "version": ver,
            "file": f"versions/{stage}/{ver}.txt",
            "sha256": hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
            "created_at": "2026-11-20T00:00:00+08:00",
            "note": status,
            "status": status,
        }

    manifest = {
        "stages": {
            stage: {"active": "v001", "ab": {"enabled": False}},
            "l1_conflict": {"active": "@working", "ab": {"enabled": False}},
            "l2_scene": {"active": "@working", "ab": {"enabled": False}},
        },
        "versions": {stage: [_entry("v001", "approved"), _entry("v002", "candidate")]},
    }
    (fake / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )

    store = PromptStore(prompts_root=fake)
    infos = {i.version: i for i in store.list_versions(stage)}
    assert infos["v002"].status == "candidate"
    assert infos["v001"].status == "approved"
    # candidate 不参与 active 判定：active 仍是 approved 的 v001，get() 也只取 v001
    assert store.stage_config(stage)["active"] == "v001"
    assert store.get(stage).version == "v001"
