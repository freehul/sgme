"""sgme/log 统一日志模块测试（v0.7 §12）。

覆盖：get_logger 打点与幂等、setup 的 level 生效、console/json 双格式输出、
JSON 输出可解析且含关键字段、config 解析（有 section / 无 section 默认值 /
非法值）、setup 多次调用不重复添加 handler（幂等）。
"""

from __future__ import annotations

import json
import logging

import pytest

from sgme.log import get_logger, setup
from sgme.log.config import DEFAULT_FORMAT, DEFAULT_LEVEL, parse_logging_config

# setup() 安装 handler 时打的管理标记（与 sgme.log.__init__ 保持一致）
_MARK = "_sgme_managed"


class ListHandler(logging.Handler):
    """测试用 handler：把格式化后的文本收集进 records 列表，不写任何流。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def _managed_handlers() -> list[logging.Handler]:
    """返回当前由 setup() 安装的 handler（用于幂等断言，忽略 pytest 自带 handler）。"""
    return [h for h in logging.getLogger().handlers if getattr(h, _MARK, False)]


def test_get_logger_returns_logger_with_name() -> None:
    """① get_logger 返回标准 logging.Logger，且 name 正确打点。"""
    logger = get_logger("sgme.test.module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "sgme.test.module"


def test_get_logger_idempotent_same_instance() -> None:
    """② 同 name 多次调用返回同一实例（幂等）。"""
    assert get_logger("sgme.test.same") is get_logger("sgme.test.same")


def test_setup_level_effect() -> None:
    """③ setup 后 level 生效：根 logger 与 get_logger 返回的 logger 级别均变化。"""
    setup(level="DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    logger = get_logger("sgme.test.level")
    assert logger.level == logging.DEBUG  # 新 logger 显式继承根级别
    assert logger.getEffectiveLevel() == logging.DEBUG
    # setup 可反复切换级别
    setup(level="INFO")
    assert logging.getLogger().level == logging.INFO
    # int 级别同样支持
    setup(level=logging.ERROR)
    assert logging.getLogger().level == logging.ERROR


def test_setup_json_format_output() -> None:
    """④ setup(format="json") 后 handler 输出可 json.loads，且含关键字段。"""
    handler = ListHandler()
    setup(format="json", output=handler)
    logger = get_logger("sgme.test.json")
    logger.info("hello %s", "world")
    assert len(handler.records) == 1
    data = json.loads(handler.records[0])  # 必须能被 json.loads 解析
    for key in ("time", "level", "name", "message"):
        assert key in data
    assert data["level"] == "INFO"
    assert data["name"] == "sgme.test.json"
    assert data["message"] == "hello world"


def test_setup_console_format_output() -> None:
    """console 格式输出为人类可读文本，且包含名称与消息内容。"""
    handler = ListHandler()
    setup(format="console", output=handler, level="INFO")
    logger = get_logger("sgme.test.console")
    logger.info("console message")
    assert len(handler.records) == 1
    assert "sgme.test.console" in handler.records[0]
    assert "console message" in handler.records[0]


def test_config_parse_with_logging_section() -> None:
    """⑤a 有 logging section 时正确取值（含大小写归一化）。"""
    cfg = {"logging": {"level": "debug", "format": "JSON", "output": "logs/app.log"}}
    parsed = parse_logging_config(cfg)
    assert parsed == {"level": "DEBUG", "format": "json", "output": "logs/app.log"}


def test_config_parse_missing_section_defaults() -> None:
    """⑤b 无 logging section（None / 空 dict / 只有其他 section）时返回默认值。"""
    expected = {"level": DEFAULT_LEVEL, "format": DEFAULT_FORMAT, "output": None}
    assert parse_logging_config(None) == expected
    assert parse_logging_config({}) == expected
    assert parse_logging_config({"l2": {"max_scenes": 5}}) == expected


def test_config_parse_invalid_values_raise() -> None:
    """⑤c 非法 level / format / output 类型抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_logging_config({"logging": {"level": "TRACE"}})
    with pytest.raises(ValueError):
        parse_logging_config({"logging": {"format": "xml"}})
    with pytest.raises(ValueError):
        parse_logging_config({"logging": {"output": 123}})


def test_setup_idempotent_no_duplicate_handlers() -> None:
    """⑥ setup 多次调用不重复添加 handler（幂等）。"""
    setup(level="INFO", format="console")
    assert len(_managed_handlers()) == 1
    setup(level="DEBUG", format="json")
    assert len(_managed_handlers()) == 1
    setup()  # 全默认参数再调一次
    assert len(_managed_handlers()) == 1
