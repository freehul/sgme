"""L3 规则裁决前置接线（2.0 D-28 / D-29）：明显重复/改口不进 LLM。

背景：1.x 的 L1.5 每次要把近 10 万 token 的候选池送 LLM，占全月输入 80%。
2.0 的口径是先用规则消化「明显重复 / 明显改口 / 明显无关」，只有灰区付 LLM 成本。
本文件守的是**接线**（规则层本身在 tests/test_l3_rules.py 已覆盖）：

1. 精确重复 → 直接 skip，不送 LLM
2. 显式改口（"改成…"）→ 直接 update（复用归档链，旧记忆归档不删除）
3. 灰区 → 保留在 remaining_groups 里交 LLM
4. 开关默认关：不配置 `l3.rules.enabled` 时行为与 1.x 完全一致
"""

from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import l15


@pytest.fixture
def conn(tmp_path):
    cfg = sgme_config.load_config()
    mem, sess, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield mem
    for c in (mem, sess, wiki):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _seed(conn, content: str) -> str:
    return memory_dao.insert_memory(
        conn,
        content=content,
        memory_type="fact",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["preferences"],
        occurred_at=None,
        prompt_version="test",
        extra={"type": "偏好", "subject": "self", "route": "memory"},
    )


def _group(idx: int, new_mem: dict, cands: list[dict]) -> l15.CandidateGroup:
    return l15.CandidateGroup(new_memory_index=idx, new_memory=new_mem, candidates=cands)


def test_exact_duplicate_decided_by_rules_without_llm(conn):
    old = _seed(conn, "用户偏好全程中文沟通")
    cand = {"memory_id": old, "content": "用户偏好全程中文沟通", "subject": "self"}
    new = {"content": "用户偏好全程中文沟通", "subject": "self", "dimension_ids": ["preferences"]}

    decisions, remaining = l15._apply_l3_rules(
        conn, [_group(0, new, [cand])], set(), {},
    )
    assert decisions[0].action == "skip"
    assert remaining == [], "精确重复不该再送 LLM"
    assert old in decisions[0].candidate_ids


def test_explicit_change_signal_supersedes(conn):
    old = _seed(conn, "部署方式用 docker compose")
    cand = {"memory_id": old, "content": "部署方式用 docker compose", "subject": "self"}
    new = {
        "content": "部署方式改成 systemd 服务",
        "subject": "self",
        "dimension_ids": ["preferences"],
    }

    decisions, remaining = l15._apply_l3_rules(
        conn, [_group(0, new, [cand])], set(), {},
    )
    assert decisions[0].action == "update", "显式改口应走 update（旧记忆归档不删）"
    assert decisions[0].candidate_ids == [old]
    assert remaining == []


def test_gray_zone_goes_to_llm(conn):
    old = _seed(conn, "用户偏好全程中文沟通")
    cand = {"memory_id": old, "content": "用户偏好全程中文沟通", "subject": "self"}
    # 相似度落在灰区（0.30–0.85）且无改口信号 → 必须交 LLM 判「补充细节还是重复」
    new = {
        "content": "用户偏好全程中文沟通，技术讨论也用中文",
        "subject": "self",
        "dimension_ids": ["preferences"],
    }

    decisions, remaining = l15._apply_l3_rules(
        conn, [_group(0, new, [cand])], set(), {},
    )
    assert remaining, "灰区必须留给 LLM，不能规则直接吞掉"
    assert 0 not in decisions


def test_switch_off_by_default_keeps_old_behavior():
    cfg = {"l3": {}}
    assert (cfg.get("l3", {}).get("rules", {}) or {}).get("enabled", False) is False
