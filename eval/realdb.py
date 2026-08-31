"""eval/realdb.py：T-129 内部回归基线——真实库副本通路 + 中文检索 GT 构造。

与 `retrieval_gt` 的分工：
- `retrieval_gt` 面向「评测用例 GT 记忆落进 eval DB」的**合成语料**；
- `realdb` 面向「已存在的生产库副本（memory.db 拷贝）」，直接在其上做检索回归，
  **不重新落库、不跑提炼、零 token**（边已存在：memory_archive.superseded_by /
  scene_memories，实测 9,822 + 20,699 条）。

GT 构造策略（GT = memory）：
- single-hop：抽样记忆 → query = llm_fn(content, id)（桩：内容关键词拼接）→ relevant=[id]
- multi-hop：从 **scene 簇**（成员均 live、可检索，≥2 条）取相关集，
  刻意制造 relevant 集合 ≥2 的查询，作为图召回（T-134）的前置护栏；
  supersession 边作为「解析」型单目标查询（相关集 = live 后继，可检索）。
- LLM 生成中文自然问句的调用**可注入**（`llm_fn`），本次用桩函数跑通链路；
  真正用 agnes 跑全量 GT 留待用户确认后再做（0-token 基线不受影响）。

可复现性：抽样 / 边选取均用 `random.Random(seed)` 固定，同 seed 两次运行
report.json 的 rrf.recall_at_k 逐字段相等。
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("eval.realdb")

FIXED_TS = "2026-01-01T00:00:00Z"


# ── GT 数据结构 ──

@dataclass
class RealDbGtItem:
    """单条真实库 GT：一个中文查询 + 其相关记忆集合 + hop 类型。"""
    query: str = ""
    relevant_ids: list[str] = field(default_factory=list)
    hop_type: str = "single"          # "single" | "scene" | "supersession"


@dataclass
class RealDbGt:
    """真实库副本 GT 容器。"""
    items: list[RealDbGtItem] = field(default_factory=list)
    source: str = "stub"              # "stub" | "llm"
    replica_path: str = ""

    def to_ground_truth(self) -> dict[str, list[str]]:
        """转成 `RRFGridSearch.search` 需要的 `{query: [relevant_ids]}`。

        query 重复时合并相关集（去重保序），避免后一条静默覆盖前一条。
        """
        gt: dict[str, list[str]] = {}
        for it in self.items:
            bucket = gt.setdefault(it.query, [])
            for mid in it.relevant_ids:
                if mid not in bucket:
                    bucket.append(mid)
        return gt

    def counts_by_hop(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.hop_type] = out.get(it.hop_type, 0) + 1
        return out

    def save(self, path: Path) -> Path:
        """保存 GT 到 JSON（可复用：避免每次重跑 LLM 生成，保证可复现 + 0-token）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.source,
            "replica_path": self.replica_path,
            "items": [
                {"query": it.query, "relevant_ids": it.relevant_ids, "hop_type": it.hop_type}
                for it in self.items
            ],
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path: Path) -> "RealDbGt":
        """从 JSON 加载 GT（与 `save` 互逆）。"""
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        items = [
            RealDbGtItem(
                query=it["query"],
                relevant_ids=list(it["relevant_ids"]),
                hop_type=it.get("hop_type", "single"),
            )
            for it in payload.get("items", [])
        ]
        return cls(
            items=items,
            source=payload.get("source", "unknown"),
            replica_path=payload.get("replica_path", ""),
        )


@dataclass
class MultiHopPair:
    """一条 multi-hop 边（用于构造相关集 ≥2 的 GT）。"""
    kind: str = "scene"               # "scene" | "supersession"
    anchor_id: str = ""
    anchor_content: str = ""
    related_ids: list[str] = field(default_factory=list)  # GT 相关集（均 live、可检索）


# ── 副本 I/O ──

def snapshot_replica(src: Path, dst: Path) -> Path:
    """把生产库 memory.db 干净拷贝成离线副本（在线备份，含 WAL 落盘）。

    直接拷文件可能读到半截页；用 sqlite3 在线 backup 保证副本事务一致。
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(f"file:{dst.as_posix()}", uri=False)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    logger.info("副本快照完成: %s → %s", src, dst)
    return dst


def open_replica(path: Path, readonly: bool = True) -> sqlite3.Connection:
    """打开副本连接（默认只读；0-token 基线只读副本，不回写）。"""
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(f"file:{path.as_posix()}", uri=False)
    conn.row_factory = sqlite3.Row
    return conn


def replica_corpus_stats(conn: sqlite3.Connection) -> dict:
    """读取副本语料规模 + 向量覆盖。"""
    size = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status != 'rejected'"
    ).fetchone()[0]
    vec = 0
    try:
        vec = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
    except sqlite3.Error:
        vec = 0
    return {
        "size": size,
        "vector_count": vec,
        "vector_coverage": round(vec / size, 4) if size else 0.0,
    }


# ── 抽样 / 边读取 ──

def sample_memories(conn: sqlite3.Connection, n: int, seed: int = 0) -> list[dict]:
    """确定性抽样 n 条 live 记忆（memory_id + content）。

    用 `random.Random(seed)` 固定抽样，保证可复现。
    """
    rows = conn.execute(
        "SELECT memory_id, content FROM memories "
        "WHERE content IS NOT NULL AND content != '' AND status != 'rejected'"
    ).fetchall()
    ids = [{"memory_id": r["memory_id"], "content": r["content"]} for r in rows]
    if n <= 0 or n >= len(ids):
        return ids
    rng = random.Random(seed)
    return rng.sample(ids, n)


def _content_of(conn: sqlite3.Connection, memory_id: str) -> str:
    row = conn.execute(
        "SELECT content FROM memories WHERE memory_id=?", (memory_id,)
    ).fetchone()
    return row["content"] if row else ""


def multi_hop_pairs(
    conn: sqlite3.Connection,
    limit: int | None = None,
    seed: int = 0,
) -> list[MultiHopPair]:
    """从副本读 multi-hop 边，产出 GT 相关集。

    - scene 簇：成员均 live（JOIN memories 过滤），相关集 = 整簇（≥2 条，可检索）
      → 这是图召回（T-134）的主要护栏来源
    - supersession：相关集 = live 后继（可检索），query 文本来自被取代的旧记忆内容
      （「旧话题应解析到新记忆」的解析型查询；旧记忆本身已归档不可检索，故不入相关集）

    边缺失（表不存在）时静默跳过，不炸调用方。
    """
    pairs: list[MultiHopPair] = []

    # 1) scene 簇（成员均为 live，可检索）
    try:
        rows = conn.execute(
            "SELECT sm.scene_id, GROUP_CONCAT(sm.memory_id) AS mids "
            "FROM scene_memories sm JOIN memories m ON m.memory_id = sm.memory_id "
            "WHERE m.status != 'rejected' GROUP BY sm.scene_id"
        ).fetchall()
        for r in rows:
            mids = [x for x in (r["mids"] or "").split(",") if x]
            if len(mids) < 2:
                continue
            content_by_id = {x: _content_of(conn, x) for x in mids}
            anchor = mids[0]
            pairs.append(MultiHopPair(
                kind="scene",
                anchor_id=anchor,
                anchor_content=content_by_id.get(anchor, ""),
                related_ids=list(mids),
            ))
    except sqlite3.Error as e:
        logger.warning("scene 边读取失败（跳过）: %s", e)

    # 2) supersession 解析（相关集 = live 后继，可检索）
    try:
        rows = conn.execute(
            "SELECT memory_id, superseded_by, content "
            "FROM memory_archive WHERE superseded_by IS NOT NULL"
        ).fetchall()
        for r in rows:
            live = r["superseded_by"]
            if not live:
                continue
            pairs.append(MultiHopPair(
                kind="supersession",
                anchor_id=r["memory_id"],
                anchor_content=r["content"] or "",
                related_ids=[live],
            ))
    except sqlite3.Error as e:
        logger.warning("supersession 边读取失败（跳过）: %s", e)

    if limit and len(pairs) > limit:
        rng = random.Random(seed)
        pairs = rng.sample(pairs, limit)
    return pairs


# ── GT 构造 ──

def _stub_query_fn(content: str, memory_id: str) -> str:
    """桩 query 生成（0-token，确定）：取内容关键词拼成「像用户会问」的问句。

    用 sgme.segment 分词取 ≥2 字的前几个词空格连接——BM25 可召回，
    且不含整段内容（非 content 模式那种退化形态）。真实 LLM 生成由 llm_fn 注入替代。
    """
    try:
        from sgme.segment import segment_terms
        terms = [t for t in segment_terms(content or "") if len(t) >= 2][:4]
    except Exception:
        terms = []
    if not terms:
        base = (content or memory_id or "").strip()
        return base[:12] if base else "记忆"
    return " ".join(terms)


def build_realdb_gt(
    conn: sqlite3.Connection,
    *,
    sample_n: int = 200,
    multi_hop_ratio: float = 0.3,
    seed: int = 0,
    llm_fn: Callable[[str, str], str] | None = None,
    source: str = "stub",
) -> RealDbGt:
    """在副本上构造中文检索 GT（GT = memory）。

    - single-hop：抽样记忆 → query=llm_fn(content,id) → relevant=[id]
    - multi-hop：从 scene/supersession 边取相关集（≥1 条，scene 簇 ≥2 条）→
      query=llm_fn(anchor_content, anchor_id) → relevant=整组
    - 占比 ≈ multi_hop_ratio（至少 0，若有边则至少含 1 条 multi）
    - LLM 调用可注入（llm_fn）；留空用桩函数，本次 0-token 跑通链路
    """
    llm_fn = llm_fn or _stub_query_fn

    single_n = max(0, int(sample_n * (1 - multi_hop_ratio)))
    hop_n = max(0, int(sample_n * multi_hop_ratio))

    singles = sample_memories(conn, single_n, seed=seed)
    pairs = multi_hop_pairs(conn, limit=hop_n, seed=seed)

    items: list[RealDbGtItem] = []
    for m in singles:
        q = llm_fn(m["content"], m["memory_id"])
        items.append(RealDbGtItem(
            query=q, relevant_ids=[m["memory_id"]], hop_type="single",
        ))
    for p in pairs:
        q = llm_fn(p.anchor_content, p.anchor_id)
        items.append(RealDbGtItem(
            query=q, relevant_ids=list(p.related_ids), hop_type=p.kind,
        ))

    logger.info(
        "构建真实库 GT: 共 %d 条 query（single=%d multi=%d），source=%s seed=%d",
        len(items),
        sum(1 for it in items if it.hop_type == "single"),
        sum(1 for it in items if it.hop_type != "single"),
        source, seed,
    )
    return RealDbGt(items=items, source=source)


# ── 合成 mini 副本（CI / 端到端，无需真实 NAS 拷贝） ──

def make_mini_replica(tmp_dir: Path, n: int = 12, seed: int = 0) -> Path:
    """构造最小但结构完整的 memory.db 副本（含 FTS 索引 + scene/supersession 边）。

    用于 CI 与端到端验证，无需真实 NAS 拷贝。返回 memory.db 路径。
    """
    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.data.search import init_fts

    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_dir)
    dims = sgme_config.load_dimensions()
    aliases = sgme_config.load_aliases()
    memory_dao.import_registry(mem_conn, dims, aliases)
    init_fts(mem_conn)

    dim_ids = [d["id"] for d in dims] or ["identity"]

    contents = [
        "张明在深圳做后端开发，主用 Python 和 Go，常用 FastAPI 框架",
        "李娜是产品经理，负责记忆引擎的需求梳理，偏好简洁的原型",
        "团队每周三上午开技术评审会，讨论本周的架构变更",
        "项目 SGME 使用 sqlite-vec 做向量检索，嵌入模型走 agnes",
        "用户吹吹风家的 NAS 出口 IP 是 171.40.166.121，位于湖北孝感电信",
        "记忆系统支持 multi-hop 检索，通过 superseded_by 和 scene_memories 构图",
        "LoCoMo 是长程对话记忆评测集，SGME 早期用它做基线对照",
        "RRF 融合默认 k=60，当前评测集上 rrf_k 对 NDCG 零区分度",
        "FTS5 的 unicode61 分词器对中文按标点整段切 token，召回极窄",
        "agent 主动关怀靠 signal_claim 原子认领后 signal_ack 回执",
        "turtle 主题记忆已归档，被 turtle-v2 取代（superseded_by 指向新记忆）",
        "场景「技术周会」聚合了评审会与架构变更两类记忆",
    ]
    rng = random.Random(seed)
    chosen = rng.sample(contents, min(n, len(contents)))

    mids: list[str] = []
    for i, c in enumerate(chosen):
        mid = f"mini#{i}"
        memory_dao.insert_memory(
            mem_conn,
            content=c,
            memory_type="persona",
            priority=50,
            time_velocity="static",
            ttl_days=None,
            dimension_ids=[dim_ids[i % len(dim_ids)]],
            sources=None,
            agent_tag="mini",
            memory_id=mid,
            created_at=FIXED_TS,
            updated_at=FIXED_TS,
        )
        mids.append(mid)

    # supersession 边：归档 mini#0 → 指向 mini#1（live 后继）
    if len(mids) >= 2:
        memory_dao.archive_memory(mem_conn, mids[0], superseded_by=mids[1])

    # scene 簇：scene-tech-weekly 关联 mini#2, mini#3（均 live，相关集 ≥2）
    # ⚠️ v0.7 起 scenes/scene_memories 已迁入 memory.db（非 wiki.db）
    if len(mids) >= 4:
        scene_id = "scene-tech-weekly"
        mem_conn.execute(
            "INSERT OR IGNORE INTO scenes"
            "(scene_id,title,content,heat,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (scene_id, "技术周会", "技术评审与架构变更", 1, "active", FIXED_TS, FIXED_TS),
        )
        for mid in (mids[2], mids[3]):
            mem_conn.execute(
                "INSERT OR IGNORE INTO scene_memories(scene_id,memory_id) VALUES(?,?)",
                (scene_id, mid),
            )
        mem_conn.commit()

    mem_conn.commit()
    mem_conn.close()
    session_conn.close()
    wiki_conn.close()
    logger.info("合成 mini 副本就绪: %s（记忆=%d，含 scene/supersession 边）", tmp_dir / "memory.db", len(mids))
    return tmp_dir / "memory.db"
