"""eval/locomo.py：LoCoMo 公开基准（snap-research/locomo）数据解析。

LoCoMo = Long-Context Memory / Longitudinal Conversation Memory，是长程对话记忆
系统的业界公开基准。Mem0 / LangMem / Zep 等均以它给出可比数字。

数据形态（10 conversation 档，官方只发 `data/locomo10.json`）：
- 每个 conversation：两个 speaker 跨**多次 session**（跨天数/月）的长期对话
- turn：`{speaker, dia_id, text}`，`dia_id` 形如 `D1:3` = 第 1 个 session 的第 3 轮（1-based）
- QA：`{question, answer, evidence, category}`，evidence = 支撑答案的 dia_id 列表

★ GT 映射的关键（也是全链路最容易翻车的点）：
QA 的 evidence 是 **dia_id 级**，而检索返回的是 **memory_id 级**。
本模块只负责「dia_id ↔ (session_idx, turn_idx)」的确定性定位，
灌库侧（`locomo_ingest`）负责把 (session, turn) → memory_id 的映射落盘，
评测侧（`locomo_eval`）再把二者对上。**任何一环对不上都会让 GT 覆盖率掉下来**，
所以覆盖率必须先测（见审查意见 R2）。

category 语义（实测 locomo10.json 反推，非猜）：
    1 = multi_hop      (282)  跨多轮才能回答
    2 = temporal       (321)  时间推理（When did X...）
    3 = open_domain     (96)  开放域推断（证据 92 条，4 条无 evidence）
    4 = single_hop     (841)  单轮可答
    5 = adversarial    (446)  **答案不可答**（answer=null），主评测惯例剔除

主评测口径（Mem0 论文口径）：**剔除 category 5**，即 1,540 条；
其中带 evidence 的 1,536 条可用于检索类指标（另 4 条 open-domain 无 evidence）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DEFAULT_LOCOMO_PATH = Path(r"D:/GitHubDownloads/LoCoMo/data/locomo10.json")

# category 数字 → 语义名（locomo10.json 用 int 编码，原始仓库无文档说明，此处为实测反推）
CATEGORY_NAMES: dict[int, str] = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}

# 主评测口径参与统计的 category（剔除 adversarial）
MAIN_CATEGORIES: tuple[int, ...] = (1, 2, 3, 4)

_DIA_RE = re.compile(r"^D(\d+):(\d+)$")


# ── 数据结构 ──

@dataclass
class LocomoTurn:
    """单轮对话。"""
    dia_id: str = ""
    speaker: str = ""
    text: str = ""
    session_idx: int = 0      # 1-based，与 dia_id 的 D{n} 对齐
    turn_idx: int = 0         # 1-based，与 dia_id 的 :{m} 对齐
    date_time: str = ""


@dataclass
class LocomoSession:
    """一次会话（同一天的连续对话）。"""
    conv_id: str = ""
    session_idx: int = 0
    date_time: str = ""
    turns: list[LocomoTurn] = field(default_factory=list)


@dataclass
class LocomoQA:
    """一条评测问答。"""
    conv_id: str = ""
    qa_index: int = 0
    question: str = ""
    answer: str | None = None          # adversarial 类为 None
    evidence: list[str] = field(default_factory=list)   # **原始** evidence 字符串列表
    category: int = 0

    @property
    def dia_ids(self) -> list[str]:
        """规范化后的 dia_id 列表（由 `normalize_evidence` 派生）。

        ⚠️ 必须用这个做 GT 映射，不能直接用 `evidence`——原始数据里
        存在「一个字符串塞多个 dia_id」的脏数据（见 `normalize_evidence` 说明）。
        """
        return normalize_evidence(self.evidence)

    @property
    def category_name(self) -> str:
        return CATEGORY_NAMES.get(self.category, f"unknown_{self.category}")

    @property
    def is_adversarial(self) -> bool:
        return self.category == 5


@dataclass
class LocomoConversation:
    """一个完整 conversation（两个 speaker 的长期关系）。"""
    sample_id: str = ""
    speaker_a: str = ""
    speaker_b: str = ""
    sessions: list[LocomoSession] = field(default_factory=list)
    qas: list[LocomoQA] = field(default_factory=list)

    @property
    def turn_count(self) -> int:
        return sum(len(s.turns) for s in self.sessions)

    @property
    def char_count(self) -> int:
        return sum(len(t.text or "") for s in self.sessions for t in s.turns)


# ── 加载 ──

def _session_keys(conv: dict) -> list[tuple[int, str, str]]:
    """提取 (session_idx, content_key, date_key) 三元组，按 idx 升序。

    ⚠️ 坑：部分 conversation 有 `session_N_date_time` 但**没有** `session_N`
    （该次会话无对话内容），解析时不能假设 content 键一定存在。
    """
    out: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for k in conv.keys():
        m = re.match(r"^session_(\d+)$", k)
        if m:
            idx = int(m.group(1))
            seen.add(idx)
            out.append((idx, k, f"session_{idx}_date_time"))
    out.sort(key=lambda x: x[0])
    return out


def load_locomo(path: str | Path | None = None) -> list[LocomoConversation]:
    """解析 locomo10.json → LocomoConversation 列表。

    - speaker_a / speaker_b 来自 conversation 顶层
    - session 顺序按编号升序（不依赖 JSON key 顺序）
    - turn 的 session_idx/turn_idx 与 dia_id 对齐（1-based）

    缺字段容错：turn 无 dia_id 时按 ``D{session}:{i+1}`` 补号，
    保证下游定位不因个别脏数据整条崩掉。
    """
    path = Path(path) if path else DEFAULT_LOCOMO_PATH
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"LoCoMo 数据格式异常：期望 list[dict]，实际 {type(raw).__name__}")

    convs: list[LocomoConversation] = []
    for item in raw:
        conv = item.get("conversation") or {}
        cid = str(item.get("sample_id") or "")
        c = LocomoConversation(
            sample_id=cid,
            speaker_a=str(conv.get("speaker_a") or ""),
            speaker_b=str(conv.get("speaker_b") or ""),
        )
        for idx, content_key, date_key in _session_keys(conv):
            turns_raw = conv.get(content_key) or []
            if not isinstance(turns_raw, list) or not turns_raw:
                continue
            sess = LocomoSession(
                conv_id=cid,
                session_idx=idx,
                date_time=str(conv.get(date_key) or ""),
            )
            for i, t in enumerate(turns_raw):
                if not isinstance(t, dict):
                    continue
                dia = str(t.get("dia_id") or f"D{idx}:{i + 1}")
                sess.turns.append(LocomoTurn(
                    dia_id=dia,
                    speaker=str(t.get("speaker") or ""),
                    text=str(t.get("text") or ""),
                    session_idx=idx,
                    turn_idx=i + 1,
                    date_time=sess.date_time,
                ))
            c.sessions.append(sess)

        for i, q in enumerate(item.get("qa") or []):
            ev = q.get("evidence") or []
            if isinstance(ev, str):            # 极端脏数据兜底
                ev = [ev]
            c.qas.append(LocomoQA(
                conv_id=cid,
                qa_index=i,
                question=str(q.get("question") or ""),
                answer=q.get("answer"),
                evidence=[str(x) for x in ev],
                category=int(q.get("category") or 0),
            ))
        convs.append(c)
    return convs


# ── dia_id 定位 ──

def parse_dia_id(dia_id: str) -> tuple[int, int] | None:
    """`D1:3` → (1, 3)；非法返回 None。"""
    m = _DIA_RE.match((dia_id or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def normalize_evidence(raw: list[str]) -> list[str]:
    """把 evidence 原始字符串列表规范成 dia_id 列表（去重保序）。

    ★ 数据脏点（实测 locomo10.json 2,815 条 evidence 中 6 条异常，均在 multi_hop 类）：
      - `['D8:6; D9:17']`      —— **一个字符串塞两个 dia_id，分号分隔**
      - `['D9:1 D4:4 D4:6']`   —— 空格分隔的多个 dia_id
      - `['D:11:26']` / `['D']` —— 畸形 token，无法定位，直接丢弃

    不做这层规范化的后果很隐蔽：multi_hop 类 QA 的相关集本该是 2-3 条，
    解析失败会退化成空集 → 该 QA 被算进「覆盖不到」，召回率被系统性压低，
    而报告上只会看到「multi_hop 效果差」，误导优化方向。
    """
    out: list[str] = []
    for s in raw or []:
        for tok in re.split(r"[,;\s]+", str(s).strip()):
            tok = tok.strip()
            if not tok:
                continue
            if _DIA_RE.match(tok) and tok not in out:
                out.append(tok)
    return out


def build_dia_index(conv: LocomoConversation) -> dict[str, LocomoTurn]:
    """单 conversation 内 dia_id → turn 的索引（覆盖 session/turn 全量）。"""
    idx: dict[str, LocomoTurn] = {}
    for s in conv.sessions:
        for t in s.turns:
            idx[t.dia_id] = t
    return idx


# ── 迭代 / 过滤 ──

def iter_qa(
    convs: list[LocomoConversation],
    *,
    include_adversarial: bool = False,
    require_evidence: bool = True,
    categories: tuple[int, ...] | None = None,
) -> Iterator[LocomoQA]:
    """按过滤条件迭代 QA。

    默认口径 = Mem0 主评测口径：剔除 adversarial + 必须有 evidence。
    """
    wanted = set(categories) if categories else (set(CATEGORY_NAMES) if include_adversarial else set(MAIN_CATEGORIES))
    for c in convs:
        for q in c.qas:
            if q.category not in wanted:
                continue
            if require_evidence and not q.dia_ids:     # 用规范化后的 dia_ids 判空
                continue
            yield q


def iter_turns(convs: list[LocomoConversation]) -> Iterator[LocomoTurn]:
    """迭代全部 turn（跨 conversation）。"""
    for c in convs:
        for s in c.sessions:
            for t in s.turns:
                yield t


# ── 统计 ──

def locomo_stats(convs: list[LocomoConversation]) -> dict:
    """语料规模与 QA 分布统计（写进评测报告头部，保证口径可复现）。"""
    by_cat: dict[str, int] = {}
    qa_total = 0
    qa_with_ev = 0
    for c in convs:
        for q in c.qas:
            qa_total += 1
            name = q.category_name
            by_cat[name] = by_cat.get(name, 0) + 1
            if q.dia_ids:
                qa_with_ev += 1
    main_n = sum(1 for _ in iter_qa(convs, include_adversarial=False, require_evidence=False))
    main_ev = sum(1 for _ in iter_qa(convs, include_adversarial=False, require_evidence=True))
    turns = list(iter_turns(convs))
    return {
        "conversations": len(convs),
        "sessions": sum(len(c.sessions) for c in convs),
        "turns": len(turns),
        "chars": sum(len(t.text or "") for t in turns),
        "qa_total": qa_total,
        "qa_with_evidence": qa_with_ev,
        "qa_main": main_n,                 # 主评测口径（剔除 adversarial）
        "qa_main_with_evidence": main_ev,  # 且带 evidence（检索类指标可用）
        "qa_by_category": dict(sorted(by_cat.items())),
    }


if __name__ == "__main__":
    import sys

    p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOCOMO_PATH
    data = load_locomo(p)
    st = locomo_stats(data)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    for c in data:
        print(f"  {c.sample_id}: sessions={len(c.sessions)} turns={c.turn_count} "
              f"chars={c.char_count} qa={len(c.qas)}")
