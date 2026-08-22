# 场景相似度分析：全量 active 场景向量两两相似度，找可合并对
import sqlite3, json, sys
import numpy as np

db = sqlite3.connect('/data/data/memory.db')
db.row_factory = sqlite3.Row

# 取 active 场景的向量
rows = db.execute("""
    SELECT s.scene_id, s.title, s.heat, sv.embedding, sv.dims
    FROM scenes s JOIN scene_vectors sv ON sv.scene_id = s.scene_id
    WHERE s.status='active'
""").fetchall()
print('有向量的 active 场景:', len(rows))

vecs = []
meta = []
for r in rows:
    v = np.frombuffer(r['embedding'], dtype=np.float32)
    if v.shape[0] != r['dims']:
        print('  SKIP 维度不一致:', r['scene_id'], v.shape[0], r['dims'])
        continue
    vecs.append(v.astype(np.float32))
    meta.append({'scene_id': r['scene_id'], 'title': r['title'] or '', 'heat': r['heat']})

V = np.stack(vecs)  # (N, D)
n = V.shape[0]
# 归一化
V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
# 相似度矩阵
S = V @ V.T

# 只取上三角（排除自身），找相似度 >= 阈值的对
pairs = []
for i in range(n):
    for j in range(i+1, n):
        sim = float(S[i][j])
        if sim >= 0.80:
            pairs.append((sim, meta[i], meta[j]))

pairs.sort(reverse=True)
print()
print('== 相似度 >= 0.80 的场景对: %d 对 ==' % len(pairs))
for sim, a, b in pairs[:40]:
    print('  %.3f | %s(heat=%s) <=> %s(heat=%s)' % (
        sim, (a['title'] or a['scene_id'][:8])[:28], a['heat'],
        (b['title'] or b['scene_id'][:8])[:28], b['heat']))

# 高相似度对覆盖的场景数（可能合并的）
covered = set()
for sim, a, b in pairs:
    if sim >= 0.85:
        covered.add(a['scene_id']); covered.add(b['scene_id'])
print()
print('== 相似度 >= 0.85 覆盖场景数: %d（可合并对 %d 对）==' % (
    len(covered), sum(1 for s,_,_ in pairs if s >= 0.85)))

# 相似度直方图
import collections
hist = collections.Counter()
for i in range(n):
    for j in range(i+1, n):
        s = float(S[i][j])
        if s >= 0.5:
            hist[int(s*10)/10] += 1
print()
print('== 相似度直方图（>=0.5）==')
for k in sorted(hist, reverse=True):
    print('  %.1f: %d 对' % (k, hist[k]))
