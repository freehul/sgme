# -*- coding: utf-8 -*-
"""清理 e2e 测试数据（2026-08-07）：标记 rejected 而非删除。

e2e 测试会话（e2e-v1/v2/smoke）衍生的记忆与场景是测试数据污染，
按「打标记不删除」原则标记 rejected：不参与查询/时间线，数据保留可溯源。

v0.7 三库拆分：raw_files 读 session.db（sconn），
memories / scenes 系列读 memory.db（mconn）。
"""
import sys
sys.path.insert(0, '.')
from sgme.data.db import get_memory_conn, get_session_conn
from sgme.data import memory_dao, scene_dao

mconn = get_memory_conn()
sconn = get_session_conn()

# 1. e2e 会话列表（raw_files 现居 session.db）
e2e = sconn.execute("SELECT file_id, session_key FROM raw_files WHERE session_key LIKE 'e2e%'").fetchall()
e2e_ids = [f[0] for f in e2e]
print(f"e2e 会话: {len(e2e_ids)} 个")

# 2. 标记这些会话衍生的记忆为 rejected
marked_mem = 0
for fid, key in e2e:
    rows = mconn.execute(
        "SELECT DISTINCT memory_id FROM memory_sources WHERE source_ref LIKE ?",
        (fid + "%",),
    ).fetchall()
    for (mid,) in rows:
        # 检查是否还在 memories 表（未被 L1.5 归档）
        cur = mconn.execute("SELECT status FROM memories WHERE memory_id=?", (mid,)).fetchone()
        if cur and cur[0] == "active":
            memory_dao.reject_memory(mconn, mid, "e2e 测试数据污染")
            marked_mem += 1

# 3. 标记相关场景为 rejected（内容含 e2e 测试特征；scenes 现居 memory.db）
scenes = mconn.execute(
    "SELECT scene_id FROM scenes WHERE status='active' AND (content LIKE '%MCP 写入测试%' OR content LIKE '%e2e%')"
).fetchall()
marked_scene = 0
for (sid,) in scenes:
    scene_dao.update_scene_status(mconn, sid, "rejected")
    marked_scene += 1

print(f"标记记忆: {marked_mem} 条 → rejected")
print(f"标记场景: {marked_scene} 个 → rejected")

# 4. 验证
print()
print("=== 验证 ===")
print("rejected 记忆总数:", mconn.execute("SELECT COUNT(*) FROM memories WHERE status='rejected'").fetchone()[0])
print("rejected 场景总数:", mconn.execute("SELECT COUNT(*) FROM scenes WHERE status='rejected'").fetchone()[0])
print("active 记忆:", mconn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0])
print("active 场景:", mconn.execute("SELECT COUNT(*) FROM scenes WHERE status='active'").fetchone()[0])
