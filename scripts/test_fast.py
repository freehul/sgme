# -*- coding: utf-8 -*-
"""test_fast.py：按关键词/改动文件快速跑相关模块测试（避免每次提交全量 pytest）。

用法（在项目根任意位置）：
    python scripts/test_fast.py <关键词...>   # 匹配 tests/ 下文件名（如 wiki、mcp、ideas）
    python scripts/test_fast.py               # 按 git diff HEAD 自动推导改动文件的测试
    python scripts/test_fast.py --all         # 全量 pytest（里程碑/发布/跨模块重构用）

设计（2026-08-13 用户定分档）：
- 常规改动：相关模块测试秒级验证，够绿即提交
- 全量：仅里程碑/发布/跨模块重构后跑（约 10 分钟，零 LLM/网络消耗）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
TESTS_DIR = os.path.join(BASE, "tests")
# 路径中无信息量的目录名，不作为关键词
_STOPWORDS = {"sgme", "server", "operations", "data", "engine", "adapters", "tests", "scripts", "wiki"}


def derive_keywords() -> tuple[set[str], list[str]]:
    """从 git diff HEAD 的改动文件推导关键词。"""
    out = subprocess.run(
        ["git", "diff", "HEAD", "--name-only"], capture_output=True, text=True, cwd=BASE
    ).stdout
    files = [l for l in out.splitlines() if l.strip()]
    kws: set[str] = set()
    for f in files:
        if f.startswith("docs/") or not f.endswith(".py"):
            continue
        base = os.path.basename(f)
        kws.add(re.sub(r"\.py$", "", base))
        parts = re.split(r"[/\\]", f)
        for p in parts[:-1]:
            if p and p not in _STOPWORDS:
                kws.add(p)
    return kws, files


def find_tests(kws: set[str]) -> list[str]:
    """在 tests/ 下按文件名子串匹配（关键词长度 ≥2 防误匹配）。"""
    kws = {k.lower() for k in kws if len(k) >= 2}
    if not kws:
        return []
    matched: set[str] = set()
    for tf in sorted(os.listdir(TESTS_DIR)):
        if not (tf.startswith("test_") and tf.endswith(".py")):
            continue
        low = tf.lower()
        if any(k in low for k in kws):
            matched.add(os.path.join(TESTS_DIR, tf))
    return sorted(matched)


def main() -> None:
    args = sys.argv[1:]
    os.chdir(BASE)

    if "--all" in args:
        print("全量 pytest（约 10 分钟，零 LLM/网络）...")
        sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=BASE).returncode)

    if args:
        kws = set(args)
    else:
        kws, files = derive_keywords()
        if not files:
            print("工作区无 git diff HEAD 改动，跳过 pytest")
            return
        print("改动文件:", ", ".join(files))

    matched = find_tests(kws)
    if not matched:
        print(f"关键词 {sorted(kws)} 未匹配到测试文件（纯文档/配置改动？），跳过 pytest")
        return
    print(f"关键词: {sorted(kws)}")
    print("匹配测试:", ", ".join(os.path.relpath(m, BASE) for m in matched))
    # 项目 pytest 配置抑制 summary 行——手动数点汇报数字（汇报铁律：贴 passed/failed）
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"] + matched, cwd=BASE,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = proc.stdout + proc.stderr
    dots = out.count(".")
    fails = out.count("FAILED") + out.count("ERROR")  # 警告路径含 File，不能数裸 F/E
    print(f"结果: {dots} passed / {fails} failed / exit={proc.returncode}")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
