import sqlite3
db = sqlite3.connect('/data/data/memory.db')
print('== scene_vectors 结构 ==')
for r in db.execute('PRAGMA table_info(scene_vectors)'):
    print(' ', r[1], r[2])
print('== 表 ==')
for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%vector%' OR name LIKE 'vec_%')"):
    print(' ', r[0])
