"""refinery/validate.py：质量门框架。

validate(content, rules) → ValidationReport：
- rules 为可调用列表，每个规则接收 content 返回判定结果
- 支持全局注册自定义规则（register_rule），与显式传入的 rules 合并执行
- 内置规则：non_empty（非空）、min_length(n)（最小长度）、max_length(n)（最大长度）

规则返回值约定（三者皆可）：
- bool：True 通过 / False 失败
- (bool, reason)：带失败原因的二元组
- str：非空字符串视为失败原因（空串视为通过）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 全局自定义规则注册表：规则名 → 可调用
_CUSTOM_RULES: dict[str, Callable[[str], Any]] = {}


def register_rule(name: str, fn: Callable[[str], Any]) -> None:
    """全局注册自定义验证规则。

    注册后可省略显式传入 rules，validate 会自动合并执行。
    """
    _CUSTOM_RULES[name] = fn


def non_empty(content: str) -> bool:
    """内置规则：内容非空（去空白后仍有内容）。"""
    return bool(content and content.strip())


def min_length(n: int) -> Callable[[str], Any]:
    """内置规则工厂：内容长度 ≥ n。"""

    def _rule(content: str):
        return len(content) >= n or (False, f"内容长度 {len(content)} < 最小 {n}")

    _rule.__name__ = f"min_length_{n}"
    return _rule


def max_length(n: int) -> Callable[[str], Any]:
    """内置规则工厂：内容长度 ≤ n。"""

    def _rule(content: str):
        return len(content) <= n or (False, f"内容长度 {len(content)} > 最大 {n}")

    _rule.__name__ = f"max_length_{n}"
    return _rule


@dataclass
class ValidationReport:
    """质量门结果。

    - passed: 通过的规则名列表
    - failed: 失败详情列表，每项 {"rule": 规则名, "reason": 失败原因}
    - ok: 是否全部通过
    """

    passed: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否全部通过（无失败项）。"""
        return not self.failed

    def summary(self) -> str:
        """一行摘要，便于记日志/异常信息。"""
        if self.ok:
            return f"校验通过 ({len(self.passed)} 项)"
        detail = "；".join(f"{f['rule']}: {f['reason']}" for f in self.failed)
        return f"校验失败 ({len(self.failed)} 项): {detail}"


def _run_rule(name: str, fn: Callable[[str], Any], content: str) -> dict | None:
    """执行单个规则并归一化结果；通过返回 None，失败返回 {"rule", "reason"}。"""
    try:
        result = fn(content)
    except Exception as e:  # noqa: BLE001 —— 规则内部异常按失败处理
        return {"rule": name, "reason": f"规则执行异常: {e}"}
    if isinstance(result, tuple):
        ok, reason = result
        if not ok:
            return {"rule": name, "reason": str(reason or "未通过")}
        return None
    if isinstance(result, str):
        return {"rule": name, "reason": result} if result else None
    return None if result else {"rule": name, "reason": "未通过"}


def validate(content: str, rules: list[Callable[[str], Any]] | None = None) -> ValidationReport:
    """质量门：按规则列表校验 content。

    Args:
        content: 待校验内容（通常为提取出的正文）。
        rules: 规则可调用列表；None 时使用内置默认（non_empty + min_length(50)），
            并自动合并全局注册的自定义规则。

    Returns:
        ValidationReport。
    """
    if rules is None:
        rules = [non_empty, min_length(50)]
    # 合并全局注册的自定义规则（显式传入与注册表去重）
    merged: dict[str, Callable[[str], Any]] = {fn.__name__: fn for fn in rules}
    merged.update(_CUSTOM_RULES)

    report = ValidationReport()
    for name, fn in merged.items():
        failure = _run_rule(name, fn, content)
        if failure:
            report.failed.append(failure)
        else:
            report.passed.append(name)
    return report
