"""eval/embed_cache.py：embedding 磁盘缓存（评测可复现性基础设施）。

问题背景（为什么必须有这个模块）
--------------------------------
RRF 评测的向量通路依赖外部 embeddings 端点（LM Studio）。这带来三个致命后果：

1. **不可离线复现**：CI/无端点环境下向量通路整条消失，`vector_available=False`，
   RRF 退化单路，评测结论与本地跑出来的完全不是一回事。
2. **跨 run 漂移**：端点本身即使确定性，只要中途有一条请求超时/被限流，
   语料的向量覆盖率就会变化（84/84 vs 41/84），NDCG 随之整段漂移
   （实测 0.9546 → 0.5691）。可复现性验收永远过不了。
3. **慢**：84 条语料 + 50 条 query 每次 run 都要打 130+ 次 HTTP。

解决办法：按 `sha256(content) + model_name` 做磁盘缓存，命中直接返回向量。
真实跑一次后把缓存库归档进 git（`eval/fixtures/embed_cache_v001.sqlite`），
此后所有 run（含 CI）全部命中缓存 ⇒ **零网络、逐字段可复现**。

三条设计约定
------------
1. **key = sha256(utf-8 content) + model**。
   带上 model 是为了换模型时天然不串味（不同模型的向量不可混检）。

2. **dims 不匹配视为未命中**。
   同名模型换代（如 nomic v1.5 → v2，768 → 1024 维）时 key 不变但向量维度变了，
   若照旧命中会把新旧维度的向量混进同一个 `memory_vectors` 表，
   `_numpy_cosine_search` 会静默 `continue` 掉维度不符的行——**不报错，只是召回变少**。
   本模块用「进程内 dims 锁定 + 可选 expected_dims」双保险把这种脏命中挡掉。

3. **存 float32 little-endian**（与 `sgme.search.vector._serialize_vector` 同精度）。
   落库与检索两条路径最终都会把向量压成 float32
   （`_serialize_vector` 打包 float32、`_numpy_cosine_search` 用 `np.float32` 读），
   因此缓存存 float32 后，「缓存命中 run」与「真实请求 run」写进 DB 的 BLOB
   **逐字节相同**，不会引入任何精度差异导致的排序抖动。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("eval.embed_cache")

# 项目根目录（eval/ 与 sgme/ 并列）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 归档目录与默认缓存库路径（真实跑一次后提交进 git，保证 CI 离线可复现）
FIXTURES_DIR = PROJECT_ROOT / "eval" / "fixtures"
CACHE_VERSION = "v001"
DEFAULT_CACHE_PATH = FIXTURES_DIR / f"embed_cache_{CACHE_VERSION}.sqlite"

# 单个 float32 的字节数
_FLOAT32_SIZE = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embed_cache (
    content_sha256 TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    dims           INTEGER NOT NULL,
    vector         BLOB    NOT NULL,
    written_at     TEXT    NOT NULL,
    PRIMARY KEY (content_sha256, model)
);
"""


def content_key(text: str) -> str:
    """内容指纹：`sha256(utf-8)` 十六进制串。

    `None` / 空串统一映射为空串的 sha256（不抛异常，缓存层不该成为故障点）。
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def pack_vector(vec: Iterable[float]) -> bytes:
    """向量 → float32 little-endian BLOB。"""
    values = [float(x) for x in vec]
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> list[float]:
    """float32 little-endian BLOB → 向量。"""
    count = len(blob) // _FLOAT32_SIZE
    if count <= 0:
        return []
    return list(struct.unpack(f"<{count}f", blob))


@dataclass
class EmbedCacheStats:
    """缓存命中统计（写进 report.json 的 rrf 段，供复现性排障）。"""

    hits: int = 0
    misses: int = 0
    writes: int = 0
    dims_mismatch: int = 0
    corrupt: int = 0

    def as_dict(self) -> dict:
        """转成可 JSON 序列化的 dict。"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "dims_mismatch": self.dims_mismatch,
            "corrupt": self.corrupt,
        }


class EmbedCache:
    """`sha256(content) + model` → 向量 的 SQLite 磁盘缓存。

    典型用法（评测 runner）::

        cache = EmbedCache(EmbedCache.default_path())
        prev = sgme_vector.set_embed_cache(cache)
        try:
            ...  # 期间所有 sgme.search.vector.embed() 调用自动走缓存
        finally:
            sgme_vector.set_embed_cache(prev)
            cache.close()

    线程安全性：`check_same_thread=False` + 单连接串行使用。
    评测流水线是单线程的，这里不额外加锁（加了也只是掩盖误用）。
    """

    def __init__(
        self,
        path: Path | str | None = None,
        expected_dims: int | None = None,
        seed_path: Path | str | None = None,
        readonly: bool = False,
    ):
        """打开（必要时创建）缓存库。

        path: 缓存库路径，None ⇒ `DEFAULT_CACHE_PATH`
        expected_dims: 期望维度；非 None 时，dims 不等于它的行一律视为未命中
        seed_path: 种子库；`path` 不存在而种子存在时先整库拷贝过去
                   （用于「归档 fixture 只读、运行时写副本」的场景）
        readonly: True ⇒ `put()` 静默不写盘（仍计 misses，便于验证缓存完整性）
        """
        self.path = Path(path) if path is not None else Path(DEFAULT_CACHE_PATH)
        self.expected_dims = expected_dims
        self.readonly = readonly
        self.stats = EmbedCacheStats()

        # model → 本进程首次见到的 dims。后续该 model 的行 dims 不一致即视为未命中，
        # 防御「同名模型换代」把不同维度的向量混进同一次评测。
        self._locked_dims: dict[str, int] = {}
        if expected_dims is not None and expected_dims > 0:
            self._default_lock: int | None = int(expected_dims)
        else:
            self._default_lock = None

        self.path.parent.mkdir(parents=True, exist_ok=True)

        if seed_path is not None and not self.path.exists():
            seed = Path(seed_path)
            if seed.exists():
                shutil.copyfile(seed, self.path)
                logger.info("embedding 缓存：从种子库拷贝 %s → %s", seed, self.path)

        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── 便捷入口 ──

    @staticmethod
    def default_path() -> Path:
        """归档缓存库的默认路径（`eval/fixtures/embed_cache_v001.sqlite`）。"""
        return Path(DEFAULT_CACHE_PATH)

    # ── 读写 ──

    def _lock_for(self, model: str) -> int | None:
        """取该 model 当前锁定的 dims（未锁定返回 None）。"""
        if model in self._locked_dims:
            return self._locked_dims[model]
        return self._default_lock

    def get(self, text: str, model: str) -> list[float] | None:
        """查缓存。命中返回向量，未命中返回 None。

        判定为**未命中**的三种情况（均计入 stats，不抛异常）：
        - 库中无该 (sha256, model) 行
        - 行的 `dims` 与当前锁定维度不一致（模型换代 ⇒ 脏命中，必须挡掉）
        - BLOB 长度与 `dims` 对不上（写入被截断，视为损坏）
        """
        key = content_key(text)
        try:
            cur = self.conn.execute(
                "SELECT dims, vector FROM embed_cache "
                "WHERE content_sha256 = ? AND model = ?",
                (key, model),
            )
            row = cur.fetchone()
        except sqlite3.Error as e:
            logger.warning("embedding 缓存读取失败（降级为未命中）: %s", e)
            self.stats.misses += 1
            return None

        if row is None:
            self.stats.misses += 1
            return None

        dims = int(row[0])
        blob = row[1]

        lock = self._lock_for(model)
        if lock is not None and dims != lock:
            logger.warning(
                "embedding 缓存 dims 不匹配（model=%s 期望 %d 实得 %d），视为未命中；"
                "疑似模型换代，请删除或升级缓存库 %s",
                model, lock, dims, self.path,
            )
            self.stats.dims_mismatch += 1
            self.stats.misses += 1
            return None

        if not isinstance(blob, (bytes, bytearray)) or len(blob) != dims * _FLOAT32_SIZE:
            logger.warning(
                "embedding 缓存行损坏（model=%s dims=%d blob=%s 字节），视为未命中",
                model, dims, len(blob) if blob is not None else "None",
            )
            self.stats.corrupt += 1
            self.stats.misses += 1
            return None

        vec = unpack_vector(bytes(blob))
        self._locked_dims.setdefault(model, dims)
        self.stats.hits += 1
        return vec

    def put(self, text: str, model: str, vector: Iterable[float]) -> bool:
        """写缓存（同 key 覆盖）。返回是否真正写盘。

        `readonly=True` 或向量为空时不写。
        dims 与当前锁定维度冲突时同样拒写（宁可不缓存，也不污染归档库）。
        """
        values = [float(x) for x in (vector or [])]
        if not values:
            return False
        if self.readonly:
            return False

        dims = len(values)
        lock = self._lock_for(model)
        if lock is not None and dims != lock:
            logger.warning(
                "embedding 缓存拒绝写入：model=%s 锁定 dims=%d，本次向量 dims=%d",
                model, lock, dims,
            )
            self.stats.dims_mismatch += 1
            return False

        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO embed_cache "
                "(content_sha256, model, dims, vector, written_at) VALUES (?, ?, ?, ?, ?)",
                (
                    content_key(text),
                    model,
                    dims,
                    pack_vector(values),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            logger.warning("embedding 缓存写入失败（不影响本次评测）: %s", e)
            return False

        self._locked_dims.setdefault(model, dims)
        self.stats.writes += 1
        return True

    # ── 运维 ──

    def row_count(self) -> int:
        """缓存条目总数。"""
        try:
            cur = self.conn.execute("SELECT COUNT(*) FROM embed_cache")
            return int(cur.fetchone()[0])
        except sqlite3.Error:
            return 0

    def models(self) -> list[str]:
        """库中出现过的模型名（升序）。"""
        try:
            cur = self.conn.execute(
                "SELECT DISTINCT model FROM embed_cache ORDER BY model"
            )
            return [r[0] for r in cur.fetchall()]
        except sqlite3.Error:
            return []

    def stats_dict(self) -> dict:
        """命中统计 + 库规模（写进 report.json）。"""
        d = self.stats.as_dict()
        d["rows"] = self.row_count()
        d["path"] = str(self.path)
        d["readonly"] = self.readonly
        return d

    def close(self) -> None:
        """关闭底层连接（幂等）。"""
        try:
            self.conn.close()
        except Exception as e:  # pragma: no cover - 关闭失败不影响评测结论
            logger.warning("关闭 embedding 缓存连接失败: %s", e)

    def __enter__(self) -> "EmbedCache":
        """上下文管理器入口。"""
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """上下文管理器出口：关闭连接。"""
        self.close()
