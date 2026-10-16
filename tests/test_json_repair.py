r"""ST-58/T-292：`sgme/engine/json_repair.py` 保守 JSON 修复单测（P8）。

生产实证（2026-10-16 审计）：近 30 天 `L1.5 输出解析失败` 的 error 行**全部**
是 `Invalid \escape`，失败样本 `merged_content` 内含 Windows 路径
（`C:\work\demo` 形态）——模型输出时未转义反斜杠。原行为是整批重发 LLM。

⚠️ 测试用语法的关键事实：JSON 的**合法**转义只有
`\" \\ \/ \b \f \n \r \t \u`。因此 `\P` `\G` `\D` 非法，而
`D:\build` `D:\temp` `D:\new` 恰好**合法**（会静默变成退格/制表/换行）——
后者本模块无从判断，不在修复范围（见模块 docstring 说明）。

数据卫生：用例路径一律用占位符或纯假路径，不复制生产真实路径。
"""

from __future__ import annotations

import json

import pytest

from sgme.engine import json_repair


# ---------- 原文合法：必须返回 None（不得改动） ----------


def test_valid_json_returns_none():
    """原文就能解析 → 返回 None（本函数只在解析失败后才被调用）。"""
    assert json_repair.repair_json_text('[{"a": 1}]') is None


def test_valid_with_escaped_backslash_returns_none():
    """已正确转义的反斜杠不得被二次转义。"""
    assert json_repair.repair_json_text(r'[{"p": "C:\\work\\demo"}]') is None


def test_valid_with_literal_escape_chars_returns_none():
    r"""`\n` / `\t` / `\b` 都是**合法**转义 → 原文可解析 → 不介入。"""
    assert json_repair.repair_json_text(r'[{"a": "l1\nl2", "b": "t\there"}]') is None


def test_empty_input():
    assert json_repair.repair_json_text(None) is None
    assert json_repair.repair_json_text("") is None
    assert json_repair.repair_json_text("   ") is None


# ---------- 反斜杠转义（主战场） ----------


def test_repair_windows_path_escape():
    r"""`C:\work\demo` 未转义（`\P` 非法）→ 修复后可解析且语义为字面反斜杠。"""
    bad = r'[{"action": "update", "merged_content": "源码放在 C:\work\demo 下"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)

    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None
    data = json.loads(fixed)
    assert data[0]["merged_content"] == r"源码放在 C:\work\demo 下"


def test_repair_placeholder_path_escape():
    """占位路径形态同样可修（避免用例引入真实路径）。"""
    bad = r'[{"action": "store", "merged_content": "<PROJECT_ROOT>\src"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None
    assert json.loads(fixed)[0]["merged_content"] == r"<PROJECT_ROOT>\src"


def test_repair_preserves_legal_escapes():
    r"""同一文本内混有合法与非法转义：合法者必须原样保留（不被双重转义）。"""
    bad = r'[{"a": "l1\nl2", "b": "t\there", "c": "D:\Proj"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)

    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None
    data = json.loads(fixed)
    assert data[0]["a"] == "l1\nl2"        # 换行语义保留（未退化成字面 \n）
    assert data[0]["b"] == "t\there"       # 制表语义保留
    assert data[0]["c"] == r"D:\Proj"      # 非法转义被补齐为字面反斜杠


def test_repair_does_not_double_escape_existing_pairs():
    r"""回归：`\\` 已配对时，其后紧跟需转义字符也不得被误判。

    这是朴素正则方案的已知缺陷（正则会把 `D:\\Proj` 的第二个反斜杠当非法转义，
    改成 `D:\\\Proj` 反而制造新错误）——本用例锁定扫描式实现。
    """
    bad = r'[{"a": "C:\\work\\demo ok", "b": "C:\Proj"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)  # b 的 \P 非法

    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None
    data = json.loads(fixed)
    assert data[0]["a"] == r"C:\work\demo ok"   # 原有合法配对不变
    assert data[0]["b"] == r"C:\Proj"


def test_repair_backslash_at_end_of_string():
    r"""字符串末尾的孤立反斜杠（非法）→ 补齐后仍是字面反斜杠。"""
    bad = '[{"a": "trailing\\"}]'
    fixed = json_repair.repair_json_text(bad)
    # 末尾反斜杠会转义掉收尾引号，属结构性破损 → 保守放弃
    assert fixed is None


# ---------- 尾逗号 ----------


def test_repair_trailing_comma_array():
    assert json_repair.repair_json_text('[{"a": 1},]') == '[{"a": 1}]'


def test_repair_trailing_comma_object():
    fixed = json_repair.repair_json_text('{"a": 1, "b": 2,}')
    assert fixed is not None
    assert json.loads(fixed) == {"a": 1, "b": 2}


def test_repair_trailing_comma_multiline():
    fixed = json_repair.repair_json_text('[\n  {"a": 1},\n]')
    assert fixed is not None
    assert json.loads(fixed) == [{"a": 1}]


# ---------- 组合 ----------


def test_repair_combined_escape_and_comma():
    bad = r'[{"a": "C:\work\demo"},]'
    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None
    assert json.loads(fixed)[0]["a"] == r"C:\work\demo"


# ---------- 不可修复：必须返回 None（不得猜） ----------


def test_truncated_array_not_repaired():
    """被截断的数组（括号不配对）不修——语义不可控。"""
    assert json_repair.repair_json_text('[{"a": 1') is None


def test_missing_quote_not_repaired():
    """缺右引号不修。"""
    assert json_repair.repair_json_text('[{"a": 1]') is None


def test_plain_text_not_repaired():
    """纯自然语言（模型跑题）不修。"""
    assert json_repair.repair_json_text("抱歉，我无法完成该请求。") is None


# ---------- 真实失败形态（脱敏复刻） ----------


def test_production_failure_shape():
    r"""复刻 2026-10-16 生产 error 行的形态（路径已脱敏为占位符）。

    原始形态（审计样本）：`"merged_content": "活跃项目源码放在 C:\work\demo，..."`
    """
    bad = (
        '[{"new_memory_index": 0, "candidate_ids": ["fake-id-0001"], '
        '"action": "update", "merged_content": "活跃项目源码放在 <PROJECT_ROOT>，'
        '非活跃项目放在 <DOWNLOAD_ROOT>；用户将仓库下载到本地 <PROJECT_ROOT>\\sub\\work", '
        '"reason": "同一主体 + 信息增量"}]'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)

    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None
    data = json.loads(fixed)
    assert data[0]["action"] == "update"
    assert data[0]["candidate_ids"] == ["fake-id-0001"]
    assert r"<PROJECT_ROOT>\sub\work" in data[0]["merged_content"]


def test_known_limitation_legal_letter_after_backslash():
    r"""**已知局限（显式记录，非遗漏）**：反斜杠后恰好是合法转义字母时无法纠正。

    `\tmp` 的 `\t` 在 JSON 里是制表符 —— 解析器只能照此理解，本模块无从判断
    模型本意是「路径分隔符 + tmp」。后果是修好的文本中该段变成 TAB + "mp"。

    取舍：仍接受修复。因为替代路径是**整批重发 LLM**（token 翻倍）→ 两次失败
    则默认 store（**丢失整批冲突检测**），代价高于个别路径段的字符失真。
    治本手段在提示词侧（要求输出时转义或改用正斜杠），见 T-292 第 ③ 项。
    """
    bad = r'[{"a": "see D:\sub\tmp"}]'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)  # \s 非法导致整体不可解析

    fixed = json_repair.repair_json_text(bad)
    assert fixed is not None, "文档级破损仍应被修复（避免整批重发）"
    got = json.loads(fixed)[0]["a"]
    assert got == "see D:\\sub\tmp"      # \sub 已修正为字面反斜杠
    assert "\t" in got                    # \tmp 仍是制表符（局限所在）


# ---------- 依赖面 ----------


def test_no_third_party_dependency():
    """不引入 json_repair 等第三方（方案 §六.3：不新增外部依赖）。"""
    import sgme.engine.json_repair as m

    src = open(m.__file__, encoding="utf-8").read()
    assert "import json_repair" not in src
    assert "from json_repair" not in src
