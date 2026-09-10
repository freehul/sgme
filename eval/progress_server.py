# -*- coding: utf-8 -*-
"""T-150 评测进度监视服务（零依赖，仅标准库）。

职责：只读评测产物目录，产出进度 JSON，并托管同目录的 progress.html。

Why（2026-09-11）：长跑评测约 75 分钟，此前日志只有 20 秒心跳一条线，
无法分辨「正在干活」与「已卡死」（当日实测：`.bat` 静默失败两次，
进程表里查无新进程才发现根本没跑起来）。本服务暴露真进度信号：
L0 落盘数 / 已提炼数 / 记忆条数 / L2 场景数 + 心跳新鲜度。

用法：
    python eval/progress_server.py --output eval/results/t150_rerun_verify --port 8899
    python eval/progress_server.py --output <目录> --once    # 打印一次 JSON 后退出（调试用）

设计约束：
- 只读：数据库一律以 `file:...?mode=ro` 打开，绝不写入评测产物
- 零依赖：仅标准库（评测机上不装 flask）
- 读失败不崩：评测进程正在写库，读可能被锁 → 该字段返回 null，不抛异常
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 心跳超过该秒数未见更新，判定为可疑（心跳周期 20 秒，留 4 倍余量）
STALE_SECONDS = 90


def _count(db_path: Path, table: str):
    """只读统计单表行数；库或表不存在、被锁时返回 None（不抛异常）。"""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


def _load_state(state_path: Path):
    """读取 refine_state.json，返回 ingested/refined 计数。"""
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return {
            "ingested": len(data.get("ingested") or []),
            "refined": len(data.get("refined") or []),
        }
    except Exception:
        return None


def _collect_arm(arm_dir: Path) -> dict:
    """收集单个臂（问题）的进度。"""
    mem = arm_dir / "memory.db"
    return {
        "arm": arm_dir.name,
        "session_raw_files": _count(arm_dir / "session.db", "raw_files"),
        "state": _load_state(arm_dir / "refine_state.json"),
        "memories": _count(mem, "memories"),
        "memory_tags": _count(mem, "memory_tags"),
        "memory_vectors": _count(mem, "memory_vectors"),
        "scenes": _count(mem, "scenes"),
        "refine_runs": _count(mem, "refine_runs"),
    }


def _collect_log(log_path: Path):
    """读取日志尾部、心跳新鲜度、已运行秒数、是否收尾。"""
    result = {"tail": [], "heartbeat_age_s": None, "elapsed_s": None, "done": False}
    if not log_path or not log_path.exists():
        return result
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return result

    result["tail"] = [ln for ln in lines[-80:] if ln.strip()][-8:]

    # 心跳格式：[HH:MM:SS] [心跳] 已运行 N 秒
    for line in reversed(lines):
        m = re.search(
            r"\[(\d{2}):(\d{2}):(\d{2})\]\s*\[心跳\]\s*已运行\s*(\d+)\s*秒", line
        )
        if m:
            hh, mm, ss, sec = (int(m.group(i)) for i in range(1, 5))
            now = datetime.now()
            beat = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            result["heartbeat_age_s"] = max(0.0, (now - beat).total_seconds())
            result["elapsed_s"] = sec
            break

    # 收尾标志：正式评测结束会在日志尾部给出评分
    tail_text = "\n".join(lines[-40:])
    result["done"] = ("recall@8" in tail_text) or ("评测完成" in tail_text)
    return result


def collect(output_dir: str, log_path: str) -> dict:
    """汇总一次进度快照。"""
    out = Path(output_dir)
    tmp = out / "tmp"
    run_id, arms = None, []

    if tmp.exists():
        runs = sorted((p for p in tmp.iterdir() if p.is_dir()), key=lambda p: p.name)
        if runs:
            run_id = runs[-1].name
            arms = [
                _collect_arm(p)
                for p in sorted((q for q in runs[-1].iterdir() if q.is_dir()), key=lambda q: q.name)
            ]

    log = _collect_log(Path(log_path)) if log_path else {"tail": [], "heartbeat_age_s": None, "elapsed_s": None, "done": False}
    age = log["heartbeat_age_s"]

    return {
        "run_id": run_id,
        "elapsed_s": log["elapsed_s"],
        "heartbeat_age_s": age,
        "alive": None if age is None else age < STALE_SECONDS,
        "done": log["done"],
        "arms": arms,
        "log_tail": log["tail"],
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


class Handler(BaseHTTPRequestHandler):
    """仅两个端点：/status 返 JSON，/ 返网页。"""

    def _reply(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # 允许本机浏览器跨机读取
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            payload = collect(ARGS.output, ARGS.log)
            self._reply(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            page = HERE / "progress.html"
            if page.exists():
                self._reply(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._reply(404, "progress.html 缺失".encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self._reply(404, b"not found", "text/plain")

    def log_message(self, format, *args):  # noqa: A002 - 基类签名如此
        pass  # 静默，避免轮询刷屏


def main():
    global ARGS
    parser = argparse.ArgumentParser(description="T-150 评测进度监视服务")
    parser.add_argument("--output", required=True, help="评测产物目录（如 eval/results/t150_rerun_verify）")
    parser.add_argument("--log", default=None, help="评测日志文件（默认取 <output>.log）")
    parser.add_argument("--port", type=int, default=8899, help="监听端口（默认 8899）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，便于跨机查看）")
    parser.add_argument("--once", action="store_true", help="打印一次 JSON 后退出（调试用）")
    ARGS = parser.parse_args()

    if not ARGS.log:
        ARGS.log = str(Path(ARGS.output).with_suffix(".log"))

    if ARGS.once:
        print(json.dumps(collect(ARGS.output, ARGS.log), ensure_ascii=False, indent=2))
        return

    server = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print(f"进度监视服务已启动：http://{ARGS.host}:{ARGS.port}/")
    print(f"  产物目录：{ARGS.output}")
    print(f"  日志文件：{ARGS.log}")
    print("  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
