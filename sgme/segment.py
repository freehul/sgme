"""sgme/segment.py：中文分词顶层公共模块（零内部依赖）。

写入侧（storage/memory_dao）与查询侧（search）共用同一分词口径，保证
FTS5 索引列与查询串一致——否则精确 token 匹配必然错位（中文检索分词方案
v0.3 §1.3：两层共用同一函数）。

分层：
- 主模式：jieba 分词（`jieba.cut`，保留英文/数字原词）
- 降级模式：bigram-v1（jieba 未安装时），自研二元组切分，同口径兜底

★ 懒加载硬约束：模块 import 时**不得** `import jieba`、不得触发词典构建。
首次调用 `segment()`/`segment_terms()`/`current_segmenter_id()` 时才
`import jieba` + `jieba.initialize()`（模块级 `_jieba_ready` 标志守护，
仅一次）。否则 pytest 收集期与 server 启动各白付 1~2s 词典加载。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("sgme.segment")

# 降级模式标识（bigram-v1：自研二元组切分，零依赖）
BIGRAM_ID = "bigram-v1"

# 模块级标志：jieba 是否已就绪（懒加载守护，首次调用成功后置 True）
_jieba_ready = False


def _ensure_jieba() -> bool:
    """懒加载 jieba：首次调用时 import + initialize（幂等、可重入）。

    失败（未安装/初始化异常）返回 False → 调用方降级 bigram-v1。
    不抛异常：分词的职责是「尽力给出可用 token」，不允许因依赖缺失炸掉写入路径。
    """
    global _jieba_ready
    if _jieba_ready:
        return True
    try:
        # Python 3.12+ 兼容 shim：jieba 0.42.1（最新发行版）内部依赖
        # `pkgutil.ImpImporter`，该 API 已于 3.12 移除 → 直接 import 必抛
        # AttributeError 而静默降级 bigram-v1（全量评测曾因此整场以降级口径跑，
        # 写入与查询两侧仍同口径故不报错，但检索质量受损且不可观测）。
        # 补一个 zipimporter 别名即可恢复 jieba；已在 3.13 实测通过。
        import pkgutil
        if not hasattr(pkgutil, "ImpImporter"):
            pkgutil.ImpImporter = pkgutil.zipimporter  # type: ignore[attr-defined]

        import jieba  # 首次调用才 import（模块 import 时不得触发词典构建）
        jieba.initialize()  # 显式构建词典（首次约 1~2s）
        _jieba_ready = True
        logger.info("segment: jieba 分词器已就绪（%s）", jieba.__version__)
        return True
    except Exception as e:  # noqa: BLE001 - ImportError 等任何加载失败 → 降级
        _jieba_ready = False
        logger.warning("segment: jieba 不可用，降级 bigram-v1: %s", e)
        return False


def current_segmenter_id() -> str:
    """当前运行时生效的分词器标识：`jieba-<version>` | `bigram-v1`。

    供 `fts_meta.segmenter` 持久化与口径漂移比对（方案 §3.5 缺口 A：
    口径不一致必须可观测，禁止静默降级后用旧口径凑合）。
    """
    if _ensure_jieba():
        import jieba
        return f"jieba-{jieba.__version__}"
    return BIGRAM_ID


def segment(text: str | None) -> str:
    """把文本切成空格分隔的 token 串（供 FTS5 索引列与查询串使用）。

    - jieba 模式：`jieba.cut` 保留英文/数字原词
    - bigram-v1 降级：按空白分段后逐段 2-gram（同口径兜底）
    """
    terms = segment_terms(text)
    return " ".join(terms)


def segment_terms(text: str | None) -> list[str]:
    """返回 token 列表（供 LIKE 兜底等需要词表的场景）。

    与 `segment(text).split()` 严格一致（同一分词口径）。
    """
    if text is None:
        return []
    text = str(text).strip()
    if not text:
        return []
    if _ensure_jieba():
        import jieba
        return [t for t in jieba.cut(text) if t and t.strip()]
    return _bigram_segment(text)


def _bigram_segment(text: str) -> list[str]:
    """bigram-v1：按空白分段，段内逐字符 2-gram（零依赖兜底，同口径）。

    例："深圳 后端开发" → ["深圳", "后端", "端开", "开发"]。
    跨段不生成二元组，避免空格/标点切出的假词；单字段原样保留。
    """
    terms: list[str] = []
    for part in re.split(r"\s+", text):
        if not part:
            continue
        if len(part) == 1:
            terms.append(part)
            continue
        terms.extend(part[i:i + 2] for i in range(len(part) - 1))
    return terms
