"""refinery/extract.py：LLM 提取。

extract(prompt, output_schema, model_cfg) → dict：
调 LLM（复用 sgme.llm.chain.call_with_fallback 降级链）→ 解析 JSON →
校验 schema（缺失键/类型错）→ 失败重试（最多 3 次）→ 仍失败抛 ExtractError。

不绑定具体 prompt 或 schema：wiki 提炼和蒸馏套装均可调用（§9.4）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from sgme.llm import chain as llm_chain

logger = logging.getLogger("sgme.refinery.extract")

# 重试上限（首次调用 + 最多 2 次重试）
MAX_ATTEMPTS = 3

# 重试时追加到 prompt 尾部的纠错提示（帮助模型输出合法 JSON）
_RETRY_HINT = "\n\n【上次输出无效】{error}\n请重新输出一个符合 schema 的合法 JSON 对象，不要输出其他内容。"


class ExtractError(Exception):
    """LLM 提取失败：JSON 解析或 schema 校验在重试耗尽后仍失败。"""


class SchemaValidationError(ValueError):
    """输出不满足 output_schema（缺键 / 类型不符）。"""


# ---------- JSON 解析 ----------

def parse_json_output(text: str) -> dict:
    """从 LLM 返回文本中解析 JSON 对象。

    容错处理：
    - 剔除 ```json ... ``` 代码围栏
    - 找不到围栏时，截取首个 { 到末尾 } 的子串解析
    仍失败抛 json.JSONDecodeError。
    """
    if not text or not text.strip():
        raise json.JSONDecodeError("空输出", text, 0)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("未找到 JSON 对象", candidate, 0)
    return json.loads(candidate[start : end + 1])


# ---------- schema 校验 ----------

# 字符串类型名 → Python 类型（output_schema 里可用 "str"/"list" 等写法）
_TYPE_NAMES: dict[str, Any] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": (int, float),
    "bool": bool,
    "boolean": bool,
    "list": list,
    "array": list,
    "dict": dict,
    "object": dict,
}


def _matches_type(value: Any, expected: Any) -> bool:
    """值是否满足期望类型。

    expected 支持：
    - Python 类型：str / list / dict / int / float / bool
    - 字符串类型名："str" / "list" / "dict" / "number" / ...
    - 类型元组：(str, type(None)) 表示可空字段
    """
    if expected is None:
        return True
    if isinstance(expected, str):
        expected = _TYPE_NAMES.get(expected.lower())
        if expected is None:
            raise ValueError(f"未知 schema 类型名: {expected}")
    if isinstance(expected, tuple):
        return any(_matches_type(value, t) for t in expected)
    if expected is bool:
        # bool 是 int 子类，必须先判 bool
        return isinstance(value, bool)
    return isinstance(value, expected)


def validate_schema(data: dict, output_schema: dict) -> list[str]:
    """校验提取结果是否满足 schema。

    Args:
        data: LLM 输出的 dict。
        output_schema: {字段名: 期望类型}，如 {"title": str, "tags": list}。

    Returns:
        错误列表；为空表示校验通过。
    """
    errors: list[str] = []
    for field, expected in output_schema.items():
        if field not in data:
            errors.append(f"缺失字段: {field}")
            continue
        if not _matches_type(data[field], expected):
            errors.append(f"字段类型不符: {field} 期望 {_type_label(expected)}，实际 {type(data[field]).__name__}")
    return errors


def _type_label(expected: Any) -> str:
    """期望类型的可读描述（用于报错信息）。"""
    if isinstance(expected, str):
        return expected
    if isinstance(expected, tuple):
        return " | ".join(_type_label(t) for t in expected)
    return expected.__name__


# ---------- 主入口 ----------

def extract(
    prompt: str,
    output_schema: dict,
    model_cfg: dict,
    chain_name: str = "refinement",
    client: Any = None,
    attempts: int = MAX_ATTEMPTS,
) -> dict:
    """调 LLM 提取结构化数据。

    - 调用 sgme.llm.chain.call_with_fallback（降级链，全挂抛 LLMUnavailable）
    - 解析 JSON → 校验 schema → 任一失败则带纠错提示重试
    - 重试耗尽仍失败 → 抛 ExtractError（携带最后一次错误与尝试次数）

    Args:
        prompt: 提取提示词（含材料内容与输出要求）。
        output_schema: {字段名: 期望类型}。
        model_cfg: llm.yaml 配置 dict（load_config() 的返回值）。
        chain_name: 降级链名，默认 refinement。
        client: 可复用的 httpx.Client（可选，透传给降级链）。
        attempts: 最大尝试次数，默认 3。

    Returns:
        校验通过的 dict。

    Raises:
        ExtractError: 重试耗尽仍失败。
    """
    current_prompt = prompt
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            text, _provider, _usage = llm_chain.call_with_fallback(
                model_cfg, current_prompt, chain_name=chain_name, client=client
            )
            data = parse_json_output(text)
            if not isinstance(data, dict):
                raise json.JSONDecodeError("顶层不是 JSON 对象", text, 0)
            errors = validate_schema(data, output_schema)
            if errors:
                raise SchemaValidationError("；".join(errors))
            if attempt > 1:
                logger.info("extract 重试成功 (attempt=%s)", attempt)
            return data
        except Exception as e:  # noqa: BLE001 —— 所有失败统一走重试
            last_error = e
            logger.warning("extract attempt=%s 失败: %s", attempt, e)
            current_prompt = current_prompt + _RETRY_HINT.format(error=e)
    raise ExtractError(
        f"提取失败：重试 {attempts} 次仍失败，最后一次错误: {last_error}"
    ) from last_error
