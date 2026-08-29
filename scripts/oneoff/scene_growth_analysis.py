# 场景增长速率分析：判断收敛趋势 vs 持续膨胀
import sqlite3

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

print('== active 场景创建时间分布（按天）==')
rows = db.execute("""
    SELECT substr(created_at,1,10) d, COUNT(*) c
    FROM scenes WHERE status='active'
    GROUP BY d ORDER BY d
""").fetchall()
for r in rows:
    print('  %s: %s' % (r['d'], r['c']))

print()
print('== 近 7 天新增 active 场景 ==')
rows = db.execute("""
    SELECT substr(created_at,1,10) d, COUNT(*) c
    FROM scenes WHERE status='active' AND created_at >= '2026-08-16'
    GROUP BY d ORDER BY d
""").fetchall()
total = 0
for r in rows:
    total += r['c']
    print('  %s: +%s' % (r['d'], r['c']))
print('  近7天合计新增: %s' % total)

print()
print('== heat 分布（active）==')
for r in db.execute("""
    SELECT CASE
        WHEN heat >= 10 THEN '>=10 活跃'
        WHEN heat >= 5 THEN '5-9 常驻'
        WHEN heat >= 3 THEN '3-4'
        ELSE '1-2 冷'
    END b, COUNT(*) c FROM scenes WHERE status='active'
    GROUP BY b ORDER BY c DESC
"""):
    print('  %s: %s' % (r['b'], r['c']))

print()
print('== L2 动作趋势（近 3 天 refine_runs action 分布）==')
rows = db.execute("""
    SELECT date(started_at) d, action_counts, status
    FROM refine_runs WHERE stage='l2_scene' AND started_at >= '2026-08-20'
    ORDER BY started_at DESC LIMIT 40
""").fetchall()
import json
from collections import Counter
day_actions = {}
for r in rows:
    d = r['d']
    if d not in day_actions:
        day_actions[d] = Counter()
    try:
        ac = json.loads(r['action_counts'])
        for k, v in ac.items():
            day_actions[d][k] += v
    except Exception:
        pass
for d in sorted(day_actions):
    c = day_actions[d]
    print('  %s: create=%s update=%s merge=%s' % (d, c.get('create',0), c.get('update',0), c.get('merge',0)))
