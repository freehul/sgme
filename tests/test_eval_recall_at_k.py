"""test_eval_recall_at_k：recall@k 指标单元测试（T-129）。

覆盖：基本命中、空相关集、零命中、重复相关、k 超出长度、聚合。
"""

from __future__ import annotations

from eval.metrics import aggregate_recall_at_k, compute_recall_at_k
from eval.models import RecallAtK


def test_basic_recall():
    predicted = ["a", "b", "c", "d"]
    relevant = ["a", "c"]
    r = compute_recall_at_k(predicted, relevant, ks=(1, 3, 5, 10))
    # a 在位置 1；c 在位置 3
    assert r[1] == 0.5          # 前 1 条仅命中 a（1/2）
    assert r[3] == 1.0          # 前 3 条命中 a,c（2/2）
    assert r[5] == 1.0
    assert r[10] == 1.0


def test_empty_relevant():
    # 空相关集必须返 0.0，禁止污染均值
    r = compute_recall_at_k(["a", "b"], [], ks=(1, 3, 5, 10))
    assert r == {1: 0.0, 3: 0.0, 5: 0.0, 10: 0.0}


def test_zero_hit():
    predicted = ["x", "y", "z"]
    relevant = ["a", "b"]
    r = compute_recall_at_k(predicted, relevant, ks=(1, 3, 5, 10))
    assert all(v == 0.0 for v in r.values())


def test_duplicate_relevant_counted_once():
    # predicted 中重复出现的相关文档只计一次
    predicted = ["a", "a", "b", "a"]
    relevant = ["a", "b"]
    r = compute_recall_at_k(predicted, relevant, ks=(1, 3, 5, 10))
    assert r[1] == 0.5      # 位置1命中 a（1/2）
    assert r[3] == 1.0      # 位置3 b 也命中（2/2）


def test_k_beyond_length_capped():
    # k 超过预测长度：按已命中数计算，不超 1.0
    predicted = ["a", "b"]
    relevant = ["a", "b", "c"]
    r = compute_recall_at_k(predicted, relevant, ks=(1, 3, 5, 10))
    assert r[1] == round(1 / 3, 4)
    assert r[3] == round(2 / 3, 4)      # 仅命中 2/3
    assert r[5] == round(2 / 3, 4)      # 超出长度不增长
    assert r[10] == round(2 / 3, 4)


def test_aggregate():
    per_q = [
        {1: 0.5, 3: 1.0, 5: 1.0, 10: 1.0},
        {1: 0.0, 3: 0.0, 5: 0.5, 10: 0.5},
    ]
    agg = aggregate_recall_at_k(per_q)
    assert isinstance(agg, RecallAtK)
    assert agg.query_count == 2
    assert agg.recall_at_1 == 0.25
    assert agg.recall_at_3 == 0.5
    assert agg.recall_at_5 == 0.75
    assert agg.recall_at_10 == 0.75


def test_aggregate_empty():
    agg = aggregate_recall_at_k([])
    assert agg.query_count == 0
    assert agg.recall_at_1 == 0.0
