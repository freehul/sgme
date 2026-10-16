r"""LLM 输出的**保守** JSON 修复（ST-58/T-292，P8）。

为什么要有它（生产实证）：
- 近 30 天 `L1.5 输出解析失败` 的 error 行 **全部**是 `Invalid \escape`
  （`json.JSONDecodeError`），失败样本的 `merged_content` 内含 Windows 路径
  （`C:\work\demo`、`C:\work\dl`）——模型输出时未把反斜杠转义。
- 原行为：解析失败 → **整批重发**一次 LLM（同 prompt + 一句提示），失败批
  token 翻倍；两次仍失败则默认 store（丢冲突检测）。日志累计 55 次。

修复原则（宁可放弃，不可猜错）：
1. **只在原文解析失败后调用**（本模块自身不做「要不要修」的决策）；
2. 只处理两类实测出现的破损：非法反斜杠转义、尾逗号；
3. 修复结果必须能通过 `raw_decode` 校验，否则返回 None；
4. **不引入第三方依赖**（如 `json_repair`），保持依赖面不变（方案 §六.3）。

不做什么：不补缺失引号、不修括号不配对、不猜测被截断的数组——那类破损语义
不可控，一律交回原重试链路。

⚠️ 已知不可检测的静默风险（不在本模块职责内）：`D:\build`、`D:\temp`、`D:\new`
这类路径的反斜杠后恰好是**合法**转义字符（`\b` `\t` `\n`），JSON 会静默解析成
退格/制表/换行字符 —— 原文「能解析」故本模块不会介入（无从判断这是路径还是
真转义）。**治本靠提示词侧约束**（要求模型输出时转义或改用正斜杠）。
"""

from __future__ import annotations

import json
import re

# JSON 合法转义字符（`\u` 后接 4 位十六进制，交由解析器自行校验）
_LEGAL_ESCAPES = frozenset('\\"/bfnrtu')

# 尾逗号：对象/数组最后一个元素后的多余逗号
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _decodable(text: str) -> bool:
    """能否解析出首个完整 JSON 值（与 l15/l2 解析器的 `raw_decode` 口径一致）。"""
    try:
        json.JSONDecoder().raw_decode(text.lstrip())
        return True
    except Exception:  # noqa: BLE001——任何解析异常都视为不可解码
        return False


def _fix_invalid_escapes(s: str) -> str:
    r"""把非法转义的反斜杠补成 `\\`；**已配对的反斜杠必须原样保留**。

    逐字符扫描而非正则：正则 `\\(?![...])` 会把 `C:\\work\\demo` 中**第二个**反斜杠
    误判为非法转义（因为它的后继是 `P`），从而把合法的 `\\` 改成 `\\\`，
    反而制造新的非法转义。扫描式消费掉「已配对」的两字符即可避免。
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = s[i + 1] if i + 1 < n else ""
        if nxt in _LEGAL_ESCAPES:
            out.append(ch)
            out.append(nxt)
            i += 2  # 合法转义：整体消费，不被再次扫描
            continue
        out.append("\\")
        out.append("\\")  # 非法转义：补一个反斜杠（原字符留在原位，下轮输出）
        i += 1
    return "".join(out)


def _fix_trailing_commas(s: str) -> str:
    """去掉 `,}` / `,]` 形态的尾逗号。"""
    return _TRAILING_COMMA_RE.sub(r"\1", s)


def repair_json_text(text: str | None) -> str | None:
    """保守修复非法 JSON 文本；无法修复（或原文本就合法）返回 None。

    返回**修复后的文本**，不返回解析结果——解析仍由各阶段自己的 `parse_*` 负责，
    避免这里与业务结构耦合。

    候选顺序（先单一修复，后组合）：
      1. 去尾逗号
      2. 非法反斜杠转义 → 双反斜杠
      3. 两者组合
    取第一个能通过 `raw_decode` 校验的候选。
    """
    if not text:
        return None
    src = text.strip()
    if not src:
        return None
    if _decodable(src):
        return None  # 原文就合法：调用方不该走到这里，且不得改动

    cands: list[str] = []
    a = _fix_trailing_commas(src)
    if a != src:
        cands.append(a)
    b = _fix_invalid_escapes(src)
    if b != src:
        cands.append(b)
    c = _fix_invalid_escapes(a)
    if c != src and c not in cands:
        cands.append(c)

    for cand in cands:
        if _decodable(cand):
            return cand
    return None
