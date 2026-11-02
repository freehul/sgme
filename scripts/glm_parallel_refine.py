#!/usr/bin/env python3
"""GLM 赠额并行提炼调度器（2026-11-02）。

与 ``glm_full_refine.py`` 的分工：那个脚本串行触发 + 每次等队列空闲，只适合单文件
冒烟；本脚本并发触发多个文件并持续跟踪进度，用于在 10 小时赠额窗口内把待提炼的
大文件跑完。

改动前必读的三条事实（2026-11-02 实测）：
1. **单文件内逐块串行**：``engine/l1.extract_l1`` 是 for 循环，一个文件的墙钟时间
   = 块数 × 单次调用耗时，跟并发度无关。并发只对「多文件」有意义。
2. **节流器是进程级全局令牌桶**（``llm/provider.py::_acquire_throttle``）：所有并发
   线程共享 ``llm.yaml`` 的 ``rules.throttle.rps``。rps 太低时并发等于白搭。
3. **中途崩溃 = 该文件从头重来**：游标只在 ``commit_refine`` 推进，所以看门狗
   「卡住就重触发」非常保守——宁可慢，不可把已跑的几千块推倒重来。

用法（必须在 SGME 容器内跑，需要直读 session.db / memory.db）：
    python glow_parallel_refine.py run --parallel 9 --until 08:30
    python glow_parallel_refine.py status          # 只看进度，不触发
    python glow_parallel_refine.py run --dry-run   # 只列将要触发的文件

环境变量（不写死任何地址/密钥）：
    SGME_BASE         SGME 服务地址（默认 http://127.0.0.1:9910）
    SGME_ADMIN_KEY    管理员 Key（必需，容器内自带）
    SGME_SESSION_DB   session.db（默认 /data/data/session.db）
    SGME_MEMORY_DB    memory.db（默认 /data/data/memory.db）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SGME_BASE = os.environ.get("SGME_BASE", "http://127.0.0.1:9910").rstrip("/")
ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "")
SESSION_DB = os.environ.get("SGME_SESSION_DB", "/data/data/session.db")
MEMORY_DB = os.environ.get("SGME_MEMORY_DB", "/data/data/memory.db")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _http(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    """调 SGME 管理端点（回环，不走代理）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{SGME_BASE}{path}",
        data=data,
        method=method,
        headers={"X-API-Key": ADMIN_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def pending_files() -> list[dict]:
    """待提炼文件（status='new'），按体积降序。"""
    if not Path(SESSION_DB).exists():
        raise SystemExit(f"找不到 {SESSION_DB}——本脚本需在 SGME 容器内运行")
    conn = sqlite3.connect(f"file:{SESSION_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select file_id, coalesce(size,0), path from raw_files "
            "where status='new' order by size desc"
        ).fetchall()
    finally:
        conn.close()
    return [{"file_id": r[0], "size": r[1], "path": r[2]} for r in rows]


def file_status(file_id: str) -> str:
    conn = sqlite3.connect(f"file:{SESSION_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select status from raw_files where file_id=?", (file_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else "missing"


def chunk_totals(file_ids: list[str]) -> dict[str, int]:
    """用服务端自己的分块逻辑算每个文件的总块数（用于显示进度百分比）。

    只读、纯本地解析，实测 25MB 文件 0.5s；失败时返回空字典不阻塞主流程。
    """
    try:
        import sgme.config as C
        from sgme.engine import l1
        from sgme.raw import store

        cs = (C.load_sgme_config().get("l1") or {}).get("chunk_size", 6000)
    except Exception as exc:  # noqa: BLE001
        _log(f"总块数预估跳过（{type(exc).__name__}: {exc}）")
        return {}
    out: dict[str, int] = {}
    for fid in file_ids:
        try:
            parsed = store.parse_file(fid, source_type="session")
            inc = store.extract_incremental(parsed, 0)
            out[fid] = len(l1.chunk_messages_by_turn(inc, chunk_size=cs))
        except Exception as exc:  # noqa: BLE001
            _log(f"总块数预估失败 {fid[:8]}: {type(exc).__name__}: {exc}")
    return out


def run_stats(file_ids: list[str]) -> dict[str, dict]:
    """从 memory.db 的 refine_runs 汇总每个文件的进度（块数 / token / 最近活跃）。"""
    if not Path(MEMORY_DB).exists() or not file_ids:
        return {}
    marks = ",".join("?" for _ in file_ids)
    conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"""select file_id,
                       sum(case when stage='l1_extraction' then 1 else 0 end) as l1_runs,
                       sum(case when status='running' then 1 else 0 end) as running,
                       coalesce(sum(total_tokens),0) as tokens,
                       max(started_at) as last_at,
                       max(case when status='running' then started_at end) as running_at
                  from refine_runs where file_id in ({marks}) group by file_id""",
            file_ids,
        ).fetchall()
    finally:
        conn.close()
    return {
        r[0]: {
            "l1_runs": r[1] or 0,
            "running": r[2] or 0,
            "tokens": r[3] or 0,
            "last_at": r[4],
            "running_at": r[5],
        }
        for r in rows
    }


def trigger(file_id: str) -> dict:
    """异步触发单文件提炼（kebab-case 规范路径）。"""
    return _http("POST", "/v1/admin/refine/trigger-async", {"file_id": file_id, "limit": 1})


def _deadline(until: str | None, max_hours: float | None) -> float | None:
    """算截止时刻（epoch 秒）。--until 形如 08:30（下一个该时刻）。"""
    if max_hours:
        return time.time() + max_hours * 3600
    if not until:
        return None
    hh, mm = until.split(":")
    now = datetime.now()
    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def _load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"started": _now(), "files": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def print_status(targets: list[dict], totals: dict[str, int], state: dict) -> list[dict]:
    """打印一行一文件，返回尚未完成的目标。"""
    fids = [t["file_id"] for t in targets]
    st = run_stats(fids)
    remaining: list[dict] = []
    _log("文件      体积   状态     块进度        已用token   最近活跃")
    for t in targets:
        fid = t["file_id"]
        size_mb = (t.get("size") or 0) / 1024 / 1024
        status = file_status(fid)
        s = st.get(fid) or {}
        done_blocks = s.get("l1_runs", 0)
        total = totals.get(fid)
        prog = f"{done_blocks}/{total}" if total else f"{done_blocks}/?"
        if status == "new":
            remaining.append(t)
        _log(
            f"{fid[:8]}  {size_mb:6.1f}MB {status:8s} {prog:14s} "
            f"{s.get('tokens',0):>10,} {str(s.get('last_at') or '-')[:19]}"
        )
    total_tokens = sum((st.get(f) or {}).get("tokens", 0) for f in fids)
    _log(f"合计已用 token: {total_tokens:,}；未完成 {len(remaining)}/{len(targets)} 个文件")
    return remaining


def main() -> None:
    ap = argparse.ArgumentParser(description="GLM 赠额并行提炼调度器")
    ap.add_argument("action", choices=["run", "status"], help="run=触发并跟踪；status=只看进度")
    ap.add_argument("--parallel", type=int, default=9, help="同时跑的文件数上限")
    ap.add_argument("--until", default="08:30", help="截止时刻 HH:MM（默认 08:30）")
    ap.add_argument("--max-hours", type=float, default=None, help="改用小时数作截止")
    ap.add_argument("--state", default="/data/tmp/glm_parallel_state.json", help="进度文件")
    ap.add_argument("--poll", type=int, default=45, help="轮询间隔秒")
    ap.add_argument("--stall-min", type=int, default=20,
                    help="文件无新 run 超过该分钟数且无 running 才考虑重触发")
    ap.add_argument("--max-retries", type=int, default=1, help="单文件最大重触发次数")
    ap.add_argument("--file-id", action="append", default=None, help="只处理指定文件（可多次）")
    ap.add_argument("--dry-run", action="store_true", help="只列目标，不触发")
    ap.add_argument("--no-count", action="store_true", help="不预估总块数（省内存）")
    args = ap.parse_args()

    if not ADMIN_KEY:
        raise SystemExit("缺 SGME_ADMIN_KEY 环境变量")

    targets = pending_files()
    if args.file_id:
        want = set(args.file_id)
        targets = [t for t in targets if t["file_id"] in want or t["file_id"][:8] in want]
    if not targets:
        _log("没有待提炼文件")
        return

    total_mb = sum(t.get("size") or 0 for t in targets) / 1024 / 1024
    _log(f"目标 {len(targets)} 个文件，合计 {total_mb:.1f}MB，并发上限 {args.parallel}")

    totals = {} if args.no_count else chunk_totals([t["file_id"] for t in targets])
    if totals:
        _log(f"总块数预估: {sum(totals.values())} 块")

    if args.action == "status":
        print_status(targets, totals, _load_state(Path(args.state)))
        return
    if args.dry_run:
        for t in targets:
            fid = t["file_id"]
            n = totals.get(fid)
            _log(f"将触发 {fid[:8]}（{(t.get('size') or 0)/1024/1024:.1f}MB"
                 f"{'，' + str(n) + ' 块' if n else ''}）")
        return

    state_path = Path(args.state)
    state = _load_state(state_path)
    deadline = _deadline(args.until, args.max_hours)
    if deadline:
        _log(f"截止时刻: {datetime.fromtimestamp(deadline):%Y-%m-%d %H:%M}")

    while True:
        pending = print_status(targets, totals, state)
        if not pending:
            _log("全部文件已离开 status=new，收工")
            break
        if deadline and time.time() >= deadline:
            _log("已到截止时刻，停止触发（未完成的文件保持 status=new，等下一个窗口）")
            break

        st = run_stats([t["file_id"] for t in pending])
        active = sum(1 for f in st.values() if f.get("running") and _age_min(f.get("running_at") or "") < args.stall_min)
        for t in pending:
            fid = t["file_id"]
            entry = state["files"].setdefault(fid, {"attempts": 0})
            s = st.get(fid) or {}
            last_at = s.get("last_at") or ""
            # 是否在跑：有**新鲜**的 running 记录，或最近一次 run 距今不到 stall_min
            # （僵死 running 不能算——否则一次崩溃就把文件永久锁死）
            live_running = (s.get("running") or 0) > 0 and _age_min(s.get("running_at") or "") < args.stall_min
            busy = live_running or (bool(last_at) and _age_min(last_at) < args.stall_min)
            if busy:
                continue
            if active >= args.parallel:
                break
            if entry["attempts"] >= 1 + args.max_retries:
                if not entry.get("gave_up"):
                    _log(f"{fid[:8]} 已达最大重试次数，放弃（保持 status=new 等人工）")
                    entry["gave_up"] = True
                continue
            entry["attempts"] += 1
            entry["last_trigger"] = _now()
            try:
                resp = trigger(fid)
                _log(f"触发 {fid[:8]} 第 {entry['attempts']} 次 → {resp.get('status')}")
                active += 1
            except Exception as exc:  # noqa: BLE001
                _log(f"触发 {fid[:8]} 失败: {type(exc).__name__}: {exc}")
            _save_state(state_path, state)
        _save_state(state_path, state)
        time.sleep(args.poll)


def _age_min(iso: str) -> float:
    """ISO 时间（可带 Z）到现在的分钟数。"""
    try:
        dt = datetime.fromisoformat(str(iso))  # 3.11+ 原生支持末尾 Z
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return (datetime.now() - dt).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return 999.0


if __name__ == "__main__":
    main()
