# -*- coding: utf-8 -*-
"""对账：本地 WorkBuddy 活跃会话 vs SGME L0 已入库会话，断言缺口为 0。

只读本地 ~/.workbuddy/workbuddy.db；只向 SGME 发 GET；不改任何数据。
地址从 ~/.sgme/install.json 读（兜底 127.0.0.1:9910），
admin key 从环境变量 SGME_ADMIN_KEY 读（兜底项目 .env），全程不回显密钥。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import urllib.parse
import urllib.request

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DB_PATH = pathlib.Path.home() / ".workbuddy" / "workbuddy.db"


def load_env(path: pathlib.Path) -> dict:
    out: dict = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, val = line.partition("=")
            out[name.strip()] = val.strip().strip('"').strip("'")
    return out


def base_url() -> str:
    p = pathlib.Path.home() / ".sgme" / "install.json"
    try:
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("base_url"):
                return str(d["base_url"]).rstrip("/")
    except (OSError, json.JSONDecodeError):
        pass
    return "http://127.0.0.1:9910"


def fetch_all_l0(base: str, key: str) -> tuple[set[str], int]:
    """分页拉全量 agent_id=workbuddy 会话，返回 session_key 前 8 位集合与总条数。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    keys: set[str] = set()
    page = 1
    total = None
    while True:
        q = urllib.parse.urlencode(
            {"agent_id": "workbuddy", "page": page, "limit": 200})
        req = urllib.request.Request(base + "/v1/admin/sessions?" + q,
                                     headers={"X-API-Key": key})
        try:
            with opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"❌ 第 {page} 页请求失败: {type(e).__name__}: {e}")
            sys.exit(2)
        items = data.get("items") if isinstance(data, dict) else None
        if items is None:
            print(f"❌ 响应无 items 字段（顶层键: {sorted(data) if isinstance(data, dict) else type(data).__name__}）")
            sys.exit(2)
        if total is None:
            total = int(data.get("total", 0))
        for it in items:
            if not isinstance(it, dict):
                continue
            sk = str(it.get("session_key") or "")
            if sk.startswith("workbuddy-"):
                keys.add(sk[len("workbuddy-"):][:8])
        got = len(items)
        if page * 200 >= (total or 0) or got == 0:
            break
        page += 1
    return keys, int(total or 0)


def local_active() -> set[str]:
    if not DB_PATH.is_file():
        print(f"❌ 未找到本地会话库: {DB_PATH}")
        sys.exit(2)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE deleted_at IS NULL").fetchall()
    finally:
        conn.close()
    return {str(r[0])[:8] for r in rows}


def main() -> int:
    env = load_env(ENV_FILE)
    key = os.environ.get("SGME_ADMIN_KEY") or env.get("SGME_ADMIN_KEY") or ""
    if not key:
        print("❌ 缺少 SGME_ADMIN_KEY（环境变量或项目 .env）")
        return 2
    base = base_url()
    print(f"SGME 端点: {base}  |  admin key: 已取到（不回显）")

    l0_keys, l0_total = fetch_all_l0(base, key)
    local_keys = local_active()
    gap = sorted(local_keys - l0_keys)

    print(f"\nSGME L0 (agent_id=workbuddy): total={l0_total}, 收集到前缀={len(l0_keys)}")
    print(f"本地活跃会话 (deleted_at IS NULL): {len(local_keys)}")
    print(f"对账缺口 (本地活跃但 L0 缺失): {len(gap)}")
    if gap:
        for g in gap:
            print(f"  ❌ 缺失 workbuddy-{g}")
        return 1
    print("✅ 对账通过：本地活跃会话全部已入 L0，缺口为 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
