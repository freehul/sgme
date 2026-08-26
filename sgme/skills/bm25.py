"""sgme/skills/bm25.py：技能 BM25 内存索引（ST-36 M1 两步门——不建库）。

中文分词 jieba（镜像 wiki/fts.py 方案），纯内存打分，进程生命周期内复用；
记录集变化（内容 SHA 或数量）即整表重建（百条规模毫秒级，无需增量）。
"""
from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_SPLIT = re.compile(r"[\s\-_/.,:;!?()（）\[\]{}\"']+")
# jieba 延迟导入失败兜底（无 jieba 环境退化为字符 bigram，保底可搜）
_JIEBA_OK: bool | None = None


def _tokenize(text: str) -> list[str]:
    """中英混合分词：jieba 优先，退化 bigram；英文/数字整词保留。"""
    global _JIEBA_OK
    text = (text or "").lower()
    chunks = [c for c in _TOKEN_SPLIT.split(text) if c]
    toks: list[str] = []
    for ch in chunks:
        if ch.isascii():
            toks.append(ch)
            continue
        if _JIEBA_OK is None:
            try:
                import jieba

                _JIEBA_OK = True
            except Exception:
                _JIEBA_OK = False
        if _JIEBA_OK:
            try:
                import jieba

                toks.extend(t.strip() for t in jieba.lcut(ch) if t.strip())
            except Exception:
                toks.extend(_bigram(ch))
        else:
            toks.extend(_bigram(ch))
    return [t for t in toks if t]


def _bigram(s: str) -> list[str]:
    return [s[i:i + 2] for i in range(len(s) - 1)]


def _match_query(q: str) -> str:
    """FTS MATCH 查询构造（OR 连接，镜像 wiki fts._query_tokens）。"""
    parts = [t.replace('"', '""') for t in _tokenize(q)]
    return " OR ".join(f'"{p}"' for p in parts)


class SkillsBm25:
    """技能内存 BM25 索引：names 与 records 同序；score(query) 返回 {name: 分数}。

    Args:
        records: 技能记录列表（index_all 的产物）。
        k1/b: BM25 超参（业界默认 k1=1.5, b=0.75）。
    """

    def __init__(self, records, k1: float = 1.5, b: float = 0.75):
        self.records = list(records)
        self.k1, self.b = k1, b
        self._doc_tf: dict[str, Counter] = {}
        self._doc_len: dict[str, int] = {}
        self._df: Counter = Counter()
        self._doc_hash: dict[str, int] = {}
        self._build()

    def _build(self) -> None:
        for rec in self.records:
            text = " ".join([rec.name, rec.description, rec.category,
                             " ".join(rec.tags), rec.content or ""])
            tf = Counter(_tokenize(text))
            self._doc_tf[rec.name] = tf
            self._doc_len[rec.name] = sum(tf.values()) or 1
            self._doc_hash[rec.name] = hash(text)
            for term in tf:
                self._df[term] += 1

    def is_stale_for(self, records) -> bool:
        """候选记录集与当前索引是否不一致（数量或任一内容指纹变化）。"""
        if len(list(records)) != len(self._doc_len):
            return True
        for rec in records:
            want = " ".join([rec.name, rec.description, rec.category,
                             " ".join(rec.tags), rec.content or ""])
            if self._doc_hash.get(rec.name) != hash(want):
                return True
        return False

    def score(self, query: str) -> dict[str, float]:
        """BM25 打分：返回 {name: 分数}，零命中的名字不出现在结果里。"""
        q_terms = _tokenize(query)
        scores: dict[str, float] = {}
        n_docs = max(len(self._doc_len), 1)
        avgdl = (sum(self._doc_len.values()) / n_docs) or 1.0
        for name, tf in self._doc_tf.items():
            dl = self._doc_len[name]
            s = 0.0
            hit_any = False
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                hit_any = True
                df = self._df.get(term, 0)
                idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
                s += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / avgdl)
                )
            if hit_any and s > 0:
                scores[name] = s
        return scores


def rebuild_if_stale(index: SkillsBm25, records) -> SkillsBm25:
    """便捷函数：索引对候选记录集已过期则重建，否则复用。"""
    if index is None or index.is_stale_for(records):
        return SkillsBm25(records)
    return index
