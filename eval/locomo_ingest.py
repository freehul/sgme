"""eval/locomo_ingest.py：LoCoMo 语料灌库（零 token 直灌）+ dia_id→memory_id 索引。

与 `locomo.py` 的分工：
- `locomo.py`：解析 JSON → conversation/session/turn/QA（含 dia_id 定位）
- **本模块**：把 turn/session 直灌进一个**独立的 memory.db 副本**，并产出
  `dia_id → [memory_id]` 索引（GT 映射的桥梁）
- `locomo_eval.py`：拿索引把 QA.evidence（dia_id）翻成相关集，跑检索评测

★ 为什么走「直灌」而不是「真实提炼链路」（`append` + `refine_trigger`）：
1. **成本**：全量 10 conversation 灌入经 l1_extraction + l1_conflict + l2_scene
   实测推算 350~380 万 tokens / 1-2 小时（见方案 v0.2 审查意见 S1）；
2. **职责**：ST-40 的验收口径是**检索侧**的 recall@k 与端到端 J-score，
   直灌不掺入「提炼质量」这个额外变量，指标更干净、可归因；
3. **可复现**：直灌是确定性的（无 LLM 温度），同 seed 两次跑结果逐字节相等。

代价与边界（必须在报告写明，不能含糊）：
- 直灌**不产出 memory_edges**（无提炼就无语义边/冲突边）→ 本通路**测不到图召回**；
- 直灌**不测提炼质量**（L1 维度标注 / L1.5 冲突裁决 / L2 场景），那部分由
  `eval/run.py` 的中文用例集（v001_baseline）承担；
- 因此本模块出的数字只能与「同样直灌口径」的基线横向比，不能直接当作
  SGME 端到端生产效果的等价物。

粒度（granularity，默认 `turn`，与 mem0 的 LoCoMo 评测灌库口径一致）：
- `turn`：每轮对话 1 条记忆（5,882 条）——GT 映射精确 1:1，召回难度最高
- `window`：滑窗 N 轮 1 条记忆（默认 5 轮，~1,180 条）——折中，兼顾上下文连贯
- `session`：每次会话 1 条记忆（272 条）——GT 映射退化为 session 级（粗粒度），
  recall 会被系统性抬高（相关集不变但候选变少），仅供粒度对照，不作主口径

时间上下文（`with_date`，默认开）：
LoCoMo 的 temporal 类 QA（321 条）问「When did X...」，gold answer 是日期，
而对话原文常写作「yesterday」这类相对时间——**不带 session 日期灌库，
temporal 类既检索不到也无法作答**。故默认在 chunk 头部加 `[{date_time}]`。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data.search import init_fts

from eval.locomo import LocomoConversation, LocomoTurn

logger = logging.getLogger("eval.locomo_ingest")

FIXED_TS = "2026-01-01T00:00:00Z"

GRANULARITIES = ("turn", "window", "session")

# LoCoMo session 时间形如 "1:56 pm on 8 May, 2023"
_DATE_FORMATS = (
    "%I:%M %p on %d %B, %Y",
    "%I:%M %p on %d %B %Y",
    "%d %B, %Y",
)


def parse_locomo_datetime(s: str) -> str | None:
    """LoCoMo 时间字符串 → ISO（UTC 名义）。解析失败返回 None。

    只用于填 occurred_at（记忆事件发生时刻），失败时回退 FIXED_TS，
    **绝不**因此中断灌库——时间只是检索辅助信息，不是 GT 正确性依赖。
    """
    s = (s or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


# ── 配置 ──

@dataclass
class IngestConfig:
    """灌库配置（所有字段进报告，保证可复现）。"""
    granularity: str = "turn"        # turn / window / session
    window: int = 5                  # window 粒度：每 chunk 的 turn 数
    stride: int = 5                  # window 粒度：滑动步长（=window 即不重叠）
    with_date: bool = True           # chunk 头部加 session 日期（temporal 类必需）
    speaker_prefix: bool = True      # 每轮前缀 "Speaker: "
    dimension_id: str = "social"     # LoCoMo 是双人社交对话，维度只作标签不参与过滤
    agent_tag: str = "locomo"
    memory_type: str = "episodic"
    # ★ 每个 conversation 用独立 agent_tag（默认开）——LoCoMo 的 10 个 conversation
    #   是 10 组**互不相识的人**，评测时必须按 conversation 隔离检索
    #   （Mem0 的 LoCoMo 评测同样是每个 conversation 一个 user_id）。
    #   同时 dia_id（`D8:6`）只在单个 conversation 内唯一，跨 conv 会撞号，
    #   GT 索引因此必须按 conv 作用域建（见 LocomoIndex.dia_to_mem 的 key 形态）。
    per_conv_agent_tag: bool = True

    def as_dict(self) -> dict:
        return {
            "granularity": self.granularity,
            "window": self.window,
            "stride": self.stride,
            "with_date": self.with_date,
            "speaker_prefix": self.speaker_prefix,
            "dimension_id": self.dimension_id,
            "agent_tag": self.agent_tag,
            "memory_type": self.memory_type,
            "per_conv_agent_tag": self.per_conv_agent_tag,
        }


@dataclass
class Chunk:
    """一个待灌库的文本块（= 一条记忆）。"""
    memory_id: str = ""
    content: str = ""
    dia_ids: list[str] = field(default_factory=list)
    occurred_at: str | None = None


# ── 切块 ──

def _render_body(turns: list[LocomoTurn], cfg: IngestConfig) -> str:
    lines: list[str] = []
    for t in turns:
        line = f"{t.speaker}: {t.text}" if cfg.speaker_prefix else t.text
        lines.append(line.strip())
    return "\n".join(lines)


def build_chunks(
    conv: LocomoConversation,
    cfg: IngestConfig,
) -> list[Chunk]:
    """把一个 conversation 切成 chunks（按粒度）。

    window 粒度**不跨 session**——跨天对话拼在一起会让时间上下文自相矛盾
    （temporal 类的 gold answer 依赖「哪一天」）。
    """
    out: list[Chunk] = []
    for sess in conv.sessions:
        if not sess.turns:
            continue
        head = f"[{sess.date_time}] " if (cfg.with_date and sess.date_time) else ""
        occurred = parse_locomo_datetime(sess.date_time)
        prefix = f"{conv.sample_id}|S{sess.session_idx}"

        if cfg.granularity == "session":
            out.append(Chunk(
                memory_id=f"{prefix}",
                content=(head + "\n" + _render_body(sess.turns, cfg)).strip(),
                dia_ids=[t.dia_id for t in sess.turns],
                occurred_at=occurred,
            ))
            continue

        if cfg.granularity == "turn":
            for t in sess.turns:
                out.append(Chunk(
                    memory_id=f"{prefix}|T{t.turn_idx}",
                    content=(head + f"{t.speaker}: {t.text}".strip()).strip()
                    if cfg.speaker_prefix else (head + t.text.strip()).strip(),
                    dia_ids=[t.dia_id],
                    occurred_at=occurred,
                ))
            continue

        # window
        n = max(1, cfg.window)
        step = max(1, cfg.stride)
        seq = 0
        for start in range(0, len(sess.turns), step):
            piece = sess.turns[start:start + n]
            if not piece:
                break
            seq += 1
            out.append(Chunk(
                memory_id=f"{prefix}|W{seq}",
                content=(head + "\n" + _render_body(piece, cfg)).strip(),
                dia_ids=[t.dia_id for t in piece],
                occurred_at=occurred,
            ))
    return out


# ── 灌库 ──

@dataclass
class LocomoIndex:
    """dia_id ↔ memory_id 双向索引（GT 映射桥梁）。

    ★ `dia_to_mem` 的 key **不是裸 dia_id**，而是 `f"{conv_id}|{dia_id}"`：

    dia_id（`D8:6`）只在**单个 conversation 内**唯一，10 个 conversation 灌进
    同一个库后会撞号。初版用裸 dia_id 建索引，全量评测时每条 QA 的相关集被
    放大 ~10 倍（混入另外 9 个 conv 的同号记忆），recall@10 从单 conv 冒烟的
    0.5632 崩到 0.0808 —— 指标被稀释而非检索变差。这个坑必须靠 key 作用域根治，
    不能靠「忍着」。
    """
    granularity: str = "turn"
    conv_ids: list[str] = field(default_factory=list)
    memory_count: int = 0
    dia_to_mem: dict[str, list[str]] = field(default_factory=dict)   # key: "conv-26|D8:6"
    mem_to_dias: dict[str, list[str]] = field(default_factory=dict)
    db_path: str = ""
    config: dict = field(default_factory=dict)

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "granularity": self.granularity,
            "conv_ids": self.conv_ids,
            "memory_count": self.memory_count,
            "dia_to_mem": self.dia_to_mem,
            "mem_to_dias": self.mem_to_dias,
            "db_path": self.db_path,
            "config": self.config,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "LocomoIndex":
        p = Path(path)
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            granularity=d.get("granularity", "turn"),
            conv_ids=list(d.get("conv_ids") or []),
            memory_count=int(d.get("memory_count") or 0),
            dia_to_mem={k: list(v) for k, v in (d.get("dia_to_mem") or {}).items()},
            mem_to_dias={k: list(v) for k, v in (d.get("mem_to_dias") or {}).items()},
            db_path=d.get("db_path", ""),
            config=dict(d.get("config") or {}),
        )


def build_locomo_replica(
    out_dir: str | Path,
    convs: Iterable[LocomoConversation],
    cfg: IngestConfig | None = None,
) -> tuple[Path, LocomoIndex]:
    """把 LoCoMo 会话直灌进独立 memory.db，返回 (db_path, index)。

    幂等：同 out_dir 重复调用会先清空旧库文件（直接删文件重建，
    不复用可能残留的旧记忆，避免 GT 索引与库内容不一致）。

    ⚠️ out_dir 必须是**评测专用目录**，绝不能指向生产 DATA_DIR。
    """
    cfg = cfg or IngestConfig()
    if cfg.granularity not in GRANULARITIES:
        raise ValueError(f"未知粒度 {cfg.granularity!r}，可选 {GRANULARITIES}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "memory.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    mem_conn, session_conn, wiki_conn = db_mod.init_databases(out_dir)
    try:
        dims = sgme_config.load_dimensions()
        aliases = sgme_config.load_aliases()
        memory_dao.import_registry(mem_conn, dims, aliases)
        init_fts(mem_conn)

        conv_list = list(convs)
        index = LocomoIndex(
            granularity=cfg.granularity,
            conv_ids=[c.sample_id for c in conv_list],
            db_path=str(db_path),
            config=cfg.as_dict(),
        )

        n = 0
        for conv in conv_list:
            tag = conv.sample_id if cfg.per_conv_agent_tag else cfg.agent_tag
            for ck in build_chunks(conv, cfg):
                memory_dao.insert_memory(
                    mem_conn,
                    content=ck.content,
                    memory_type=cfg.memory_type,
                    priority=50,
                    time_velocity="static",
                    ttl_days=None,
                    dimension_ids=[cfg.dimension_id],
                    sources=[(d, "locomo_turn") for d in ck.dia_ids],
                    agent_tag=tag,
                    memory_id=ck.memory_id,
                    created_at=FIXED_TS,
                    updated_at=FIXED_TS,
                    occurred_at=ck.occurred_at or FIXED_TS,
                )
                n += 1
                index.mem_to_dias[ck.memory_id] = list(ck.dia_ids)
                for d in ck.dia_ids:
                    key = f"{conv.sample_id}|{d}"      # conv 作用域，见类 docstring
                    index.dia_to_mem.setdefault(key, [])
                    if ck.memory_id not in index.dia_to_mem[key]:
                        index.dia_to_mem[key].append(ck.memory_id)
        mem_conn.commit()
        index.memory_count = n
        logger.info("LoCoMo 灌库完成：%d 条记忆 → %s（粒度=%s）", n, db_path, cfg.granularity)
        return db_path, index
    finally:
        mem_conn.close()
        session_conn.close()
        wiki_conn.close()


def resolve_evidence(
    index: LocomoIndex,
    dia_ids: Iterable[str],
    conv_id: str = "",
) -> tuple[list[str], list[str]]:
    """QA.evidence(dia_id 列表) → (memory_id 列表, 未命中的 dia_id 列表)。

    `conv_id` 必填（dia_id 只在单 conversation 内唯一）：索引 key = `conv_id|dia_id`。
    多对多映射：一个 dia 可能落在多个 chunk（window 重叠时），
    一个 chunk 也可能含多个 dia（window/session 粒度）。
    """
    hit: list[str] = []
    miss: list[str] = []
    for d in dia_ids:
        mids = index.dia_to_mem.get(f"{conv_id}|{d}")
        if not mids:
            miss.append(d)
            continue
        for m in mids:
            if m not in hit:
                hit.append(m)
    return hit, miss
