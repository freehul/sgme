"""日志格式化器：控制台（可带颜色）+ JSON（结构化）双格式（v0.7 §12）。

- ConsoleFormatter: 人类可读文本 ``时间 级别 名称: 消息``，
  DEBUG/INFO/WARNING/ERROR/CRITICAL 五级带简单 ANSI 颜色（可关闭）。
- JsonFormatter: 每行一条 JSON 结构化日志，可被 ``json.loads`` 解析，
  含 time/level/name/message 关键字段（另附 pathname/lineno 便于定位）。
- make_formatter(): 按格式名（"console"|"json"）创建对应 formatter 的工厂。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

# ANSI 颜色表（仅控制台格式使用；非 tty / 文件输出时关闭）
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",      # 青色
    "INFO": "\033[32m",       # 绿色
    "WARNING": "\033[33m",    # 黄色
    "ERROR": "\033[31m",      # 红色
    "CRITICAL": "\033[35m",   # 品红
}
_RESET = "\033[0m"


class ConsoleFormatter(logging.Formatter):
    """控制台文本格式化器：``时间 级别 名称: 消息``，级别名带简单颜色。

    Args:
        color: 是否启用 ANSI 颜色（写文件或非 tty 时应传 False）。
    """

    def __init__(self, color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        # 先按标准流程渲染（含 asctime/message/异常文本），再对级别名上色
        text = super().format(record)
        if not self.color:
            return text
        color = _LEVEL_COLORS.get(record.levelname, "")
        if not color:
            return text
        level_str = f"{record.levelname:<8s}"
        # 级别名位于行首前缀区，替换第一次出现处即可（消息体不受影响）
        return text.replace(level_str, f"{color}{level_str}{_RESET}", 1)


class JsonFormatter(logging.Formatter):
    """JSON 结构化格式化器：每行一条 JSON，可被 json.loads 解析。

    关键字段：time（ISO8601 本地时间）/ level / name / message，
    辅助字段：pathname / lineno（定位源码位置）。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="seconds"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "pathname": record.pathname,
            "lineno": record.lineno,
        }
        # ensure_ascii=False 保留中文可读；default=str 兜底不可序列化字段
        return json.dumps(payload, ensure_ascii=False, default=str)


def make_formatter(fmt: str = "console", color: bool = True) -> logging.Formatter:
    """按格式名创建对应 Formatter 的工厂。

    Args:
        fmt: "console"（默认）或 "json"。
        color: 仅对 console 格式生效，是否启用 ANSI 颜色。

    Raises:
        ValueError: fmt 不是 "console" / "json"。
    """
    if fmt == "json":
        return JsonFormatter()
    if fmt == "console":
        return ConsoleFormatter(color=color)
    raise ValueError(f"未知日志格式: {fmt!r}（可选: console / json）")
