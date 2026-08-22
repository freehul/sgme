import sqlite3, datetime

def ts(v):
    if not v:
        return '-'
    try:
        return datetime.datetime.fromtimestamp(v, datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d')
    except Exception:
        return str(v)

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

# heat=1 全部（73 个）标题 + 时间 + 长度
print('== heat=1 全部场景（含创建时间）==')
rows = db.execute("""
    SELECT scene_id, title, LENGTH(content) ln, updated_at
    FROM scenes WHERE status='active' AND heat=1
    ORDER BY updated_at
""").fetchall()
print('heat=1 总数:', len(rows))
for r in rows:
    print('  %s | %s字符 | %s | %s' % (r['scene_id'][:8], r['ln'], ts(r['updated_at']), (r['title'] or '(空标题)')[:40]))

print()
print('== 按时间分段 ==')
for r in db.execute("""
    SELECT CASE
        WHEN updated_at < '2026-08-08' THEN 'a. 08-07前(历史导入期)'
        WHEN updated_at < '2026-08-12' THEN 'b. 08-08~11(Trae全量提炼)'
        WHEN updated_at < '2026-08-17' THEN 'c. 08-12~16'
        ELSE 'd. 08-17后'
    END seg, COUNT(*) c
    FROM scenes WHERE status='active' AND heat=1
    GROUP BY seg ORDER BY seg
"""):
    print('  %s: %s' % (r['seg'], r['c']))

print()
print('== 全部 active 按时间分段 ==')
for r in db.execute("""
    SELECT CASE
        WHEN updated_at < '2026-08-08' THEN 'a. 08-07前(历史导入期)'
        WHEN updated_at < '2026-08-12' THEN 'b. 08-08~11(Trae全量提炼)'
        WHEN updated_at < '2026-08-17' THEN 'c. 08-12~16'
        ELSE 'd. 08-17后'
    END seg, COUNT(*) c
    FROM scenes WHERE status='active'
    GROUP BY seg ORDER BY seg
"""):
    print('  %s: %s' % (r['seg'], r['c']))
