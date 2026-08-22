# 验证：合并新场景向量回填 + 当前场景数 + 剩余相似对
import sqlite3

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

print('== 合并产生的新场景是否都有向量 ==')
rows = db.execute("""
    SELECT s.scene_id, s.title, s.heat,
           (sv.scene_id IS NOT NULL) AS has_vec
    FROM scenes s LEFT JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active' AND (s.title LIKE 'merged_%' OR s.title LIKE 'scene_%')
      AND s.heat >= 5
    ORDER BY s.heat DESC LIMIT 10
""").fetchall()
for r in rows:
    print('  %s h%s vec=%s %s' % (r['scene_id'][:8], r['heat'], r['has_vec'], (r['title'] or '')[:30]))

print()
print('== 无向量 active 场景 ==')
r = db.execute("""
    SELECT COUNT(*) c FROM scenes s
    LEFT JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active' AND sv.scene_id IS NULL
""").fetchone()
print('  无向量:', r['c'])

print()
print('== 场景总数 ==')
for r in db.execute("SELECT status, COUNT(*) c FROM scenes GROUP BY status"):
    print('  %s: %s' % (r['status'], r['c']))
