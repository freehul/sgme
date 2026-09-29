"""T-225（0930 深度审查 P0-4）：LIKE 兜底 SQL OR/AND 优先级缺陷回归 + 双保险。

缺陷（修复前形态）`sgme/data/search/__init__.py::_search_like_fallback`：
1. `like_clauses` 多个 `content LIKE ?` 用 OR 拼接后直接拼 `AND status != 'rejected'`
   —— SQLite 运算符优先级 AND > OR，`A OR B OR C AND D` 被解析为
   `A OR B OR (C AND D)`：**前 N-1 条 OR 臂完全绕过 status（及维度）过滤**，
   rejected 记忆经兜底被错误召回（触发链：FTS 路过滤 rejected 后空召回 →
   兜底守卫「过滤后为空」触发 → 兜底把 rejected 捞回）。
2. `match="all"` + dimensions 时兜底只做「命中任一维度」的 IN 子查询，
   与 FTS 路径（`GROUP BY memory_id HAVING COUNT(DISTINCT dimension_id)=N`）
   的 all 语义不一致——只带部分维度的记忆也被召回。

修复（本文件的回归线）：
① OR 组显式括号；② match=all 补 HAVING 全维度语义（无 dimensions 时行为不变）；
③ 返回前 `_drop_rejected` Python 侧复核 status（双保险，防同类拼接回归）。

用例形态对齐 tests/test_search_v04.py / test_operations_search.py：真实 sqlite +
init_fts，隔离 tmp_path，零网络。
"""
from __future__ import annotations

import pytest

from sgme import config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.search import (
    _build_fts_query,
    _search_like_fallback,
    _search_no_dims,
    init_fts,
    recall_routes,
    search_memories,
)

CONTENT = "参加团队复盘会议，每周日带女儿去兴趣班。"

# 端到端兜底触发串：分词结果 ["队复", "xqzz"]——「队复」横跨 content_seg
# 的 团队|复盘 切分边界（索引无此 token）→ FTS 空召回；LIKE '%队复%' 子串命中。
E2E_QUERY = "队复 xqzz"


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def mem_conn(tmp_path, cfg):
    """memory.db + registry + FTS5（照 test_search_v04.py 夹具模式）。"""
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    init_fts(conn)
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


def _dims(mem_conn) -> list[str]:
    return [d["id"] for d in memory_dao.list_dimensions(mem_conn, active_only=True)]


def _insert(mem_conn, dim_ids, content: str = CONTENT) -> str:
    return memory_dao.insert_memory(
        mem_conn, content=content, memory_type="fact", priority=3,
        time_velocity="static", ttl_days=None, dimension_ids=list(dim_ids),
    )


def _reject(mem_conn, memory_id: str) -> None:
    memory_dao.reject_memory(mem_conn, memory_id, "T-225 回归：用户纠错")


def _ids(rows) -> set[str]:
    return {r["memory_id"] for r in rows}


# ---------- ① 首词命中 rejected：首条 OR 臂绕过 status 过滤 ----------

def test_like_fallback_first_term_arm_not_bypassing_status(mem_conn):
    """多词查询且**首词**命中 rejected 行 → 不得召回；活跃同内容记忆照常召回。

    修复前：SQL 解析为 `臂1 OR (臂2 AND status != 'rejected')`——rejected 被臂1
    直接放行（生产 40/40 必现形态）。
    """
    dims = _dims(mem_conn)
    zombie = _insert(mem_conn, dims[:2])
    _reject(mem_conn, zombie)
    live = _insert(mem_conn, dims[:2])

    rows = _search_like_fallback(mem_conn, "兴趣班 zzqqxx", None, "any", 10)
    ids = _ids(rows)
    assert zombie not in ids, "rejected 记忆经首条 OR 臂泄漏"
    assert live in ids, "对照组（活跃同内容）应被召回——防修复矫枉过正"


# ---------- ② 中词命中 rejected：中间 OR 臂同样不得绕过 ----------

def test_like_fallback_middle_term_arm_not_bypassing_status(mem_conn):
    """三词查询、命中词在中间 → 该 OR 臂同样不得绕过 status 过滤。"""
    dims = _dims(mem_conn)
    zombie = _insert(mem_conn, dims[:2])
    _reject(mem_conn, zombie)
    live = _insert(mem_conn, dims[:2])

    rows = _search_like_fallback(mem_conn, "zzqq1 兴趣班 yyww2", None, "any", 10)
    ids = _ids(rows)
    assert zombie not in ids, "rejected 记忆经中间 OR 臂泄漏"
    assert live in ids, "对照组（活跃同内容）应被召回"


# ---------- ③ dimensions + match=all：不得被绕过，且 all 语义与 FTS 路径对齐 ----------

def test_like_fallback_dims_match_all_not_bypassed(mem_conn):
    """dimensions + match=all：rejected 与「部分维度」活跃记忆都不得返回；
    全维度活跃记忆必须返回（all 语义与 FTS 路径 HAVING 对齐）。

    修复前双缺陷叠加：OR 臂绕过维度子查询（rejected 泄漏）+ 缺 HAVING
    （只带部分维度的活跃记忆被 all 召回）。
    """
    d1, d2 = _dims(mem_conn)[:2]
    zombie_full = _insert(mem_conn, [d1, d2])
    _reject(mem_conn, zombie_full)
    zombie_partial = _insert(mem_conn, [d1])
    _reject(mem_conn, zombie_partial)
    live_full = _insert(mem_conn, [d1, d2])
    live_partial = _insert(mem_conn, [d1])

    all_ids = _ids(_search_like_fallback(mem_conn, "兴趣班 zzqqxx", [d1, d2], "all", 10))
    assert zombie_full not in all_ids, "rejected（全维度）经 all 路径泄漏"
    assert zombie_partial not in all_ids, "rejected（部分维度）经 all 路径泄漏"
    assert live_partial not in all_ids, "match=all 不得召回只带部分维度的记忆（缺 HAVING）"
    assert live_full in all_ids, "对照组（活跃全维度）应被召回"

    # match=any 语义不变：部分维度活跃记忆照常召回，rejected 仍被过滤
    any_ids = _ids(_search_like_fallback(mem_conn, "兴趣班 zzqqxx", [d1, d2], "any", 10))
    assert live_partial in any_ids, "match=any 语义回归（部分维度应召回）"
    assert zombie_full not in any_ids
    assert zombie_partial not in any_ids


# ---------- ④ 端到端：FTS 空召回 → 兜底路径 ----------

def test_recall_routes_end_to_end_fallback_not_recalling_rejected(mem_conn, session_conn):
    """FTS 真空召回 → 触发 LIKE 兜底：rejected 不得经该路径泄漏，活跃记忆须可召回。

    前置断言 FTS 空（现状如此，非 mock）：查询词横跨 content_seg 分词边界。
    """
    dims = _dims(mem_conn)
    zombie = _insert(mem_conn, dims[:2])
    _reject(mem_conn, zombie)
    live = _insert(mem_conn, dims[:2])

    # 前置：FTS 确为空召回（否则本用例没有覆盖兜底路径）
    assert _search_no_dims(mem_conn, _build_fts_query(E2E_QUERY), 10) == [], "FTS 未空召回，用例失效"

    bm25, _vec, _routes = recall_routes(mem_conn, E2E_QUERY, cfg=None)
    bm25_ids = _ids(bm25)
    assert zombie not in bm25_ids, "rejected 记忆经端到端兜底路径泄漏"
    assert live in bm25_ids, "对照组（活跃同内容）应经兜底召回"

    rs = search_memories(mem_conn, session_conn, E2E_QUERY, limit=10, cfg=None)
    rs_ids = _ids(rs)
    assert zombie not in rs_ids, "rejected 记忆经 search_memories 泄漏"
    assert live in rs_ids, "对照组应经 search_memories 返回"


# ---------- ③ 双保险：返回前 Python 侧复核 status 独立生效 ----------

def test_drop_rejected_defense_layer_standalone(mem_conn):
    """防御层独立生效：即便 SQL 拼接将来再次回归（行已返回），
    `_drop_rejected` 复核也必须剔除 rejected、保留活跃行。"""
    from sgme.data.search import _drop_rejected

    dims = _dims(mem_conn)[:1]
    zombie = _insert(mem_conn, dims)
    _reject(mem_conn, zombie)
    live = _insert(mem_conn, dims)

    leak_rows = [
        {"memory_id": zombie, "content": CONTENT},
        {"memory_id": live, "content": CONTENT},
    ]
    kept = _drop_rejected(mem_conn, leak_rows)
    assert [r["memory_id"] for r in kept] == [live]
    assert _drop_rejected(mem_conn, []) == []
