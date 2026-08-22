# 查看 0.75+ 候选合并对的内容（判断是否真语义相近）
import sqlite3

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

def show(sid_short):
    r = db.execute(
        "SELECT scene_id, title, heat, substr(content,1,150) c FROM scenes WHERE scene_id LIKE ? AND status='active'",
        (sid_short + "%",)
    ).fetchone()
    if r:
        print('    [%s h%s] %s | %s' % (r['scene_id'][:8], r['heat'], r['title'] or '(空)', (r['c'] or '').replace('\n', ' ')))
    else:
        print('    [%s] 不存在或已归档' % sid_short)

for a, b, sim in [
    ('c99fda69', 'a6b6d7d5', 0.796),  # 透析伴侣 ↔ ?
    ('efd0178c', 'b4a7a5e4', 0.776),
    ('32843080', '24c750f6', 0.755),
    ('160e9df7', 'acdc5e31', 0.798),
    ('53be8712', '07a3a621', 0.775),
]:
    print('== 对 %.3f ==' % sim)
    show(a)
    show(b)
