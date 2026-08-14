"""operations/errors.py：operations 层统一返回契约与异常族（v0.7 §7.4）。

职责边界（重要）：
- 本模块**不认识任何协议**——不 import fastapi、不 import mcp、不知道 HTTP 状态码。
  错误码 → HTTP 状态码的映射由入口层（server/app.py::ERROR_CODES）负责；
  错误 → MCP error JSON 的转换由 mcp_server.py::_op_json 负责。
- operations 层只负责表达「成功/失败 + 机器可读错误码 + 人可读文案」。

两种失败表达方式，按语义选用：
1. 返回 ``OperationResult(ok=False, error_code=..., message=...)``
   —— 用于「可预期的业务失败」（如资源不存在、LLM 不可用）。
2. 抛 ``InvalidArgs`` / ``OperationError``
   —— 用于「参数校验不通过」或「深层调用栈里就地失败、不便层层回传」的场景。
   入口层的 run_operation / _op_json 会统一捕获并翻译，两种方式对调用方等价。

依赖方向：operations.errors 是叶子模块，零内部依赖，任何模块都可安全 import。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------- 统一错误码 ----------
# 取值必须是 server/app.py::ERROR_CODES 的键，否则入口层会回落到 500。
ERR_INVALID_ARGS = "ERR_INVALID_ARGS"        # → HTTP 400
ERR_NOT_FOUND = "ERR_NOT_FOUND"              # → HTTP 404
ERR_CONFLICT = "ERR_CONFLICT"                # → HTTP 409
ERR_LLM_UNAVAILABLE = "ERR_LLM_UNAVAILABLE"  # → HTTP 503
ERR_INTERNAL = "ERR_INTERNAL"                # → HTTP 500


@dataclass
class OperationResult:
    """operations 层统一返回体（v0.7 §7.4）。

    Attributes:
        ok: 操作是否成功。
        data: 成功时的业务数据（协议无关的**信息超集**，由入口层投影为各自形态）。
        error_code: 失败时的机器可读错误码（见本模块 ERR_* 常量）。
        message: 失败时的人可读文案。设计文档 §7.4 未列出此字段，
            但入口层渲染错误（api_error(code, message) / {"error": msg}）必须要有文案，
            否则只能由入口层臆造，反而把语义散回入口层。作为**带默认值的附加字段**引入，
            对 §7.4 的三字段契约向后兼容。
        details: 失败时的结构化补充信息（可选，同上，附加字段）。
    """

    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None

    @classmethod
    def succeed(cls, data: dict[str, Any] | None = None) -> "OperationResult":
        """构造成功结果。"""
        return cls(ok=True, data=data)

    @classmethod
    def fail(
        cls,
        error_code: str = ERR_INTERNAL,
        message: str = "操作失败",
        details: dict[str, Any] | None = None,
    ) -> "OperationResult":
        """构造失败结果。"""
        return cls(ok=False, data=None, error_code=error_code, message=message, details=details)


class InvalidArgs(Exception):
    """参数非法（入口层映射为 HTTP 400/422，MCP 映射为 error JSON）。"""

    error_code: str = ERR_INVALID_ARGS

    def __init__(self, message: str = "参数非法", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.details: dict[str, Any] | None = details


class OperationError(Exception):
    """操作执行失败（入口层映射为 HTTP 500/503，MCP 映射为 error JSON）。

    error_code 默认 ERR_INTERNAL；LLM 不可用等场景显式传 ERR_LLM_UNAVAILABLE。
    """

    error_code: str = ERR_INTERNAL

    def __init__(
        self,
        message: str = "操作失败",
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        # 实例属性覆盖类属性（子类可通过类属性声明默认错误码）
        self.error_code = error_code or type(self).error_code
        self.details: dict[str, Any] | None = details


def result_from_exception(exc: Exception) -> OperationResult:
    """异常 → OperationResult（协议无关）。

    仅识别本模块定义的异常族；其余异常一律归为 ERR_INTERNAL。
    入口层可直接用它把 try/except 收敛成一行，避免 8 个模块各写一遍。
    """
    if isinstance(exc, InvalidArgs):
        return OperationResult.fail(ERR_INVALID_ARGS, exc.message, exc.details)
    if isinstance(exc, OperationError):
        return OperationResult.fail(exc.error_code, exc.message, exc.details)
    return OperationResult.fail(ERR_INTERNAL, str(exc))
