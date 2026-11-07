#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/zcode_stage.py：把重建 home 的待提炼文件打包成「ZCode agent 任务包」。零 LLM 调用。

背景（2026-11-07）：外部网关（magpie / 官方 CLI 桥）本质仍是脚本调 API，体验桶会因
客户端指纹校验/风控被封。改为「ZCode 客户端里的 agent 自己推理」：本脚本只负责把
每个文件渲染成模型该看到的完整提示词（与线上 L2 提取同一条模板、同一套维度），
agent 只需读提示词、产出 JSON、写到指定路径；再由 zcode_import.py 零 LLM 落库。

产物：
  <out>/in/<task_id>.txt        提示词全文（直接发给模型）
  <out>/out/<task_id>.json      agent 应产出的 JSON 数组
  <out>/manifest.json           任务清单 + 每文件分块数 / 最大 seq
  <out>/README.md               给 agent 的格式说明

用法：
    python scripts/zcode_stage.py --home rebuild-20261106/home --limit 5
    python scripts/zcode_stage.py --home rebuild-20261106/home
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def bootstrap(home: Path) -> None:
    os.environ["SGME_HOME"] = str(home)
    os.environ.setdefault("SGME_L2_SCHEMA", "2.0")
    os.environ.pop("SGME_CONFIG_PATH", None)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重建 home 转 ZCode agent 任务包")
    ap.add_argument("--home", default=str(REPO_ROOT / "rebuild-20261106" / "home"))
    ap.add_argument("--out", default=None, help="输出目录（默认 home 同级 staging）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理 N 个文件（冒烟）")
    ap.add_argument("--chunk-chars", type=int, default=30000,
                    help="单任务最大会话字符数（默认 30000，超出按回合切块）")
    args = ap.parse_args(argv)

    home = Path(args.home).resolve()
    if not (home / "data" / "session.db").exists():
        print("home 不存在或没有 session.db: " + str(home))
        return 2
    out = Path(args.out).resolve() if args.out else (home.parent / "staging")
    (out / "in").mkdir(parents=True, exist_ok=True)
    (out / "out").mkdir(parents=True, exist_ok=True)

    bootstrap(home)
    from sgme import config as cfg_mod
    from sgme.data import db as db_mod, session_dao
    from sgme.engine import l2_extraction, prune as prune_mod, refine as refine_mod
    from sgme.prompts import PromptStore
    from sgme.raw import store as raw_store

    cfg = cfg_mod.load_config()
    dims = cfg["dimensions"]
    pv = PromptStore().get("l1_extraction")
    prune_cfg = (cfg.get("refine") or {}).get("prune")

    mem_conn, session_conn, _ = db_mod.init_databases(home / "data")
    try:
        pending = session_dao.list_by_status(session_conn, "new", limit=args.limit or 100000)
        tasks: list[dict] = []
        files: dict[str, dict] = {}
        empty_files: list[str] = []
        total_chars = 0
        for rf in pending:
            file_id = rf["file_id"]
            try:
                parsed = raw_store.parse_file(file_id)
            except Exception as e:
                print("跳过 " + file_id + ": 解析失败 " + str(e))
                continue
            incremental = raw_store.extract_incremental(parsed, rf.get("last_refined_seq") or 0)
            if not incremental:
                continue
            incremental = prune_mod.prune_messages(incremental, prune_cfg)
            for m in incremental:
                try:
                    m.content = refine_mod._strip_injected_profile(m.content or "")
                except Exception:
                    pass
            chars = sum(len(m.content or "") for m in incremental)
            if chars == 0:
                # 剪枝后无正文（纯 tool 输出/系统注入的 cron 快照）→ 不产生任务，
                # 由 import 直接标 refined（与线上「空会话提炼出 []」语义一致），
                # 免得 agent 白读 882 个空提示词。
                empty_files.append(file_id)
                files[file_id] = {"chunks_total": 0, "max_seq": rf.get("last_refined_seq") or 0, "chars": 0}
                continue
            if chars <= args.chunk_chars:
                chunks = [incremental]
            else:
                chunks = l2_extraction.chunk_messages_by_turn(incremental, chunk_size=args.chunk_chars)
            max_seq = max((m.seq for m in incremental), default=rf.get("last_refined_seq") or 0)
            files[file_id] = {"chunks_total": len(chunks), "max_seq": max_seq, "chars": chars}
            for idx, chunk_msgs in enumerate(chunks):
                task_id = file_id + "__" + str(idx).zfill(3)
                conversation = refine_mod._format_conversation(chunk_msgs, file_id)
                prompt = l2_extraction._render_extraction_text(pv.text, conversation, dims, "")
                prompt_path = out / "in" / (task_id + ".txt")
                prompt_path.write_text(prompt, encoding="utf-8")
                tasks.append({
                    "task_id": task_id,
                    "file_id": file_id,
                    "chunk_index": idx,
                    "chunks_total": len(chunks),
                    "chars": len(conversation),
                    "prompt_path": prompt_path.relative_to(out).as_posix(),
                    "out_path": "out/" + task_id + ".json",
                })
                total_chars += len(prompt)
        manifest = {
            "home": str(home),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "prompt_version": pv.version,
            "total_tasks": len(tasks),
            "total_chars": total_chars,
            "files": files,
            "empty_files": empty_files,
            "tasks": tasks,
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8",
        )
        (out / "README.md").write_text(_readme(pv.version), encoding="utf-8")
        print("已生成任务包: " + str(out))
        print(f"文件 {len(files)} 个（其中剪枝后为空 {len(empty_files)} 个）/ 任务 {len(tasks)} 个 / 提示词合计 {total_chars/1e6:.2f}M 字符")
        return 0
    finally:
        mem_conn.close()
        session_conn.close()


def _readme(version: str) -> str:
    head = [
        "# ZCode agent 执行说明（零 API：你自己就是模型）",
        "",
        "任务：对 in/ 下每个 .txt（提示词全文）生成记忆数组，写到 out/ 下同名 .json。",
        "",
        "## 每个任务怎么做",
        "1. 读 in/<task_id>.txt（这就是发给模型的完整提示词，含规则、维度表、会话原文）",
        "2. 按提示词要求产出 JSON 数组（纯数组，不要代码块、不要解释）",
        "3. 写到 out/<task_id>.json（数组为空写 []）",
        "",
        "## 硬性要求",
        "- 不要调用任何外部 API / 网关；用你自己的推理完成。",
        "- 不要修改 in/ 里的文件、不要动 ../home/ 下的任何数据库。",
        "- 一次处理一个任务；写完 json 再下一个。",
        "- 提示词版本：" + version + "（与线上 L2 提取同一模板，别自行改写规则）。",
        "",
    ]
    return chr(10).join(head)


if __name__ == "__main__":
    sys.exit(main())