"""清理污染数据（2026-08-06 全量提炼前重置）。

- memory.db：清空 memories/memory_archive/memory_tags/memory_sources/
  memory_vectors/refine_runs/signal_events（保留维度注册表/FTS 结构）
  v0.7 起还包含 scenes/scene_memories/scene_versions（场景系列已迁入 memory.db）
- session.db：raw_files 状态重置为 new（v0.7 起 raw_files 居 session.db）
- 备份已由调用方完成（data/backup_20260806/）
"""
import sys
sys.path.insert(0, '.')
from sgme.data.db import get_memory_conn, get_session_conn

mconn = get_memory_conn()
sconn = get_session_conn()

# 1. memory.db 清空（按依赖顺序：子表 → 父表）
mconn.execute("DELETE FROM memory_vectors")   # FK → memories
mconn.execute("DELETE FROM memory_sources")   # FK → memories
mconn.execute("DELETE FROM memory_tags")      # FK → memories
mconn.execute("DELETE FROM memories_fts")
mconn.execute("DELETE FROM memories")
mconn.execute("DELETE FROM memory_archive")
mconn.execute("DELETE FROM refine_runs")
mconn.execute("DELETE FROM signal_events")

# 2. memory.db 清空场景系列（v0.7 三库拆分后场景与记忆同库）
mconn.execute("DELETE FROM scenes")
mconn.execute("DELETE FROM scene_memories")
mconn.execute("DELETE FROM scene_versions")
mconn.commit()

# 3. session.db 重置 raw_files
sconn.execute("UPDATE raw_files SET status='new', refined_at=NULL, last_refined_seq=0")
sconn.commit()

# 4. 验证
print("=== 清理后验证 ===")
print(f"memories: {mconn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]}")
print(f"memory_archive: {mconn.execute('SELECT COUNT(*) FROM memory_archive').fetchone()[0]}")
print(f"memory_tags: {mconn.execute('SELECT COUNT(*) FROM memory_tags').fetchone()[0]}")
print(f"memory_sources: {mconn.execute('SELECT COUNT(*) FROM memory_sources').fetchone()[0]}")
print(f"memory_vectors: {mconn.execute('SELECT COUNT(*) FROM memory_vectors').fetchone()[0]}")
print(f"refine_runs: {mconn.execute('SELECT COUNT(*) FROM refine_runs').fetchone()[0]}")
print(f"dimension_registry(保留): {mconn.execute('SELECT COUNT(*) FROM dimension_registry').fetchone()[0]}")
print(f"scenes: {mconn.execute('SELECT COUNT(*) FROM scenes').fetchone()[0]}")
print(f"scene_memories: {mconn.execute('SELECT COUNT(*) FROM scene_memories').fetchone()[0]}")
print(f"raw_files: {sconn.execute('SELECT status, COUNT(*) FROM raw_files GROUP BY status').fetchall()}")
