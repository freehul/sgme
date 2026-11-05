"""tests/test_refine_cost_alert.py：refine_cost 告警文案 + 进程内节流（F2，2026-11-05）。

背景：生产实测 signal_events 里 refine_cost 82 条 / 10 小时，且 message 仍写
「预期 zhipu 免费」——zhipu 已于 2026-08-29（B121）移出降级链，现行链是
agnes → deepseek → rule。F2 要做两件事：
1. 文案对齐现行降级链（不再指向已删除 provider）；
2. signal 事件按进程内 3600s 窗口节流（同一 source 最多每窗口 1 条），
   但**日志每次照常输出**（线索不丢，只压事件面）。

全部零真实 LLM：L1 走 monkeypatch，signal publish 走替身。
"""

from __future__ import annotations

import logging

import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.engine import refine as refine_mod
from sgme.raw import store


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    """每个用例重置模块级节流时间戳（等价新进程）。"""
    monkeypatch.setattr(refine_mod, "_refine_cost_alert_last_at", None)


@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


def _setup_raw_file(session_conn, file_id="f-cost"):
    msgs = [
        {"timestamp": "2026-11-05T13:00:00Z", "role": "user", "content": "我用 Python 写 SGME"},
        {"timestamp": "2026-11-05T13:01:00Z", "role": "assistant", "content": "了解"},
    ]
    store.write_new_file(
        file_id=file_id, session_key="sess_cost",
        started_at="2026-11-05T13:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key="sess_cost", started_at="2026-11-05T13:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


def _stub_deepseek_l1(monkeypatch):
    """L1 桩：固定 provider=deepseek（触发 refine_cost 告警分支），零 LLM。"""

    def fake_extract(*args, **kwargs):
        return ([{
            "content": "用户使用 Python 写 SGME",
            "memory_type": "persona",
            "priority": 70,
            "time_velocity": "static",
            "dimensions": ["技术栈"],
        }], "deepseek", {"stage": "l1_extraction", "version": "v019", "variant": None})

    monkeypatch.setattr(refine_mod.l2_extraction, "extract_memories", fake_extract)


def _capture_publish(monkeypatch):
    calls: list[dict] = []

    def fake_publish(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("sgme.signal.engine.publish", fake_publish)
    return calls


# ======================================================================
# 1. 节流判定窗口
# ======================================================================

def test_throttle_window_blocks_inside_and_allows_outside(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(refine_mod.time, "monotonic", lambda: clock["now"])

    assert refine_mod._throttle_refine_cost_alert() is True   # 首次放行
    clock["now"] += refine_mod.REFINE_COST_ALERT_INTERVAL_S - 1
    assert refine_mod._throttle_refine_cost_alert() is False  # 窗口内压住
    clock["now"] += 1
    assert refine_mod._throttle_refine_cost_alert() is True   # 窗口外恢复


def test_throttle_interval_is_one_hour():
    assert refine_mod.REFINE_COST_ALERT_INTERVAL_S == 3600.0


# ======================================================================
# 2. 集成：事件节流 + 日志每次都打 + 文案对齐现行链
# ======================================================================

def test_deepseek_alert_throttled_but_logged_every_time(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch, caplog
):
    fid = _setup_raw_file(session_conn)
    _stub_deepseek_l1(monkeypatch)
    published = _capture_publish(monkeypatch)

    clock = {"now": 5000.0}
    monkeypatch.setattr(refine_mod.time, "monotonic", lambda: clock["now"])

    with caplog.at_level(logging.WARNING, logger="sgme.engine.refine"):
        for _ in range(2):  # 第一次发布；第二次窗口内不发布
            result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
            assert result.status == "refined"
        clock["now"] += refine_mod.REFINE_COST_ALERT_INTERVAL_S + 1  # 跨窗口
        result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
        assert result.status == "refined"

    # 事件面：窗口内第二次不发布，窗口外恢复 → 共 2 条
    assert len(published) == 2
    first = published[0]
    assert first["event_type"] == "anomaly_warn"
    assert first["source"] == "refine_cost"
    payload = first["payload"]
    assert payload["provider"] == "deepseek"
    assert "agnes" in payload["message"]
    assert "agnes→deepseek→rule" in payload["message"]
    assert "zhipu" not in payload["message"], "文案不得再指向已移出链的 zhipu"

    # 日志面：每次实际使用 deepseek 都打（3 次），不随节流丢失
    logs = [r for r in caplog.records if "提炼走 deepseek" in r.getMessage()]
    assert len(logs) == 3
