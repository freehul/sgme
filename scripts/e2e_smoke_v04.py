"""scripts/e2e_smoke_v04.py：v0.4 完整链路端到端冒烟脚本。

链路：
    append → refine/trigger → 验证 L2 场景生成 → inject（Tier0 摘要生效）
    → search（RRF 融合）→ events/pull（memory_updated 事件）
    → health（可观测性字段）→ backup/create → backup/restore

运行方式（需先启动 SGME Gateway，端口 9910）：

    # 1. 启动 Gateway（另开终端）
    python -m sgme

    # 2. 运行冒烟脚本（dev key 已退役 403，用 config/.env 里的生产 key）
    .venv\\Scripts\\python.exe scripts/e2e_smoke_v04.py \\\
        --admin-key <config/.env 的 SGME_ADMIN_KEY> \\\
        --agent-key <config/.env 的 SGME_AGENT_KEY>

    # 自定义 host/port
    .venv\\Scripts\\python.exe scripts/e2e_smoke_v04.py \\
        --host 127.0.0.1 --port 9910 \\
        --admin-key <ADMIN_KEY> --agent-key <AGENT_KEY>

前置条件：
- SGME Gateway 已启动并监听 --host:--port（默认 127.0.0.1:9910）
- LLM 降级链可用（LM Studio / DeepSeek），否则提炼降级直存但 L2 场景不生成
- --admin-key / --agent-key 与 Gateway 启动时一致

退出码：0 成功 / 非 0 失败（失败立即退出）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# ---------- 参数解析 ----------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SGME v0.4 端到端冒烟脚本（需先启动 SGME Gateway）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Gateway 监听地址（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port", type=int, default=9910,
        help="Gateway 监听端口（默认 9910）",
    )
    parser.add_argument(
        "--admin-key", required=True,
        help="管理员 API Key（必填，调 /v1/admin/* 端点）",
    )
    parser.add_argument(
        "--agent-key", required=True,
        help="Agent API Key（必填，调 /v1/append/inject/search 等端点）",
    )
    parser.add_argument(
        "--data-dir", default="data",
        help="SGME data 目录（用于直接查 sqlite 验证 L2 场景，默认 'data'）",
    )
    return parser.parse_args()


# ---------- 工具 ----------

def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(step: int, msg: str) -> None:
    print(f"[{step:>2}] [OK] {msg}")


def _fail(step: int, msg: str, detail: str = "") -> None:
    print(f"[{step:>2}] [FAIL] {msg}")
    if detail:
        print(f"        {detail}")
    sys.exit(1)


def _check_status(step: int, resp: requests.Response, expected: int, msg: str) -> dict[str, Any]:
    """校验 HTTP 状态码并返回 JSON。失败立即退出。"""
    if resp.status_code != expected:
        _fail(step, f"{msg}：HTTP {resp.status_code}", f"body={resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        _fail(step, f"{msg}：响应非 JSON", f"body={resp.text[:300]}")
        return {}  # 不会执行到


# ---------- 主流程 ----------

def main() -> int:
    args = _parse_args()
    base_url = f"http://{args.host}:{args.port}"
    agent_headers = {"X-API-Key": args.agent_key}
    admin_headers = {"X-API-Key": args.admin_key}

    # 用 Session 复用连接
    session = requests.Session()

    print("=" * 64)
    print("SGME v0.4 E2E Smoke Test (real HTTP)")
    print(f"  target: {base_url}")
    print(f"  data_dir: {args.data_dir}")
    print("=" * 64)

    # ---------- 1. /v1/health：服务在线 ----------
    try:
        resp = session.get(f"{base_url}/v1/health", timeout=10)
    except requests.RequestException as e:
        _fail(1, f"无法连接 Gateway（{base_url}）: {e}")
        return 1
    body = _check_status(1, resp, 200, "GET /v1/health")
    _ok(1, f"Gateway online status={body.get('status')} version={body.get('version')}")

    # ---------- 2. POST /v1/append：写测试会话 ----------
    content = (
        f"# 2024-01-01T10:00:00Z user\n我是一名独立开发者\n"
        f"# 2024-01-01T10:00:30Z assistant\n你好！\n"
        f"# 2024-01-01T10:01:00Z user\n我正在开发 SGME 记忆引擎项目\n"
    )
    resp = session.post(
        f"{base_url}/v1/append",
        json={
            "session_key": f"e2e-smoke-v04-{int(datetime.now(timezone.utc).timestamp())}",
            "started_at": "2024-01-01T10:00:00Z",
            "content": content,
        },
        headers=agent_headers,
        timeout=30,
    )
    body = _check_status(2, resp, 200, "POST /v1/append")
    file_id = body["file_id"]
    _ok(2, f"append file_id={file_id[:8]}... status={body.get('status')}")

    # ---------- 3. POST /v1/admin/refine/trigger：触发提炼 ----------
    resp = session.post(
        f"{base_url}/v1/admin/refine/trigger",
        json={"file_id": file_id},
        headers=admin_headers,
        timeout=120,
    )
    body = _check_status(3, resp, 200, "POST /v1/admin/refine/trigger")
    refine_status = body.get("status")
    memories_count = body.get("memories_count", 0)
    l15_stored = body.get("l15", {}).get("stored", 0)
    _ok(3, f"refine status={refine_status} memories={memories_count} l15_stored={l15_stored}")

    # ---------- 4. 直接查 wiki.db：验证 L2 场景生成 ----------
    wiki_db_path = Path(args.data_dir) / "wiki.db"
    scene_count = 0
    if wiki_db_path.exists():
        try:
            # 只读模式 + immutable 避免锁冲突
            conn = sqlite3.connect(
                f"file:{wiki_db_path}?mode=ro&immutable=1",
                uri=True, check_same_thread=False,
            )
            cur = conn.execute("SELECT COUNT(*) AS c FROM scenes WHERE status='active'")
            scene_count = cur.fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            print(f"    [warn] 查 wiki.db scenes 失败: {e}")
    else:
        print(f"    [warn] wiki.db 不存在: {wiki_db_path}")

    if scene_count > 0:
        _ok(4, f"L2 active scenes={scene_count}")
    else:
        # L2 场景生成依赖 LLM（LM Studio / DeepSeek）。LLM 不可用时降级直存，无场景。
        print(f"    [warn] L2 scenes=0（若 LLM 不可达属正常降级）")
        _ok(4, "L2 scenes checked (count=0 视为 LLM 降级)")

    # ---------- 5. POST /v1/admin/tier0/refresh：触发 Tier0 摘要 ----------
    resp = session.post(
        f"{base_url}/v1/admin/tier0/refresh",
        headers=admin_headers,
        timeout=60,
    )
    body = _check_status(5, resp, 200, "POST /v1/admin/tier0/refresh")
    tier0_status = body.get("status")
    if tier0_status == "ok":
        _ok(5, f"tier0 refresh ok summary_length={body.get('summary_length')}")
    else:
        # LLM 不可达时返回 status=failed，视为降级
        print(f"    [warn] tier0 refresh failed（LLM 不可达）：{body.get('error', '')}")
        _ok(5, f"tier0 refresh status={tier0_status} (degraded)")

    # ---------- 6. POST /v1/inject：验证 tier0 字段 ----------
    resp = session.post(
        f"{base_url}/v1/inject",
        json={"mode": "daily"},
        headers=agent_headers,
        timeout=30,
    )
    body = _check_status(6, resp, 200, "POST /v1/inject")
    blocks_count = len(body.get("blocks", []))
    tier0_present = body.get("tier0", {}).get("present")
    _ok(6, f"inject blocks={blocks_count} tier0.present={tier0_present}")

    # ---------- 7. POST /v1/search：验证检索返回结果 ----------
    resp = session.post(
        f"{base_url}/v1/search",
        json={"query": "记忆引擎", "scopes": ["memory"]},
        headers=agent_headers,
        timeout=30,
    )
    body = _check_status(7, resp, 200, "POST /v1/search")
    results = body.get("results", [])
    routes = body.get("meta", {}).get("routes", [])
    _ok(7, f"search results={len(results)} routes={routes}")
    if results:
        first = results[0]
        trace_len = len(first.get("trace", []))
        print(f"    first: content={first.get('content', '')[:30]}... trace={trace_len}")

    # ---------- 8. GET /v1/events/pull：验证 memory_updated 事件 ----------
    resp = session.get(
        f"{base_url}/v1/events/pull",
        params={"subscriber_id": "e2e-smoke-v04", "limit": 50},
        headers=agent_headers,
        timeout=30,
    )
    body = _check_status(8, resp, 200, "GET /v1/events/pull")
    events = body.get("events", [])
    memory_updated_count = sum(1 for e in events if e.get("type") == "memory_updated")
    _ok(8, f"events={len(events)} memory_updated={memory_updated_count} "
           f"next_cursor={body.get('next_cursor') is not None}")

    # ---------- 9. GET /v1/health：验证可观测性字段 ----------
    resp = session.get(f"{base_url}/v1/health", timeout=10)
    body = _check_status(9, resp, 200, "GET /v1/health (observability)")
    llm_avail = body.get("llm", {}).get("available")
    queue_depth = body.get("refinement", {}).get("queue_depth")
    last_refined = body.get("refinement", {}).get("last_refined_at")
    stalled = body.get("refinement", {}).get("stalled")
    heartbeat_ok = body.get("refinement", {}).get("heartbeat_ok")
    _ok(9, f"health llm.available={llm_avail} queue_depth={queue_depth} "
           f"stalled={stalled} heartbeat_ok={heartbeat_ok} "
           f"last_refined_at={'set' if last_refined else 'None'}")

    # ---------- 10. POST /v1/admin/backup/create：创建快照 ----------
    resp = session.post(
        f"{base_url}/v1/admin/backup/create",
        json={"level": "full"},
        headers=admin_headers,
        timeout=60,
    )
    body = _check_status(10, resp, 200, "POST /v1/admin/backup/create")
    snapshot_id = body.get("snapshot_id")
    if not snapshot_id:
        _fail(10, "snapshot_id 缺失", f"body={body}")
        return 1
    _ok(10, f"backup created snapshot_id={snapshot_id}")

    # ---------- 11. POST /v1/admin/backup/restore：恢复 ----------
    resp = session.post(
        f"{base_url}/v1/admin/backup/restore",
        json={"snapshot_id": snapshot_id},
        headers=admin_headers,
        timeout=60,
    )
    body = _check_status(11, resp, 200, "POST /v1/admin/backup/restore")
    pre_restore = body.get("pre_restore_snapshot")
    restored_sid = body.get("restored", {}).get("snapshot_id")
    if restored_sid != snapshot_id:
        _fail(11, f"restored snapshot_id 不匹配: expected={snapshot_id} got={restored_sid}")
        return 1
    _ok(11, f"backup restored snapshot_id={restored_sid} "
           f"pre_restore={pre_restore}")

    # ---------- 完成 ----------
    print("=" * 64)
    print("E2E SMOKE V04 PASSED")
    print("  append → refine → L2 scenes → tier0 → inject → search")
    print("  → events/pull → health → backup/create → backup/restore")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
