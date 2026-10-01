# -*- coding: utf-8 -*-
"""claude-code 适配器测试引导：把 adapters/claude-code 注入 sys.path。

与 adapters/codex/tests/conftest.py 同口径——测试既能在适配器目录内跑，
也能从仓库根以 ``python -m pytest adapters/claude-code/tests`` 跑（CI 口径）。
"""
import sys
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).resolve().parents[1]  # -> adapters/claude-code
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))
