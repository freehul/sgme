# -*- coding: utf-8 -*-
"""operations/evolve.py：自进化管线（W4 方案 v0.3 §5.4，独立第三条管线）。

会话 → wiki 手册（经验回写）。与 engine（会话→memory.db）和 refinery
（文件/URL→wiki_pages）**并行不交叉**：
- 独立游标：wiki_evolve 表（session_key PK），不复用 memory 的 refine_cursor
- 复用：sgme/llm 降级链（call_with_fallback）+ prompts 版本管理 + operations/wiki 写入
- 不接 refinery.refine（其入口只吃文件/URL/图片/视频，不接会话，P0-2 修正）

流程：触发 → 费用门禁（会话消息块 ≥ min_rounds）→ LLM 提炼（结构化 JSON）
→ 规则闸门（type/category/title/entry 校验）→ 写入（append 到手册踩坑记录 /
create 新手册页）→ 审计（wiki_evolve 记录）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from sgme.data import evolve_dao
from sgme.data import wiki_dao
from sgme.llm import chain as llm_chain
from sgme.operations.errors import OperationResult
from sgme.operations.wiki import create_page as create_page_operation
from sgme.operations.wiki import update_page as update_page_operation
from sgme.prompts import PromptStore

logger = logging.getLogger(__name__)

VALID_TYPES = ("append", "create")
MAX_ENTRY_CHARS = 200


# ---------- 可注入点（测试 mock 用） ----------

def _load_prompt(stage: str) -> str:
    """加载提示词（prompts 版本管理，@working 热更新）。"""
    return PromptStore().get(stage).text


def _llm_call(cfg: dict, prompt: str) -> str:
    """降级链调 LLM（chain=refinement，与提炼同链）。返回文本。"""
    text, _provider, _usage = llm_chain.call_with_fallback(cfg, prompt, chain_name="refinement")
    return text


# ---------- 内部 ----------

def _read_session_content(path: str, data_dir: str | None) -> tuple[str | None, int]:
    """读会话文件，返回 (正文内容, 消息块数)。文件缺失返回 (None, 0)。"""
    p = Path(path)
    if not p.is_absolute() and data_dir:
        p = Path(data_dir) / p
    if not p.exists():
        return None, 0
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
    blocks = sum(
        1 for line in text.splitlines()
        if line.startswith("# ") or line.startswith("## ")
    )
    return text.strip(), blocks


def _parse_entries(raw: str) -> list[dict]:
    """解析 LLM 输出为条目列表（容错：剥离 markdown 围栏）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _gate(entry: dict) -> tuple[bool, str]:
    """规则闸门：type/category/title/entry 校验（结构化写入规范，非人工门禁）。"""
    if entry.get("type") not in VALID_TYPES:
        return False, f"非法 type: {entry.get('type')!r}"
    for field in ("category", "title", "entry"):
        if not entry.get(field) or not str(entry[field]).strip():
            return False, f"缺必填字段: {field}"
    if len(str(entry.get("entry", ""))) > MAX_ENTRY_CHARS:
        return False, "entry 超长"
    return True, ""


def _find_handbook(conn: sqlite3.Connection, category: str, title: str) -> dict | None:
    """按 category+title 找现有手册页（确定性判等，不靠 embedding）。"""
    for page in wiki_dao.list_pages(conn, category=category):
        if page["title"] == title:
            return page
    return None


def _apply_entry(
    conn_wiki: sqlite3.Connection, entry: dict, source_session: str
) -> tuple[str, str]:
    """写入条目（append 到现有手册 / create 新手册页）。返回 (action, page_id)。"""
    category = str(entry["category"]).strip()
    title = str(entry["title"]).strip()
    body = str(entry["entry"]).strip()
    author = f"evolve:{source_session}"
    existing = _find_handbook(conn_wiki, category, title)
    if existing is not None:
        res = update_page_operation(
            conn_wiki, existing["page_id"], body, append=True, author=author,
        )
        if not res.ok:
            raise RuntimeError(res.message or "append 失败")
        return "appended", existing["page_id"]
    res = create_page_operation(
        conn_wiki, title=title, content=body, category=category,
        tags=["skill", "evolved"], description=body[:100], author=author,
    )
    if not res.ok:
        raise RuntimeError(res.message or "create 失败")
    return "created", res.data["page_id"]


# ---------- 主操作 ----------

def evolve_trigger(
    conn_wiki: sqlite3.Connection,
    conn_session: sqlite3.Connection,
    cfg: dict,
    session_key: str | None = None,
    limit: int = 5,
    min_rounds: int = 5,
    data_dir: str | None = None,
) -> OperationResult:
    """自进化触发（W4 方案 v0.3 §5.4）。

    - session_key 指定 → 只处理该会话（已处理则幂等跳过）；
      缺省 → 扫 raw_files 中不在 wiki_evolve 的会话（独立游标）
    - 费用门禁：会话消息块 < min_rounds → skipped（不调 LLM）
    - LLM 提炼 → 规则闸门 → 写入（append/create）→ 审计
    """
    if session_key:
        if evolve_dao.has_run(conn_wiki, session_key):
            return OperationResult.succeed(
                {"status": "skipped", "reason": "已处理（幂等跳过）", "processed": []}
            )
        candidates = [session_key]
    else:
        rows = conn_session.execute(
            "SELECT DISTINCT session_key FROM raw_files"
            " ORDER BY started_at DESC LIMIT ?",
            (limit * 3,),
        ).fetchall()
        candidates = [
            r["session_key"] for r in rows
            if r["session_key"] and not evolve_dao.has_run(conn_wiki, r["session_key"])
        ][:limit]

    processed: list[dict] = []
    for sk in candidates:
        row = conn_session.execute(
            "SELECT path FROM raw_files WHERE session_key=?"
            " ORDER BY started_at DESC LIMIT 1",
            (sk,),
        ).fetchone()
        if row is None:
            continue
        content, blocks = _read_session_content(row["path"], data_dir)
        if content is None or blocks < min_rounds:
            evolve_dao.create_run(conn_wiki, sk)
            evolve_dao.update_run(conn_wiki, sk, status="skipped", action="skipped")
            processed.append({"session_key": sk, "status": "skipped"})
            continue

        # LLM 提炼（一次调用；失败 → error 审计，不抛）
        try:
            prompt = _load_prompt("evolve_extraction")
            raw = _llm_call(
                cfg, prompt + "\n\n<session>\n" + content[:6000] + "\n</session>"
            )
            entries = _parse_entries(raw)
        except Exception as e:
            evolve_dao.create_run(conn_wiki, sk)
            evolve_dao.update_run(conn_wiki, sk, status="error", error=str(e)[:200])
            processed.append({"session_key": sk, "status": "error", "error": str(e)[:100]})
            continue

        evolve_dao.create_run(conn_wiki, sk)
        if not entries:
            evolve_dao.update_run(conn_wiki, sk, status="done", action="noop")
            processed.append({"session_key": sk, "status": "done", "action": "noop"})
            continue

        # 规则闸门 + 写入
        accepted = 0
        rejected_reason: str | None = None
        for entry in entries:
            ok, reason = _gate(entry)
            if not ok:
                rejected_reason = rejected_reason or reason
                continue
            try:
                action, page_id = _apply_entry(conn_wiki, entry, sk)
                evolve_dao.update_run(
                    conn_wiki, sk, status="done", action=action, page_id=page_id,
                )
                accepted += 1
            except Exception as e:
                rejected_reason = rejected_reason or str(e)[:100]

        if rejected_reason and accepted == 0:
            evolve_dao.update_run(
                conn_wiki, sk, status="rejected", error=rejected_reason,
            )
        processed.append({
            "session_key": sk,
            "status": "done" if accepted > 0 else ("rejected" if rejected_reason else "noop"),
            "accepted": accepted,
            "error": rejected_reason,
        })

    # 单会话模式：直接返回该会话状态（skipped/done/rejected/error）；
    # 批量模式：status=done + processed 明细。
    if session_key:
        final = processed[0] if processed else {"status": "skipped"}
        return OperationResult.succeed({"status": final.get("status", "skipped"), "detail": final})
    return OperationResult.succeed({"status": "done", "processed": processed})
