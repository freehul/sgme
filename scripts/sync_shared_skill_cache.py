#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/sync_shared_skill_cache.py：把共用技能缓存层同步到各适配器（T-406）。

真源 = ``adapters/_shared/skill_cache.py``（唯一可编辑处）；
副本 = 各适配器分发包内的一份 ``skill_cache.py``（安装后不能回指仓库，
必须自带；``publish_adapter_skills.py`` 只打包适配器目录本身）。

为什么用「源码副本」而不是 import：
- hermes 插件被复制到 ``$HERMES_HOME/plugins/sgme/``，doubao/mimo/workbuddy/zcode
  的 ``scripts/`` 被复制进各自技能目录——安装后都不再与仓库同路径；
- 兼容 Python 3.9+ 的裸拷贝场景（纯标准库单文件）。

用法::

    python scripts/sync_shared_skill_cache.py            # 写副本
    python scripts/sync_shared_skill_cache.py --check    # 只校验（不一致退出 1）

dsh 是 TypeScript 移植版（``adapters/dsh/sgme-bridge/src/skill-cache.ts``），
语言不同无法逐字节共用；由 ``tests/test_skill_cache_sync.py`` 校验两端
``CACHE_PROTOCOL_VERSION`` 同值、清单文件名同值。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "adapters" / "_shared" / "skill_cache.py"

#: 需要携带副本的适配器（dsh 用 TS 移植版，见模块 docstring）
TARGETS = (
    ROOT / "adapters" / "hermes" / "skill_cache.py",
    ROOT / "adapters" / "doubao" / "scripts" / "skill_cache.py",
    ROOT / "adapters" / "mimo" / "scripts" / "skill_cache.py",
    ROOT / "adapters" / "workbuddy" / "scripts" / "skill_cache.py",
    ROOT / "adapters" / "zcode" / "scripts" / "skill_cache.py",
)


def _read(path: Path) -> str:
    """按文本读并统一换行（Windows 工作副本可能是 CRLF，内容比较按 LF）。"""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def drift() -> list[Path]:
    """返回与真源不一致的副本路径列表（缺文件也算漂移）。"""
    source = _read(SOURCE)
    bad: list[Path] = []
    for target in TARGETS:
        if not target.is_file() or _read(target) != source:
            bad.append(target)
    return bad


def sync() -> list[Path]:
    """全量写副本；返回本次实际改动的路径。"""
    source = _read(SOURCE)
    changed: list[Path] = []
    for target in TARGETS:
        if target.is_file() and _read(target) == source:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8", newline="\n")
        changed.append(target)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步 adapters/_shared/skill_cache.py 到各适配器")
    ap.add_argument("--check", action="store_true", help="只校验不写盘（不一致退出 1）")
    args = ap.parse_args(argv)
    if args.check:
        bad = drift()
        if bad:
            print("❌ 副本与真源不一致（先跑 python scripts/sync_shared_skill_cache.py）：")
            for p in bad:
                print(f"  - {p.relative_to(ROOT).as_posix()}")
            return 1
        print(f"✅ {len(TARGETS)} 份副本与真源一致")
        return 0
    changed = sync()
    if not changed:
        print(f"✅ 已是最新（{len(TARGETS)} 份副本）")
        return 0
    for p in changed:
        print(f"  ✅ {p.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
