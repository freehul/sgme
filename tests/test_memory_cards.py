"""tests/test_memory_cards.py：core 记忆卡（D-79，2026-11-05）。

三层记忆分工的 core 层：稳定「人」属性、一槽一值、硬上限、每次注入必带。
"""

from __future__ import annotations

import pytest

from sgme.data import db as db_mod
from sgme.data import memory_card_dao as dao
from sgme.data.db import CARD_MAX_CHARS, SLOT_MAX_CHARS
from sgme.profile import core_cards


@pytest.fixture
def conn(tmp_path):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    yield mem_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


def test_upsert_and_read_back(conn):
    dao.upsert_slot(conn, "identity", "称呼", "测试用户", changed_by="user", reason="初始")
    card = dao.get_card(conn, "identity")
    assert card["slots"]["称呼"] == "测试用户"
    assert card["version"] == 1
    assert card["updated_by"] == "user"


def test_slot_overwrite_keeps_history(conn):
    """一槽一值：覆盖式更新，旧值进历史（不删可溯源）。"""
    dao.upsert_slot(conn, "identity", "城市", "北京", changed_by="user")
    dao.upsert_slot(conn, "identity", "城市", "上海", changed_by="user", reason="搬家")
    assert dao.get_card(conn, "identity")["slots"]["城市"] == "上海"

    hist = dao.list_history(conn, "identity")
    assert len(hist) == 2
    assert hist[0]["new_value"] == "上海"
    assert hist[0]["old_value"] == "北京"
    assert hist[0]["reason"] == "搬家"


def test_multi_slot_accepts_list(conn):
    """family.成员是多值槽。"""
    dao.upsert_slot(conn, "family", "成员", ["儿子 15 岁", "父亲 透析"], changed_by="user")
    assert dao.get_card(conn, "family")["slots"]["成员"] == ["儿子 15 岁", "父亲 透析"]


def test_single_slot_rejects_list(conn):
    with pytest.raises(ValueError):
        dao.upsert_slot(conn, "identity", "称呼", ["a", "b"], changed_by="user")


def test_slot_limit_rejected(conn):
    with pytest.raises(dao.CardLimitError):
        dao.upsert_slot(conn, "identity", "称呼", "x" * (SLOT_MAX_CHARS + 1), changed_by="user")


def test_card_limit_rejected(conn):
    """单卡总字数上限。principles 六个槽位各塞满 → 超 400。"""
    slots = ["诚实", "计划", "报备", "原件", "检索", "安装"]
    with pytest.raises(dao.CardLimitError):
        for s in slots:
            dao.upsert_slot(conn, "principles", s, "y" * 100, changed_by="user")


def test_unknown_card_or_slot(conn):
    with pytest.raises(ValueError):
        dao.upsert_slot(conn, "nope", "x", "v", changed_by="user")
    with pytest.raises(ValueError):
        dao.upsert_slot(conn, "identity", "nope", "v", changed_by="user")


def test_clear_slot(conn):
    dao.upsert_slot(conn, "identity", "城市", "上海", changed_by="user")
    dao.upsert_slot(conn, "identity", "城市", None, changed_by="user", reason="清除")
    assert "城市" not in dao.get_card(conn, "identity")["slots"]


def test_render_empty_returns_none(conn):
    assert core_cards.build_core_block(conn) is None


def test_render_contains_filled_cards_only(conn):
    dao.upsert_slot(conn, "identity", "称呼", "测试用户", changed_by="user")
    dao.upsert_slot(conn, "family", "成员", ["儿子 15 岁"], changed_by="user")
    block = core_cards.build_core_block(conn)
    assert block is not None
    ids = [i["card_id"] for i in block["block"]["items"]]
    assert ids == ["identity", "family"]          # 只出填过的卡，且按 schema 顺序
    assert block["block"]["title"] == "👤 关于你"
    assert block["tokens"] > 0
    text = core_cards.render_plain(block)
    assert "测试用户" in text and "儿子 15 岁" in text


def test_list_cards_marks_filled(conn):
    dao.upsert_slot(conn, "identity", "称呼", "测试用户", changed_by="user")
    cards = dao.list_cards(conn)
    assert len(cards) == 6                        # schema 六张卡全列出
    filled = [c["card_id"] for c in cards if c["filled"]]
    assert filled == ["identity"]
