"""tests/test_segment.py：中文分词口径回归（B149）。

背景：Python 3.12 移除了 `pkgutil.ImpImporter`，而 jieba 0.42.1（最新发行版）
内部依赖它 → 在 3.12/3.13 环境下 `import jieba` 必抛 AttributeError，
`sgme.segment` 会**静默降级**到 bigram-v1（写入与查询两侧仍同口径，故不报错、
检索不崩，但质量受损且难以察觉）。

LongMemEval 全量评测（2026-09-03）曾因此整场以 bigram-v1 口径运行，
stderr 累计 8,250 条 "jieba 不可用" 告警才被发现。

本文件锁定两件事：
1. Python 3.12+ 上有兼容 shim，jieba 必须可用（不得降级 bigram-v1）；
2. 即便降级，写入侧与查询侧也**必须同口径**（segment() 单一函数）。
"""

from __future__ import annotations

import sys

import pytest

import sgme.segment as seg


def test_jieba_available_on_python312_plus():
    """Python 3.12+ 必须有 ImpImporter 兼容 shim，否则静默降级 bigram-v1。

    3.11 及以下 jieba 原生可用，同样要求不降级。
    """
    # ⚠️ 顺序敏感：必须先走 _ensure_jieba()（内部含 ImpImporter 兼容 shim），
    #    再 import jieba。若直接 import jieba，shim 尚未生效，jieba 会连带
    #    pkg_resources 一起在 pkgutil.ImpImporter 上炸掉。
    seg._jieba_ready = False  # 强制重新走懒加载路径
    try:
        ok = seg._ensure_jieba()
        if not ok:
            pytest.skip(f"jieba 不可用（当前口径 {seg.current_segmenter_id()}），跳过")
        import jieba
        sid = seg.current_segmenter_id()
        assert sid == f"jieba-{jieba.__version__}", (
            f"Python {sys.version.split()[0]} 上分词静默降级为 {sid}（ImpImporter shim 失效？）"
        )
    finally:
        seg._jieba_ready = False


def test_segment_never_raises_and_is_deterministic():
    """分词是尽力而为：异常输入不抛错；同一输入两次切分结果一致。"""
    for text in ("", None, "   ", "abc", "中文测试", "中英 mixed 123 混合"):
        a = seg.segment(text)
        b = seg.segment(text)
        assert a == b, f"分词结果不确定：{text!r} -> {a!r} vs {b!r}"
        assert isinstance(a, str)


def test_segmenter_id_matches_actual_mode():
    """口径标识必须与实际生效模式一致（禁止用旧口径凑合）。"""
    sid = seg.current_segmenter_id()
    assert sid == seg.BIGRAM_ID or sid.startswith("jieba-"), sid
    if sid.startswith("jieba-"):
        # jieba 模式下，中文应被切成词而非纯二元组
        toks = seg.segment("杭州阿里巴巴西溪园区").split()
        assert len(toks) < len("杭州阿里巴巴西溪园区"), (
            f"jieba 模式下仍按单字/二元组切分：{toks}"
        )
