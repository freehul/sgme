# -*- coding: utf-8 -*-
"""surprise / 惊讶度先验测试（设计决策 D-78）。

覆盖：中文 2-gram 分词、英文小写空格切分、标点去除、jaccard 边界
（都空 / 完全相同 / 完全不同 / 部分重叠）、surprise 值域 0-1、
同类型比对与类型过滤、仅 active 参与、冷启动样本不足 confidence=low、
全新内容 surprise≈1.0、batch_backfill 统计 / limit / 不写库。
"""
from __future__ import annotations

import sqlite3

import pytest

from sgme.data import db
from sgme.engine.surprise import batch_backfill, compute_surprise, jaccard, tokenize


# ---------- 测试库构造 ----------

def _conn(tmp_path) -> sqlite3.Connection:
    return db.connect_memory(tmp_path)


def _mem(conn, mid, content, memory_type="fact", status="active",
         updated_at="2026-01-01T00:00:00Z") -> None:
    conn.execute(
        "INSERT INTO memories (memory_id, content, memory_type, priority, time_velocity, "
        "ttl_days, created_at, updated_at, status, occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mid, content, memory_type, 50, "dynamic", None,
         updated_at, updated_at, status, updated_at),
    )
    conn.commit()


def _seed_disjoint(conn, n=40, memory_type="fact", prefix="m") -> None:
    """造 n 条同类型、两两词法不相交的记忆（每条独占一个汉字，bigram 零重叠）。"""
    for i in range(n):
        c = chr(0x4E00 + i)  # U+4E00 起：一、丁、……各条独占，互不重叠
        _mem(conn, f"{prefix}{i}", c * 3, memory_type=memory_type)


# ---------- tokenize ----------

def test_tokenize_chinese_bigram():
    assert tokenize("记忆引擎") == {"记忆", "忆引", "引擎"}


def test_tokenize_english_lowercased_by_space():
    assert tokenize("Hello World") == {"hello", "world"}


def test_tokenize_punctuation_stripped():
    # 中英文标点一律去掉，不产生 token
    assert tokenize("你好，世界！") == {"你好", "世界"}
    assert tokenize("Hello, world!") == {"hello", "world"}
    assert tokenize("。，！？") == set()


def test_tokenize_mixed_and_digits():
    # 中英混排：各自成段；数字并入英文段整体小写
    assert tokenize("用Python3写代码") == {"用", "python3", "写代", "代码"}


def test_tokenize_single_chinese_char_fallback():
    # 单个汉字无法组 2-gram，保留单字 token（避免整段信息丢失）
    assert tokenize("我") == {"我"}


def test_tokenize_empty_and_returns_set():
    assert tokenize("") == set()
    assert isinstance(tokenize("abc 你好"), set)


# ---------- jaccard ----------

def test_jaccard_both_empty_is_zero():
    # 契约明确：两者皆空返回 0.0（非数学上的 1.0）
    assert jaccard(set(), set()) == 0.0


def test_jaccard_identical_is_one():
    assert jaccard({"a", "b"}, {"b", "a"}) == 1.0


def test_jaccard_disjoint_is_zero():
    assert jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_partial_overlap():
    assert jaccard({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(0.5)


# ---------- compute_surprise ----------

def test_surprise_identical_content_is_zero(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    x = "今天下午三点开会讨论项目进度安排"
    _mem(conn, "same", x)
    res = compute_surprise(conn, x, "fact")
    assert res["surprise"] == 0.0
    assert res["compared"] == 41
    assert res["confidence"] == "normal"


def test_surprise_brand_new_content_near_one(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    x = "量子纠缠实验设备校准完成时间确定"  # 与 40 条种子词法零重叠
    res = compute_surprise(conn, x, "fact")
    assert 0.99 <= res["surprise"] <= 1.0


def test_surprise_confidence_low_when_few_samples(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 10)
    res = compute_surprise(conn, "全新的一条记忆内容", "fact")
    assert res["confidence"] == "low"
    assert res["compared"] == 10


def test_surprise_confidence_normal_at_threshold(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 30)  # compared=30 不小于 30 → normal
    res = compute_surprise(conn, "全新的一条记忆内容", "fact")
    assert res["confidence"] == "normal"
    assert res["compared"] == 30


def test_surprise_value_in_unit_range(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    for content in ("完全相同", "", "abc def", "记忆记忆记忆"):
        res = compute_surprise(conn, content, "fact")
        assert 0.0 <= res["surprise"] <= 1.0


def test_surprise_empty_db(tmp_path):
    conn = _conn(tmp_path)
    res = compute_surprise(conn, "任意内容", "fact")
    assert res == {"surprise": 1.0, "confidence": "low", "compared": 0}


def test_surprise_filters_by_memory_type(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40, memory_type="fact")
    x = "今天下午三点开会讨论项目进度安排"
    _mem(conn, "epi1", x, memory_type="episodic")
    _mem(conn, "epi2", x + "补充", memory_type="episodic")
    # fact 池里没有相似内容 → 惊讶；episodic 池里有完全相同 → 不惊讶
    assert compute_surprise(conn, x, "fact")["surprise"] == 1.0
    assert compute_surprise(conn, x, "episodic")["surprise"] == 0.0
    # memory_type=None → 不按类型过滤，与全库 active 比对（含相同 episodic）
    assert compute_surprise(conn, x, None)["surprise"] == 0.0


def test_surprise_ignores_non_active(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    x = "今天下午三点开会讨论项目进度安排"
    _mem(conn, "rej", x, status="rejected")
    res = compute_surprise(conn, x, "fact")
    assert res["surprise"] == 1.0
    assert res["compared"] == 40  # rejected 不进比对池


def test_surprise_sample_limit_caps_compared(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    res = compute_surprise(conn, "全新内容", "fact", sample_limit=10)
    assert res["compared"] == 10
    assert res["confidence"] == "low"  # 截断后样本不足


def test_surprise_result_shape(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 5)
    res = compute_surprise(conn, "内容", "fact")
    assert set(res.keys()) == {"surprise", "confidence", "compared"}
    assert isinstance(res["surprise"], float)
    assert isinstance(res["compared"], int)


# ---------- batch_backfill ----------

def test_backfill_stats_all_normal(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    assert batch_backfill(conn) == {"computed": 40, "low_confidence": 0}


def test_backfill_counts_low_confidence(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 10)
    assert batch_backfill(conn) == {"computed": 10, "low_confidence": 10}


def test_backfill_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    assert batch_backfill(conn, limit=10) == {"computed": 10, "low_confidence": 0}


def test_backfill_only_active_counted(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    _mem(conn, "rej", "被拒绝的记忆内容", status="rejected")
    assert batch_backfill(conn) == {"computed": 40, "low_confidence": 0}


def test_backfill_does_not_write(tmp_path):
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 40)
    before = conn.execute(
        "SELECT memory_id, content, updated_at FROM memories ORDER BY memory_id"
    ).fetchall()
    batch_backfill(conn)
    after = conn.execute(
        "SELECT memory_id, content, updated_at FROM memories ORDER BY memory_id"
    ).fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_backfill_empty_db(tmp_path):
    conn = _conn(tmp_path)
    assert batch_backfill(conn) == {"computed": 0, "low_confidence": 0}


def test_backfill_matches_compute_surprise(tmp_path):
    # 同口径一致性：库里有完全相同内容时，逐条置信不受自身干扰（排除自身）
    conn = _conn(tmp_path)
    _seed_disjoint(conn, 35)
    _mem(conn, "dup", "今天下午三点开会讨论项目进度安排")
    assert batch_backfill(conn) == {"computed": 36, "low_confidence": 0}
