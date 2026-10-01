# -*- coding: utf-8 -*-
"""codex 适配器测试引导：把 adapters/codex 注入 sys.path，使 codex_sgme 可导入。

收编前测试在独立仓库运行（codex_sgme 以已安装包身份可见）；收编主仓后从仓库根
运行（CI: ``python -m pytest adapters/codex/tests``）需要显式引导，与其它适配器
测试的 sys.path 注入等义——统一收在 conftest 一处，新增测试文件自动生效。
（T-241：CI adapter self-tests 跨环境 ModuleNotFoundError 修复，2026-10-01。）
"""
import sys
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).resolve().parents[1]  # -> adapters/codex
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))
