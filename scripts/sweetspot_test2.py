"""甜点区复测（关闭思考后）：5K/6K/7K/8K 四档 × 2 轮对比。
关闭思考后模型行为变化，甜点区需重新标定。
"""
import sys, time, json
sys.path.insert(0, '.')
from sgme.config import load_config
cfg = load_config()
from sgme.engine import l1, prune
from sgme.raw import store

file_id = '20260806_103007_b7304e'
parsed = store.parse_file(file_id, 'session')
messages = prune.prune_messages(parsed.messages)
print(f'剪枝后: {len(messages)} 条, {sum(len(m.content) for m in messages)} 字符', flush=True)

# 按回合分组
turns = []
current = []
for m in messages:
    if m.role == 'user' and current:
        turns.append(current)
        current = []
    current.append(m)
if current:
    turns.append(current)

def fmt_turn(turn):
    lines = []
    for m in turn:
        lines.append(f'[msg#{m.seq}] {m.timestamp} {m.role}:')
        if m.role == 'tool' and getattr(m, 'tool_name', None):
            lines.append(f'  (tool={m.tool_name})')
        lines.append(f'  {m.content}')
        lines.append('')
    return '\n'.join(lines)

def chunk_by_size(turns, chunk_size):
    """回合感知分块：贪心填充到 chunk_size，超限开新块"""
    chunks = []
    cur = []
    cur_len = 0
    for turn in turns:
        tlen = sum(len(m.content) for m in turn)
        if cur and cur_len + tlen > chunk_size:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.extend(turn)
        cur_len += tlen
    if cur:
        chunks.append(cur)
    return chunks

sizes = [5000, 6000, 7000, 8000]
results = {}

for size in sizes:
    chunks = chunk_by_size(turns, size)
    results[size] = {'success': 0, 'total': 0, 'mems': [], 'times': []}
    for rnd in range(2):
        total_mem = []
        t0 = time.time()
        ok = True
        for ci, chunk in enumerate(chunks):
            conv = fmt_turn(chunk)
            try:
                raw_mems, provider, meta = l1.extract_l1(
                    conv, cfg['dimensions'], cfg['llm'], client=None,
                    chunk_size=size, overlap=1200, bucket_ctx=None, mem_conn=None,
                )
                total_mem.extend(raw_mems)
            except Exception as e:
                ok = False
                print(f'  [chunk{ci}] FAILED: {type(e).__name__}: {str(e)[:60]}', flush=True)
        elapsed = time.time() - t0
        results[size]['total'] += 1
        if ok:
            results[size]['success'] += 1
        results[size]['mems'].append(len(total_mem))
        results[size]['times'].append(elapsed)
        print(f'[run{rnd+1}] chunk={size}: {len(chunks)}块 {"成功" if ok else "失败"} {len(total_mem)}记忆 总{elapsed:.0f}s', flush=True)

print('\n=== 汇总（每档 2 轮平均）===')
print(f'{"chunk":>6} {"成功率":>6} {"平均记忆":>8} {"总耗时":>8}')
for size in sizes:
    r = results[size]
    rate = r['success'] / r['total'] * 100
    avg_mem = sum(r['mems']) / len(r['mems'])
    avg_time = sum(r['times']) / len(r['times'])
    print(f'{size:>6} {rate:>5.0f}% {avg_mem:>8.1f} {avg_time:>7.0f}s')
