# 找 0.75-0.80 相似度场景对（用于继续合并降数）
import sqlite3
import numpy as np

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

rows = db.execute("""
    SELECT s.scene_id, s.title, s.heat, sv.embedding, sv.dims
    FROM scenes s JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active'
""").fetchall()

vecs, meta = [], []
for r in rows:
    v = np.frombuffer(r['embedding'], dtype=np.float32)
    if v.shape[0] != r['dims']:
        continue
    vecs.append(v.astype(np.float32))
    meta.append({'scene_id': r['scene_id'], 'title': r['title'] or '', 'heat': r['heat']})

V = np.stack(vecs)
V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
S = V @ V.T
n = V.shape[0]

pairs = []
for i in range(n):
    for j in range(i+1, n):
        sim = float(S[i][j])
        if 0.75 <= sim < 0.80:
            pairs.append((sim, meta[i], meta[j]))
pairs.sort(reverse=True)
print('0.75-0.80 对:', len(pairs))
for sim, a, b in pairs[:30]:
    print('  %.3f | %s(h%s) <=> %s(h%s)' % (sim, (a['title'] or a['scene_id'][:8])[:30], a['heat'],
          (b['title'] or b['scene_id'][:8])[:30], b['heat']))
