# -*- coding: utf-8 -*-
"""tests/test_json_repair_convergence.py：T-297 JSON 修复收敛回归。

缺陷（Backlog T-297）：旧 lenient 解析器的正则
``\\([^\\\"\\/bfnrtu])`` 对「已配对反斜杠 + 后随非转义字符」（如
``D:\\\\Projects`` 的第二个反斜杠后随 ``P``）会把第二个反斜杠误判为
非法转义，改成 ``\\\\`` 反而制造新的非法转义、破坏路径语义。

T-427（2026-11-20）：场景层退役，`_parse_json_lenient` 随 scene_aggregate 摘除；
本测试改为直接锁定 `sgme.engine.json_repair.repair_json_text` 的扫描式实现
（l3_adjudicate 修复链路共用同一实现）。
"""

from __future__ import annotations

import json

from sgme.engine import json_repair


def _parse_with_repair(raw: str):
    fixed = json_repair.repair_json_text(raw)
    assert fixed is not None, "应能修复"
    return json.loads(fixed)


def test_windows_path_paired_backslashes_survive():
    """合法 JSON 内的 Windows 路径（已配对反斜杠）不得被误改。"""
    value = "D:\\Projects\\new"
    raw = json.dumps([{"path": value}])  # 源码含 D:\\Projects\\new（合法转义）
    assert json_repair.repair_json_text(raw) is None  # 原文合法 → 不改
    assert json.loads(raw)[0]["path"] == value


def test_paired_backslash_then_cap_letter():
    """已配对反斜杠后随大写字母（P 不在合法转义表）是历史误判形态。"""
    value = "D:\\Projects"
    raw = json.dumps([{"p": value}])
    assert json_repair.repair_json_text(raw) is None
    assert json.loads(raw)[0]["p"] == value


def test_invalid_escape_still_fixed():
    """真非法转义（\\q 等，手工构造源码）仍被修成双反斜杠。"""
    raw = '[{"s": "a\\q b"}]'  # JSON 源码含非法 \q
    data = _parse_with_repair(raw)
    assert data[0]["s"] == "a\\q b"


def test_unc_and_multi_dir_paths():
    """多级目录与 UNC 路径均无损。"""
    cases = ["C:\\Users\\leo\\demo\\n.txt", "\\\\nas\\share\\x"]
    for value in cases:
        raw = json.dumps([{"v": value}])
        assert json_repair.repair_json_text(raw) is None
        assert json.loads(raw)[0]["v"] == value


def test_scan_impl_fixes_same_invalid_escape():
    """扫描式实现对真非法转义的修复结果与 json.loads 一致。"""
    raw = '[{"s": "a\\q b"}]'
    fixed = json_repair._fix_invalid_escapes(raw)
    assert json.loads(fixed)[0]["s"] == _parse_with_repair(raw)[0]["s"] == "a\\q b"
