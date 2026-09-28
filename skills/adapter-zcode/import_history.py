# -*- coding: utf-8 -*-
"""import_history.py — ZCode 历史会话补导入 SGME L0（幂等可重跑）。

数据源：~/.zcode/cli/db/db.sqlite（ZCode 主会话库，**只读** URI 打开，不碰 WAL）。
目标：SGME L0（POST /v1/append，session_key=zcode-<sess_id>，agent_id=zcode）。

抽取口径：
- 只取 user / assistant 消息的 **text part**（reasoning/tool/step-* 分片一律跳过——
  服务端剪枝同样丢弃 tool 输出，导入前就减容）；
- 空文本消息跳过；时间戳取 message.time.created（epoch ms → ISO UTC）。

幂等语义（与 dsh/import_history.py 同思路）：
- 每个会话导入前先查 `GET /v1/admin/sessions?session_key=zcode-<sess_id>`
  （服务端**子串匹配**，客户端必须对 items 的 session_key 精确判等）；
  已存在 → 整段跳过。历史导入是整段一次性动作，跳过即安全，不用消息数游标；
- started_at 用导出时刻（毫秒精度）——与引擎「同 session_key + 同 started_at 幂等
  丢弃」语义配合，宁可重导不可丢（重复窗口由服务端幂等兜底）。

用法：
  python adapters/zcode/import_history.py --dry-run          # 预览（本地只读、零依赖：无需 SGME 配置/连接）
  python adapters/zcode/import_history.py                    # 全量补导入（已导入自动跳过）
  python adapters/zcode/import_history.py --limit 5          # 只导最近 5 个会话
  python adapters/zcode/import_history.py --session-id sess_xxx
  python adapters/zcode/import_history.py --refine           # 导完后触发批量提炼（async）

纪律：
- ≥20 文件必须分批（每批 ≤20）+ 批间 30–60s；429 不立即重试（交服务端 batch_scan 兜底）
- 密钥只读环境变量（SGME_AGENT_KEY / SGME_ADMIN_KEY），不落盘不回显
- db.sqlite 只读；正文只写往 SGME，不在本地落任何中间文件
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
from sgme_client import (  # noqa: E402
    ADMIN_KEY_ENV,
    AGENT_ID,
    SGME,
    SGMEError,
    resolve_addresses,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

# 大小护栏：过小（无信息量）与过大（HTTP 体量风险）都会话跳过并提示
DEFAULT_MIN_CHARS = 200
DEFAULT_MAX_CHARS = 400_000


def load_repo_env() -> None:
    """仓库 config/.env 补加载（setdefault，显式 env 优先）；密钥只进进程环境。"""
    env_file = REPO_ROOT / "config" / ".env"
    if not env_file.is_file():
        return
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开 ZCode 会话库（mode=ro，绝不写、不碰 WAL 文件）。"""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def iter_sessions(conn: sqlite3.Connection, include_subagents: bool = False):
    """列出会话：默认只取主会话（subagent 的 parent_id 非空，正文多为工具过程）。"""
    sql = "SELECT id, title, time_created, time_updated FROM session"
    if not include_subagents:
        sql += " WHERE parent_id IS NULL"
    sql += " ORDER BY time_updated DESC"
    for row in conn.execute(sql):
        yield {"id": row["id"], "title": row["title"] or "",
               "time_updated": row["time_updated"]}


def _ms_to_iso(ms: int | None) -> str:
    if not ms:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """抽取 user/assistant 的 text part，按消息序还原成 [{role, ts, text}]。

    message.data JSON：role / time.created（epoch ms）；
    part.data JSON：type=="text" 才取 text 字段（reasoning/tool/step-* 跳过）。
    """
    out: list[dict] = []
    msg_rows = conn.execute(
        "SELECT id, data FROM message WHERE session_id=? ORDER BY sequence", (session_id,)
    ).fetchall()
    for m in msg_rows:
        try:
            mj = json.loads(m["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        role = str(mj.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        t = mj.get("time")
        ts_ms = t.get("created") if isinstance(t, dict) else None
        texts: list[str] = []
        for (pdata,) in conn.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY sequence", (m["id"],)
        ):
            try:
                pj = json.loads(pdata)
            except (json.JSONDecodeError, TypeError):
                continue
            if pj.get("type") != "text":
                continue
            txt = str(pj.get("text") or "").strip()
            if txt:
                texts.append(txt)
        if texts:
            out.append({"role": role, "ts_ms": ts_ms, "text": "\n".join(texts)})
    return out


def to_l0(messages: list[dict]) -> str:
    """[{role, ts_ms, text}] → L0 正文（`# {ISO} user` / `## {ISO} assistant` 块）。"""
    blocks = []
    for m in messages:
        head = "#" if m["role"] == "user" else "##"
        blocks.append(f"{head} {_ms_to_iso(m['ts_ms'])} {m['role']}\n{m['text']}")
    return "\n\n".join(blocks)


def _admin_get(http: str, path: str, admin_key: str) -> dict:
    req = urllib.request.Request(http + path, method="GET")
    req.add_header("X-API-Key", admin_key)
    req.add_header("Accept", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def session_exists(http: str, admin_key: str, session_key: str) -> bool:
    """服务端 session_key 为子串匹配（契约 §5.6.1）——必须对 items 精确判等。"""
    from urllib.parse import urlencode
    try:
        data = _admin_get(http, f"/v1/admin/sessions?{urlencode({'session_key': session_key, 'limit': 20})}",
                          admin_key)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️ 已有会话查询失败（{e}）——按「不存在」继续，重跑可兜底")
        return False
    items = data.get("items") or []
    return any(str(it.get("session_key") or "") == session_key for it in items)


def trigger_refine(http: str, admin_key: str, limit: int = 50) -> None:
    """导完后触发异步批量提炼（永远 async）。"""
    body = json.dumps({"limit": limit}).encode("utf-8")
    req = urllib.request.Request(http + "/v1/admin/refine/trigger_async", data=body, method="POST")
    req.add_header("X-API-Key", admin_key)
    req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        print(f"refine_trigger_async: HTTP {resp.status}（后台执行，进度看 refine-status）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZCode 历史会话补导入 SGME")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="ZCode 会话库路径")
    ap.add_argument("--base-url", default=None, help="SGME HTTP 地址（默认走客户端解析链）")
    ap.add_argument("--limit", type=int, default=None, help="最多导入 N 个会话（按最近更新排序）")
    ap.add_argument("--session-id", default=None, help="只导指定会话")
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS, help="小于该字符数的会话跳过")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="大于该字符数的会话跳过")
    ap.add_argument("--include-subagents", action="store_true", help="包含 subagent 会话（默认只导主会话）")
    ap.add_argument("--dry-run", action="store_true", help="预览模式：不写任何数据")
    ap.add_argument("--refine", action="store_true", help="导完后触发一次异步批量提炼")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"❌ ZCode 会话库不存在：{db_path}")
        return 2
    load_repo_env()

    # dry-run 零依赖（2026-09-28 修 CI 红：无密钥环境构造即抛 SGMEError；
    # 预览本不需要 SGME 连接——客户端与地址解析仅在真实导入时构建）
    client = None
    http = ""
    admin_key = ""
    if not args.dry_run:
        try:
            client = SGME(base_url=args.base_url)
        except SGMEError as e:
            print(f"❌ {e}")
            return 2
        http, _, _ = resolve_addresses(args.base_url)
        admin_key = os.environ.get(ADMIN_KEY_ENV, "")
        if not admin_key:
            print(f"⚠️ 未设置 {ADMIN_KEY_ENV}：无法查已有会话（幂等跳过失效）与触发提炼；"
                  "建议从仓库 config/.env 环境运行")

    conn = connect_ro(db_path)
    imported = skipped_existing = skipped_size = 0
    try:
        sessions = list(iter_sessions(conn, args.include_subagents))
        if args.session_id:
            sessions = [s for s in sessions if s["id"] == args.session_id]
            if not sessions:
                print(f"❌ 会话库中不存在 {args.session_id}")
                return 2
        print(f"ZCode 会话库：{len(sessions)} 个候选会话（db={db_path.name}）")

        for s in sessions:
            if args.limit is not None and imported >= args.limit:
                break  # --limit 语义 = 实际（或待）导入的会话数上限
            session_key = f"{AGENT_ID}-{s['id']}"
            msgs = session_messages(conn, s["id"])
            l0 = to_l0(msgs)
            n_chars = len(l0)
            label = f"{session_key}（{len(msgs)} 消息 / {n_chars} 字符｜{s['title'][:40]}）"
            if n_chars < args.min_chars:
                skipped_size += 1
                continue
            if n_chars > args.max_chars:
                print(f"  ⚠️ 超长跳过：{label}")
                skipped_size += 1
                continue
            if args.dry_run:
                print(f"  [dry-run] 待导入：{label}")
                imported += 1
                continue
            if session_exists(http, admin_key, session_key):
                skipped_existing += 1
                continue
            started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            assert client is not None  # dry-run 已在循环内提前 continue；此处置只为类型收窄
            try:
                resp = client._http("POST", "/v1/append", {
                    "session_key": session_key,
                    "started_at": started_at,
                    "content": l0,
                    "agent_id": AGENT_ID,
                    "source_type": "session",
                })
                status = str(resp.get("status") or "?")
                print(f"  ✅ 导入：{label} → file_id={resp.get('file_id')} status={status}")
                imported += 1
            except SGMEError as e:
                print(f"  ❌ 导入失败：{label} → {str(e)[:160]}")
            time.sleep(0.2)  # 轻节流，避免瞬时并发撞限流
    finally:
        conn.close()

    mode = "待导入" if args.dry_run else "已导入"
    print(f"\n完成：{mode} {imported} ｜ 已存在跳过 {skipped_existing} ｜ 大小过滤跳过 {skipped_size}")
    if args.dry_run:
        print("预览模式：未写任何数据。确认后去掉 --dry-run 执行真实导入。")
    elif imported >= 20:
        print("⚠️ 导入 ≥20 文件：批量提炼请分批（每批 ≤20）+ 批间 30–60s，或交服务端 batch_scan 兜底。")
    if args.refine and not args.dry_run:
        if not admin_key:
            print("⚠️ 无 admin key，跳过提炼触发；可手动 refine-trigger。")
        else:
            try:
                trigger_refine(http, admin_key)
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                print(f"⚠️ 提炼触发失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
