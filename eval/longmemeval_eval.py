"""eval/longmemeval_eval.py — SGME 的 LongMemEval 业界标准评测台。

替代 LoCoMo 成为 SGME 主评测标准（ST-40 演进）。协议对齐 gbrain 的
`eval longmemeval`（src/commands/eval-longmemeval.ts）与 LongMemEval 官方：
- 每题独立隔离库（重置后只灌该题 haystack，零跨题泄漏）
- 检索 top-k → 按 answer_session_ids 算 session 级 recall
- 可选 LLM 生成答案 → DeepSeek judge 算 J-score + token-F1

图召回说明：LongMemEval 直灌原始会话、不跑提炼 → memory_edges 为空 →
图召回正确休眠（贡献 0），与 gbrain 自身跑法一致，公平可比。
refined 臂（--arms 含 refined）则走 SGME 完整生产链路（L0→L1 提炼→L1.5 落库），
        L.append(f"- refined 臂提炼后端：{result.get('refine_backend', 'cloud')}（cloud=生产链 agnes→GLM-4-9B；local=LM Studio 9B）")
忠实于 LongMemEval「各系统用自己的方式 ingest 相同数据」协议——这才是完整能力评测。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅供类型检查：httpx 在各调用点按需导入，保持模块启动轻量
    import httpx

# 复用 SGME 真实接口；以下 LLM/嵌入帮手从原 locomo_eval.py 内联（LoCoMo 已移除，
# 但这些函数是通用评测基础设施，LongMemEval 评测台继续使用，故就地保留）。
from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data import search as search_mod
from sgme.data.search import init_fts
from sgme.engine import pipeline as sgme_pipeline  # append_l0 / refine_one（refined 臂走真实生产链路）

logger = logging.getLogger("eval.longmemeval")


# ── 通用评测帮手（自 locomo_eval.py 内联，LoCoMo 移除后保留）──

_ANSWER_PROMPT = """You are answering a question using ONLY the retrieved memory snippets from a long-term conversation memory system.

Retrieved memories (may be empty or partially irrelevant):
{context}

Question: {question}

Rules:
- Answer using ONLY the retrieved memories. If the memories do not contain the answer, reply exactly: NO CONTEXT
- Be concise: a short phrase or one sentence. Do NOT explain, do NOT add reasoning.
- Preserve the original wording of dates, names, and numbers as they appear in the memories.

Answer:"""

_JUDGE_PROMPT = """You are an impartial judge evaluating a question-answering system.

Question: {question}
Gold answer: {gold}
System answer: {pred}

Decide whether the system answer is CORRECT with respect to the gold answer.
- CORRECT: the system answer conveys the same key fact(s) as the gold answer (wording/tense/detail-level differences are fine).
- WRONG: it contradicts the gold answer, states a different fact, or says the information is unavailable when the gold answer does exist.

Reply with exactly one word, CORRECT or WRONG, on the first line. Optionally add a one-line reason on the second line."""


def _no_proxy_client(timeout_s: float) -> "httpx.Client":
    """评测台统一 HTTP 客户端：``trust_env=False``（项目铁律，防代理劫持）。

    2026-09-11 教训：评测脚本原先直接 ``httpx.post(...)``，会读宿主机的
    ``HTTP_PROXY/HTTPS_PROXY`` 环境变量；残留的死代理（指向未运行的本机端口）会让
    批量向量与判分全部报 10061「目标计算机积极拒绝」，而引擎侧 trust_env=False 的
    调用照常成功 —— 故障表现为「同一进程里一半通一半不通」。
    """
    import httpx

    return httpx.Client(timeout=timeout_s, trust_env=False)


def _done_qids(records: list[dict]) -> set[str]:
    """已完成题目集合：**error 记录不算完成**（resume 会重跑它）。

    2026-09-11 教训：原实现把带 error 的记录也当「已完成」跳过，一次网络抖动
    （如端点瞬断）就让题目被永久丢弃，长跑必须能自愈。
    """
    return {r["qid"] for r in records if r.get("qid") and not r.get("error")}


def make_deepseek_llm_fn(
    model: str = "deepseek-v4-flash",
    api_key_env: str = "DEEPSEEK_API_KEY_SGME",
    base_url: str = "https://api.deepseek.com/v1",
    throttle_s: float = 0.25,
    max_retry: int = 5,
    disable_thinking: bool = False,
    max_tokens: int = 4096,
    allow_no_key: bool = False,
    timeout_s: float = 1800,
) -> "Callable[[str], str]":
    """Build an llm_fn(prompt)->str that calls an OpenAI-compatible endpoint.

    Default targets DeepSeek (SGME production LLM), but also drives local
    LM Studio endpoints (Qwen 等) for zero-cloud eval runs. Retries on 429
    with exponential backoff. Returns '' on persistent failure.

    Local-model notes:
    - allow_no_key: LM Studio 不需要 key，缺失时回退 "not-required"。
    - disable_thinking: Qwen3.x 是思考模型，不关会把 token 预算烧在
      reasoning_content 导致 content 空串；本地跑必须关。
    - timeout_s: 本地 9 tok/s 生成长输出单请求常 >120s，必须抬高。
    """
    import os
    import random
    import time

    import httpx

    key = os.environ.get(api_key_env) or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        if allow_no_key:
            key = "not-required"
        else:
            raise RuntimeError(f"missing API key env {api_key_env}")

    def fn(prompt: str) -> str:
        last_err = ""
        cli = _no_proxy_client(timeout_s)
        if throttle_s > 0:
            time.sleep(throttle_s)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if disable_thinking:
            payload["enable_thinking"] = False
        for attempt in range(1, max_retry + 1):
            try:
                r = cli.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code == 429:
                    last_err = "429"
                    time.sleep(min(2.0 ** attempt, 16.0) + random.random())
                    continue
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:120]
                time.sleep(min(2.0 ** attempt, 16.0) + random.random())
        logger.warning("deepseek llm_fn failed after %d retries: %s", max_retry, last_err)
        return ""

    return fn


def embed_corpus(
    mem_conn: sqlite3.Connection,
    cfg: dict,
    *,
    limit: int | None = None,
    workers: int = 6,
    batch_size: int = 32,
) -> dict:
    """Batch-embed the corpus via Ollama /v1/embeddings (input array).

    Used by the hybrid arm. EmbedCache (sha256(text)+model) dedups across runs.
    """
    import random

    import httpx
    from sgme.data import memory_dao
    from sgme.data.search import vector as vector_mod
    try:
        from eval.embed_cache import EmbedCache
    except ImportError:  # 脚本按文件路径运行时 eval 包不可见，退化为直接导入
        from embed_cache import EmbedCache

    t0 = time.perf_counter()
    rows = mem_conn.execute(
        "SELECT memory_id, content FROM memories WHERE status != 'rejected' ORDER BY memory_id"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    total = len(rows)
    vec_cfg = (cfg.get("search") or {}).get("vector") or {}
    model = vec_cfg.get("model", "")
    base_url = (vec_cfg.get("base_url") or "").rstrip("/")
    cache = EmbedCache(EmbedCache.default_path())
    prev = vector_mod.set_embed_cache(cache)

    ok = 0
    misses: list[tuple[int, str, str]] = []
    for i, r in enumerate(rows):
        vec = cache.get(r["content"], model)
        if vec is not None:
            memory_dao.upsert_vector(
                mem_conn, r["memory_id"],
                vector_mod._serialize_vector(vec), model, dims=len(vec),
            )
            ok += 1
        else:
            misses.append((i, r["memory_id"], r["content"]))
    mem_conn.commit()

    cli = _no_proxy_client(120.0)  # trust_env=False；httpx.Client 线程安全，多线程共用

    def embed_batch(batch: list) -> list:
        texts = [b[2] for b in batch]
        for attempt in range(1, 7):
            try:
                r = cli.post(
                    f"{base_url}/embeddings",
                    json={"model": model, "input": texts},
                )
                if r.status_code == 429:
                    time.sleep(min(2.0 ** attempt, 16.0) + random.random())
                    continue
                r.raise_for_status()
                data = {d["index"]: d["embedding"] for d in r.json()["data"]}
                return [(batch[k][1], batch[k][2], data[k]) for k in range(len(batch))]
            except Exception as e:  # noqa: BLE001
                logger.warning("batch embed 失败(尝试%d): %s", attempt, str(e)[:120])
                time.sleep(min(2.0 ** attempt, 16.0) + random.random())
        raise RuntimeError(f"batch embed 耗尽重试: {texts[0][:40]!r}")

    if misses:
        batches = [misses[s:s + batch_size] for s in range(0, len(misses), batch_size)]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for res in ex.map(embed_batch, batches):
                for memory_id, text, vec in res:
                    cache.put(text, model, vec)
                    memory_dao.upsert_vector(
                        mem_conn, memory_id,
                        vector_mod._serialize_vector(vec), model, dims=len(vec),
                    )
                    ok += 1
                mem_conn.commit()
                logger.info("向量化进度 %d/%d（%.1f%%）", ok, total, ok * 100.0 / max(total, 1))

    vector_mod.set_embed_cache(prev)
    return {
        "corpus_size": total,
        "vector_count": ok,
        "coverage": round(ok / total, 4) if total else 0.0,
        "available": bool(total) and ok == total,
        "workers": max(1, workers),
        "batch_size": batch_size,
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }

DEFAULT_DATASET = r"D:/GitHubDownloads/LongMemEval/longmemeval_s.jsonl"
DEFAULT_TOP_K = 8
DEFAULT_OUT = "eval/results/longmemeval"
FIXED_TS = "2026-01-01T00:00:00Z"


def _session_date_iso(date: str | None) -> str:
    """session 日期 → ISO 时间戳（T-149⑥ 时序锚点）。

    LongMemEval 日期形如 "2023/05/20 (Sat) 12:04" 或 "2023-05-20 (Sat)"；
    解析失败回退 FIXED_TS（与旧行为一致）。
    """
    if not date:
        return FIXED_TS
    head = date.split("(")[0].strip()  # 剥星期后缀
    head = head.replace("/", "-")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", head)
    if not m:
        return FIXED_TS
    return m.group(1) + "T12:00:00Z"


# ── 数据集 ──

def load_dataset(path: str) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8")
    t = raw.strip()
    if t.startswith("["):
        return json.loads(raw)
    return [json.loads(l) for l in t.splitlines() if l.strip()]


def render_session(turns: list, session_id: str, date: str | None = None) -> str:
    """把一条 LongMemEval session（turn 列表）渲染成 markdown。

    数据集 _s 变体：haystack_sessions[i] 是 turn 列表，session_id 在平行数组
    haystack_session_ids[i]，故此处直接收 session_id 入参。
    """
    fm = ["---", "type: note"]
    if date:
        fm.append(f"date: {date}")
    fm.append(f"session_id: {session_id}")
    fm.extend(["---", ""])
    body: list[str] = []
    for turn in turns:
        body.append(f"**{turn['role']}:** {turn['content']}")
        body.append("")
    return "\n".join(fm) + "\n".join(body)


# ── 配置 ──

def make_cfg(base: dict, *, vector: bool, arm: str = "bm25", refine_backend: str = "cloud") -> dict:
    cfg = json.loads(json.dumps(base))
    cfg.setdefault("search", {})
    # 基准眠图：LongMemEval 直灌无提炼 → 无边 → 图召回贡献 0，公平
    cfg["search"]["graph"] = {"enabled": False}
    cfg["search"]["vector"] = {
        "enabled": bool(vector),
        "base_url": os.environ.get("SGME_EMBED_BASE_URL", "http://localhost:8123/v1").rstrip("/"),
        "model": os.environ.get("SGME_EMBED_MODEL", "text-embedding-bge-m3-legal-euro-r7"),
    }
    if arm == "refined" and refine_backend == "local":
        _inject_local_refine(cfg)
    # refine_backend == "cloud"：保留 base_cfg 默认 refinement 链（agnes→siliconflow→rule），
    # 即 SGME 生产环境的真实提炼后端，faithful 且可靠；受 0.5 rps 节流。
    return cfg


def _inject_local_refine(cfg: dict) -> None:
    """refined 臂：把提炼链指向本地 LM Studio（8123 聊天端点），零云依赖。

    背景：SGME 默认 refinement 链是 agnes→siliconflow→rule，免费云链被节流到
    0.5 rps 且频繁 429（撞频率上限）。25,112 个 session 全量提炼需 14h+ 且当前
    被限到 17:12 才重置。改用本地 LM Studio 推理（零限速、可并发），与 gbrain
    协议「各系统用自己的方式 ingest 相同数据」完全兼容——这才是忠于协议的跑法，
    而不是此前「零 token 直灌」的偷懒口径。
    """
    lm_base = os.environ.get("SGME_REFINE_BASE_URL", "http://localhost:8123/v1").rstrip("/")
    lm_model = os.environ.get("SGME_REFINE_MODEL", "qwen3.8-9b-distill")
    node = {
        "provider": "lmstudio_local",
        "model": lm_model,
        "provider_type": "openai_compat",
        "base_url": lm_base,
        "context_window": int(os.environ.get("SGME_REFINE_CTX", "32768")),
        "api_key_env": None,
        "max_tokens": 16384,
        # 思考已由模型级配置关闭：LM Studio 的
        # `llm.prediction.reasoning.enableThinking = false`（脚本
        # scripts/lmstudio_disable_thinking.py 落盘，见变更记录 B163）。
        # 请求侧 enable_thinking / extra_body 均被 LM Studio 忽略（官方 issue #1990），
        # 故此处不再携带——留着只会让人误以为它是生效开关。
        # max_tokens 16384 保留作 JSON 完整性的余量（实测跑满率约 10%，正文实际仅 ~1K）。
    }
    cfg.setdefault("llm", {})
    # 直接用本地节点 + rule 兜底（不回退云链，避免 429 干扰）
    cfg["llm"]["chains"] = {"refinement": [node, {"provider": "rule"}]}
    # 本地推理：免云节流 + 放宽超时（9 tok/s 生成长输出单请求常 >120s）
    cfg["llm"]["rules"] = {
        "throttle": {"rps": 100, "burst": 100},
        "timeout_s": 1800,
        "max_retries": 3,
    }
    cfg["llm"].setdefault("rules", {})["throttle"] = {"enabled": False}


# ── 每题隔离库 ──

def open_question_db(out_dir: Path, q: dict, dims, aliases, *, vector: bool, cfg: dict):
    """重置并灌入单题 haystack；返回 (mem_conn, session_conn, wiki_conn)。"""
    for name in ("memory.db", "session.db", "wiki.db"):
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(out_dir / name) + suffix)
            if p.exists():
                p.unlink()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(out_dir)
    memory_dao.import_registry(mem_conn, dims, aliases)
    init_fts(mem_conn)
    dates = q.get("haystack_dates") or []
    sessions = q.get("haystack_sessions") or []
    session_ids = q.get("haystack_session_ids") or []
    n = 0
    seen: set[str] = set()
    for i, turns in enumerate(sessions):
        sid = session_ids[i] if i < len(session_ids) else f"session_{i}"
        if sid in seen:  # LongMemEval 部分题 haystack_session_ids 含重复 sid
            continue
        seen.add(sid)
        content = render_session(turns, sid, dates[i] if i < len(dates) else None)
        memory_dao.insert_memory(
            mem_conn,
            content=content,
            memory_type="episodic",
            priority=50,
            time_velocity="static",
            ttl_days=None,
            dimension_ids=["social"],
            sources=[(sid, "lme_session")],
            agent_tag="longmemeval",
            memory_id=sid,
            created_at=FIXED_TS,
            updated_at=FIXED_TS,
            # T-149⑥ 时序锚点修复：occurred_at 用 session 真实日期（原 FIXED_TS 全库同时刻，
            # 时序推理无从谈起）；created/updated 保持 FIXED_TS 不影响排序语义
            occurred_at=_session_date_iso(dates[i] if i < len(dates) else None),
        )
        n += 1
    mem_conn.commit()
    if vector:
        embed_corpus(mem_conn, cfg, workers=6, batch_size=32)
    return mem_conn, session_conn, wiki_conn


# ── refined 臂：走 SGME 完整生产链路（L0 → L1 提炼 → L1.5 落库 → 向量化）──

def render_session_l0(turns: list, date: str | None, seq_base: int = 0) -> str:
    """把 LongMemEval session 渲染成 SGME L0 原始格式（# {ISO} {role} 头）。

    append_l0 / parse_body_messages 要求此格式；数据集 session 内无逐条时间戳，
    这里按序号在 session 日期上递增合成（仅用于 L1 时序分块，不影响答案）。
    """
    lines: list[str] = []
    # LongMemEval 日期形如 "2023-05-20 (Sat)"（带空格/星期），需截断到 YYYY-MM-DD
    base = (date or "2026-01-01").split()[0].replace("/", "-")
    for i, turn in enumerate(turns):
        iso = f"{base}T{seq_base + i:02d}:00:00Z"
        role = turn.get("role", "user")
        lines.append(f"# {iso} {role}")
        lines.append(turn.get("content", ""))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def open_question_db_refined(out_dir: Path, q: dict, dims, aliases, *, cfg: dict,
                             stage: str = "all"):
    """Refined 臂：跑完整 SGME 生产链路。

    与 direct 臂（insert_memory 整块原文）不同，这里每条 session 走
    append_l0 → refine_one（L1 提炼 + L1.5 落库），最终记忆库是 SGME 真实产物。
    返回 (mem_conn, session_conn, wiki_conn, fileid2sid, stats)。

    fileid2sid：file_id → session_id 映射，供召回计算把「记忆」还原回「来源 session」
    （refined 臂记忆的 memory_id 是 UUID，不等于 session_id；direct 臂 memory_id==sid）。
    """
    # T-150 分步：ingest/all 删库重建；refine/eval 阶段保留既有库（L0/记忆/映射全在磁盘，
    # 由 refine_state.json 的 sid 断点决定跳过哪些），只在库不存在时初始化
    state_path = out_dir / "refine_state.json"
    db_exists = (out_dir / "memory.db").exists() and state_path.exists()
    if not db_exists:
        for name in ("memory.db", "session.db", "wiki.db"):
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(out_dir / name) + suffix)
                if p.exists():
                    p.unlink()
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(out_dir)
    memory_dao.import_registry(mem_conn, dims, aliases)
    init_fts(mem_conn)
    dates = q.get("haystack_dates") or []
    sessions = q.get("haystack_sessions") or []
    session_ids = q.get("haystack_session_ids") or []
    fileid2sid: dict[str, str] = {}
    seen: set[str] = set()
    n_sessions = n_refined = n_err = 0

    # ── T-150 分步：session 级断点状态（锚 = session_key/sid，fid 是 uuid 跨库不稳定）──
    state: dict = {"ingested": [], "refined": []}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            logger.info("[refined] 恢复 session 级断点: ingested=%d refined=%d",
                        len(state.get("ingested", [])), len(state.get("refined", [])))
        except Exception:
            state = {"ingested": [], "refined": []}

    def _save_state() -> None:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)

    # 每阶段都重放 append_l0（幂等/零 LLM/秒级）：既完成 ingest，又重建 fid↔sid 映射
    refined_set = set(state.get("refined", []))
    todo_refine: list[str] = []
    for i, turns in enumerate(sessions):
        sid = session_ids[i] if i < len(session_ids) else f"session_{i}"
        if sid in seen:
            continue
        seen.add(sid)
        n_sessions += 1
        l0 = render_session_l0(turns, dates[i] if i < len(dates) else None)
        try:
            _sess_iso = _session_date_iso(dates[i] if i < len(dates) else None)
            info = sgme_pipeline.append_l0(
                session_key=sid,
                started_at=_sess_iso,
                content=l0,
                source_type="session",
                ended_at=_sess_iso,
                agent_id=None,
                metadata={"lme_session": True},
                cfg=cfg,
                mem_conn=mem_conn,
                session_conn=session_conn,
                agent_model=None,
            )
            fid = info.get("file_id")
            if fid:
                fileid2sid[fid] = sid
                if sid not in state["ingested"]:
                    state["ingested"].append(sid)
                if sid not in refined_set:
                    todo_refine.append(fid)
        except Exception as e:  # noqa: BLE001
            n_err += 1
            logger.warning("L0 落盘失败 session=%s: %s", sid, str(e)[:160])
    if set(state["ingested"]) != seen:
        state["ingested"] = sorted(seen)
    _save_state()

    if stage == "ingest":
        mem_conn.commit()
        stats = {"sessions": n_sessions, "refined": 0, "errors": n_err,
                 "ingested": len(state["ingested"])}
        return mem_conn, session_conn, wiki_conn, fileid2sid, stats

    # ── Stage 2: refine（session 级断点，逐文件幂等；重放后 fid 已重建）──
    if stage in ("all", "refine"):
        logger.info("[refined] 待提炼 session: %d / %d", len(todo_refine), len(fileid2sid))
        done_n = 0
        for fid in todo_refine:
            sid = fileid2sid.get(fid, fid)
            try:
                sgme_pipeline.refine_one(fid, mem_conn, session_conn, cfg)
                state["refined"].append(sid)
                refined_set.add(sid)
                n_refined += 1
                done_n += 1
                if done_n % 5 == 0:
                    _save_state()
                    logger.info("[refined] 进度 %d/%d", len(refined_set), len(fileid2sid))
            except Exception as e:  # noqa: BLE001
                n_err += 1
                logger.warning("refined 提炼失败 file=%s: %s", fid, str(e)[:160])
        _save_state()
        mem_conn.commit()
        if stage == "refine":
            stats = {"sessions": n_sessions, "refined": n_refined, "errors": n_err,
                     "refined_total": len(state["refined"])}
            return mem_conn, session_conn, wiki_conn, fileid2sid, stats
    else:
        n_refined = len(state.get("refined", []))

    # ── Stage 3: embed（eval/all 阶段）──
    embed = embed_corpus(mem_conn, cfg, workers=6, batch_size=32)
    stats = {"sessions": n_sessions, "refined": n_refined, "errors": n_err, "embed": embed}
    return mem_conn, session_conn, wiki_conn, fileid2sid, stats


def _source_to_sid(source_ref: str, fileid2sid: dict) -> str:
    """source_ref（形如 `<file_id>:<段号>`）→ session_id。

    ⚠️ fileid2sid 的键是裸 file_id；不剥段号会永远落到兜底分支 → sids 与 gt
    永不相交 → recall 恒 0（2026-09-10 T-150 实测：q1 原样查空、剥后缀命中
    answer_280352e9）。
    """
    base = source_ref.rpartition(":")[0] or source_ref
    return fileid2sid.get(base) or fileid2sid.get(source_ref) or source_ref


def _memory_session_map(mem_conn, mem_ids: list[str], fileid2sid: dict) -> dict[str, list[str]]:
    """memory_id → 来源 session 列表（一条记忆可能由多场会话贡献；无来源记录的不入表）。"""
    out: dict[str, list[str]] = {}
    for mid in mem_ids:
        rows = mem_conn.execute(
            "SELECT source_ref FROM memory_sources WHERE memory_id=?", (mid,)
        ).fetchall()
        sids: list[str] = []
        for (sr,) in rows:
            sid = _source_to_sid(sr, fileid2sid)
            if sid not in sids:
                sids.append(sid)
        if sids:
            out[mid] = sids
    return out


def _rank_sessions(mem_ids: list[str], mid2sids: dict[str, list[str]], k: int) -> list[str]:
    """会话按其**最好一条记忆**的排名排序、去重，取前 k 个。

    refined 库每场会话产出 8~10 条细粒度记忆，而检索按「条」给 top-k；
    同一 k 下 refined 臂只覆盖 1~2 场会话，与直灌臂（1 条=1 场原文）不可比。
    先按会话聚合再取前 k 个会话，才能让两臂拿到同等粒度的证据量。
    """
    order: list[str] = []
    seen: set[str] = set()
    for mid in mem_ids:
        for sid in mid2sids.get(mid, []):
            if sid not in seen:
                seen.add(sid)
                order.append(sid)
                if len(order) >= k:
                    return order
    return order


def _select_memories_within_budget(mem_ids: list[str], mid2sids: dict[str, list[str]],
                                   keep_sids: set[str], budget_chars: int,
                                   text_by_id: dict) -> list[str]:
    """按原排名顺序挑出「选中会话」的记忆，累计字符不超过预算；至少保留一条。"""
    picked: list[str] = []
    used = 0
    for mid in mem_ids:
        if not (set(mid2sids.get(mid, [])) & keep_sids):
            continue
        n = len(text_by_id.get(mid) or "")
        if picked and used + n > budget_chars:
            break
        picked.append(mid)
        used += n
    return picked


def _resolve_sessions(mem_conn, retrieved_ids: list[str], fileid2sid: dict) -> set[str]:
    """memory_id → 来源 session 集合。

    direct 臂：memory_id 即 session_id（memory_sources.source_ref==sid）。
    refined 臂：memory_id 是 UUID，需经 memory_sources.source_ref(=file_id) → sid 映射。
    统一返回命中 session 集合，召回计算与臂无关。
    """
    sids: set[str] = set()
    for mid in retrieved_ids:
        rows = mem_conn.execute(
            "SELECT source_ref FROM memory_sources WHERE memory_id=?", (mid,)
        ).fetchall()
        if rows:
            for (sr,) in rows:
                sids.add(_source_to_sid(sr, fileid2sid))
        else:
            sids.add(mid)  # 兜底：无 source 记录时直接用 memory_id
    return sids


# ── F1（token 级，复刻 LongMemEval 口径）──

def _norm_tokens(s: str) -> list[str]:
    s = (s or "").lower()
    return re.findall(r"[a-z0-9]+|[一-鿿]", s)


def token_f1(pred: str, gold: str) -> float:
    pt, gt = _norm_tokens(pred), _norm_tokens(gold)
    if not gt:
        return 1.0 if not pt else 0.0
    if not pt:
        return 0.0
    c_pred, c_gold = Counter(pt), Counter(gt)
    overlap = sum((c_pred & c_gold).values())
    prec = overlap / len(pt)
    rec = overlap / len(gt)
    return 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0


# ── 主流程 ──

# ---------- B146: per-question checkpoint / resume / concurrency ----------

def _fingerprint(args, arms, n):
    # resume requires exact config match
    return {
        "dataset": str(Path(args.dataset).resolve()),
        "n_questions": n,
        "offset": args.offset or 0,
        "arms": arms,
        "top_k": args.top_k,
        "qa": bool(args.qa),
        "refine_backend": args.refine_backend,
        "primary": args.primary,
    }


def _load_checkpoint(cp_path, fp):
    # returns: list = valid records (truncated tail dropped); None = discard file
    if not cp_path.exists():
        return []
    try:
        lines = cp_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return []
    try:
        meta = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    if meta.get("fingerprint") != fp:
        return None
    records = []
    for ln in lines[1:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            records.append(json.loads(ln))
        except json.JSONDecodeError:
            break
    return records


_CP_LOCK = threading.Lock()


def _append_checkpoint(cp_path, fp, rec, *, new):
    with _CP_LOCK:
        if new:
            cp_path.write_text(
                json.dumps({"fingerprint": fp}, ensure_ascii=False) + "\n",
                encoding="utf-8")
        with cp_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _process_question(q, qi, args, arms, cfgs, dims, aliases, llm_fn, run_id, out_dir):
    # one question = isolated temp DBs, no shared mutable state; thread-safe
    qtype = q.get("question_type", "unknown")
    qid = q.get("question_id") or f"idx{qi}"
    gt = set(q.get("answer_session_ids") or [])
    q_out = out_dir / "tmp" / run_id / f"q{qi}"
    q_out.mkdir(parents=True, exist_ok=True)

    per_arm_recall = {}
    last_res = None
    primary_res = None
    for arm in arms:
        vector = arm in ("hybrid", "refined")
        cache_key = ("refined" if arm == "refined"
                     else "hybrid" if arm == "hybrid"
                     else "bm25")
        cfg = cfgs[cache_key]
        fileid2sid = {}
        if arm == "refined":
            mem_conn, sc, wc, fileid2sid, stats = open_question_db_refined(
                q_out, q, dims, aliases, cfg=cfg,
                stage=getattr(args, "refine_stage", "all"))
        else:
            mem_conn, sc, wc = open_question_db(
                q_out, q, dims, aliases, vector=vector, cfg=cfg)
        try:
            # refined 臂会话级聚合（2026-09-12，见 _rank_sessions 注释）：
            # 先取记忆大池 → 按来源会话聚合取前 N 个会话 → 只把这些会话的记忆
            # 喂给 QA（控字符预算，对齐直灌臂的上下文体量）。否则同一 top-k 下
            # 两臂信息量差 ~8 倍，noctx 会系统性虚高。
            session_k = getattr(args, "refined_session_k", None)
            session_k = args.top_k if session_k is None else session_k
            pooled = (arm == "refined") and session_k > 0
            pool_k = (max(args.top_k, args.top_k * max(1, getattr(args, "refined_pool", 10)))
                      if pooled else args.top_k)
            res = search_mod.search_memories(
                mem_conn, None, query=q["question"], limit=pool_k,
                include_sources=False, cfg=cfg,
            )
            seen = set()
            retrieved = []
            for r in res:
                mid = r.get("memory_id")
                if mid and mid not in seen:
                    seen.add(mid)
                    retrieved.append(mid)
            ctx_res = res
            if pooled:
                mid2sids = _memory_session_map(mem_conn, retrieved, fileid2sid)
                top_sids = _rank_sessions(retrieved, mid2sids, session_k)
                hit_sids = set(top_sids)
                text_by_id = {r.get("memory_id"): (r.get("content") or "") for r in res}
                keep = set(_select_memories_within_budget(
                    retrieved, mid2sids, set(top_sids),
                    getattr(args, "refined_ctx_chars", 96000), text_by_id))
                ctx_res = [r for r in res if r.get("memory_id") in keep]
            else:
                hit_sids = _resolve_sessions(mem_conn, retrieved, fileid2sid)
            gt_sessions = set(gt)
            recall_frac = (
                len(gt_sessions & hit_sids) / len(gt_sessions)
                if gt_sessions else 0.0
            )
            per_arm_recall[arm] = recall_frac
            if arm == (args.primary or ("refined" if "refined" in arms else "hybrid" if "hybrid" in arms else "bm25")):
                primary_res = ctx_res
            last_res = ctx_res
        finally:
            sc.close(); wc.close(); mem_conn.close()

    record = {"qid": qid, "qi": qi, "qtype": qtype,
              "recall": per_arm_recall, "qa": None, "error": None}

    if args.qa and llm_fn:
        ctx_src = primary_res or last_res or []
        gold = str(q.get("answer", ""))
        if getattr(args, "qa_mode", "legacy") == "product":
            # T-149⑥ product 模式：复用 operations.answer 的题型分派/上下文渲染/
            # 时间线排序（评测台无生产 Server，纯函数 + llm_fn 注入等价复刻）
            from sgme.operations.answer import (
                classify_question,
                render_context,
                render_timeline,
                _stage_for,
            )
            from sgme.prompts.manager import PromptStore

            qtype_dispatch = classify_question(q["question"])
            pv = PromptStore().get(_stage_for(qtype_dispatch))
            if qtype_dispatch == "temporal":
                prompt = (pv.text
                          .replace("{{timeline}}", render_timeline(ctx_src))
                          .replace("{{context}}", render_context(ctx_src))
                          .replace("{{question}}", q["question"]))
            else:
                prompt = (pv.text
                          .replace("{{context}}", render_context(ctx_src))
                          .replace("{{question}}", q["question"]))
        else:
            context = "\n".join(
                f"[{i + 1}] {r.get('content', '')}" for i, r in enumerate(ctx_src)
            ) or "(no memories retrieved)"
            prompt = _ANSWER_PROMPT.format(context=context, question=q["question"])
        pred = llm_fn(prompt).strip()
        pred_head = pred.splitlines()[0].strip() if pred else ""
        f1 = token_f1(pred_head, gold)
        if not pred:
            outcome = "err"
        elif pred_head.upper().startswith("NO CONTEXT"):
            outcome = "noctx"
        else:
            jr = llm_fn(_JUDGE_PROMPT.format(
                question=q["question"], gold=gold, pred=pred_head,
            )).strip()
            jhead = ""
            if jr:
                m = re.search(r"\b(CORRECT|WRONG)\b", jr.upper())
                jhead = m.group(1) if m else ""
            outcome = ("correct" if jhead == "CORRECT"
                       else "wrong" if jhead == "WRONG" else "err")
        record["qa"] = {"outcome": outcome, "f1": round(f1, 4)}
    return record


def _run_one_safe(q, qi, args, arms, cfgs, dims, aliases, llm_fn, run_id, out_dir):
    # never let one question kill the whole long run
    try:
        return _process_question(q, qi, args, arms, cfgs, dims, aliases,
                                 llm_fn, run_id, out_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("[lme] question %s failed: %s",
                       q.get("question_id") or f"idx{qi}", str(e)[:200])
        return {"qid": q.get("question_id") or f"idx{qi}", "qi": qi,
                "qtype": q.get("question_type", "unknown"),
                "recall": {}, "qa": None, "error": str(e)[:200]}


def _log_progress(done, total, run_start):
    el = time.perf_counter() - run_start
    eta = int(el / done * (total - done)) if done else 0
    logger.warning("[lme] %d/%d  elapsed=%ds  eta=%ds", done, total, int(el), eta)

def _aggregate(records, args, arms, total, elapsed, workers, resumed):
    recall_by_type = defaultdict(lambda: {"hit": 0.0, "total": 0})
    j_by_type = defaultdict(list)
    f1_by_type = defaultdict(list)
    n_qa = n_correct = n_wrong = n_noctx = n_err = 0
    n_q_err = 0
    for rec in records:
        qtype = rec.get("qtype", "unknown")
        if rec.get("error"):
            n_q_err += 1
            continue
        for arm, rfrac in (rec.get("recall") or {}).items():
            b = recall_by_type.setdefault(f"{arm}:{qtype}", {"hit": 0.0, "total": 0})
            b["total"] += 1
            b["hit"] += rfrac
        qa = rec.get("qa")
        if qa:
            n_qa += 1
            f1_by_type[qtype].append(qa.get("f1", 0.0))
            outcome = qa.get("outcome", "err")
            if outcome == "correct":
                n_correct += 1
                j_by_type[qtype].append(1)
            elif outcome == "wrong":
                n_wrong += 1
                j_by_type[qtype].append(0)
            elif outcome == "noctx":
                n_noctx += 1
                j_by_type[qtype].append(0)
            else:
                n_err += 1
                j_by_type[qtype].append(-1)

    def _recall(d):
        return round(d["hit"] / d["total"], 4) if d["total"] else 0.0

    def _jscore(marks):
        valid = [m for m in marks if m >= 0]
        if not valid:
            return 0.0, 0
        return round(sum(valid) / len(valid), 4), len(valid)

    def _f1avg(vals):
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    arms_recall = {}
    for k, d in sorted(recall_by_type.items()):
        arms_recall[k] = {"recall@%d" % args.top_k: _recall(d), "hit": d["hit"], "total": d["total"]}

    j_summary = {}
    for c, marks in sorted(j_by_type.items()):
        js, judged = _jscore(marks)
        j_summary[c] = {"j_score": js, "judged": judged, "f1": _f1avg(f1_by_type.get(c, []))}

    return {
        "dataset": args.dataset,
        "n_questions": total,
        "n_done": len(records),
        "n_question_errors": n_q_err,
        "top_k": args.top_k,
        "arms": arms,
        "refine_backend": getattr(args, "refine_backend", "cloud"),
        "workers": workers,
        "resumed": resumed,
        "retrieval_recall_by_arm_type": arms_recall,
        "qa": {
            "enabled": bool(args.qa),
            "n_qa": n_qa,
            "correct": n_correct,
            "wrong": n_wrong,
            "no_context": n_noctx,
            "errors": n_err,
            "j_score": round(n_correct / n_qa, 4) if n_qa else 0.0,
            "no_context_rate": round(n_noctx / n_qa, 4) if n_qa else 0.0,
            "by_type": j_summary,
        },
        "elapsed_s": elapsed,
    }

def run(args) -> dict:
    ds = load_dataset(args.dataset)
    off = getattr(args, "offset", 0) or 0
    if off:
        ds = ds[off:]
    if args.limit and args.limit < len(ds):
        ds = ds[: args.limit]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    out_dir = Path(args.output).resolve()
    # per-run temp dir: avoid db files locked by an old process (WinError 32)
    # --run-id 指定时复用既有目录，断点续跑才能真正命中 refine_state.json
    # （2026-09-10 T-150：默认时间戳导致 q_out/RAW_DIR 每次新建 → 断点永不命中 → 删库重建）。
    run_id = args.run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
    (out_dir / "tmp" / run_id).mkdir(parents=True, exist_ok=True)
    cp_path = out_dir / "checkpoint.jsonl"
    fp = _fingerprint(args, arms, len(ds))

    base_cfg = sgme_config.load_config()
    dims = sgme_config.load_dimensions()
    aliases = sgme_config.load_aliases()

    # pre-build all arm cfgs on main thread (no cfg_cache race under workers>1)
    cfgs = {}
    for arm in arms:
        key = ("refined" if arm == "refined"
               else "hybrid" if arm == "hybrid" else "bm25")
        if key not in cfgs:
            cfgs[key] = make_cfg(
                base_cfg, vector=(arm in ("hybrid", "refined")),
                arm=arm, refine_backend=args.refine_backend)

    # refined arm: redirect L0 raw files to run-level temp dir
    if "refined" in arms:
        sgme_config.RAW_DIR = out_dir / "raw" / run_id
        sgme_config.RAW_DIR.mkdir(parents=True, exist_ok=True)

    llm_fn = None
    if args.qa:
        judge_base = (args.judge_base_url or os.environ.get(
            "SGME_JUDGE_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
        judge_key_env = args.judge_api_key_env or os.environ.get(
            "SGME_JUDGE_KEY_ENV", "DEEPSEEK_API_KEY_SGME")
        # 非 DeepSeek 云端即视为本地 LM Studio：关思考 + 免 key + 长超时 + 无 sleeps
        is_local = judge_base != "https://api.deepseek.com/v1"
        llm_fn = make_deepseek_llm_fn(
            model=args.judge_model,
            api_key_env=judge_key_env,
            base_url=judge_base,
            throttle_s=0.0 if is_local else 0.25,
            disable_thinking=is_local,
            max_tokens=8192,
            allow_no_key=is_local,
            timeout_s=1800,
        )

    # resume: reuse checkpoint only when fingerprint matches
    done_records = []
    if getattr(args, "resume", False):
        loaded = _load_checkpoint(cp_path, fp)
        if loaded is None:
            logger.warning("[lme] checkpoint fingerprint mismatch -> discard, full rerun")
        else:
            done_records = loaded
            if loaded:
                logger.warning("[lme] resume: %d questions from checkpoint, skipped", len(loaded))
    done_qids = _done_qids(done_records)

    todo = []
    for qi, q in enumerate(ds, 1):
        qid = q.get("question_id") or f"idx{qi}"
        if qid not in done_qids:
            todo.append((qi, q))

    # 口径透明化：refined 臂是否走会话级聚合（影响 recall 与 noctx 的可比性）
    if "refined" in arms:
        _sk = args.refined_session_k if getattr(args, "refined_session_k", None) is not None else args.top_k
        logger.warning(
            "[lme] refined 会话级聚合：%s",
            (f"开（取前 {_sk} 个会话；候选池 = top-k×{args.refined_pool}；"
             f"上下文 ≤{args.refined_ctx_chars} 字符）") if _sk > 0 else "关（旧口径：top-k 条记忆直出）",
        )

    run_start = time.perf_counter()
    new_records = []
    total = len(ds)
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    cp_new = not done_records

    if workers == 1 or len(todo) <= 1:
        for i, (qi, q) in enumerate(todo, 1):
            rec = _run_one_safe(q, qi, args, arms, cfgs, dims, aliases,
                                llm_fn, run_id, out_dir)
            _append_checkpoint(cp_path, fp, rec, new=(cp_new and i == 1))
            new_records.append(rec)
            _log_progress(len(done_records) + i, total, run_start)
    else:
        logger.warning("[lme] concurrent mode workers=%d, todo=%d", workers, len(todo))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_run_one_safe, q, qi, args, arms, cfgs, dims,
                            aliases, llm_fn, run_id, out_dir): qi
                for qi, q in todo
            }
            for i, fut in enumerate(as_completed(futs), 1):
                rec = fut.result()
                _append_checkpoint(cp_path, fp, rec, new=(cp_new and i == 1))
                new_records.append(rec)
                _log_progress(len(done_records) + i, total, run_start)

    elapsed = int(time.perf_counter() - run_start)
    return _aggregate(done_records + new_records, args, arms, total,
                      elapsed, workers, bool(done_records))
def report_md(result: dict) -> str:
    L = ["# SGME · LongMemEval 业界标准评测报告", ""]
    L.append(f"- 数据集：{result['dataset']}（{result['n_questions']} 题）")
    L.append(f"- top-k：{result['top_k']} ｜ 臂：{', '.join(result['arms'])}")
    L.append(f"- 图召回：休眠（直灌无提炼 → memory_edges 空，公平；refined 臂另跑 L1/L1.5 提炼，但仍无 scenes → 图召回同为 0）")
    if "refined" in result["arms"]:
        L.append(f"- refined 臂提炼模型：本地 LM Studio（{os.environ.get('SGME_REFINE_MODEL', 'qwen3.8-9b-distill')}），零云依赖")
    L.append(f"- 耗时：{result['elapsed_s']}s")
    L.append("")
    L.append("## 检索 recall（session 级，按 answer_session_ids）")
    L.append("")
    L.append("| 臂 | 题型 | recall@%d | n |" % result["top_k"])
    L.append("|---|---|---|---|")
    for k, d in result["retrieval_recall_by_arm_type"].items():
        arm, qtype = k.split(":", 1)
        L.append(f"| {arm} | {qtype} | {d['recall@%d' % result['top_k']]} | {d['total']} |")
    qa = result["qa"]
    L.append("")
    L.append("## QA（DeepSeek 生成 + judge）")
    L.append("")
    if qa["enabled"]:
        L.append(f"- J-score：{qa['j_score']}（correct {qa['correct']} / wrong {qa['wrong']}）")
        L.append(f"- NO CONTEXT 率：{qa['no_context_rate']} ｜ errors：{qa['errors']}")
        L.append("")
        L.append("| 题型 | J-score | judged | F1 |")
        L.append("|---|---|---|---|")
        for c, d in qa["by_type"].items():
            L.append(f"| {c} | {d['j_score']} | {d['judged']} | {d['f1']} |")
    else:
        L.append("- 未启用（--qa）")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="SGME LongMemEval 评测台")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--output", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0, help="起始题号，用于定向抽样特定题型")
    ap.add_argument("--arms", default="bm25,hybrid", help="bm25,hybrid,refined（refined=跑完整 L0→L1→L1.5 生产链路）")
    ap.add_argument("--run-id", default=None,
                    help="复用指定 run_id 的 tmp/raw 目录（断点续跑必需；默认按时间戳新建）")
    ap.add_argument("--refine-backend", default="cloud", choices=["cloud", "local"],
                    help="refined 臂提炼后端：cloud=SGME 生产链(agnes→siliconflow，可靠但限速0.5rps)；local=本地 LM Studio 9B(快但英文 L1 不可靠)")
    ap.add_argument("--refine-stage", default="all", choices=["all", "ingest", "refine", "eval"],
                    help="refined 臂分步执行（T-150）：ingest=只写 L0（零 LLM）；refine=只提炼（session 级断点续跑）；eval=只评测；all=一体跑。分步可随时停止，中断损失=单 session")
    ap.add_argument("--primary", default=None, help="QA 用哪条臂的检索结果")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--refined-session-k", type=int, default=None,
                    help="refined 臂会话级聚合：取前 N 个来源会话作为上下文（默认与 --top-k 同值；0=关闭，退回旧口径）")
    ap.add_argument("--refined-pool", type=int, default=10,
                    help="会话级聚合的候选池倍数（池 = top-k × 该值，默认 10）")
    ap.add_argument("--refined-ctx-chars", type=int, default=96000,
                    help="会话级聚合后拼上下文的字符上限（默认 96000，对齐直灌臂典型体量）")
    ap.add_argument("--qa", action="store_true", help="启用 LLM 生成 + judge")
    ap.add_argument("--qa-mode", default="legacy", choices=["legacy", "product"],
                    help="QA 生成模式：legacy=旧裸拼 prompt；product=T-149 answer 操作语义（facts 证据+时序时间线+题型分派）")
    ap.add_argument("--judge-model", default="deepseek-v4-flash")
    ap.add_argument("--judge-base-url", default=None, help="LLM judge base_url (env SGME_JUDGE_BASE_URL)")
    ap.add_argument("--judge-api-key-env", default=None, help="LLM judge api key env (env SGME_JUDGE_KEY_ENV)")
    ap.add_argument("--workers", type=int, default=1,
                    help="question-level concurrency (isolated per-question DBs; 2-3 lanes safe for agnes RPM 20-30)")
    ap.add_argument("--resume", action="store_true",
                    help="resume from <output>/checkpoint.jsonl (skip completed questions when fingerprint matches)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    result = run(args)
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "longmemeval_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "longmemeval_report.md").write_text(report_md(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
