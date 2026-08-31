"""sgme/data/search/stoplist.py：查询侧中英双语停用词表（T-130）。

查询侧与写入侧共用 ``sgme.segment.segment`` 分词口径（中文检索分词 v0.3 §1.3），
本模块在 ``segment`` 之后、拼 FTS5 MATCH 之前做**停用词过滤**：

- 去掉 who/with/a/the/的/了/谁/在 等功能词与高噪词，避免 OR 连接后
  常见词爆炸稀释 BM25 排序（自然语句类 precision 下降）。
- 仅过滤「非内容承载」词；领域词（NAS/server/深圳/飞盘…）一律保留，
  故对 T-129 基线 recall@k 无劣化（内容词不丢）。
- 全停用词场景（如「谁 和 在」）→ 调用方回退原 token，不直接空召回。

停用词判定按 segment 后的**整 token 精确匹配**（区分大小写仅对英文做小写归一）。
"""

from __future__ import annotations

# ---------- 英文停用词（功能词 + 检索无承载词） ----------
STOPWORDS_EN = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "else", "of", "in",
        "on", "at", "to", "for", "with", "by", "from", "as", "is", "are", "was",
        "were", "be", "been", "being", "am", "who", "whom", "whose", "what",
        "which", "when", "where", "why", "how", "this", "that", "these", "those",
        "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
        "them", "my", "your", "his", "their", "our", "its", "do", "does", "did",
        "has", "have", "had", "will", "would", "can", "could", "should", "may",
        "might", "must", "shall", "not", "no", "yes", "into", "out", "up", "down",
        "over", "under", "about", "than", "so", "such", "there", "here", "any",
        "all", "both", "each", "more", "most", "other", "some", "just", "only",
        "also", "very", "too", "own", "same", "between", "through", "during",
        "before", "after", "above", "below", "off", "again", "once", "because",
        "while", "whether", "although", "though", "against", "among", "around",
        "within", "without", "please", "thanks", "thank", "know", "want", "need",
        "like", "got", "get", "one", "two", "three", "first", "second", "last",
    }
)

# ---------- 中文停用词（功能词 + 疑问/指示/语气/连词） ----------
STOPWORDS_ZH = frozenset(
    {
        "的", "了", "吗", "呢", "吧", "啊", "呀", "哦", "嘛", "么", "是", "在",
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
        "这", "那", "这个", "那个", "这些", "那些", "哪个", "哪些", "哪儿", "哪里",
        "谁", "什么", "怎么", "怎样", "如何", "为什么", "哪", "里", "中", "上",
        "下", "和", "与", "跟", "同", "及", "或", "但", "却", "而", "因为",
        "所以", "如果", "就", "才", "都", "也", "还", "很", "太", "最", "个",
        "些", "们", "着", "过", "把", "被", "让", "给", "对", "从", "向", "往",
        "用", "等", "之", "其", "此", "该", "每", "各", "去", "来", "到", "会",
        "能", "要", "想", "知道", "一个", "一种", "有没有", "是否", "吗", "呢",
        "请问", "告诉", "关于", "一下", "进行", "已经", "可以", "应该", "现在",
        "今天", "昨天", "明天", "时候", "地方", "东西", "事情", "问题", "的话",
    }
)


def is_stopword(term: str) -> bool:
    """判定单个 segment token 是否为停用词（英文小写归一后比对）。"""
    if not term:
        return True
    t = term.strip()
    if not t:
        return True
    if t in STOPWORDS_ZH:
        return True
    # 英文大小写归一
    if t.lower() in STOPWORDS_EN:
        return True
    return False


def filter_stopwords(tokens: list[str]) -> list[str]:
    """过滤停用词，保留内容承载 token（顺序不变、去空白、去空串）。

    - 仅做精确 token 匹配，不做子串/前缀匹配（避免误伤「在野」「要么」等）。
    - 返回可能为空（全停用词）——调用方需自行决定回退策略。
    """
    out: list[str] = []
    for t in tokens:
        s = t.strip()
        if not s:
            continue
        if is_stopword(s):
            continue
        out.append(s)
    return out
