"""T-86 冒烟：bridge 新增 9 端点对 NAS 生产真实链路验证。

验证策略：读端点直测；写端点用「故意触发校验错误」的请求体——
验证路由通 + 鉴权通 + 进 operations 层，但不在生产库落数据。

用法：.venv/Scripts/python.exe scripts/oneoff/verify_t86_endpoints.py
"""
import os
import sys

import httpx

BASE = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910")


def main() -> int:
    # 环境变量缺省时从 adapters/dsh/.env 兜底读（本机安装态）
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", "adapters", "dsh", ".env")
    env_vals = {}
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            if line.startswith("SGME_AGENT_KEY="):
                env_vals["agent"] = line.split("=", 1)[1].strip()
            elif line.startswith("SGME_ADMIN_KEY="):
                env_vals["admin"] = line.split("=", 1)[1].strip()

    agent_key = os.environ.get("SGME_AGENT_KEY", "") or env_vals.get("agent", "")
    admin_key = os.environ.get("SGME_ADMIN_KEY", "") or env_vals.get("admin", "")
    if not agent_key or not admin_key:
        print("缺少 SGME_AGENT_KEY / SGME_ADMIN_KEY")
        return 1

    ag = {"X-API-Key": agent_key}
    adm = {"X-API-Key": admin_key}
    ok = fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    with httpx.Client(timeout=10, trust_env=False) as c:
        # ---- 读端点（Agent Key） ----
        r = c.get(f"{BASE}/v1/admin/roles", headers=ag)
        roles = []
        if r.status_code == 200:
            roles = r.json().get("roles", [])
        check("GET /v1/admin/roles（角色列表）", r.status_code == 200 and len(roles) > 0,
              f"HTTP {r.status_code}: {r.text[:120]}")

        r = c.get(f"{BASE}/v1/admin/care/active-role", headers=ag)
        check("GET /v1/admin/care/active-role（当前角色）", r.status_code == 200 and "role_id" in r.json(),
              f"HTTP {r.status_code}: {r.text[:120]}")

        if roles:
            rid = roles[0].get("role_id")
            r = c.get(f"{BASE}/v1/admin/roles/{rid}/assemble", headers=ag)
            body = r.json() if r.status_code == 200 else {}
            check(f"GET /v1/admin/roles/{rid}/assemble（装配）",
                  r.status_code == 200 and "system_prompt" in body,
                  f"HTTP {r.status_code}: {r.text[:120]}")

        r = c.get(f"{BASE}/v1/memory/nonexistent-t86", headers=ag)
        check("GET /v1/memory/{id}（路由+鉴权，404 为预期）", r.status_code == 404,
              f"HTTP {r.status_code}: {r.text[:120]}")

        r = c.post(f"{BASE}/v1/memory/nonexistent-t86/reject", headers=ag, json={"reason": "smoke"})
        check("POST /v1/memory/{id}/reject（路由+鉴权，404 为预期）", r.status_code == 404,
              f"HTTP {r.status_code}: {r.text[:120]}")

        r = c.put(f"{BASE}/v1/admin/care/active-role", headers=ag, json={"role_id": "nonexistent-t86"})
        check("PUT /v1/admin/care/active-role（路由+鉴权，404 为预期）", r.status_code == 404,
              f"HTTP {r.status_code}: {r.text[:120]}")

        # ---- 写端点（Admin Key）：故意触发校验错误，不落库 ----
        r = c.post(f"{BASE}/v1/admin/ideas", headers=adm, json={"content": ""})
        check("POST /v1/admin/ideas（空 content → 400 为预期）", r.status_code == 400,
              f"HTTP {r.status_code}: {r.text[:120]}")

        r = c.post(f"{BASE}/v1/admin/demands", headers=adm, json={"title": ""})
        check("POST /v1/admin/demands（空 title → 400 为预期）", r.status_code == 400,
              f"HTTP {r.status_code}: {r.text[:120]}")

        r = c.post(f"{BASE}/v1/admin/projects", headers=adm, json={"project_id": "smoke-t86"})
        check("POST /v1/admin/projects（新建缺 path → 400 为预期）", r.status_code == 400,
              f"HTTP {r.status_code}: {r.text[:120]}")

    print(f"\n结果：{ok} passed / {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
