"""ST-34 真实冒烟：health 新字段 + 更新意图端点。"""
import os, sys, tempfile, json
sys.path.insert(0, os.getcwd())

# 隔离环境：临时 SGME_HOME
tmp = tempfile.mkdtemp(prefix="sgme-smoke-")
os.environ["SGME_HOME"] = tmp
os.environ["SGME_ADMIN_KEY"] = "smoke-admin-key"
os.environ["SGME_AGENT_KEY"] = "smoke-agent-key"

from fastapi.testclient import TestClient
from sgme.server.app import create_app

app = create_app()
client = TestClient(app)

# 1. health 返回新字段
r = client.get("/v1/health")
assert r.status_code == 200, r.text
body = r.json()
print("health keys:", sorted(body.keys()))
assert "update_available" in body
assert "latest_version" in body
assert "update_checked_at" in body
assert "update_error" in body
print("update_available:", body["update_available"])
print("latest_version:", body["latest_version"])

# 2. POST 意图文件（admin key）
r = client.post(
    "/v1/admin/update/request",
    headers={"X-API-Key": "smoke-admin-key"},
    json={"target_version": "v1.0.0b5"},
)
print("POST update/request:", r.status_code, r.json())
assert r.status_code == 200, r.text

# 3. GET 意图文件
r = client.get("/v1/admin/update/request", headers={"X-API-Key": "smoke-admin-key"})
print("GET update/request:", r.status_code, r.json())
assert r.status_code == 200
assert r.json()["request"]["target_version"] == "v1.0.0b5"
assert r.json()["request"]["status"] == "pending"

# 4. 文件实际落盘
req_path = os.path.join(tmp, "update", "request.json")
assert os.path.exists(req_path), f"意图文件未落盘: {req_path}"
print("意图文件落盘:", req_path)
print(json.load(open(req_path, encoding="utf-8")))

print("\n=== ST-34 冒烟全部通过 ===")
