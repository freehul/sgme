#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/zcode_import.py：把 ZCode agent 产出的 JSON 导入重建 home。零 LLM 调用。

配套 zcode_stage.py：agent 按任务包写 out/<task_id>.json 后，本脚本
  1. 复用线上 L2 解析器校验字段；
  2. 多块合并按 content 去重；
  3. 复用 refine.normalize_memories 做维度归一化与 occurred_at；
  4. 走 pipeline._fallback_direct_store 落库（不调 L1.5/L3 的 LLM）；
  5. 推进 session.db 游标 status=refined，清失败计数。
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
    ap = argparse.ArgumentParser(description="导入 ZCode agent 产出的记忆 JSON")
    ap.add_argument("--home", default=str(REPO_ROOT / "rebuild-20261106" / "home"))
    ap.add_argument("--staging", default=None, help="任务包目录（默认 home 同级 staging）")
    ap.add_argument("--file-id", default=None, help="只导入某个文件（冒烟）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args(argv)

    home = Path(args.home).resolve()
    staging = Path(args.staging).resolve() if args.staging else (home.parent / "staging")
    manifest_path = staging / "manifest.json"
    if not manifest_path.exists():
        print("没有 manifest.json，先跑 scripts/zcode_stage.py: " + str(manifest_path))
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    bootstrap(home)
    from sgme import config as cfg_mod
    from sgme.data import db as db_mod, session_dao
    from sgme.engine import l2_extraction, pipeline, refine as refine_mod
    from sgme.raw import store as raw_store

    cfg = cfg_mod.load_config()
    dims = cfg["dimensions"]
    mem_conn, session_conn, _ = db_mod.init_databases(home / "data")
    # 维度注册表是 memory_tags 的外键目标：全新 home 必须先导入，否则 FK 失败
    try:
        from sgme.data import memory_dao

        memory_dao.import_registry(mem_conn, dims, cfg.get("aliases") or {})
    except Exception as e:  # noqa: BLE001——已导入过/幂等失败时不阻塞
        print("维度注册表导入失败（继续）: " + str(e))

    done_state_path = staging / "imported.json"
    imported: dict[str, dict] = {}
    if done_state_path.exists():
        try:
            imported = json.loads(done_state_path.read_text(encoding="utf-8"))
        except Exception:
            imported = {}

    by_file: dict[str, list[dict]] = {}
    for t in manifest.get("tasks", []):
        by_file.setdefault(t["file_id"], []).append(t)

    files = manifest.get("files", {})
    stats = {"files_ready": 0, "files_imported": 0, "files_skipped": 0, "memories": 0, "errors": 0}
    try:
        # 空文件（剪枝后无正文）没有任务，也要走一遍标记 refined —— 否则它会一直留 new
        all_files = list(by_file.keys()) + [f for f in manifest.get("empty_files", []) if f not in by_file]
        for file_id in all_files:
            tasks = by_file.get(file_id, [])
            if args.file_id and args.file_id != file_id:
                continue
            if file_id in imported:
                stats["files_skipped"] += 1
                continue
            meta = files.get(file_id, {}) or {}
            if int(meta.get("chunks_total") or 0) == 0:
                # 剪枝后为空的文件：直接收敛为 refined（0 条记忆），不产生任务
                if not args.dry_run:
                    session_dao.update_refine_cursor(
                        session_conn, file_id, int(meta.get("max_seq") or 0), status="refined",
                    )
                    imported[file_id] = {"memories": 0, "empty": True}
                stats["files_imported"] += 1
                continue
            missing = []
            for t in tasks:
                if not (staging / t["out_path"]).exists():
                    missing.append(t)
            if missing:
                stats["files_skipped"] += 1
                continue
            stats["files_ready"] += 1
            raw_memories: list[dict] = []
            seen: set[str] = set()
            parse_error = None
            ordered = sorted(tasks, key=lambda x: x["chunk_index"])
            for t in ordered:
                text = (staging / t["out_path"]).read_text(encoding="utf-8").strip()
                try:
                    # 2026-11-07 修复：v019 输出为 2.0 结构（main_event+facts），必须走
                    # 与线上提取链同一分派入口（SGME_L2_SCHEMA 默认 2.0）；
                    # 旧 1.x 公开解析器读不懂 2.0 结构，会静默丢成 0 条。
                    mems = l2_extraction._parse_extraction_output(text, dims)
                except Exception as e:
                    parse_error = str(e)
                    break
                for m in mems:
                    key = (m.get("content") or "").strip()
                    if key and key not in seen:
                        seen.add(key)
                        raw_memories.append(m)
            if parse_error is not None:
                stats["errors"] += 1
                print("解析失败 " + file_id + ": " + parse_error)
                continue
            if args.dry_run:
                print("可导入 " + file_id + " -> " + str(len(raw_memories)) + " 条")
                continue
            parsed = raw_store.parse_file(file_id)
            seq_ts = {m.seq: m.timestamp for m in parsed.messages if getattr(m, "timestamp", None)}
            normalized, _st = refine_mod.normalize_memories(
                raw_memories, dims, mem_conn,
                file_id=file_id, seq_ts=seq_ts, sunk_refs=[],
            )
            max_seq = int(files.get(file_id, {}).get("max_seq") or 0)
            if normalized:
                result = refine_mod.RefineResult(file_id=file_id, memories=normalized)
                result.prompt_versions["l1_extraction"] = {
                    "stage": "l1_extraction",
                    "version": str(manifest.get("prompt_version") or "v019"),
                    "variant": None,
                }
                src_refs = []
                for m in normalized:
                    ids = m.get("source_message_ids") or [0]
                    src_refs.append(file_id + ":" + str(ids[0]))
                pipeline._fallback_direct_store(
                    result, mem_conn, cfg, src_refs,
                    prompt_version="l1_extraction:" + str(manifest.get("prompt_version") or "v019"),
                )
            session_dao.update_refine_cursor(session_conn, file_id, max_seq, status="refined")
            try:
                session_dao.clear_refine_failure(session_conn, file_id)
            except Exception:
                pass
            imported[file_id] = {
                "memories": len(normalized),
                "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            stats["files_imported"] += 1
            stats["memories"] += len(normalized)
        if not args.dry_run:
            done_state_path.write_text(
                json.dumps(imported, ensure_ascii=False, indent=1), encoding="utf-8",
            )
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        mem_conn.close()
        session_conn.close()


if __name__ == "__main__":
    sys.exit(main())