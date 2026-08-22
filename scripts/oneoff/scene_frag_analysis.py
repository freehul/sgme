import sqlite3

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

# 默认标题（scene_xxx 前缀 = LLM 创建时没提取出有意义标题）统计
print('== 默认标题 scene_xxx 统计 ==')
for r in db.execute("""
    SELECT CASE
        WHEN title LIKE 'scene_%' THEN 'a. 默认标题(无主题)'
        ELSE 'b. 有意义标题'
    END t, COUNT(*) c
    FROM scenes WHERE status='active'
    GROUP BY t
"""):
    print('  %s: %s' % (r['t'], r['c']))

print()
print('== 默认标题 × heat 分布 ==')
for r in db.execute("""
    SELECT heat, COUNT(*) c FROM scenes
    WHERE status='active' AND title LIKE 'scene_%'
    GROUP BY heat ORDER BY heat
"""):
    print('  heat=%s: %s' % (r['heat'], r['c']))

print()
print('== 默认标题 × 时间分段 ==')
for r in db.execute("""
    SELECT CASE
        WHEN updated_at < '2026-08-08' THEN 'a. 08-07前'
        WHEN updated_at < '2026-08-12' THEN 'b. 08-08~11'
        WHEN updated_at < '2026-08-17' THEN 'c. 08-12~16'
        ELSE 'd. 08-17后'
    END seg, COUNT(*) c
    FROM scenes WHERE status='active' AND title LIKE 'scene_%'
    GROUP BY seg ORDER BY seg
"""):
    print('  %s: %s' % (r['seg'], r['c']))

print()
print('== 抽样：默认标题 + heat=1 的内容（评估价值）==')
for r in db.execute("""
    SELECT scene_id, title, heat, substr(content,1,200) c
    FROM scenes WHERE status='active' AND title LIKE 'scene_%' AND heat=1
    ORDER BY updated_at LIMIT 12
"""):
    print('  [%s heat=%s] %s' % (r['scene_id'][:8], r['heat'], (r['c'] or '').replace('\n', ' ')[:150]))
