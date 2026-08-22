# 触发 SGME 提炼（验证 L2 场景预筛真实链路），只打印响应摘要，不打印 key
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

body = json.dumps({"async_mode": True}).encode('utf-8')
req = urllib.request.Request(
    'http://192.168.10.10:9910/v1/admin/refine/trigger',
    data=body,
    headers={'X-API-Key': key, 'Content-Type': 'application/json'},
    method='POST',
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print('HTTP', resp.status)
        print(resp.read().decode('utf-8')[:800])
except Exception as e:
    print('API_ERROR:', e)
