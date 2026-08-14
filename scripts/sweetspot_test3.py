"""甜点区测试（qwen 版）：5K/6K/7K/8K 四档 × 3 轮。
用法：python scripts/sweetspot_test3.py [model]
"""
import sys, time
sys.path.insert(0, '.')
from sgme.config import load_config
cfg = load_config()
from sgme.engine import l1, prune
from sgme.raw import store

model = sys.argv[1] if len(sys.argv) > 1 else 'qwen/qwen3.5-9b'
cfg['llm']['chains']['refinement'][0]['model'] = model
print(f'测试模型: {model}', flush=True)

file_id = '20260806_103007_b7304e'
parsed = store.parse_file(file_id, 'session')
messages = prune.prune_messages(parsed.messages)
print(f'剪枝后: {len(messages)} 条, {sum(len(m.content) for m in messages)} 字符', flush=True)

turns = []
current = []
for m in messages:
    if m.role == 'user' and current:
        turns.append(current)
        current = []
    current.append(m)
if current:
    turns.append(current)

def fmt_msgs(msgs):
    lines = []
    for m in msgs:
        lines.append(f'[msg#{m.seq}] {m.timestamp} {m.role}:')
        if m.role == 'tool' and getattr(m, 'tool_name', None):
            lines.append(f'  (tool={m.tool_name})')
        lines.append(f'  {m.content}')
        lines.append('')
    return '\n'.join(lines)

def chunk_by_turn(turns, chunk_size):
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
    chunks = chunk_by_turn(turns, size)
    convs = [fmt_msgs(c) for c in chunks]
    results[size] = {'success': 0, 'total': 0, 'mems': [], 'times': [], 'chunk_sizes': [len(c) for c in convs]}
    print(f'\n=== chunk_size={size}: {len(chunks)} 块, 各块 {[len(c) for c in convs]} 字符 ===', flush=True)
    for rnd in range(3):
        t0 = time.time()
        try:
            raw_mems, provider, meta = l1.extract_l1(
                convs, cfg['dimensions'], cfg['llm'], client=None,
                chunk_size=size, overlap=1600, bucket_ctx=None, mem_conn=None,
            )
            elapsed = time.time() - t0
            results[size]['success'] += 1
            results[size]['mems'].append(len(raw_mems))
            results[size]['times'].append(elapsed)
            print(f'[run{rnd+1}] 成功 {len(raw_mems)}记忆 总{elapsed:.0f}s', flush=True)
        except Exception as e:
            elapsed = time.time() - t0
            results[size]['mems'].append(0)
            results[size]['times'].append(elapsed)
            print(f'[run{rnd+1}] 失败: {type(e).__name__}: {str(e)[:60]} 总{elapsed:.0f}s', flush=True)
        results[size]['total'] += 1

print(f'\n=== 汇总（{model}，每档 3 轮）===')
print(f'{"chunk":>6} {"成功率":>6} {"平均记忆":>8} {"总耗时":>8} {"块大小":>14}')
for size in sizes:
    r = results[size]
    rate = r['success'] / r['total'] * 100
    avg_mem = sum(r['mems']) / len(r['mems'])
    avg_time = sum(r['times']) / len(r['times'])
    print(f'{size:>6} {rate:>5.0f}% {avg_mem:>8.1f} {avg_time:>7.0f}s {str(r["chunk_sizes"]):>14}')
