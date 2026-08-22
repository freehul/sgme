import sqlite3, datetime

def ts(v):
    if not v:
        return '-'
    try:
        return datetime.datetime.fromtimestamp(v, datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d %H:%M')
    except Exception:
        return str(v)

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

print('== 无向量 active 场景（58 个）画像 ==')
rows = db.execute("""
    SELECT s.scene_id, s.title, s.heat, s.updated_at, LENGTH(s.content) ln
    FROM scenes s LEFT JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active' AND sv.scene_id IS NULL
    ORDER BY s.updated_at DESC LIMIT 30
""").fetchall()
print('无向量数:', len(rows), '/ 全部 active')
for r in rows[:20]:
    print('  %s | heat=%s | %s字符 | upd=%s | %s' % (r['scene_id'][:8], r['heat'], r['ln'], ts(r['updated_at']), (r['title'] or '')[:30]))

print()
print('== 有向量 vs 无向量 heat 对比 ==')
print('有向量 heat 分布:')
for r in db.execute("""
    SELECT s.heat, COUNT(*) c FROM scenes s
    JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active' GROUP BY s.heat ORDER BY s.heat LIMIT 8
"""):
    print('  heat=%s: %s' % (r['heat'], r['c']))
print('无向量 heat 分布:')
for r in db.execute("""
    SELECT s.heat, COUNT(*) c FROM scenes s
    LEFT JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active' AND sv.scene_id IS NULL GROUP BY s.heat ORDER BY s.heat LIMIT 8
"""):
    print('  heat=%s: %s' % (r['heat'], r['c']))

print()
print('== scene_vectors 总量 ==')
for r in db.execute("SELECT COUNT(*) c FROM scene_vectors"):
    print('  scene_vectors 总数:', r['c'])
print('== 最近向量写入时间 ==')
for r in db.execute("SELECT scene_id, model, dims, embedded_at FROM scene_vectors ORDER BY embedded_at DESC LIMIT 3"):
    print('  %s | %s | %s | %s' % (r['scene_id'][:8], r['model'], r['dims'], r['embedded_at']))
