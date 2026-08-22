# 查询 SGME 待办池中与 L2 场景/去重相关的待办（只打印标题与状态，不打印 key）
import os, re, json, urllib.request

# 从 config/.env 读 admin key（仅内存使用）
key = None
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', '.env')
if not os.path.exists(env_path):
    env_path = 'D:/Projects/SGME/config/.env'
with open(env_path, 'r', encoding='utf-8') as f:
    for line in f:
        m = re.match(r'^\s*SGME_ADMIN_KEY\s*=\s*(.+?)\s*$', line)
        if m:
            key = m.group(1).strip().strip('"').strip("'")
            break

if not key:
    print('NO_ADMIN_KEY')
    raise SystemExit(0)

url = 'http://192.168.10.10:9910/v1/admin/demands?limit=50'
req = urllib.request.Request(url, headers={'X-API-Key': key})
try:
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode('utf-8'))
except Exception as e:
    print('API_ERROR:', e)
    raise SystemExit(0)

# 打印全部待办（标题/状态/project_id），并标出相关关键词
items = data if isinstance(data, list) else data.get('items', data.get('demands', []))
print('待办总数:', len(items))
kw = ['场景', 'L2', '合并', '去重', '冲突', '预筛', 'scene']
for it in items:
    title = it.get('title', '')
    status = it.get('status', '')
    pid = it.get('project_id', '')
    hit = any(k in title for k in kw)
    flag = '  <<< 相关' if hit else ''
    print(f'- [{status}] {title} (project={pid}){flag}')
