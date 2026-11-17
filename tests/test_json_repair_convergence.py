# -*- coding: utf-8 -*-
"""tests/test_json_repair_convergence.py：T-297 JSON 修复两份实现收敛。

缺陷（Backlog T-297）：``_parse_json_lenient`` 第 2 步的正则
``\\([^\\\"\\/bfnrtu])`` 对「已配对反斜杠 + 后随非转义字符」（如
``D:\\\\Projects`` 的第二个反斜杠后随 ``P``）会把第二个反斜杠误判为
非法转义，改成 ``\\\\\\`` 反而制造新的非法转义、破坏路径语义。

``sgme/engine/json_repair._fix_invalid_escapes``（T-292）已是逐字符扫描式
的正确实现。本批收敛：lenient 复用扫描式实现，删除缺陷正则。
"""

from __future__ import annotations

import json

import pytest

from sgme.engine import json_repair
from sgme.engine.scene_aggregate import _parse_json_lenient


def test_windows_path_paired_backslashes_survive():
    """合法 JSON 内的 Windows 路径（已配对反斜杠）不得被误改。"""
    value = "D:\\Projects\\new"
    raw = json.dumps([{"path": value}])  # 源码含 D:\\Projects\\new（合法转义）
    data = _parse_json_lenient(raw)
    assert data[0]["path"] == value


def test_paired_backslash_then_cap_letter():
    """已配对反斜杠后随大写字母（P 不在合法转义表）是历史误判形态。"""
    value = "D:\\Projects"
    raw = json.dumps([{"p": value}])
    data = _parse_json_lenient(raw)
    assert data[0]["p"] == value


def test_invalid_escape_still_fixed():
    """真非法转义（\q 等，手工构造源码）仍被修成双反斜杠。"""
    raw = '[{"s": "a\\q b"}]'  # JSON 源码含非法 \q
    data = _parse_json_lenient(raw)
    assert data[0]["s"] == "a\\q b"


def test_unc_and_multi_dir_paths():
    """多级目录与 UNC 路径均无损。"""
    cases = ["C:\\Users\\leo\\demo\\n.txt", "\\\\nas\\share\\x"]
    for value in cases:
        raw = json.dumps([{"v": value}])
        data = _parse_json_lenient(raw)
        assert data[0]["v"] == value


def test_scan_impl_fixes_same_invalid_escape():
    """扫描式实现对真非法转义的修复结果与 lenient 解析一致（收敛验收）。"""
    raw = '[{"s": "a\\q b"}]'
    fixed = json_repair._fix_invalid_escapes(raw)
    assert json.loads(fixed)[0]["s"] == _parse_json_lenient(raw)[0]["s"] == "a\\q b"
