"""三会话联合提纯测试：长短会话混合，验证生产管线稳定性。

- 会话：cc30e6(211KB长)、26076e(26KB短)、b7304e(273KB长)
- 全流程：剪枝 → 回合分块(4500~5000) → qwen官方参数提炼
"""
import sys, time
sys.path.insert(0, '.')
from sgme.config import load_config
cfg = load_config()
from sgme.engine import l1, prune
from sgme.raw import store

FILE_IDS = ['20260806_101032_cc30e6', '20260806_110258_26076e', '20260806_103007_b7304e']

def fmt_msgs(msgs):
    lines = []
    for m in msgs:
        lines.append(f'[msg#{m.seq}] {m.timestamp} {m.role}:')
        if m.role == 'tool' and getattr(m, 'tool_name', None):
            lines.append(f'  (tool={m.tool_name})')
        lines.append(f'  {m.content}')
        lines.append('')
    return '\n'.join(lines)

grand_start = time.time()
for file_id in FILE_IDS:
    parsed = store.parse_file(file_id, 'session')
    raw_count = len(parsed.messages)
    raw_chars = sum(len(m.content) for m in parsed.messages)
    messages = prune.prune_messages(parsed.messages)
    pruned_chars = sum(len(m.content) for m in messages)

    chunks = l1.chunk_messages_by_turn(messages, chunk_size=5000, min_chunk=4500)
    convs = [fmt_msgs(c) for c in chunks]

    print(f'\n=== {file_id} ===', flush=True)
    print(f'剪枝: {raw_count}条/{raw_chars}字符 → {len(messages)}条/{pruned_chars}字符', flush=True)
    print(f'分块: {len(convs)} 块, 格式后 {[len(c) for c in convs]} 字符', flush=True)

    t0 = time.time()
    try:
        raw_mems, provider, meta = l1.extract_l1(
            convs, cfg['dimensions'], cfg['llm'], client=None,
            chunk_size=5000, overlap=1000, bucket_ctx=None, mem_conn=None,
        )
        elapsed = time.time() - t0
        print(f'提炼: 成功 {len(raw_mems)}记忆 耗时{elapsed:.0f}s provider={provider}', flush=True)
        for i, m in enumerate(raw_mems, 1):
            print(f'  [{i}] [{m["memory_type"]}] p{m["priority"]} {m["content"][:80]} | dims={m["dimensions"]}', flush=True)
    except Exception as e:
        elapsed = time.time() - t0
        print(f'提炼: 失败 {type(e).__name__}: {str(e)[:100]} 耗时{elapsed:.0f}s', flush=True)

print(f'\n=== 全部完成, 总耗时 {time.time()-grand_start:.0f}s ===', flush=True)
