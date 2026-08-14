"""sgme.log —— 全项目统一日志模块（v0.7 §12）。

设计动机
========
- 全项目唯一日志入口：``from sgme.log import get_logger``。
  各模块禁止直接调用 Python stdlib ``logging.getLogger`` —— 只有入口收敛，
  ``setup()`` 才能统一控制全局的 level / 格式 / 输出，日志行为才可被
  配置、被测试、被一键切换（如线上排查时切 JSON 结构化）。
- 目录名用 ``sgme/log/`` 而非 ``sgme/logging/``，避免与 stdlib ``logging``
  模块同名冲突；模块内部一律使用包内相对导入。
- ``get_logger(name)`` 返回标准 ``logging.Logger``，name 正确打点
  （``logger.name == name``），同 name 多次调用返回同一实例（幂等）。
- ``setup()`` 可重复调用且幂等：每次先摘除上一轮安装的 handler，不会重复添加。

用法
====
    from sgme.log import get_logger, setup

    setup(level="INFO", format="json")        # 进程启动时全局配置一次
    logger = get_logger("sgme.engine.pipeline")
    logger.info("pipeline 启动")               # 走统一 handler

也可从配置 dict 解析后展开调用：
    setup(**parse_logging_config(load_config()))
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import formatter
from .config import DEFAULT_FORMAT, DEFAULT_LEVEL, parse_logging_config, resolve_level

__all__ = ["get_logger", "setup"]

# setup() 是否已被调用过（get_logger 据此同步新 logger 自身的 level）
_CONFIGURED = False
# 本进程内由 setup() 安装的 handler 列表（幂等清理用）
_MANAGED_HANDLERS: list[logging.Handler] = []
# 管理标记：挂在 setup() 安装的 handler 上，便于清理与测试识别
_MARK = "_sgme_managed"


def get_logger(name: str) -> logging.Logger:
    """获取全项目唯一日志入口的 Logger（唯一合法取 logger 方式）。

    Args:
        name: 模块打点名（如 "sgme.engine.pipeline"）。

    Returns:
        标准 logging.Logger；同 name 幂等（同一实例）。

    Raises:
        ValueError: name 为空或非字符串。
    """
    if not name or not isinstance(name, str):
        raise ValueError(f"logger name 必须为非空字符串: {name!r}")
    logger = logging.getLogger(name)
    # setup() 已配置过根 logger 时，让新创建的 logger 显式继承根级别，
    # 保证 logger.level 立即可见；未配置前保持 NOTSET（跟随祖先链）。
    if _CONFIGURED and logger.level == logging.NOTSET:
        logger.setLevel(logging.getLogger().level)
    return logger


def setup(
    level: str | int | None = None,
    format: str | None = None,  # noqa: A002 - 参数名遵循设计文档 v0.7 §12
    output: object | None = None,
) -> None:
    """配置全局日志（级别 / 格式 / 输出），可重复调用（幂等）。

    Args:
        level: 日志级别，字符串名（"DEBUG"/"INFO"/"WARNING"/"ERROR"/"CRITICAL"）
               或 int 数值（如 logging.DEBUG）；缺省 "INFO"。
        format: "console"（默认，人类可读文本，可带颜色）或 "json"（结构化）。
        output: None=标准输出；str=日志文件路径（自动建目录）；
                logging.Handler=自定义 handler 实例。

    幂等性：每次调用先移除上一轮由 setup() 安装的 handler，再安装新的，
    因此多次调用不会重复添加 handler。
    """
    global _CONFIGURED

    # 1) 参数归一化 + 校验（复用 config 解析逻辑，缺省值兜底）
    level_raw: str | int = DEFAULT_LEVEL if level is None else level
    fmt_raw: str = DEFAULT_FORMAT if format is None else format
    # 环境变量覆盖（2026-08-11）：SGME_LOG_OUTPUT 优先于显式 output——
    # 测试环境经 conftest 注入隔离日志路径，防止 pytest 进程污染生产 logs/sgme.log；
    # 显式传入 logging.Handler 时不覆盖（测试用自定义 handler 捕获断言）
    env_out = os.environ.get("SGME_LOG_OUTPUT")
    if env_out and not isinstance(output, logging.Handler):
        output = env_out
    parsed = parse_logging_config({"logging": {"level": level_raw, "format": fmt_raw, "output": output}})
    level_int = resolve_level(parsed["level"])
    fmt = parsed["format"]
    out = parsed["output"]

    # 2) 幂等：先摘除上一轮安装的 handler
    root = logging.getLogger()
    for h in list(_MANAGED_HANDLERS):
        root.removeHandler(h)
    _MANAGED_HANDLERS.clear()

    # 3) 构建目标 handler（output: None=stdout / str=文件 / Handler=自定义）
    if out is None:
        color = bool(getattr(sys.stdout, "isatty", lambda: False)())
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
    elif isinstance(out, str):
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        color = False
    elif isinstance(out, logging.Handler):
        handler = out
        color = False
    else:
        raise TypeError(
            f"output 必须为 None / str 路径 / logging.Handler，收到: {type(out).__name__}"
        )

    # 4) 挂 formatter 并安装到根 logger
    handler.setFormatter(formatter.make_formatter(fmt, color=color))
    handler.setLevel(logging.NOTSET)  # 不在 handler 层过滤，级别统一由根 logger 控制
    setattr(handler, _MARK, True)
    root.addHandler(handler)
    root.setLevel(level_int)
    _MANAGED_HANDLERS.append(handler)
    _CONFIGURED = True
