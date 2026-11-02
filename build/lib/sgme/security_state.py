"""sgme/security_state.py：安全状态叶子（ST-55 / T-258，方案 v3 §2.2，K1/K2/K5）。

职责（方案 §2.2）：
- 失败计数表：按来源键（resolve_client_ip 的来源键）窗口计数，达阈值 → 触发封禁；
- 封禁表：键 → 封禁到期时刻 + 层级；
- 升级阶梯（K1 唯一实现路径）：条目内记录「上一封禁结束时刻 + 上次时长」，
  供下次触发时判定加倍。

升级语义（K1 定稿，实现唯一路径）：
    上一封禁结束时刻 → 本次新封禁触发时刻 ≤ ban_reset_sec 时，
    本次时长 = 上次时长 × 2（≤ ban_max_seconds 封顶）；
    连续 ban_reset_sec 过完无新封禁 → 阶梯归零（下次按基础时长起封）。
    - ban_escalated 在「本次新封禁带升级」时记一次：本卡在封禁决策里给出
      escalated 标志，审计落盘（security_log / T-259）由调用方据标志记事件。
    - 封禁期内的 429 请求不参与失败计数、不记 ban_escalated（T-260 调用侧保证：
      本卡把 record_failure 与 ban 查询分离，调用方先 is_banned 再计数）。

有界（K5）：
- 窗口过期清扫：惰性（读写路径顺带）+ 低频全扫（每 FULL_SWEEP_INTERVAL 次写全扫一次）；
- 尺寸上限 SECURITY_STATE_MAX_ENTRIES = 100000（模块常量，不入配置面），
  失败表 / 封禁表各自封顶，超限丢最旧 + WARNING；
- 封禁条目在「禁期 + reset 窗口」后删除（升级阶梯记忆随条目一起消失）。

纯叶子：不 import config / server / 其它业务模块（stdlib only）。
线程安全：threading.Lock 保护全部可变状态。
状态为进程内存：重启清空（宽松方向；重启 = 解封，runbook 写明）。
时间一律外部传入（now 参数）：可测可复现，本卡内部不取 time.time()。
阈值由调用方每次传入（构造与阈值分离，本卡不读配置）。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

logger = logging.getLogger("sgme.security_state")

# 状态尺寸上限（K5，模块常量，不入配置面）：失败表 / 封禁表各自的上限。
# 超限丢最旧 + WARNING，保证洪泛场景下状态占用有上界。
SECURITY_STATE_MAX_ENTRIES = 100_000

# 低频全扫间隔：每 N 次写操作做一次全表过期清扫（惰性清扫的补充）。
FULL_SWEEP_INTERVAL = 256


class SecurityState:
    """安全状态机：失败计数 → 封禁 → 升级阶梯（进程内存，线程安全）。

    构造与阈值分离：所有阈值由调用方每次调用 ``record_failure`` 传入，
    本卡不读配置。时间一律用外部 ``now``（同一时钟系内自洽即可，可测可复现）。
    """

    def __init__(self, max_entries: int = SECURITY_STATE_MAX_ENTRIES) -> None:
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        # 键 → 窗口内失败时间戳列表（OrderedDict 维护插入序，超限丢最旧用）
        self._fails: OrderedDict[str, list[float]] = OrderedDict()
        # 键 → 各键最近一次计数窗口秒数（全扫时按它判窗口过期）
        self._fail_windows: dict[str, float] = {}
        # 键 → 封禁记录：
        #   until     封禁到期时刻
        #   level     层级（基础封禁 = 1，第 n 次升级 = n）
        #   last_dur  本次封禁时长（下次加倍基数）
        #   ended_at  本封禁结束时刻（= until，K1 升级窗口起点）
        #   reset_win 建封时的 ban_reset_sec（全扫判「禁期+reset 窗口」用）
        self._bans: OrderedDict[str, dict] = OrderedDict()
        self._write_count = 0

    # ------------------------------------------------------------------
    # 失败计数与封禁触发
    # ------------------------------------------------------------------

    def record_failure(
        self,
        key: str,
        now: float,
        threshold: int,
        window_sec: int,
        ban_seconds: int,
        ban_max_seconds: int,
        ban_reset_sec: int,
    ) -> dict:
        """记一次失败并返回封禁决策。

        封禁期内的 429 请求不进来（T-260 调用侧先 ``is_banned`` 再计数）。

        Args:
            key: 来源键（resolve_client_ip 来源键 / /64 聚合键）。
            now: 当前时刻（外部时钟）。
            threshold: 达阈值即封；0 = 只计数不封（观察期，K2）。
            window_sec: 失败计数窗口（秒）。
            ban_seconds: 基础封禁时长（秒）。
            ban_max_seconds: 升级上限（秒，v3 定稿 3600）。
            ban_reset_sec: 连续该时长无新封禁 → 阶梯归零（秒）。

        Returns:
            封禁决策 dict：
            - banned: bool    本次是否触发新封禁
            - until: float    封禁到期时刻（未触发 0.0）
            - duration: float 本次封禁时长（未触发 0.0）
            - escalated: bool 本次封禁是否带升级（时长 > 基础时长）
            - level: int      层级（基础 = 1；未触发 0）
            - fail_count: int 当前窗口内失败计数（含本次；观察期读数来源）
        """
        with self._lock:
            now = float(now)
            self._maybe_full_sweep(now)

            # 禁期内：不重复封、不覆盖（调用方本应先查 is_banned；防御兜底）。
            ban = self._bans.get(key)
            if ban is not None and now < ban["until"]:
                stamps = self._window_fails(
                    key, now, self._fail_windows.get(key, float(window_sec))
                )
                return {
                    "banned": False,
                    "until": float(ban["until"]),
                    "duration": 0.0,
                    "escalated": False,
                    "level": int(ban["level"]),
                    "fail_count": len(stamps),
                }

            # 窗口内失败计数（惰性清除窗口外旧时间戳）。
            self._fail_windows[key] = float(window_sec)
            stamps = self._window_fails(key, now, float(window_sec))
            stamps.append(now)
            self._fails[key] = stamps
            self._fails.move_to_end(key)
            fail_count = len(stamps)

            # 触发判定：threshold 0 = 观察期（只计数不封，K2）。
            if threshold <= 0 or fail_count < int(threshold):
                self._touch_write()
                return {
                    "banned": False,
                    "until": 0.0,
                    "duration": 0.0,
                    "escalated": False,
                    "level": 0,
                    "fail_count": fail_count,
                }

            # ---- 触发封禁：升级阶梯判定（K1 唯一实现路径） ----
            # 封禁触发即清空该键失败时间戳（下一轮重新起算）。
            self._fails.pop(key, None)

            base = max(0.0, float(ban_seconds))
            ban_max = max(0.0, float(ban_max_seconds))
            reset_win = max(0.0, float(ban_reset_sec))
            duration = base
            escalated = False
            prev = self._bans.pop(key, None)

            if (
                prev is not None
                and reset_win > 0
                and now - prev["ended_at"] <= reset_win
            ):
                # 上一封禁结束 → 本次触发落在 reset 窗口内 → 时长加倍（≤ 上限封顶）。
                duration = min(prev["last_dur"] * 2.0, ban_max)
                escalated = duration > base
            # 否则（无前科 / 超 reset 窗口 / reset_win=0）：阶梯归零，按基础时长起封。

            level = int(prev["level"]) + 1 if (prev is not None and escalated) else 1
            until = now + duration
            self._bans[key] = {
                "until": until,
                "level": level,
                "last_dur": duration,
                "ended_at": until,
                "reset_win": reset_win,
            }
            self._bans.move_to_end(key)
            self._touch_write()
            return {
                "banned": True,
                "until": until,
                "duration": duration,
                "escalated": escalated,
                "level": level,
                "fail_count": fail_count,
            }

    # ------------------------------------------------------------------
    # 封禁查询（纯读）
    # ------------------------------------------------------------------

    def is_banned(self, key: str, now: float) -> tuple[bool, int, int]:
        """查询键当前是否在禁期内。

        Returns:
            ``(是否封禁, 剩余秒, 层级)``；未封禁 → ``(False, 0, 0)``。
            剩余秒向上取整（Retry-After 语义，D8）。
            到期条目不在此处删除——升级阶梯记忆要保留到 reset 窗口过完。
        """
        with self._lock:
            ban = self._bans.get(key)
            if ban is None or float(now) >= ban["until"]:
                return False, 0, 0
            return True, _ceil_sec(ban["until"] - float(now)), int(ban["level"])

    # ------------------------------------------------------------------
    # 快照（供 T-265 status 端点，纯读 + 锁）
    # ------------------------------------------------------------------

    def snapshot(self, now: float) -> dict:
        """当前状态快照：活封禁列表 + 计数概览。纯读、加锁。

        Returns:
            {
              "active_bans": [{"key", "remaining_sec", "level"}, ...],
              "fail_counts": {key: 窗口内失败计数},   # 顺带惰性清各自窗口过期戳
              "fail_keys": int,                      # 有失败计数的键数
              "ban_entries": int,                    # 封禁表条目数（含到期未清扫的阶梯记忆）
              "size": int,                           # 失败表 + 封禁表总条目数
              "max_entries": int,                    # 单表上限
            }
        """
        with self._lock:
            now = float(now)
            active = []
            for key, ban in self._bans.items():
                if now < ban["until"]:
                    active.append(
                        {
                            "key": key,
                            "remaining_sec": _ceil_sec(ban["until"] - now),
                            "level": int(ban["level"]),
                        }
                    )
            fail_counts = {
                key: len(self._window_fails(key, now, self._fail_windows.get(key, 0.0)))
                for key in list(self._fails)
            }
            return {
                "active_bans": active,
                "fail_counts": fail_counts,
                "fail_keys": len(self._fails),
                "ban_entries": len(self._bans),
                "size": len(self._fails) + len(self._bans),
                "max_entries": self._max_entries,
            }

    # ------------------------------------------------------------------
    # 内部：清扫与有界（K5）
    # ------------------------------------------------------------------

    def _window_fails(self, key: str, now: float, window_sec: float) -> list[float]:
        """取键的窗口内失败时间戳列表（惰性清除窗口外旧戳）。须持锁调用。"""
        stamps = self._fails.get(key)
        if stamps is None:
            return []
        cutoff = now - window_sec
        stamps[:] = [t for t in stamps if t > cutoff]
        if not stamps:
            del self._fails[key]
            return []
        return stamps

    def _maybe_full_sweep(self, now: float) -> None:
        """低频全扫（每 FULL_SWEEP_INTERVAL 次写一次）。须持锁调用。"""
        self._write_count += 1
        if self._write_count % FULL_SWEEP_INTERVAL == 0:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        """全表过期清扫。须持锁调用。

        - 失败表：按各键记录的窗口清除过期时间戳，空键删除；
        - 封禁表：删除「禁期 + reset 窗口」均已过的条目（阶梯记忆随之消失）。
        """
        for key in list(self._fails):
            self._window_fails(key, now, self._fail_windows.get(key, 0.0))
        for key in list(self._bans):
            ban = self._bans[key]
            if now >= ban["until"] and now - ban["ended_at"] >= ban["reset_win"]:
                del self._bans[key]
                self._fail_windows.pop(key, None)

    # ------------------------------------------------------------------
    # 内部：尺寸上限（K5）
    # ------------------------------------------------------------------

    def _touch_write(self) -> None:
        """写操作收尾：各表超限丢最旧 + WARNING。须持锁调用。"""
        for table, name in ((self._fails, "失败计数表"), (self._bans, "封禁表")):
            while len(table) > self._max_entries:
                oldest_key, _ = table.popitem(last=False)  # OrderedDict 头部 = 最旧
                self._fail_windows.pop(oldest_key, None)
                logger.warning(
                    "安全状态超限（%s %d > %d）：丢弃最旧条目 %r",
                    name,
                    len(table) + 1,
                    self._max_entries,
                    oldest_key,
                )


def _ceil_sec(seconds: float) -> int:
    """剩余秒向上取整（Retry-After 语义，D8；非正值不会传入）。"""
    return max(1, int(seconds) + (1 if seconds % 1 else 0))
