# -*- coding: utf-8 -*-
"""tests/test_skill_cache_sync.py：共用技能缓存层的「真源 ↔ 副本」一致性（T-406）。

D-59 的缓存实现有两类载体：
1. Python 真源 ``adapters/_shared/skill_cache.py`` → 各适配器的随包副本
   （hermes 平铺 + doubao/mimo/workbuddy/zcode 的 scripts/）——必须逐字节一致；
2. dsh 的 TypeScript 移植 ``adapters/dsh/sgme-bridge/src/skill-cache.ts``——
   语言不同不能共用，但**协议常量必须同值**（清单文件、目录状态文件、协议版本）。

本测试把「改了真源忘了同步副本」变成机器可见（对标 skill_fetch 真源/分发副本的
sha256 纪律），并防止 TS 移植悄悄漂移。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sync_shared_skill_cache as sync  # noqa: E402

TS_CACHE = ROOT / "adapters" / "dsh" / "sgme-bridge" / "src" / "skill-cache.ts"
TS_LIB = ROOT / "adapters" / "dsh" / "sgme-bridge" / "lib" / "index.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_vendored_copies_match_source():
    """五份 Python 副本与真源一致（不一致时给出修复命令）。"""
    bad = sync.drift()
    assert bad == [], (
        "副本与真源不一致，先跑 `python scripts/sync_shared_skill_cache.py`："
        + ", ".join(p.relative_to(ROOT).as_posix() for p in bad)
    )


def test_sync_cli_check_mode_passes():
    """--check 退出码 0（发布门禁可直接挂）。"""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_shared_skill_cache.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_ts_port_constants_match_python_source():
    """TS 移植版的协议常量必须与 Python 真源同值。"""
    py = _read(ROOT / "adapters" / "_shared" / "skill_cache.py")
    ts = _read(TS_CACHE)
    for const, pattern in (
        ("CACHE_PROTOCOL_VERSION", r"CACHE_PROTOCOL_VERSION\s*=\s*(\d+)"),
        ("MANIFEST_FILE", r"MANIFEST_FILE\s*=\s*['\"]([^'\"]+)['\"]"),
        ("CATALOG_FILE", r"CATALOG_FILE\s*=\s*['\"]([^'\"]+)['\"]"),
        ("PACKAGE_MANIFEST", r"PACKAGE_MANIFEST\s*=\s*['\"]([^'\"]+)['\"]"),
    ):
        py_value = re.search(pattern, py)
        ts_value = re.search(pattern, ts)
        assert py_value and ts_value, f"{const} 未在两端声明"
        assert py_value.group(1) == ts_value.group(1), (
            f"{const} 两端不一致：python={py_value.group(1)} ts={ts_value.group(1)}"
        )


def test_ts_cache_is_bundled_into_lib():
    """dsh 产物必须包含 skill-cache 源码（防「改了 src 没重打包」）。"""
    lib = _read(TS_LIB)
    assert "//#region src/skill-cache.ts" in lib
    assert ".sgme-manifest.json" in lib
