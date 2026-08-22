# 待办池登记：L2 场景聚合向量预筛（T-97）
import os, re, json, urllib.request

key = None
with open('D:/Projects/SGME/config/.env', 'r', encoding='utf-8') as f:
    for line in f:
        m = re.match(r'^\s*SGME_ADMIN_KEY\s*=\s*(.+?)\s*$', line)
        if m:
            key = m.group(1).strip().strip('"').strip("'")
            break

if not key:
    print('NO_ADMIN_KEY')
    raise SystemExit(0)

body = json.dumps({
    "title": "T-97 L2 场景聚合向量预筛 + 场景超限治理（active 276 > max 200 红警）",
    "project_id": "sgme",
    "content": "L2 只喂 50 个场景摘要给 LLM，merge 收敛跟不上 create，场景超限触发红警；对齐 T-25 l15.prescreen 给 L2 加场景级向量预筛（Top-K 召回相似场景再裁决）",
}).encode('utf-8')

req = urllib.request.Request(
    'http://192.168.10.10:9910/v1/admin/demands',
    data=body,
    headers={'X-API-Key': key, 'Content-Type': 'application/json'},
    method='POST',
)
try:
    with urllib.request.urlopen(req, timeout=8) as resp:
        print('HTTP', resp.status)
        print(resp.read().decode('utf-8')[:500])
except Exception as e:
    print('API_ERROR:', e)
