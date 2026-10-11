"""sgme/security_log.py：安全审计日志叶子（T-259，ST-55，方案 v3 §2.5）。

安全事件落盘可查（G3 收口）：JSONL 按天文件 + K4 量控写策略 + 脱敏 + 保留清理 + 只读接口。

纯叶子约束（D7）：
- **不 import config**——日志目录 / 保留天数由调用方注入（测试友好，无全局态）；
- 路径约定：``<log_dir>/security-YYYYMMDD.jsonl``（按天文件；log_dir 生产传 $SGME_HOME/logs）；
- 落地细节：写前 ``mkdir(parents=True, exist_ok=True)``；文件权限对齐 agent_keys.json
  收紧先例（POSIX 0600 尽力，Windows 下无害跳过）；``json.dumps`` 转义 + 自由文本字段
  截断 ≤200（UA 防注入换行，单行不破）；
- key_ref 脱敏：``mask_key`` 与 server/app.py::SecurityKeys._mask_key **同款语义、本地实现**
  （前 6 + … + 后 2，≤8 全隐）——防循环依赖，绝不落明文 key。

写策略（K4 量控，§2.5 表逐行）：
- 状态迁移（banned / ban_escalated / ban_expired）：按事件记（每次一条）；
- auth_fail：按（来源 × 窗口）**首条 + 触发阈值条**记，窗口内其余仅内存计数；
  **回环失败不逐条落盘**（探针噪音非安全信号，仅计数入 status 概览）；
- ip_limited / rate_limited：按（来源 × 窗口）去重——窗口内首条记全量，其后仅累计
  （计数入 status 概览；§六 验收按「窗口首条」断言）；
- admin_op：管理操作，按事件记（每次一条）。

写失败 → WARNING 不阻塞（fail-open，§2.0-5）：审计是旁路，主请求面永不因审计失败失败。

保留（Q4）：``cleanup_expired(keep_days)``——keep_days=0 不清理；启动 + 每日首写惰性触发
（启动调用点 T-262 接线；本卡提供构造注入 keep_days 的每日首写自动触发）。
量级估算（K4）：量控后落盘量 ∝（来源数 × 窗口数 + 状态事件数），个人级正常流量约
十至百行/日，90 天 ≈ 数 MB 量级（方案 §2.5 原文）。

读接口（供 T-265 端点）：``read_events(date, limit)``——date 严格正则校验归路由层（D4），
本叶子仅做防穿越的防御性校验；文件不存在 → 空列表。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------- 事件七类（§2.5 事件表） ----------

EVENT_AUTH_FAIL = "auth_fail"
EVENT_RATE_LIMITED = "rate_limited"
EVENT_IP_LIMITED = "ip_limited"
EVENT_BANNED = "banned"
EVENT_BAN_ESCALATED = "ban_escalated"
EVENT_BAN_EXPIRED = "ban_expired"
EVENT_ADMIN_OP = "admin_op"

# 自由文本字段截断上限（UA 截断 ≤200 防注入换行；其余自由文本同限，D7）
_TEXT_FIELD_MAX = 200
# read_events 的 limit 钳制上限（与端点 D4 clamp 口径一致）
_READ_LIMIT_MAX = 1000
# 读接口日期防御性校验（严格校验归路由层 D4；此处仅防路径穿越）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 保留清理只认本模块命名的日志文件
_LOGFILE_RE = re.compile(r"^security-(\d{8})\.jsonl$")


def mask_key(key: str | None) -> str:
    """Key 脱敏指纹：前 6 字符 + '…' + 后 2 字符（_mask_key 同款语义，本地实现）。

    🔴 绝不返回明文 Key，也不返回哈希全文。
    过短的 Key（≤ 8 字符）无法安全截断，整体隐藏为 '…'。
    """
    if not key:
        return ""
    if len(key) <= 8:
        return "…"
    return f"{key[:6]}…{key[-2:]}"


class SecurityLog:
    """安全审计日志器：构造注入 log_dir，时间外部传入（now 参数，可测）。

    去重窗口状态内存持有（重启即清零——审计去重是量控手段，丢失无害，
    状态机真源在 security_state，本叶子只管审计面）。
    """

    def __init__(
        self,
        log_dir: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
        keep_days: int = 0,
    ) -> None:
        """Args:
        log_dir: 日志目录（调用方注入，生产传 $SGME_HOME/logs）。
        clock: 时间源（缺省本地时区时间）；测试注入固定 now。
        keep_days: 保留天数（调用方注入 security_log_keep_days；0 = 不清理）。
            >0 时每日首写惰性触发 cleanup_expired（启动调用点 T-262）。
        """
        self._log_dir = Path(log_dir)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._keep_days = keep_days
        self._lock = threading.Lock()
        # 去重窗口状态：(event, source, window_id) -> {"count", "first_logged", "threshold_logged"}
        self._windows: dict[tuple[str, str, int], dict[str, bool | int]] = {}
        self._last_cleanup_date: str | None = None

    # ---------- 内部：落盘统一入口 ----------

    @staticmethod
    def _window_id(now: datetime, window_sec: int) -> int:
        """窗口序号：epoch 秒整除窗口长（时间对齐切窗，跨进程一致）。"""
        return int(now.timestamp()) // max(1, window_sec)

    def _file_for(self, now: datetime) -> Path:
        """按天文件：<log_dir>/security-YYYYMMDD.jsonl（本地日期）。"""
        return self._log_dir / f"security-{now:%Y%m%d}.jsonl"

    def _maybe_cleanup_daily(self, now: datetime) -> None:
        """每日首写惰性触发保留清理（keep_days>0 时；启动触发点 T-262）。"""
        today = f"{now:%Y%m%d}"
        if self._keep_days > 0 and self._last_cleanup_date != today:
            self._last_cleanup_date = today
            try:
                self.cleanup_expired(self._keep_days, now=now)
            except Exception:  # (BLE001 豁免) 清理失败不影响审计主链路（fail-open）
                logger.warning("security log daily cleanup failed", exc_info=True)

    def _append_event(self, event: dict, now: datetime) -> bool:
        """事件写入统一入口：补 ISO 时间戳 / 截断自由文本 / json 转义单行落盘。

        Returns:
            True=已落盘；False=写失败（已 WARNING，不抛——fail-open，§2.0-5）。
        """
        record = dict(event)
        record["ts"] = now.isoformat()
        self._maybe_cleanup_daily(now)
        # 自由文本截断 ≤200（UA/原因/详情等；source/ts/event 为结构字段不截）
        for k, v in record.items():
            if isinstance(v, str) and k not in ("source", "ts", "event"):
                record[k] = v[:_TEXT_FIELD_MAX]
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._file_for(now)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # 文件权限对齐 agent_keys.json 收紧先例（POSIX 生效；Windows 尽力跳过）
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception:  # (BLE001 豁免) 写失败不阻塞主请求（fail-open）
            logger.warning(
                "security log write failed for event=%s",
                record.get("event"),
                exc_info=True,
            )
            return False
        return True

    def _count_and_maybe_write(
        self,
        event: str,
        source: str,
        *,
        window_sec: int,
        build: Callable[[int], dict | None],
        now: datetime,
        threshold: int = 0,
    ) -> bool:
        """K4 窗口去重通用骨架：计数恒增，落盘与否由类别规则决定。

        build(count) 返回待落盘事件 dict；返回 None 表示本条不落盘（仅计数）。
        threshold>0 时（auth_fail 语义）：count 首次达到 threshold 追加一条「触发阈值条」。
        """
        wid = self._window_id(now, window_sec)
        with self._lock:
            # 窗口卫生：远期窗口状态惰性清退（防长期运行内存缓涨）
            stale = [k for k in self._windows if k[2] < wid - 100]
            for k in stale:
                del self._windows[k]
            key = (event, source, wid)
            state = self._windows.setdefault(
                key, {"count": 0, "first_logged": False, "threshold_logged": False}
            )
            state["count"] = int(state["count"]) + 1
            count = int(state["count"])

            payload: dict | None = None
            if not state["first_logged"]:
                # （来源 × 窗口）首条：记全量
                state["first_logged"] = True
                payload = build(count)
            elif threshold > 0 and count == threshold and not state["threshold_logged"]:
                # 触发阈值条（auth_fail：达到封禁阈值那一刻记一条）
                state["threshold_logged"] = True
                payload = build(count)
            # 其余：仅内存计数（K4——回环失败/同窗口重复均不逐条落盘）

        if payload is None:
            return False
        return self._append_event(payload, now)

    # ---------- 写接口（record_* 按七类事件展开） ----------

    def record_auth_fail(
        self,
        source: str,
        *,
        key: str | None = None,
        reason: str = "",
        user_agent: str = "",
        now: datetime | None = None,
        window_sec: int = 300,
        threshold: int = 0,
        is_loopback: bool = False,
    ) -> bool:
        """auth_fail：按（来源 × 窗口）首条 + 触发阈值条记；回环失败不逐条落盘。

        Args:
            key: 明文 key（仅用于脱敏指纹，绝不落盘）。
            threshold: auth_fail_window 窗口内的封禁阈值（0 = 观察期，只计数不封）。
            is_loopback: 回环来源（调用方以 resolve 后来源判定传入）——仅计数
                （入 status 概览），不落盘（探针噪音非安全信号，§2.5）。

        Returns:
            True=本条已落盘。
        """
        now = now or self._clock()

        def build(count: int) -> dict:
            return {
                "event": EVENT_AUTH_FAIL,
                "source": source,
                "key_ref": mask_key(key),
                "reason": reason,
                "user_agent": user_agent,
                "count": count,
            }

        if is_loopback:
            # 回环：仅计数不落盘（K4——探针噪音非安全信号，计数入 status 概览）
            with self._lock:
                wid = self._window_id(now, window_sec)
                state = self._windows.setdefault(
                    (EVENT_AUTH_FAIL, source, wid),
                    {"count": 0, "first_logged": False, "threshold_logged": False},
                )
                state["count"] = int(state["count"]) + 1
            return False
        return self._count_and_maybe_write(
            EVENT_AUTH_FAIL,
            source,
            window_sec=window_sec,
            build=build,
            now=now,
            threshold=threshold,
        )

    def record_rate_limited(
        self,
        source: str,
        *,
        scope: str = "key",
        limit: int | None = None,
        user_agent: str = "",
        now: datetime | None = None,
        window_sec: int = 60,
    ) -> bool:
        """rate_limited：按（来源 × 窗口）去重——窗口首条记全量，其后仅累计。"""
        now = now or self._clock()

        def build(count: int) -> dict:
            return {
                "event": EVENT_RATE_LIMITED,
                "source": source,
                "scope": scope,
                "limit": limit,
                "user_agent": user_agent,
                "count": count,
            }

        return self._count_and_maybe_write(
            EVENT_RATE_LIMITED, source, window_sec=window_sec, build=build, now=now
        )

    def record_ip_limited(
        self,
        source: str,
        *,
        limit: int | None = None,
        user_agent: str = "",
        now: datetime | None = None,
        window_sec: int = 60,
    ) -> bool:
        """ip_limited：无 key IP 限流；按（来源 × 窗口）去重同 rate_limited。"""
        now = now or self._clock()

        def build(count: int) -> dict:
            return {
                "event": EVENT_IP_LIMITED,
                "source": source,
                "limit": limit,
                "user_agent": user_agent,
                "count": count,
            }

        return self._count_and_maybe_write(
            EVENT_IP_LIMITED, source, window_sec=window_sec, build=build, now=now
        )

    def record_banned(
        self,
        source: str,
        *,
        duration_sec: int,
        escalated: bool = False,
        reason: str = "",
        now: datetime | None = None,
    ) -> bool:
        """banned：状态迁移按事件记（每次一条）。escalated=True 表示带升级。"""
        return self._append_event(
            {
                "event": EVENT_BANNED,
                "source": source,
                "duration_sec": duration_sec,
                "escalated": escalated,
                "reason": reason,
            },
            now or self._clock(),
        )

    def record_ban_escalated(
        self,
        source: str,
        *,
        prev_duration_sec: int,
        new_duration_sec: int,
        now: datetime | None = None,
    ) -> bool:
        """ban_escalated：本次新封禁带升级（时长×2）时记一次（K1）。"""
        return self._append_event(
            {
                "event": EVENT_BAN_ESCALATED,
                "source": source,
                "prev_duration_sec": prev_duration_sec,
                "new_duration_sec": new_duration_sec,
            },
            now or self._clock(),
        )

    def record_ban_expired(self, source: str, *, now: datetime | None = None) -> bool:
        """ban_expired：封禁到期解封记一条。"""
        return self._append_event(
            {"event": EVENT_BAN_EXPIRED, "source": source}, now or self._clock()
        )

    def record_admin_op(
        self,
        op: str,
        *,
        source: str | None = None,
        detail: str = "",
        now: datetime | None = None,
    ) -> bool:
        """admin_op：管理操作审计（§2.8-3；每次一条）。"""
        return self._append_event(
            {"event": EVENT_ADMIN_OP, "op": op, "source": source, "detail": detail},
            now or self._clock(),
        )

    # ---------- 读接口（供 T-265 端点） ----------

    def read_events(self, date: str, *, limit: int = 100) -> list[dict]:
        """按日期读事件（时间升序，至多 limit 条）。

        D4：date 严格校验（``^\\d{4}-\\d{2}-\\d{2}$``）与 clamp 归路由层；
        本叶子仅做防穿越的防御性校验（不合法 → 空列表）。文件不存在 → 空列表。
        """
        if not _DATE_RE.match(date or ""):
            return []
        limit = max(0, min(int(limit), _READ_LIMIT_MAX))
        if limit == 0:
            return []
        path = self._log_dir / f"security-{date.replace('-', '')}.jsonl"
        if not path.is_file():
            return []
        out: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 坏行跳过（读面 resilient）
                    if len(out) >= limit:
                        break
        except OSError:
            return []
        return out

    # ---------- 保留清理（Q4） + 计数概览（S4 status 用） ----------

    def cleanup_expired(self, keep_days: int, *, now: datetime | None = None) -> int:
        """删除早于 keep_days 的按天日志文件；keep_days<=0 不清理（0 = 不清理）。

        只认 ``security-YYYYMMDD.jsonl`` 命名（正则严格匹配，防误删他文件）。
        Returns:
            删除的文件数。
        """
        if keep_days <= 0:
            return 0
        now = now or self._clock()
        cutoff = (now - timedelta(days=keep_days)).date()
        removed = 0
        try:
            entries = list(self._log_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            m = _LOGFILE_RE.match(entry.name)
            if not m or not entry.is_file():
                continue
            try:
                file_date = (
                    datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=UTC).date()
                )
            except ValueError:
                continue
            if file_date < cutoff:
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    logger.warning(
                        "security log cleanup failed for %s", entry.name, exc_info=True
                    )
        return removed

    def window_count(
        self, event: str, source: str, *, window_sec: int, now: datetime | None = None
    ) -> int:
        """（事件 × 来源）在当前窗口内的内存计数（status 计数概览，S4）。"""
        now = now or self._clock()
        with self._lock:
            state = self._windows.get((event, source, self._window_id(now, window_sec)))
            return int(state["count"]) if state else 0
