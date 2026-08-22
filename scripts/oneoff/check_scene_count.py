# 触发提炼验证场景预警状态，只打印摘要
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

# 先查 health 看场景数（refinement 无场景数，走 stats）
req = urllib.request.Request(
    'http://192.168.10.10:9910/v1/admin/stats',
    headers={'X-API-Key': key},
)
try:
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        # 找场景数相关字段
        print(json.dumps(data, ensure_ascii=False)[:600])
except Exception as e:
    print('API_ERROR:', e)
