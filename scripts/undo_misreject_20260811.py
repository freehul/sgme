"""撤销误标：xiaozhi 关键词误伤了 smart-robot 项目记忆（2026-08-11）
保留 rejected 的 = 明确属 AIXM 控制台方案/代码迁移语境
撤销的 = smart-robot（ESP32/小智固件/NAS 容器/设备）等活跃项目记忆
"""
import sys, sqlite3
sys.path.insert(0, '.')
from sgme.data import memory_dao, db

conn = db.connect_memory()
conn.row_factory = sqlite3.Row

# 刚才补标批次的记忆（reason 含"语音方案"）
rows = conn.execute(
    "SELECT memory_id, content FROM memories "
    "WHERE status='rejected' AND reject_reason LIKE '%语音方案%'"
).fetchall()
print(f"补标批次共 {len(rows)} 条")

# 保留 rejected（明确 AIXM 语境）
KEEP_REJECTED = [
    'd78038d5',   # 方案 v3.0 语音引擎从 Web Speech 改 py-xiaozhi（AIXM 控制台方案）
    '1fa17aa3',   # 旧代码不复用：vendor/xiaozhi_core（aixm→SGME 迁移决策）
    'f630f154',   # vendor/xiaozhi_core 子模块变更（AIXM 代码）
    '45be0a77',   # Fable 5 审查 v4.0 方案（AIXM 控制台方案审查）
]

undo, keep = [], []
for r in rows:
    mid8 = r['memory_id'][:8]
    if mid8 in KEEP_REJECTED:
        keep.append(r)
    else:
        undo.append(r)

print(f"撤销（smart-robot 等误伤）: {len(undo)} 条")
print(f"保留 rejected（AIXM 语境）: {len(keep)} 条")
for r in keep:
    print(f"  保留: {r['memory_id'][:8]} :: {r['content'][:50]}")

# 撤销：恢复 active（reject_reason 清空）
for r in undo:
    memory_dao.unreject_memory(conn, r['memory_id'])
print(f"\n已撤销 {len(undo)} 条")

# 验证：确认 smart-robot 相关恢复
n = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active' AND (content LIKE '%smart-robot%' OR content LIKE '%ESP32%')").fetchone()[0]
print(f"smart-robot/ESP32 active 记忆: {n} 条")
conn.close()
