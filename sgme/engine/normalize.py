"""engine/normalize.py：维度归一化。

流程（§1）：
1. 预归一化：strip / 全角→半角 / 小写
2. 精确匹配：alias_map（alias→dimension_id）或 display_name 全等
3. 相似度兜底：difflib.SequenceMatcher ratio ≥ 0.85（与 display_name/aliases 取最大）
4. 命中 → dimension_id；未命中 → None（丢弃 + 计数）

fuzzy 命中审计：相似度兜底命中记录明细（原始名→目标id,score）。
不自动注册：未知标签丢弃 + 告警。
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger("sgme.engine.normalize")

# 相似度阈值（§1，初值 0.85，评测后校准）
FUZZY_THRESHOLD = 0.85

# 单批丢弃率告警阈值（§2）
DROP_RATE_WARN = 0.20


@dataclass
class NormalizeStats:
    """归一化批次统计。"""
    total: int = 0           # 输入标签总数
    alias_hits: int = 0      # 精确匹配命中（别名或 display_name）
    fuzzy_hits: int = 0      # 相似度兜底命中
    drops: int = 0           # 未知丢弃
    fuzzy_audit: list[dict] = field(default_factory=list)  # fuzzy 命中明细
    dropped_names: list[str] = field(default_factory=list)  # 丢弃的原始名

    @property
    def normalize_hits(self) -> int:
        return self.alias_hits + self.fuzzy_hits

    @property
    def drop_rate(self) -> float:
        return self.drops / self.total if self.total else 0.0


# ---------- 预归一化 ----------

def _pre_normalize(name: str) -> str:
    """预归一化：strip + 全角→半角 + 大小写归一（中文不变）。"""
    if not name:
        return ""
    # NFKC：全角→半角（含数字/字母/标点）
    s = unicodedata.normalize("NFKC", name)
    s = s.strip()
    # 英文小写（中文不受影响）
    s = s.lower()
    return s


# ---------- 单标签归一化 ----------

def normalize_dimension(
    name: str,
    alias_map: dict[str, str],
    registry_names: dict[str, str],
) -> tuple[str | None, str, float | None]:
    """归一化单个维度标签。

    - alias_map: {alias: dimension_id}（含 display_name 与别名）
    - registry_names: {dimension_id: display_name}
    - 返回 (dimension_id | None, hit_type, score)
      hit_type ∈ {'alias', 'fuzzy', 'drop'}
    """
    pre = _pre_normalize(name)
    if not pre:
        return None, "drop", None

    # ① 注册表 id 精确匹配（LLM 可能直接输出提示词清单中的英文 id，如 "identity"）
    if pre in registry_names:
        return pre, "alias", 1.0

    # ② 预归一化后别名精确匹配（alias_map 的 key 也需预归一化，但调用方应保证已归一）
    # 这里对 alias_map 的 key 也做预归一化（运行时构建一次更优，但此处简单实现每次算）
    for alias, dim_id in alias_map.items():
        if _pre_normalize(alias) == pre:
            return dim_id, "alias", 1.0

    # ③ registry display_name 精确匹配
    for dim_id, display in registry_names.items():
        if _pre_normalize(display) == pre:
            return dim_id, "alias", 1.0

    # ④ 相似度兜底：与 display_name/aliases 取最大
    best_id: str | None = None
    best_score: float = 0.0
    # 候选 = display_name + aliases
    candidates: list[tuple[str, str]] = []  # (text, dim_id)
    for dim_id, display in registry_names.items():
        candidates.append((display, dim_id))
    for alias, dim_id in alias_map.items():
        candidates.append((alias, dim_id))

    for cand_text, dim_id in candidates:
        cand_pre = _pre_normalize(cand_text)
        score = difflib.SequenceMatcher(None, pre, cand_pre).ratio()
        if score > best_score:
            best_score = score
            best_id = dim_id

    if best_score >= FUZZY_THRESHOLD and best_id:
        return best_id, "fuzzy", best_score

    # ⑤ 未命中 → 丢弃
    return None, "drop", None


# ---------- 批量归一化 ----------

def normalize_batch(
    names: list[str],
    alias_map: dict[str, str],
    registry_names: dict[str, str],
) -> tuple[list[str], NormalizeStats]:
    """批量归一化维度标签。

    - 返回 (归一化后的 dimension_id 列表 [去重保序], 统计)
    - 单个失败不影响其余
    - 丢弃率 > 20% → 标记 anomaly_warn（通过 stats.drop_rate 判定，调用方检查）
    """
    stats = NormalizeStats(total=len(names))
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        dim_id, hit_type, score = normalize_dimension(name, alias_map, registry_names)
        if dim_id is None:
            stats.drops += 1
            stats.dropped_names.append(name)
            logger.warning("维度标签丢弃: %r (未匹配注册表/别名)", name)
            continue
        if hit_type == "alias":
            stats.alias_hits += 1
        elif hit_type == "fuzzy":
            stats.fuzzy_hits += 1
            stats.fuzzy_audit.append({
                "original": name, "target_id": dim_id, "score": round(score, 4),
            })
            logger.info("fuzzy 命中: %r → %s (score=%.3f)", name, dim_id, score)
        if dim_id not in seen:
            seen.add(dim_id)
            result.append(dim_id)

    if stats.drop_rate > DROP_RATE_WARN:
        logger.warning(
            "归一化丢弃率 %.1f%% > 20%% 阈值（drops=%d/%d）→ anomaly_warn",
            stats.drop_rate * 100, stats.drops, stats.total,
        )
    return result, stats


def should_warn(stats: NormalizeStats) -> bool:
    """是否应产 anomaly_warn（丢弃率 > 20%）。"""
    return stats.drop_rate > DROP_RATE_WARN
