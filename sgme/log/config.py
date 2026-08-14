"""日志配置解析：从配置 dict 的 logging section 解析（v0.7 §12）。

约定：配置 dict 形如 ``{"logging": {"level": ..., "format": ..., "output": ...}}``
（即 sgme.config.load_config() 返回字典的顶层结构），section 缺失时返回
默认值：level=INFO、format=console、output=None（标准输出）。

示例（config/sgme.yaml）::

    logging:
      level: INFO
      format: json        # console | json
      output: "logs/sgme.log"   # 缺省 None = 标准输出
"""

from __future__ import annotations

import logging

# 默认配置常量（section 缺失时兜底）
DEFAULT_LEVEL = "INFO"
DEFAULT_FORMAT = "console"
DEFAULT_OUTPUT = None

# 合法取值
VALID_FORMATS = ("console", "json")
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_level(level: str | int) -> int:
    """把级别（字符串名或 int 数值）解析为 logging 数值级别。

    Raises:
        ValueError: 级别名不在合法集合内。
    """
    if isinstance(level, int):
        return level
    name = str(level).upper()
    if name not in VALID_LEVELS:
        raise ValueError(
            f"未知日志级别: {level!r}（可选: {', '.join(VALID_LEVELS)} 或 int 数值）"
        )
    return getattr(logging, name)


def parse_logging_config(cfg: dict | None) -> dict:
    """从配置 dict 解析日志配置。

    Args:
        cfg: 配置 dict；日志配置位于其顶层 "logging" section。
            传入 None / 空 dict / 无 logging section 的 dict 均返回默认值。

    Returns:
        {"level": str|int, "format": str, "output": None|str|logging.Handler}。
        output 为 None 表示标准输出；str 为日志文件路径。
        注：handler 实例仅在 setup() 直传时出现，YAML 配置只可能给路径。

    Raises:
        ValueError: level 名 / format 值非法，或 output 类型非法。
    """
    section = (cfg or {}).get("logging")
    if not isinstance(section, dict):
        # section 缺失或类型错误 → 默认值兜底
        return {
            "level": DEFAULT_LEVEL,
            "format": DEFAULT_FORMAT,
            "output": DEFAULT_OUTPUT,
        }

    # level：字符串名（自动大写归一化）或 int 数值
    level: str | int = section.get("level", DEFAULT_LEVEL)
    if not isinstance(level, int):
        level = str(level).upper()
        if level not in VALID_LEVELS:
            raise ValueError(
                f"未知日志级别: {level!r}（可选: {', '.join(VALID_LEVELS)} 或 int 数值）"
            )

    # format：console | json（小写归一化）
    fmt = str(section.get("format", DEFAULT_FORMAT)).lower()
    if fmt not in VALID_FORMATS:
        raise ValueError(f"未知日志格式: {fmt!r}（可选: {', '.join(VALID_FORMATS)}）")

    # output：None（stdout）/ str（文件路径）/ logging.Handler（setup 直传）
    output = section.get("output", DEFAULT_OUTPUT)
    if output is not None and not isinstance(output, (str, logging.Handler)):
        raise ValueError(
            f"logging.output 必须为 str 路径或省略（当前: {output!r}）"
        )

    return {"level": level, "format": fmt, "output": output}
