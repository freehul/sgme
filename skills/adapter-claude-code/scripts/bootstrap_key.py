"""Bootstrap: mint a dedicated SGME agent key for claude-code.

Reads SGME_ADMIN_KEY from the environment (never printed), registers the
agent_id "claude-code" via POST /v1/admin/agents/register, persists the
returned key to ~/.sgme/claude-agent.json and the Windows user environment
(SGME_CLAUDE_KEY), then runs a 4-point connectivity self-check.

Secrets never touch stdout: only redacted summaries are printed.
Re-running is safe: if the agent_id already exists, the script stops
without re-registering.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AGENT_ID = "claude-code"
AGENT_MODEL = "deepseek-flash[1m]"


def out(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def reconfigure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        rc = getattr(stream, "reconfigure", None)
        if callable(rc):
            rc(encoding="utf-8")


def make_opener() -> urllib.request.OpenerDirector:
    # trust_env=False 等价：内网流量绝不走系统代理
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(op, method: str, path: str, *, key: str | None, body: dict | None = None):
    base = os.environ.get("SGME_BASE_URL") or os.environ.get("SGME_HTTP_URL")
    if not base:
        raise SystemExit("FATAL: SGME_BASE_URL / SGME_HTTP_URL missing")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with op.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(detail)
        except Exception:
            payload = {"raw": detail[:300]}
        return exc.code, payload


def fingerprint(secret: str) -> str:
    return "sha1:" + hashlib.sha1(secret.encode()).hexdigest()[:10] + f" len={len(secret)}"


def main() -> int:
    reconfigure_utf8()
    base = os.environ.get("SGME_BASE_URL") or os.environ.get("SGME_HTTP_URL")
    admin = os.environ.get("SGME_ADMIN_KEY")
    if not base:
        raise SystemExit("FATAL: SGME_BASE_URL / SGME_HTTP_URL missing")
    if not admin:
        raise SystemExit("FATAL: SGME_ADMIN_KEY missing; cannot mint a key")

    op = make_opener()
    out(f"base={base}  target agent_id={AGENT_ID}")

    # 1) 列出已有 agents（只打印元数据，不含任何 key 字段）
    status, payload = call(op, "GET", "/v1/admin/agents", key=admin)
    if status == 200:
        agents = payload if isinstance(payload, list) else payload.get("agents", [])
        out(f"existing agents ({len(agents)}):")
        already = False
        for a in agents:
            if not isinstance(a, dict):
                continue
            info = {
                k: a.get(k)
                for k in ("agent_id", "role", "scope", "agent_model", "status", "created_at")
                if k in a
            }
            out("  - " + json.dumps(info, ensure_ascii=False))
            if a.get("agent_id") == AGENT_ID:
                already = True
        if already:
            out(f"NOTE: agent_id '{AGENT_ID}' already exists -> not re-registering. Stop.")
            return 3
    else:
        out(f"list agents -> HTTP {status}: {json.dumps(payload, ensure_ascii=False)[:200]}")
        out("(listing unavailable; continuing to register anyway)")

    # 2) 签发专属 key
    status, payload = call(
        op,
        "POST",
        "/v1/admin/agents/register",
        key=admin,
        body={"agent_id": AGENT_ID, "scope": [], "agent_model": AGENT_MODEL},
    )
    if status != 200:
        out(f"register -> HTTP {status}: {json.dumps(payload, ensure_ascii=False)[:300]}")
        return 4
    api_key = str(payload.get("api_key") or "")
    if not api_key.startswith("agt_"):
        out("register response carried no agt_ key; aborting before persisting anything")
        return 5
    out(
        "registered: "
        f"agent_id={payload.get('agent_id')} role={payload.get('role')} "
        f"key=({fingerprint(api_key)})"
    )

    # 3) 落盘：部署状态文件 + 用户环境变量（都只在本地，不进对话）
    state_path = Path.home() / ".sgme" / "claude-agent.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "agent_id": AGENT_ID,
        "base_url": base.rstrip("/"),
        "mcp_url": (os.environ.get("SGME_MCP_URL") or "").rstrip("/"),
        "api_key": api_key,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    out(f"saved deployment state -> {state_path}")
    try:
        subprocess.run(
            ["setx", "SGME_CLAUDE_KEY", api_key],
            check=True, capture_output=True, text=True, timeout=15,
        )
        out("saved user env var SGME_CLAUDE_KEY (effective for new processes)")
    except Exception as exc:  # noqa: BLE001
        out(f"WARN: setx failed ({exc}); state file still holds the key")

    # 4) 用新 key 跑联通自检
    status, payload = call(
        op, "POST", "/v1/search", key=api_key, body={"query": "接入自检", "limit": 3}
    )
    n = len(payload.get("results", [])) if isinstance(payload, dict) else "?"
    out(f"selfcheck/search(auth) -> HTTP {status} results={n}")

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    session_key = "selfcheck-claude-code-20261001"
    content = (
        f"# {now} user\n"
        "接通 SGME 的 claude-code agent 专属钥匙自检。\n\n"
        f"## {now} assistant\n"
        "claude-code 接入自检通过：专属 agt key 已签发，并保存到 SGME_CLAUDE_KEY "
        "与 ~/.sgme/claude-agent.json。"
    )
    status, payload = call(
        op,
        "POST",
        "/v1/append",
        key=api_key,
        body={
            "session_key": session_key,
            "started_at": now,
            "agent_id": AGENT_ID,
            "source_type": "session",
            "content": content,
        },
    )
    st = payload.get("status") if isinstance(payload, dict) else payload
    out(f"selfcheck/append -> HTTP {status} status={st}")

    status, payload = call(
        op, "POST", "/v1/search", key=api_key,
        body={"query": "claude-code 专属钥匙自检", "limit": 3},
    )
    hits = payload.get("results", []) if isinstance(payload, dict) else []
    top = hits[0].get("memory_id") if hits else None
    out(f"selfcheck/search(verify) -> HTTP {status} hits={len(hits)} top={top}")
    out("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
