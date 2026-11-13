# -*- coding: utf-8 -*-
"""WorkBuddy 会话自动落盘到 SGME（T2，2026-11-13）。

背景：WorkBuddy 适配器此前只有 SKILL.md 的「每轮 append」纪律，没有自动同步；
11-07~11-13 本机 35 场会话只落 2 条手写摘要。本脚本把 WorkBuddy 自己的会话库
（``~/.workbuddy/workbuddy.db`` 的 ``sessions`` 表 + ``~/.workbuddy/projects/
<编码后 cwd>/<sessionId>.jsonl`` 的消息流）增量同步成 SGME 的 L0 原件。

口径（用户 2026-11-13 拍板 D1 / D2）：

- **全量入库但打标**：``is_playground`` / ``is_background_automation`` 会话照常
  落 L0，metadata 写 ``origin`` / ``playground`` / ``background_automation``；
  「巡检类不进记忆池」由服务端提炼侧按 metadata 过滤（不在入库侧丢弃）。
- **已删会话不导入**：``deleted_at`` 非空跳过。
- **游标**：本地状态文件按 ``session_id`` 记 ``updated_at`` + 已落盘 ``file_id``
  + 已同步消息数（sessions 表没有消息数列，只能以 jsonl 行为准）。``updated_at``
  未变直接跳过；变了只发新增消息（同 ``session_key`` 追加语义，不重发旧内容）。
- **幂等**：首条 append 的 ``started_at`` 取该批首条消息时间（确定性），
  崩溃重跑被服务端按 ``session_key + started_at`` 幂等丢弃。

窗口（2026-11-13 追加，防裸跑灌全量）：

- 默认只同步 `updated_at` 在**最近 7 天**内的会话（全量历史 700MB+，裸跑会灌爆 L0
  并触发海量提炼）；
- `--days N` 改窗口天数（`0` = 不限）；
- `--all` 显式关闭窗口（等价 `--days 0`）；
- `--since YYYY-MM-DD` 显式指定下界，优先于 `--days` / `--all`。

用法::

    python scripts/sync_sessions.py              # 增量同步（默认近 7 天）
    python scripts/sync_sessions.py --all        # 全量历史（谨慎）
    python scripts/sync_sessions.py --days 30 --dry-run
    python scripts/sync_sessions.py --json       # 机器可读报告

退出码：0 = 跑完（个别会话失败也记在报告里）；2 = 参数/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sgme_client import SGME, SGMEError  # noqa: E402  同目录客户端

#: 单条消息/工具回执截断（防 LLM 输入膨胀；与 T1 补录口径一致）
MSG_CAP = 8000
RESULT_CAP = 1500
ARGS_CAP = 800
#: WorkBuddy 会话时间以本机时区（+08:00）落 L0，与 ``since=YYYY-MM-DD`` 字符串过滤对齐
TZ = timezone(timedelta(hours=8))
STATE_VERSION = 1
#: 默认同步窗口（天）：只处理 updated_at 落在窗口内的会话，防裸跑灌全量历史
DEFAULT_WINDOW_DAYS = 7


def home_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def default_db_path() -> Path:
    return home_dir() / ".workbuddy" / "workbuddy.db"


def default_projects_dir() -> Path:
    return home_dir() / ".workbuddy" / "projects"


def default_state_path() -> Path:
    return home_dir() / ".workbuddy" / "sgme-sync-state.json"


# ---------------------------------------------------------------- 状态文件

def load_state(path: Path) -> dict:
    """读游标文件；损坏/缺失都按空状态处理（不阻断同步）。"""
    if not path.is_file():
        return {"version": STATE_VERSION, "sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "sessions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"version": STATE_VERSION, "sessions": {}}
    data.setdefault("version", STATE_VERSION)
    return data


def save_state(path: Path, state: dict) -> None:
    """原子写（tmp + replace），防中断留半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 会话发现

def discover_sessions(db_path: Path, since_ms: int | None = None) -> list[dict]:
    """读 WorkBuddy ``sessions`` 表。``since_ms`` 按 ``updated_at`` 过滤（含已删，留给调用方报原因）。"""
    if not db_path.is_file():
        raise SystemExit(f"未找到 WorkBuddy 会话库：{db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id, cwd, title, status, created_at, updated_at, deleted_at, "
            "is_playground, is_background_automation FROM sessions"
        )
        rows = [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()
    if since_ms:
        rows = [r for r in rows if _as_int(r.get("updated_at")) >= since_ms]
    rows.sort(key=lambda r: _as_int(r.get("updated_at")), reverse=True)
    return rows


def _as_int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def find_jsonl(projects_dir: Path, session_id: str, cwd: str | None = None) -> Path | None:
    """定位会话消息文件：先按 cwd 编码直查，再全库按文件名兜底。"""
    if cwd:
        enc = encode_cwd(cwd)
        p = projects_dir / enc / f"{session_id}.jsonl"
        if p.is_file():
            return p
    hits = list(projects_dir.glob(f"*/{session_id}.jsonl")) if projects_dir.is_dir() else []
    return hits[0] if hits else None


def encode_cwd(cwd: str) -> str:
    """WorkBuddy 的 projects 目录名编码：盘符小写、去冒号、斜杠转连字符。"""
    s = cwd.replace(":", "").replace("/", "-").replace("\\", "-")
    return s[:1].lower() + s[1:] if s else s


# ---------------------------------------------------------------- 消息解析

def ms_to_iso_local(ms) -> str:
    """epoch 毫秒 → 本机时区 ISO8601（+08:00）；无效值回落当前时刻。"""
    try:
        n = int(str(ms).strip())
    except (TypeError, ValueError):
        n = 0
    dt = datetime.fromtimestamp(n / 1000, TZ) if n > 0 else datetime.now(TZ)
    return dt.isoformat(timespec="seconds")


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts)


def _strip_sysrem(text: str) -> str:
    """环境型 system-reminder 不是会话内容，剥掉防噪音。"""
    return re.sub(r"<system-reminder[\s\S]*?</system-reminder>", "", text or "").strip()


def _clip(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…[truncated {len(text) - cap} chars]"


def parse_jsonl(path: Path) -> list[dict]:
    """解析 jsonl → ``[{role, content, ts, name?}]``（user / assistant / tool）。"""
    msgs: list[dict] = []
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        ts = ms_to_iso_local(o.get("timestamp"))
        if t == "message" and o.get("role") in ("user", "assistant"):
            txt = _strip_sysrem(_blocks_text(o.get("content")))
            if txt:
                msgs.append({"role": o["role"], "content": _clip(txt, MSG_CAP), "ts": ts})
        elif t == "function_call":
            args = o.get("arguments")
            body = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            msgs.append({
                "role": "tool", "name": str(o.get("name") or "tool"),
                "content": _clip(f"调用参数: {body}", ARGS_CAP), "ts": ts,
            })
        elif t == "function_call_result":
            out = o.get("output") or {}
            txt = out.get("text") if isinstance(out, dict) else str(out)
            if isinstance(txt, str) and txt.strip():
                msgs.append({
                    "role": "tool", "name": str(o.get("name") or "tool"),
                    "content": _clip(txt, RESULT_CAP), "ts": ts,
                })
    return msgs


def render_l0(messages: list[dict]) -> str:
    """消息流 → L0 正文（``# ts user`` / ``## ts assistant`` / ``## ts tool``）。"""
    blocks = []
    for m in messages:
        if m["role"] == "user":
            blocks.append(f"# {m['ts']} user\n{m['content']}")
        elif m["role"] == "tool":
            blocks.append(f"## {m['ts']} tool\n**tool**: {m['name']}\n{m['content']}")
        else:
            blocks.append(f"## {m['ts']} assistant\n{m['content']}")
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------- 同步

def session_metadata(row: dict) -> dict:
    """D1 打标字段（写入 L0 frontmatter.metadata，提炼侧据此过滤）。"""
    return {
        "origin": "workbuddy",
        "playground": bool(_as_int(row.get("is_playground"))),
        "background_automation": bool(_as_int(row.get("is_background_automation"))),
    }


def sync_one(client, row: dict, state: dict, projects_dir: Path, *, dry_run: bool = False) -> dict:
    """同步单个会话，返回报告条目（不抛异常；失败写 ``status=failed``）。"""
    sid = str(row.get("id") or "")
    entry = state["sessions"].get(sid) or {}
    item = {
        "session_id": sid, "session_key": f"workbuddy-{sid[:8]}",
        "file_id": entry.get("file_id"), "status": "skipped",
        "reason": None, "messages": 0,
    }

    if str(row.get("deleted_at") or "").strip() not in ("", "None", "null", "0"):
        item["reason"] = "deleted"
        return item

    path = find_jsonl(projects_dir, sid, str(row.get("cwd") or ""))
    if not path:
        item["reason"] = "no_jsonl"
        return item
    messages = parse_jsonl(path)
    if not messages:
        item["reason"] = "empty"
        return item

    updated_at = str(row.get("updated_at") or "")
    synced = int(entry.get("synced_messages") or 0)
    if entry.get("updated_at") == updated_at and entry.get("file_id") and synced:
        item["reason"] = "unchanged"
        item["messages"] = synced
        return item
    if synced > len(messages):  # jsonl 被截断/重写：不猜，留待人工
        item["reason"] = "jsonl_shrunk"
        item["messages"] = synced
        return item
    pending = messages[synced:]
    if not pending:
        item["reason"] = "unchanged"
        item["messages"] = synced
        return item

    started_at = pending[0]["ts"]          # 确定性：崩溃重跑被服务端幂等丢弃
    text = render_l0(pending)
    item.update({"messages": len(pending), "status": "planned" if dry_run else "appended"})
    if dry_run:
        return item

    try:
        resp = client.append(
            item["session_key"], text, started_at=started_at,
            metadata=session_metadata(row), raw=True,
        )
    except SGMEError as exc:
        item.update({"status": "failed", "reason": f"append_error: {exc}"})
        return item

    if isinstance(resp, dict) and resp.get("file_id"):
        item["file_id"] = resp["file_id"]
    state["sessions"][sid] = {
        "updated_at": updated_at,
        "file_id": item["file_id"],
        "synced_messages": synced + len(pending),
        "session_key": item["session_key"],
    }
    return item


def sync_all(client, rows: list[dict], state: dict, projects_dir: Path, *,
             dry_run: bool = False, state_path: Path | None = None) -> dict:
    """批量同步 + 覆盖报告（扫到 N / 入库 M / 跳过 K + 原因）。"""
    items = []
    for row in rows:
        item = sync_one(client, row, state, projects_dir, dry_run=dry_run)
        items.append(item)
        if state_path and not dry_run:
            save_state(state_path, state)

    skip_reasons: dict[str, int] = {}
    for it in items:
        if it["status"] == "skipped":
            skip_reasons[it["reason"] or "unknown"] = skip_reasons.get(it["reason"] or "unknown", 0) + 1
    ingested = sum(1 for it in items if it["status"] in ("appended", "planned"))
    return {
        "scanned": len(items),
        "ingested": ingested,
        "skipped": sum(1 for it in items if it["status"] == "skipped"),
        "failed": sum(1 for it in items if it["status"] == "failed"),
        "appended_messages": sum(
            it["messages"] for it in items if it["status"] in ("appended", "planned")
        ),
        "skip_reasons": skip_reasons,
        "dry_run": dry_run,
        "sessions": items,
    }


# ---------------------------------------------------------------- CLI

def _parse_since(value: str | None) -> int | None:
    """``--since 2026-11-07``（本机时区零点）或 epoch 毫秒。"""
    if not value:
        return None
    if value.isdigit():
        return int(value)
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        raise SystemExit(f"--since 需为 YYYY-MM-DD 或 epoch 毫秒：{value}")
    return int(dt.timestamp() * 1000)


def resolve_since_ms(
    since: str | None,
    days: int | None,
    all_sessions: bool,
    now: datetime | None = None,
) -> int | None:
    """决定同步窗口下界（epoch 毫秒）。

    优先级：`--since` > `--all` > `--days`（缺省 `DEFAULT_WINDOW_DAYS` 天）。
    返回 None 表示不限（全量历史）。
    """
    if since:
        return _parse_since(since)
    if all_sessions:
        return None
    window = DEFAULT_WINDOW_DAYS if days is None else days
    if window <= 0:
        return None
    base = now or datetime.now(TZ)
    return int((base - timedelta(days=window)).timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WorkBuddy 会话增量同步到 SGME")
    p.add_argument("--db", default=str(default_db_path()), help="workbuddy.db 路径")
    p.add_argument("--projects", default=str(default_projects_dir()), help="projects 目录")
    p.add_argument("--state", default=str(default_state_path()), help="游标文件")
    p.add_argument("--since", help="只同步 updated_at >= 该日期（YYYY-MM-DD）；优先于 --days/--all")
    p.add_argument("--days", type=int, default=None,
                   help=f"窗口天数（缺省 {DEFAULT_WINDOW_DAYS}；0 = 不限）")
    p.add_argument("--all", action="store_true", help="不限窗口，全量历史（谨慎）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 场（0=不限）")
    p.add_argument("--dry-run", action="store_true", help="只报告，不写 SGME/游标")
    p.add_argument("--json", action="store_true", help="输出 JSON 报告")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = SGME()
    except SGMEError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    projects_dir = Path(args.projects)
    state_path = Path(args.state)
    since_ms = resolve_since_ms(args.since, args.days, args.all)
    rows = discover_sessions(db_path, since_ms)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    state = load_state(state_path)
    report = sync_all(client, rows, state, projects_dir, dry_run=args.dry_run, state_path=state_path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(
            f"[窗口 since={args.since or ('all' if args.all or args.days == 0 else f'近 {args.days if args.days is not None else DEFAULT_WINDOW_DAYS} 天')} since_ms={since_ms}] "
            f"扫描 {report['scanned']} 场 / 入库 {report['ingested']} 场"
            f"（新增消息 {report['appended_messages']} 条）/ 跳过 {report['skipped']} 场"
            f" / 失败 {report['failed']} 场"
            + ("（dry-run，未写入）" if report["dry_run"] else "")
        )
        if report["skip_reasons"]:
            print("跳过原因：" + "，".join(f"{k}={v}" for k, v in sorted(report["skip_reasons"].items())))
        for it in report["sessions"]:
            if it["status"] in ("failed", "skipped") and it["reason"] not in ("unchanged",):
                print(f"  - {it['session_id'][:8]} {it['status']}: {it['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
