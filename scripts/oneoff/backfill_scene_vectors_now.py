"""存量回填：对无向量的 active 场景补场景向量（T-97 补完）。

容器内执行：docker exec sgme python3 /data/backfill_scene_vectors_now.py
幂等：scene_vectors 已存在的场景跳过（upsert 语义本就幂等）。
"""
import sqlite3
import sys

sys.path.insert(0, "/app")  # 容器内代码路径（compose 镜像布局）

from sgme import config
from sgme.data import db as db_mod
from sgme.data.search import vector as vector_mod

cfg = config.load_config()

# 容器内真库
mem_conn = db_mod.connect_memory("/data/data")  # 若路径不对则直接连文件
if mem_conn is None:
    mem_conn = sqlite3.connect("/data/data/memory.db")
mem_conn.row_factory = sqlite3.Row

# 找无向量 active 场景
rows = mem_conn.execute("""
    SELECT s.scene_id, s.title, s.content
    FROM scenes s LEFT JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active' AND sv.scene_id IS NULL
""").fetchall()
print("待回填场景数:", len(rows))

ok, fail = 0, 0
for r in rows:
    text = f"{r['title'] or ''} {r['content'] or ''}".strip()
    if not text:
        print("  SKIP 空内容:", r["scene_id"])
        continue
    try:
        done = vector_mod.upsert_scene_vector(mem_conn, r["scene_id"], text, cfg)
        if done:
            ok += 1
            print("  OK", r["scene_id"][:8], (r["title"] or "")[:30])
        else:
            fail += 1
            print("  FAIL(embed None)", r["scene_id"][:8])
    except Exception as e:
        fail += 1
        print("  ERR", r["scene_id"][:8], e)

print(f"\n完成: 成功 {ok} / 失败 {fail}")
