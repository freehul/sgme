# -*- coding: utf-8 -*-
"""T-139 测试：Guardrail 敏感信息过滤层。

覆盖：规则检测（身份证/手机号/银行卡/密钥/邮箱/内网 IP）/ 脱敏 / 决策三模式 /
search 召回后过滤（默认关灰度）。
"""

from __future__ import annotations

import sqlite3

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao
from sgme.data import search as search_mod
from sgme.operations import guardrail


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    cfg = config.load_config()
    c = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(c, cfg["dimensions"], cfg["aliases"])
    yield c
    db_mod.close(c)


# ---------- 规则检测 ----------

def test_detect_hits():
    assert "id_card" in guardrail.detect("用户身份证号 110105199003078888")
    assert "phone" in guardrail.detect("联系手机 13812345678")
    assert "bank_card" in guardrail.detect("银行卡 6222021234567890123")
    assert "api_key" in guardrail.detect("密钥 sk-ccf27ee1ca004400ae20169c0e557454")
    assert "email" in guardrail.detect("邮箱 leo@example.com")
    assert "private_ip" in guardrail.detect("部署在 192.168.10.10 的 NAS")


def test_detect_clean_text_empty():
    assert guardrail.detect("今天天气不错，去公园散步") == []
    assert guardrail.detect("") == []
    assert guardrail.detect(None) == []


def test_email_rule_no_npm_version_false_positive():
    """2026-08-31 生产抽检实锤误报：pnpm@11.21.0 被 email 规则命中。

    收紧后域名段必须以字母开头——npm 版本号（数字开头）不再误判，
    真实邮箱（含子域名/后缀路径/数字前缀）仍命中。
    """
    assert "email" not in guardrail.detect("用 node24 执行 pnpm@11.21.0 安装")
    assert "email" not in guardrail.detect("升级到 package@2.1.0")
    assert "email" not in guardrail.detect("npm i @scope/pkg@1.0.0")
    # 真实邮箱仍命中
    assert "email" in guardrail.detect("联系 leo@example.com")
    assert "email" in guardrail.detect("user.name+tag@mail.example.co.uk")
    assert "email" in guardrail.detect("13812345678@qq.com")


def test_mask():
    masked, hits = guardrail.mask("手机 13812345678 和邮箱 leo@example.com")
    assert "13812345678" not in masked and "leo@example.com" not in masked
    assert "***" in masked
    assert "phone" in hits and "email" in hits


# ---------- 决策 ----------

def test_decision_default_off():
    """默认关（灰度）：任何内容 pass，行为与 T-139 前一致。"""
    action, text, matched = guardrail.decision({}, "手机 13812345678")
    assert action == guardrail.ACTION_PASS and matched == []
    assert guardrail.decision({"enabled": False}, "密钥 sk-abc")[0] == guardrail.ACTION_PASS


def test_decision_block():
    grd = {"enabled": True, "write_mode": "block"}
    action, _, matched = guardrail.decision(grd, "手机 13812345678")
    assert action == guardrail.ACTION_BLOCK and "phone" in matched
    # 干净文本 pass
    assert guardrail.decision(grd, "普通记忆内容")[0] == guardrail.ACTION_PASS


def test_decision_mask():
    grd = {"enabled": True, "write_mode": "mask"}
    action, masked, matched = guardrail.decision(grd, "手机 13812345678")
    assert action == guardrail.ACTION_MASK
    assert "13812345678" not in masked and "***" in masked
    assert matched == ["phone"]


# ---------- search 召回后过滤 ----------

def _ins(conn, content):
    return memory_dao.insert_memory(
        conn, content=content, memory_type="persona", priority=60,
        time_velocity="static", ttl_days=None, dimension_ids=["goals"],
    )


def test_search_guardrail_default_off(conn):
    """默认关：敏感记忆正常返回（灰度，行为不变）。"""
    _ins(conn, "alpha 手机 13812345678")
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False,
        cfg={"search": {"vector": {"enabled": False}}},
    )
    assert len(res) == 1


def test_search_guardrail_filter(conn):
    _ins(conn, "alpha 手机 13812345678 敏感")
    _ins(conn, "alpha 普通记忆")
    cfg = {
        "search": {"vector": {"enabled": False}},
        "guardrail": {"enabled": True, "read_mode": "filter"},
    }
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False, cfg=cfg,
    )
    assert len(res) == 1
    assert "13812345678" not in res[0]["content"]


def test_search_guardrail_read_off(conn):
    """enabled 但 read_mode=off → 不过滤。"""
    _ins(conn, "alpha 手机 13812345678")
    cfg = {
        "search": {"vector": {"enabled": False}},
        "guardrail": {"enabled": True, "read_mode": "off"},
    }
    res = search_mod.search_memories(
        conn, None, query="alpha", limit=10, include_sources=False, cfg=cfg,
    )
    assert len(res) == 1


def test_filter_guardrail_unit(conn):
    _ins(conn, "alpha 敏感手机 13812345678")
    mid2 = _ins(conn, "alpha 干净内容")
    results = [{"memory_id": "x", "content": "手机 13812345678"},
               {"memory_id": mid2, "content": "干净内容"}]
    cfg = {"guardrail": {"enabled": True, "read_mode": "filter"}}
    kept = search_mod._filter_guardrail(results, cfg)
    assert len(kept) == 1 and kept[0]["content"] == "干净内容"
    # 默认关 → 原样
    assert search_mod._filter_guardrail(results, {}) == results
