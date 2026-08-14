"""甜点区测试：同一剪枝后会话，多档 chunk_size 对比质量/失败率/耗时。"""
import sys, time, json
sys.path.insert(0, '.')
from sgme.config import load_config
cfg = load_config()
from sgme.engine import l1, prune
from sgme.raw import store
import httpx

def test_chunk(file_id: str, chunk_size: int, overlap: int, run: int) -> dict:
    """单档测试，返回统计。"""
    parsed = store.parse_file(file_id, 'session')
    messages = prune.prune_messages(parsed.messages)
    conversation = '\n'.join(
        f'[msg#{m.seq}] {m.timestamp} {m.role}:\n  {m.content}' for m in messages
    )
    chunks = l1.chunk_conversation(conversation, chunk_size, overlap)
    total_start = time.time()
    ok_chunks = 0
    fail_chunks = 0
    total_memories = 0
    chunk_times = []
    for ci, chunk in enumerate(chunks):
        prompt = l1.render_l1(chunk, cfg['dimensions'])
        cstart = time.time()
        try:
            resp = httpx.post('http://127.0.0.1:1014/v1/chat/completions', json={
                'model': 'qwythos-9b-v2-i1',
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 16384,
                'temperature': 0.3,
            }, timeout=240)
            content = resp.json()['choices'][0]['message'].get('content', '')
            try:
                parsed_json = json.loads(content)
                total_memories += len(parsed_json)
                ok_chunks += 1
            except Exception:
                fail_chunks += 1
        except Exception:
            fail_chunks += 1
        chunk_times.append(time.time() - cstart)
    total = time.time() - total_start
    return {
        'chunk_size': chunk_size,
        'chunks': len(chunks),
        'ok': ok_chunks,
        'fail': fail_chunks,
        'memories': total_memories,
        'total_s': round(total, 1),
        'avg_chunk_s': round(sum(chunk_times)/len(chunk_times), 1) if chunk_times else 0,
    }

# 测试参数：5 档 × 2 轮
file_id = '20260806_103007_b7304e'
sizes = [4000, 5000, 6000, 7000, 8000]
results = []
for size in sizes:
    for run in [1, 2]:
        r = test_chunk(file_id, size, size // 5, run)
        r['run'] = run
        results.append(r)
        print(f"[run{run}] chunk={size}: {r['ok']}/{r['chunks']} 成功, {r['memories']} 记忆, "
              f"总{r['total_s']}s 均{r['avg_chunk_s']}s/块", flush=True)

print("\n=== 汇总（每档取2轮平均）===")
from collections import defaultdict
agg = defaultdict(list)
for r in results:
    agg[r['chunk_size']].append(r)
print(f"{'chunk':>6} {'成功率':>8} {'平均记忆':>8} {'总耗时':>8} {'均块耗时':>8}")
for size in sizes:
    rs = agg[size]
    ok_rate = sum(r['ok'] for r in rs) / sum(r['chunks'] for r in rs)
    avg_mem = sum(r['memories'] for r in rs) / len(rs)
    avg_total = sum(r['total_s'] for r in rs) / len(rs)
    avg_chunk = sum(r['avg_chunk_s'] for r in rs) / len(rs)
    print(f"{size:>6} {ok_rate*100:>7.0f}% {avg_mem:>8.1f} {avg_total:>7.0f}s {avg_chunk:>7.0f}s")
