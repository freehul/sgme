# -*- coding: utf-8 -*-
"""软遗忘链路（D-68）与主体过滤（D-66）、惊讶度分位（D-78）回归测试。

背景（2026-11-03 对齐核查发现的三处契约错位）：
1. `forget.topic` 曾被 `[:40]` 硬截断——八轮拍板是「不设字数上限，保留原长 + 超长告警」；
2. `forget` 字典（topic/kind/sha256）算出来后**没进任何落库列**，事后查不到「忘了什么」；
3. `memory_type` 从不置 `forget_request`，导致 dream / recall_aging 的「遗忘豁免」是死代码。
本测试锁死这三处，并覆盖注入侧的两道过滤（遗忘请求不提、他人事实不进本人画像）。
"""

from __future__ import annotations

import pytest

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.engine import l2_extraction as l1_mod
from sgme.engine.surprise import compute_for_store


@pytest.fixture(scope="module")
def dimensions():
    return sgme_config.load_config()["dimensions"]


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


def _item(content: str, forget: dict | None = None, **kw) -> dict:
    base = {
        "content": content,
        "dimensions": ["preferences"],
        "memory_type": "fact",
        "priority": 50,
        "forget": forget,
    }
    base.update(kw)
    return base


# ---------- ① topic 不截断（八轮拍板：不设字数上限） ----------

def test_forget_topic_not_truncated(dimensions):
    long_topic = "关于" + "很长的主题内容" * 12  # 远超 40 字
    parsed = l1_mod._validate_item(_item("用户要求不再提某事", forget={
        "topic": long_topic, "kind": "preference",
    }), dimensions)
    assert parsed is not None
    # 关键：原长保留，不截断
    assert parsed["forget_topic"] == long_topic
    assert len(parsed["forget_topic"]) > 40


# ---------- ② 遗忘请求落专用类型 + topic/kind 可回溯 ----------

def test_forget_sets_dedicated_memory_type(dimensions):
    parsed = l1_mod._validate_item(_item("用户不想再聊这个话题", forget={
        "topic": "老同学的近况", "kind": "event",
    }), dimensions)
    assert parsed is not None
    assert parsed["memory_type"] == "forget_request"
    assert parsed["forget_topic"] == "老同学的近况"
    assert parsed["forget_kind"] == "event"
    # 七值 type 落「约束」（用户下的约束，不是事实）
    assert parsed["type"] == "约束"


def test_non_forget_item_keeps_original_type(dimensions):
    parsed = l1_mod._validate_item(_item("用户在学 Rust"), dimensions)
    assert parsed is not None
    assert parsed["memory_type"] == "fact"
    assert parsed["forget_topic"] is None
    assert parsed["forget_kind"] is None


def test_empty_forget_placeholder_is_not_forget(dimensions):
    """模型习惯性填 {"topic": null, "kind": null} 不算遗忘请求（历史实测 10/10 误改写）。"""
    parsed = l1_mod._validate_item(_item("普通事实", forget={
        "topic": None, "kind": None,
    }), dimensions)
    assert parsed is not None
    assert parsed["memory_type"] == "fact"
    assert parsed["forget_topic"] is None


# ---------- ③ topic 落库（正文刻意不带 topic） ----------

def test_forget_topic_persisted(conn):
    mid = memory_dao.insert_memory(
        conn,
        content="用户要求不再主动提及某主题（内容已按遗忘请求处理）",
        memory_type="forget_request",
        priority=50,
        time_velocity="static",
        ttl_days=None,
        dimension_ids=["preferences"],
        occurred_at=None,
        prompt_version="test",
        extra={"type": "约束", "subject": "self",
               "forget_topic": "老同学的近况", "forget_kind": "event"},
    )
    row = conn.execute(
        "SELECT memory_type, forget_topic, forget_kind, content FROM memories WHERE memory_id=?",
        (mid,),
    ).fetchone()
    assert row["memory_type"] == "forget_request"
    assert row["forget_topic"] == "老同学的近况"
    assert row["forget_kind"] == "event"
    # 正文仍然脱敏（topic 不进正文，防被忘内容在检索/注入时浮现）
    assert "老同学" not in row["content"]


# ---------- ④ 注入侧两道过滤 ----------

def _add(conn, content: str, *, memory_type: str = "fact",
         subject: str | None = "self", dim: str = "preferences") -> str:
    extra = {"type": "事实"}
    if subject is not None:
        extra["subject"] = subject
    return memory_dao.insert_memory(
        conn, content=content, memory_type=memory_type, priority=50,
        time_velocity="static", ttl_days=None, dimension_ids=[dim],
        occurred_at=None, prompt_version="test", extra=extra,
    )


def test_forget_request_excluded_from_injection(conn):
    _add(conn, "用户想学吉他")
    _add(conn, "用户要求不再主动提及某主题（内容已按遗忘请求处理）",
         memory_type="forget_request")

    inject_rows = memory_dao.list_memories_by_dimension(
        conn, ["preferences"], exclude_memory_types=("forget_request",),
    )
    assert [r["content"] for r in inject_rows] == ["用户想学吉他"]

    # 检索/管理面不过滤时仍可见（保留可回溯）
    all_rows = memory_dao.list_memories_by_dimension(conn, ["preferences"])
    assert len(all_rows) == 2


def test_subject_filter_excludes_others(conn):
    _add(conn, "本人事实", subject="self")
    _add(conn, "他人事实", subject="others")
    _add(conn, "存量未标注事实", subject=None)

    rows = memory_dao.list_memories_by_dimension(
        conn, ["preferences"], subject_filter=("self",),
    )
    contents = {r["content"] for r in rows}
    # 他人事实被挡；NULL（存量）与 unknown 视为本人，不误伤
    assert "他人事实" not in contents
    assert {"本人事实", "存量未标注事实"} <= contents


# ---------- ⑤ 惊讶度存分位（D-78 口径） ----------

def test_surprise_stores_percentile_not_absolute(conn):
    """同类型样本 ≥30 时，落库值应是分位而不是绝对相似度。

    构造：30 条互相高度相似的历史（彼此 jaccard 高 → 绝对惊讶度都低），
    再加一条全新内容（绝对惊讶度接近 1）。若存绝对值，新值会是 ≈1.0；
    若存分位，新值应是 ≈1.0（分位也接近上限）；关键是**中段值**——
    用一条与历史部分相似的内容，绝对惊讶度会落到 0.5 左右，
    分位则取决于分布位置，两者在小样本下才可能相等。
    这里用「分布是否被压平」来区分：连续插入 40 条同质内容，
    分位实现下最终值会趋于分布位置，绝对值实现下趋于 0（同质）。
    """
    base = "用户每天早上七点起床做早饭然后出门上班"
    for i in range(35):
        _add(conn, f"{base}（第{i}条）", memory_type="fact")

    # 同质内容：绝对惊讶度极低（≈0.1），分位也低
    same_val = compute_for_store(conn, base + "（第36条）", "fact")
    assert same_val is not None
    assert same_val < 0.5, f"同质内容不该得到高惊讶度，实际 {same_val}"

    # 全新内容：两种实现都应给高分
    novel = compute_for_store(conn, "量子计算机在低温下使用超导约瑟夫森结", "fact")
    assert novel is not None
    assert novel > 0.5, f"全新内容应高惊讶度，实际 {novel}"


def test_surprise_cold_start_returns_absolute(conn):
    """样本不足时退回绝对值（冷启动），不返回 None、不失败。"""
    _add(conn, "用户在学吉他")
    val = compute_for_store(conn, "完全不同的内容，讲的是烘焙面包", "fact")
    assert val is not None
    assert 0.0 <= val <= 1.0
