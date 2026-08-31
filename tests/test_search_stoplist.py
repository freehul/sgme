"""T-130 测试：查询侧停用词过滤（中英双语）+ 英文清理 + 空结果降级护栏。

覆盖：
- 单元：is_stopword / filter_stopwords（中英）
- 单元：_build_fts_query 过滤停用词 + 英文清理 + 全停用词回退
- 集成：合成语料上，stoplist 开启可滤除「纯停用词」干扰记忆、
      且目标记忆 recall@k 不劣化（内容词保留）
"""
from __future__ import annotations

import sqlite3

from sgme.data import db as db_mod
from sgme.data.search import init_fts, recall_routes
from sgme.data.search import stoplist as stoplist_mod
from sgme.data.search import _build_fts_query, _clean_en_term, _stoplist_enabled
from sgme.segment import segment


# ---------- 单元：停用词表 ----------

def test_is_stopword_en_zh():
    assert stoplist_mod.is_stopword("the")
    assert stoplist_mod.is_stopword("WHO")        # 英文小写归一
    assert stoplist_mod.is_stopword("的")
    assert stoplist_mod.is_stopword("哪个")
    assert not stoplist_mod.is_stopword("深圳")
    assert not stoplist_mod.is_stopword("frisbee")


def test_filter_stopwords_drops_noise_keeps_content():
    toks = ["谁", "在", "深圳", "玩", "飞盘", "吗"]
    out = stoplist_mod.filter_stopwords(toks)
    assert "深圳" in out and "飞盘" in out and "玩" in out
    assert "谁" not in out and "在" not in out and "吗" not in out


def test_filter_stopwords_empty_when_all_stopwords():
    assert stoplist_mod.filter_stopwords(["的", "了", "吗", "the"]) == []


# ---------- 单元：英文清理 + MATCH 构造 ----------

def test_clean_en_term_lowercase_and_collapse():
    assert _clean_en_term("NAS") == "nas"
    assert _clean_en_term("NAS  server") == "nas server"  # 折叠内部空白
    # 含 CJK 的不强制小写（避免误改中文），仅折叠空白
    assert _clean_en_term("深圳 NAS") == "深圳 nas"


def test_build_fts_query_filters_stopwords():
    q = "who played frisbee with a dog"
    m = _build_fts_query(q, use_stoplist=True)
    assert '"who"' not in m and '"with"' not in m and '"a"' not in m
    assert '"frisbee"' in m and '"played"' in m and '"dog"' in m
    assert " OR " in m


def test_build_fts_query_keeps_content_chinese():
    q = "谁带着狗去公园玩飞盘"
    m = _build_fts_query(q, use_stoplist=True)
    assert '"公园"' in m and '"飞盘"' in m
    assert '"谁"' not in m


def test_build_fts_query_all_stopwords_fallback_to_original():
    # 全停用词 → 不能空召回，回退原 token（仍 OR 连接，交 LIKE 兜底）
    q = "谁 和 在 吗"
    m = _build_fts_query(q, use_stoplist=True)
    assert m  # 非空
    assert " OR " in m
    assert '"谁"' in m  # 回退后保留


def test_build_fts_query_stoplist_toggle_off():
    q = "who played frisbee with a dog"
    m_on = _build_fts_query(q, use_stoplist=True)
    m_off = _build_fts_query(q, use_stoplist=False)
    assert '"who"' not in m_on
    assert '"who"' in m_off


def test_stoplist_enabled_cfg_default():
    assert _stoplist_enabled(None) is True
    assert _stoplist_enabled({}) is True
    assert _stoplist_enabled({"search": {"stoplist": {"enabled": False}}}) is False
    assert _stoplist_enabled({"search": {"stoplist": {"enabled": True}}}) is True


# ---------- 集成：合成语料 + recall_routes 双路对照 ----------

def _make_conn(tmp_path):
    conn = db_mod.connect_memory(tmp_path)
    init_fts(conn)
    return conn


def _insert(conn, mid, content):
    conn.execute(
        "INSERT INTO memories(memory_id,content,content_seg,status,memory_type,"
        "priority,time_velocity,created_at,updated_at,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (mid, content, segment(content), "active", "persona", 1, "static",
         "2026-08-31T00:00:00", "2026-08-31T00:00:00", "2026-08-31T00:00:00"),
    )


def test_stoplist_removes_pure_stopword_distractors(tmp_path):
    conn = _make_conn(tmp_path)
    _insert(conn, "m-t", "深圳 飞盘 俱乐部 吹吹风 常去")
    _insert(conn, "n1", "请问你知道这是什么地方吗")      # 纯停用词
    _insert(conn, "n2", "谁在哪个城市上班呢")            # 含停用词 + 内容词
    conn.commit()

    # 自然语句问句（内容词裹停用词）
    query = "请问 你 知道 谁 在 深圳 飞盘 吗"

    on_bm25, _v, _r = recall_routes(
        conn, query, limit=10,
        cfg={"search": {"vector": {"enabled": False}, "stoplist": {"enabled": True}}},
    )
    on_ids = [r["memory_id"] for r in on_bm25]

    off_bm25, _v2, _r2 = recall_routes(
        conn, query, limit=10,
        cfg={"search": {"vector": {"enabled": False}, "stoplist": {"enabled": False}}},
    )
    off_ids = [r["memory_id"] for r in off_bm25]

    # 目标记忆两臂均召回（recall 不劣化）
    assert "m-t" in on_ids
    assert "m-t" in off_ids
    # 开启后纯停用词干扰被滤除
    assert "n1" not in on_ids
    assert "n2" not in on_ids
    # 关闭后停用词污染结果集（n1 含 请问/你/知道/吗）
    assert "n1" in off_ids
    # 结果集更纯净（开启返回更少）
    assert len(on_ids) < len(off_ids)
    conn.close()


def test_stoplist_recall_preserved_on_real_content_query(tmp_path):
    """内容词 query（T-129 基线形态）两臂 recall@k 完全一致（不劣化）。"""
    from eval import metrics as eval_metrics

    conn = _make_conn(tmp_path)
    _insert(conn, "m-t", "深圳 飞盘 俱乐部 吹吹风 常去")
    conn.commit()
    query = "深圳 飞盘"

    on_bm25, _, _ = recall_routes(
        conn, query, limit=10,
        cfg={"search": {"vector": {"enabled": False}, "stoplist": {"enabled": True}}},
    )
    off_bm25, _, _ = recall_routes(
        conn, query, limit=10,
        cfg={"search": {"vector": {"enabled": False}, "stoplist": {"enabled": False}}},
    )
    on_rec = eval_metrics.compute_recall_at_k([r["memory_id"] for r in on_bm25], ["m-t"])
    off_rec = eval_metrics.compute_recall_at_k([r["memory_id"] for r in off_bm25], ["m-t"])
    assert on_rec == off_rec  # 内容词不被误删
    assert on_rec[1] == 1.0
    conn.close()
