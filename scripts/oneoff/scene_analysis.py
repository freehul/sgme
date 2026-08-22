# 分析 L2 场景分布：热度/大小/时间/相似度，为超限治理找对象
import sqlite3, datetime

def ts(v):
    if not v:
        return '-'
    try:
        return datetime.datetime.fromtimestamp(v, datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d')
    except Exception:
        return str(v)

db = sqlite3.connect('/vol1/1000/Docker/sgme/data/data/memory.db')
db.row_factory = sqlite3.Row

print('== active 场景总数 ==')
for r in db.execute("SELECT COUNT(*) c FROM scenes WHERE status='active'"):
    print('  active:', r['c'])

print()
print('== heat 分布 ==')
for r in db.execute("""
    SELECT CASE
        WHEN heat >= 50 THEN '>=50'
        WHEN heat >= 20 THEN '20-49'
        WHEN heat >= 10 THEN '10-19'
        WHEN heat >= 5 THEN '5-9'
        WHEN heat >= 3 THEN '3-4'
        WHEN heat >= 2 THEN '2'
        ELSE '1'
    END bucket, COUNT(*) c
    FROM scenes WHERE status='active' GROUP BY bucket ORDER BY c DESC
"""):
    print('  heat %s: %s' % (r['bucket'], r['c']))

print()
print('== 内容长度分布（字符数）==')
for r in db.execute("""
    SELECT CASE
        WHEN LENGTH(content) >= 2000 THEN '>=2000'
        WHEN LENGTH(content) >= 1000 THEN '1000-1999'
        WHEN LENGTH(content) >= 500 THEN '500-999'
        WHEN LENGTH(content) >= 200 THEN '200-499'
        WHEN LENGTH(content) >= 100 THEN '100-199'
        ELSE '<100'
    END bucket, COUNT(*) c
    FROM scenes WHERE status='active' GROUP BY bucket ORDER BY c DESC
"""):
    print('  len %s: %s' % (r['bucket'], r['c']))

print()
print('== 最后更新月份分布 ==')
for r in db.execute("""
    SELECT substr(updated_at,1,7) ym, COUNT(*) c
    FROM scenes WHERE status='active'
    GROUP BY ym ORDER BY ym
"""):
    print('  %s: %s' % (r['ym'], r['c']))

print()
print('== 热度最低 25 个（heat<=2，按更新时间）==')
for r in db.execute("""
    SELECT scene_id, title, heat, LENGTH(content) ln, updated_at
    FROM scenes WHERE status='active' AND heat <= 2
    ORDER BY heat ASC, updated_at ASC LIMIT 25
"""):
    print('  %s | heat=%s | %s字符 | upd=%s | %s' % (r['scene_id'][:8], r['heat'], r['ln'], ts(r['updated_at']), (r['title'] or '')[:30]))

print()
print('== 标题重复/相似粗查（title 前 12 字分组）==')
for r in db.execute("""
    SELECT substr(title,1,12) t, COUNT(*) c
    FROM scenes WHERE status='active'
    GROUP BY substr(title,1,12) HAVING c >= 2 ORDER BY c DESC LIMIT 15
"""):
    print('  "%s" x%s' % (r['t'], r['c']))
