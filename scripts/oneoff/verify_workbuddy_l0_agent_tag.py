# -*- coding: utf-8 -*-
"""验收脚本：核对 WorkBuddy 适配器写入的 L0 心跳是否以 agent_id=workbuddy 落库。

背景（B194）：WorkBuddy 适配器刻意把 `SGME_AGENT_KEY` 降为密钥**兜底**——本机该
环境变量实测绑定 `agent_id=dsh`，若照搬其它适配器的「环境变量优先」顺序，WorkBuddy
写入的 L0 会被打上 dsh 的 `agent_tag`，污染多 Agent 溯源与隔离（T-140）。

本脚本是这条设计的**端到端证据**：跑完 `selfcheck.py`（会 append 一条
`sgme-selfcheck` 心跳）后执行，确认落库的 agent_id 是 `workbuddy`，不是 `dsh`/`default`。

只读；地址从 `~/.sgme/install.json` 读、admin key 从项目 `.env` 读；
**只打印字段状态，绝不打印密钥原文**。

用法：
  .venv/Scripts/python.exe scripts/oneoff/verify_workbuddy_l0_agent_tag.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
EXPECT_AGENT = "workbuddy"
SESSION_KEY = f"{EXPECT_AGENT}-selfcheck"


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


def main() -> int:
    env = load_env(ENV_FILE)
    admin = env.get("SGME_ADMIN_KEY") or os.environ.get("SGME_ADMIN_KEY") or ""
    if not admin:
        print("❌ 缺少 admin key（项目 .env 的 SGME_ADMIN_KEY）")
        return 2

    url = base_url() + "/v1/admin/sessions?" + urllib.parse.urlencode(
        {"session_key": SESSION_KEY})
    print(f"端点: {url}")
    print(f"admin key: 已取到（不回显）\n")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"X-API-Key": admin})
    try:
        with opener.open(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — 验收脚本需如实报出任何失败
        print(f"❌ 请求失败: {type(e).__name__}: {e}")
        return 2

    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        keys = sorted(data) if isinstance(data, dict) else type(data).__name__
        print(f"⚠️ 响应无 items 字段（顶层键: {keys}）")
        print(json.dumps(data, ensure_ascii=False)[:400])
        return 2

    print(f"匹配 {SESSION_KEY} 的会话 {len(items)} 条：")
    hit = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sk = it.get("session_key") or "?"
        aid = it.get("agent_id")
        st = it.get("status") or "?"
        hit.append(aid)
        print(f"  session_key={sk}  agent_id={aid!r}  status={st}  "
              f"file_id={str(it.get('file_id') or it.get('id') or '?')[:12]}")

    print()
    if not hit:
        print("❌ 未找到心跳会话（先跑 selfcheck.py 写入）")
        return 1
    if EXPECT_AGENT in hit:
        print(f"✅ L0 打标正确：agent_id={EXPECT_AGENT}（降级设计生效，未误用 dsh 的 key）")
        return 0
    print(f"❌ 打标异常：期望 {EXPECT_AGENT}，实际 {sorted(set(map(str, hit)))}")
    print("   → 检查 env-info 的「agent key 来源」是否走了 SGME_AGENT_KEY 兜底")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
