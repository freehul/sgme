"""tests/test_security_log.py：安全审计日志叶子测试（T-259，ST-55，U5 全项）。

覆盖（方案 §四 U5 行 + §2.5 逐项）：
1. 字段齐：事件记录含 event/source/ts/key_ref 等约定字段；
2. 脱敏：日志文件内容无明文 key（mask_key 语义：前 6 + … + 后 2，≤8 全隐）；
3. 窗口去重：rate_limited/ip_limited 窗口内首条记全量、其后仅累计（不落盘）；
4. auth_fail 首条 + 阈值条、回环不落盘（is_loopback 入参语义）；
5. 写失败不阻塞：目录被文件占位构造失败 → 返回 False、WARNING、主链路不抛；
6. json 转义：含换行/引号的 UA 与消息单行不破；UA 截断 ≤200；
7. read_events：日期读 / limit 钳制 / 文件不存在空表 / 防御性日期校验；
8. cleanup_expired：keep_days=0 不清理、过期文件删除、近期文件保留。

时间全部外部注入（now 参数），零 sleep、零真实时钟依赖。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from sgme.security_log import (
    EVENT_AUTH_FAIL,
    EVENT_RATE_LIMITED,
    SecurityLog,
    mask_key,
)

# 固定时区锚点（测试零真实时钟依赖）
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)

# 编造假 key 样例（数据卫生：test- 前缀假串，与 tests/test_security.py 同款惯例——
# 不命中 pre-commit 密钥扫描的 token 形状，非真实凭据）
SAMPLE_KEY = "test-agent-key-security-log"


def mk(tmp_path, **kw) -> SecurityLog:
    """构造被测实例（log_dir 注入 tmp_path；时间源固定锚点）。"""
    return SecurityLog(tmp_path, clock=lambda: T0, **kw)


def read_lines(tmp_path, name: str) -> list[dict]:
    """直接读 jsonl 文件内容（测试断言走文件本体，不走读接口）。"""
    p = tmp_path / name
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ---------- 1. mask_key 脱敏语义（_mask_key 同款） ----------


class TestMaskKey:
    def test_long_key(self):
        # 16+ 字符：前 6 + … + 后 2（头尾可不同）
        assert mask_key(SAMPLE_KEY) == "test-a…og"  # scan-allow

    def test_short_key_fully_hidden(self):
        # ≤8 全隐
        assert mask_key("12345678") == "…"
        assert mask_key("short") == "…"

    def test_empty(self):
        # 空值不产指纹（_mask_key 参考语义：空串/None → ""）
        assert mask_key("") == ""
        assert mask_key(None) == ""

    @pytest.mark.parametrize(
        ("key", "expect"),
        [
            (SAMPLE_KEY, "test-a…og"),
            ("k" * 9, "kkkkkk…kk"),
            ("12345678", "…"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_semantics_match_reference(self, key, expect):
        # 与 server/app.py::_mask_key 同款语义（本地实现，防循环依赖）
        assert mask_key(key) == expect


# ---------- 2. 字段齐 + 事件七类可落盘 ----------


class TestFieldsAndEventTypes:
    def test_auth_fail_fields(self, tmp_path):
        log = mk(tmp_path)
        # scan-allow: 编造假 key 样例
        ok = log.record_auth_fail(
            "203.0.113.7",
            key=SAMPLE_KEY,
            reason="invalid_key",
            user_agent="UA",
            now=T0,
        )
        assert ok is True
        rec = read_lines(tmp_path, "security-20261011.jsonl")[0]
        for field in (
            "event",
            "source",
            "ts",
            "key_ref",
            "reason",
            "user_agent",
            "count",
        ):
            assert field in rec
        assert rec["event"] == "auth_fail"
        # ts 为 ISO 格式
        datetime.fromisoformat(rec["ts"])

    def test_all_seven_event_types(self, tmp_path):
        log = mk(tmp_path)
        assert log.record_auth_fail("203.0.113.7", key=SAMPLE_KEY, now=T0)
        assert log.record_rate_limited("203.0.113.7", now=T0)
        assert log.record_ip_limited("203.0.113.7", now=T0)
        assert log.record_banned("203.0.113.7", duration_sec=300, now=T0)
        assert log.record_ban_escalated(
            "203.0.113.7", prev_duration_sec=300, new_duration_sec=600, now=T0
        )
        assert log.record_ban_expired("203.0.143.9", now=T0)
        assert log.record_admin_op("unban", source="127.0.0.1", now=T0)
        events = [r["event"] for r in read_lines(tmp_path, "security-20261011.jsonl")]
        assert events == [
            "auth_fail",
            "rate_limited",
            "ip_limited",
            "banned",
            "ban_escalated",
            "ban_expired",
            "admin_op",
        ]


# ---------- 3. 脱敏：无明文 key 出现在日志文件内容 ----------


class TestNoPlaintextKeyOnDisk:
    def test_auth_fail_no_plaintext(self, tmp_path):
        log = mk(tmp_path)
        plaintext = "test-super-secret-key-0987654321"  # scan-allow: 编造假 key 样例
        log.record_auth_fail("203.0.113.7", key=plaintext, now=T0)
        content = (tmp_path / "security-20261011.jsonl").read_text(encoding="utf-8")
        assert plaintext not in content
        assert "key_ref" in content
        # 脱敏指纹照常生成（前 6 + … + 后 2）
        assert json.loads(content.splitlines()[0])["key_ref"] == "test-s…21"

    def test_status_counts_visible_without_disk(self, tmp_path):
        # 计数概览：不落盘也能从内存取（status 用）
        log = mk(tmp_path)
        for _ in range(5):
            log.record_auth_fail("203.0.113.7", now=T0, is_loopback=True)
        assert log.window_count(EVENT_AUTH_FAIL, "203.0.113.7", window_sec=300) == 5
        assert read_lines(tmp_path, "security-20261011.jsonl") == []


# ---------- 4. K4 写策略：窗口去重 / auth_fail 首条+阈值条 / 回环不落盘 ----------


class TestWindowDedup:
    def test_rate_limited_first_full_then_count_only(self, tmp_path):
        log = mk(tmp_path)
        src = "203.0.113.7"
        assert log.record_rate_limited(src, limit=120, now=T0) is True  # 首条记全量
        for _ in range(9):
            assert (
                log.record_rate_limited(src, limit=120, now=T0) is False
            )  # 其后仅累计
        recs = read_lines(tmp_path, "security-20261011.jsonl")
        assert len(recs) == 1
        assert recs[0]["count"] == 1
        # 内存计数仍在涨（status 概览可读）
        assert log.window_count(EVENT_RATE_LIMITED, src, window_sec=60) == 10

    def test_ip_limited_dedup_same_window(self, tmp_path):
        log = mk(tmp_path)
        src = "203.0.113.8"
        log.record_ip_limited(src, now=T0)
        log.record_ip_limited(src, now=T0)
        log.record_ip_limited(src, now=T0)
        recs = read_lines(tmp_path, "security-20261011.jsonl")
        assert len(recs) == 1 and recs[0]["event"] == "ip_limited"

    def test_new_window_logs_again(self, tmp_path):
        # 窗口滚动后重新记首条
        log = mk(tmp_path)
        src = "203.0.113.7"
        log.record_rate_limited(src, now=T0)
        # 窗口 60s：T0+61s 进新窗口
        assert log.record_rate_limited(src, now=T0 + timedelta(seconds=61)) is True
        recs = read_lines(tmp_path, "security-20261011.jsonl")
        assert len(recs) == 2

    def test_sources_isolated(self, tmp_path):
        # 不同来源互不影响去重窗口
        log = mk(tmp_path)
        log.record_rate_limited("203.0.113.7", now=T0)
        assert log.record_rate_limited("203.0.113.8", now=T0) is True
        assert len(read_lines(tmp_path, "security-20261011.jsonl")) == 2

    def test_auth_fail_threshold_line(self, tmp_path):
        # 首条 + 阈值条
        log = mk(tmp_path)
        src = "203.0.113.9"
        results = [log.record_auth_fail(src, now=T0, threshold=3) for _ in range(4)]
        # 第 1 条（首条）+ 第 3 条（触发阈值）落盘；第 2、4 条仅计数
        assert results == [True, False, True, False]
        recs = read_lines(tmp_path, "security-20261011.jsonl")
        assert [r["count"] for r in recs] == [1, 3]

    def test_auth_fail_threshold_not_relogged(self, tmp_path):
        # 同窗口内阈值条只记一次
        log = mk(tmp_path)
        src = "203.0.113.9"
        for i in range(6):
            log.record_auth_fail(src, now=T0, threshold=3)
        recs = read_lines(tmp_path, "security-20261011.jsonl")
        assert len(recs) == 2

    def test_loopback_not_logged(self, tmp_path):
        # 回环失败不逐条落盘（is_loopback 入参语义）
        log = mk(tmp_path)
        for _ in range(50):
            log.record_auth_fail("127.0.0.1", now=T0, is_loopback=True)
        assert read_lines(tmp_path, "security-20261011.jsonl") == []
        # 仅计数入内存（status 概览）
        assert log.window_count(EVENT_AUTH_FAIL, "127.0.0.1", window_sec=300) == 50


# ---------- 5. 写失败不阻塞（fail-open） ----------


class TestWriteFailureFailOpen:
    def test_dir_replaced_by_file(self, tmp_path):
        # log_dir 位置被普通文件占位 → mkdir/open 全失败 → 请求仍成功返回 False
        blocker = tmp_path / "blocked"
        blocker.write_text("not a dir", encoding="utf-8")
        log = SecurityLog(blocker, clock=lambda: T0)
        assert log.record_banned("203.0.113.7", duration_sec=60, now=T0) is False
        assert log.record_admin_op("x", now=T0) is False
        assert log.record_auth_fail("203.0.113.7", now=T0) is False
        # 主链路不抛、内存窗口状态正常维护
        assert log.window_count(EVENT_AUTH_FAIL, "203.0.113.7", window_sec=300) == 1

    def test_read_on_broken_dir_returns_empty(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a dir", encoding="utf-8")
        log = SecurityLog(blocker, clock=lambda: T0)
        assert log.read_events("2026-10-11") == []
        assert log.cleanup_expired(90) == 0


# ---------- 6. json 转义（单行不破） + UA 截断 ----------


class TestEscapingAndTruncation:
    def test_ua_with_newline_quote_escaped_single_line(self, tmp_path):
        log = mk(tmp_path)
        nasty_ua = 'evil"key\nlog forgery\u2028more'
        log.record_auth_fail("203.0.113.7", user_agent=nasty_ua, now=T0)
        raw = (tmp_path / "security-20261011.jsonl").read_text(encoding="utf-8")
        lines = raw.splitlines()
        assert len(lines) == 1  # 单行不破（换行被转义）
        rec = json.loads(lines[0])
        assert rec["user_agent"] == nasty_ua  # 语义无损（json 转义还原）

    def test_ua_truncated_to_200(self, tmp_path):
        log = mk(tmp_path)
        log.record_auth_fail("203.0.113.7", user_agent="A" * 500, now=T0)
        rec = read_lines(tmp_path, "security-20261011.jsonl")[0]
        assert len(rec["user_agent"]) == 200

    def test_reason_also_truncated(self, tmp_path):
        log = mk(tmp_path)
        log.record_auth_fail("203.0.113.7", reason="r" * 999, now=T0)
        rec = read_lines(tmp_path, "security-20261011.jsonl")[0]
        assert len(rec["reason"]) == 200

    def test_structural_fields_not_truncated(self, tmp_path):
        # source / event / ts 为结构字段不截断
        long_src = "2" * 300  # 非 IP 串（调用方可能传 ip_key 聚合串）
        log = mk(tmp_path)
        log.record_auth_fail(long_src, now=T0)
        recs = read_lines(tmp_path, "security-20261011.jsonl")
        assert recs[0]["source"] == long_src


# ---------- 7. read_events（D4 防御层） ----------


class TestReadEvents:
    def test_read_by_date(self, tmp_path):
        log = mk(tmp_path)
        log.record_banned("203.0.113.7", duration_sec=60, now=T0)
        log.record_ban_expired("203.0.113.7", now=T0 + timedelta(seconds=61))
        events = log.read_events("2026-10-11")
        assert [e["event"] for e in events] == ["banned", "ban_expired"]

    def test_file_absent_empty(self, tmp_path):
        assert mk(tmp_path).read_events("2026-01-01") == []

    def test_limit(self, tmp_path):
        log = mk(tmp_path)
        for i in range(5):
            log.record_banned(f"203.0.113.{i}", duration_sec=1, now=T0)
        assert len(log.read_events("2026-10-11", limit=2)) == 2
        # limit 钳制 ≤1000
        assert len(log.read_events("2026-10-11", limit=9999)) == 5

    def test_malformed_date_defensive(self, tmp_path):
        # 防穿越防御：非常规 date 一律空表（严格校验归路由层）
        log = mk(tmp_path)
        for bad in ("2026-10-11\r", "../../etc/passwd", "20261011", "abc", ""):
            assert log.read_events(bad) == []

    def test_bad_line_skipped(self, tmp_path):
        log = mk(tmp_path)
        log.record_banned("203.0.113.7", duration_sec=1, now=T0)
        p = tmp_path / "security-20261011.jsonl"
        p.write_text(p.read_text(encoding="utf-8") + "{broken json\n", encoding="utf-8")
        events = log.read_events("2026-10-11")
        assert len(events) == 1


# ---------- 8. cleanup_expired（Q4 保留清理） ----------


class TestCleanupExpired:
    def test_keep_days_zero_noop(self, tmp_path):
        log = mk(tmp_path)
        old = tmp_path / "security-20260101.jsonl"
        old.write_text("{}", encoding="utf-8")
        assert log.cleanup_expired(0) == 0
        assert old.exists()

    def test_expired_removed_recent_kept(self, tmp_path):
        log = mk(tmp_path)
        (tmp_path / "security-20260101.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "security-20261010.jsonl").write_text(
            "{}", encoding="utf-8"
        )  # T0 前一天，保留
        removed = log.cleanup_expired(90, now=T0)
        assert removed == 1
        assert not (tmp_path / "security-20260101.jsonl").exists()
        assert (tmp_path / "security-20261010.jsonl").exists()

    def test_non_logfile_untouched(self, tmp_path):
        # 只认 security-YYYYMMDD.jsonl 命名，他文件不动
        log = mk(tmp_path)
        (tmp_path / "security-20260101.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "agent_keys.json").write_text("{}", encoding="utf-8")
        (tmp_path / "security-notadate.jsonl").write_text("{}", encoding="utf-8")
        log.cleanup_expired(90, now=T0)
        assert (tmp_path / "agent_keys.json").exists()
        assert (tmp_path / "security-notadate.jsonl").exists()

    def test_daily_first_write_triggers_cleanup(self, tmp_path):
        # keep_days>0：每日首写惰性触发清理
        log = SecurityLog(tmp_path, clock=lambda: T0, keep_days=90)
        (tmp_path / "security-20260101.jsonl").write_text("{}", encoding="utf-8")
        log.record_banned("203.0.113.7", duration_sec=1, now=T0)
        assert not (tmp_path / "security-20260101.jsonl").exists()

    def test_daily_cleanup_once_per_day(self, tmp_path):
        # 同日多次写只触发一次清理
        log = SecurityLog(tmp_path, clock=lambda: T0, keep_days=90)
        log.record_banned("203.0.113.7", duration_sec=1, now=T0)
        (tmp_path / "security-20260101.jsonl").write_text("{}", encoding="utf-8")
        log.record_banned("203.0.113.8", duration_sec=1, now=T0)  # 同日第二写
        assert (tmp_path / "security-20260101.jsonl").exists()
