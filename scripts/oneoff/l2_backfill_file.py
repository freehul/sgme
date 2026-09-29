# -*- coding: utf-8 -*-
"""L2 漏批补聚合工具（一次性，T-234）。

背景：L2 批遇坏 JSON 时整批跳过（不重试、不阻塞），该批记忆永不进场景
（scene_memories 无关联）。本工具把指定文件「未关联场景」的 active 记忆
补喂给 L2 聚合入口（sgme.engine.l2.aggregate），缺口清零。不碰提炼游标、
不重跑 L1/L1.5。

用法（宿主机）：
    cat scripts/oneoff/l2_backfill_file.py | ssh <nas> "docker exec -i -e PYTHONPATH=/app sgme python3 - <file_id> [--apply]"
    # 默认 dry-run 只打印；--apply 才真正执行聚合。
    # 前置：FILE_ID 为 raw_files.file_id（完整 uuid；前缀亦可，按 "<id>:%" 前缀匹配）。
"""
import sys

FILE_ID = sys.argv[1] if len(sys.argv) > 1 else ""
APPLY = "--apply" in sys.argv
if not FILE_ID or FILE_ID.startswith("--"):
    print("用法: python - <file_id> [--apply]")
    raise SystemExit(2)

from sgme.config import load_config
from sgme.data import db as db_mod

cfg = load_config()
mem_conn = db_mod.connect_memory("/data/data")

# 1) 找出未关联场景的 active 记忆（source_ref 形如 "<file_id>:<seq>"）
rows = mem_conn.execute("""
  SELECT m.memory_id, m.content, m.memory_type, m.status
  FROM memories m
  WHERE m.status='active'
    AND m.memory_id IN (SELECT memory_id FROM memory_sources WHERE source_ref LIKE ?)
    AND NOT EXISTS (SELECT 1 FROM scene_memories sm WHERE sm.memory_id = m.memory_id)
  ORDER BY m.rowid
""", (f"{FILE_ID}:%",)).fetchall()

memories = []
for r in rows:
    dims = [x[0] for x in mem_conn.execute(
        "SELECT dimension_id FROM memory_tags WHERE memory_id=?", (r["memory_id"],))]
    memories.append({
        "memory_id": r["memory_id"],
        "content": r["content"],
        "dimension_ids": dims,
        "memory_type": r["memory_type"],
    })

print(f"[l2-backfill] file={FILE_ID}")
print(f"[l2-backfill] 待补记忆 {len(memories)} 条")
for m in memories:
    print(f"  - {m['memory_id'][:8]} dims={m['dimension_ids']} len={len(m['content'])}")

if not memories:
    print("[l2-backfill] 无缺口，退出")
    mem_conn.close()
    raise SystemExit(0)

if not APPLY:
    print("[l2-backfill] dry-run 结束（如需执行加 --apply）")
    mem_conn.close()
    raise SystemExit(0)

# 2) 补跑 L2 聚合（与 finalize_refinement 同路径）
from sgme.prompts import BucketCtx
from sgme.engine import l2

res = l2.aggregate(memories, mem_conn, cfg, bucket_ctx=BucketCtx(bucket_key=FILE_ID))
print(f"[l2-backfill] created={res.created} updated={res.updated} merged={res.merged} archived={res.archived}")
print(f"[l2-backfill] error={res.error}")

# 3) 复核：仍无关联应为 0
left = mem_conn.execute("""
  SELECT COUNT(*) FROM memories m
  WHERE m.status='active'
    AND m.memory_id IN (SELECT memory_id FROM memory_sources WHERE source_ref LIKE ?)
    AND NOT EXISTS (SELECT 1 FROM scene_memories sm WHERE sm.memory_id = m.memory_id)
""", (f"{FILE_ID}:%",)).fetchone()[0]
mem_conn.commit()
print(f"[l2-backfill] 复核：仍无关联 {left} 条（应为 0）")
mem_conn.close()
