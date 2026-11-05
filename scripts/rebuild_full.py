#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/rebuild_full.py：SGME 全量重建 runner（隔离 home，可断点续跑）。

用途（2026-11-06 ZCode GLM Trial 窗口）：用 Magpie 网关的 GLM-5.3-Flash-Trial
把 raw_files 里 status='new' 的 L0 原件全量重新提炼到**隔离 home**（生产零接触）。

设计要点
--------
- 隔离：进程内先设 SGME_HOME=<home>、SGME_L2_SCHEMA=2.0、MAGPIE_API_KEY（必须在
  import sgme.* 之前设置——config 模块级常量按环境变量解析）；所有 DB/raw 读写只落在
  <home>/data 与 <home>/raw。
- 可断点续跑：主进程只挑 status='new' 的 file_id；已 refined 的文件天然跳过。
  失败文件按重建口径计次（<home>/logs/rebuild_state.json），达 --max-attempts 后
  标 status='error' + last_error='rebuild_max_attempts'，后续运行不再拾起。
- 并发：--workers 个线程，每线程自己 db.init_databases() 拿独立 conns（sqlite 连接
  不跨线程共享）；cfg 只读共享。
- 前置自检：正式跑前对网关发一次最小 chat 请求，非 200/无 choices 直接退出（0 消耗）。
- 每文件输出：file_id / size / 耗时 / 记忆数 / status / 最新 refine_run 的 provider、
  tokens；--dry-run 只列表。

用法示例
--------
    python scripts/rebuild_full.py --init                 # 建/校验 home，重置 raw_files
    python scripts/rebuild_full.py --limit 3              # 冒烟 3 个（按体积降序）
    python scripts/rebuild_full.py --workers 3            # 全量（1454 个）

退出码：0 全部成功/无待办；1 有失败但有进展；2 自检失败/环境错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = REPO_ROOT / "rebuild-20261106" / "home"
STATE_FILE_NAME = "rebuild_state.json"
LOG_FILE_NAME = "rebuild.log"


# ----------------------------------------------------------------------------
# 环境引导（必须在 import sgme.* 之前执行）
# ----------------------------------------------------------------------------

def bootstrap_env(home: Path, l2_schema: str = "2.0", api_key: str = "local-gateway") -> None:
    """设置隔离 home 与模型环境变量（幂等；先于任何 sgme 导入调用）。"""
    os.environ["SGME_HOME"] = str(home)
    os.environ["SGME_L2_SCHEMA"] = l2_schema
    os.environ.setdefault("MAGPIE_API_KEY", api_key)
    # 清掉可能从父进程继承的可写配置重定向，避免误读其他 home
    os.environ.pop("SGME_CONFIG_PATH", None)
    for extra in (str(REPO_ROOT),):
        if extra not in sys.path:
            sys.path.insert(0, extra)


# ----------------------------------------------------------------------------
# 日志 / 状态
# ----------------------------------------------------------------------------

def setup_logging(home: Path, verbose: bool = True) -> logging.Logger:
    logger = logging.getLogger("rebuild")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(home / "logs" / LOG_FILE_NAME, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    logger.propagate = False
    return logger


class StateStore:
    """rebuild_state.json 原子读写（每文件一次，进程间单写者）。"""

    def __init__(self, home: Path):
        self.path = home / "logs" / STATE_FILE_NAME
        self.lock = threading.Lock()
        self.data = {"started_at": _now(), "updated_at": _now(),
                     "attempts": {}, "done": {}, "stats": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
                    self.data.setdefault("attempts", {})
                    self.data.setdefault("done", {})
            except Exception as e:  # 坏状态文件不阻塞：备份后重来（已 refined 文件本就跳过）
                backup = self.path.with_suffix(".json.broken-" + _stamp())
                try:
                    self.path.replace(backup)
                except Exception:
                    pass
                logging.getLogger("rebuild").warning("状态文件损坏已备份: %s (%s)", backup, e)

    def attempts(self, file_id: str) -> int:
        return int(self.data["attempts"].get(file_id, 0))

    def record(self, file_id: str, ok: bool, attempts: int, info: dict) -> None:
        with self.lock:
            self.data["attempts"][file_id] = attempts
            if ok:
                self.data["done"][file_id] = info
            self.data["updated_at"] = _now()
            self._flush()

    def bump_stats(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                self.data["stats"][k] = self.data["stats"].get(k, 0) + v
            self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


# ----------------------------------------------------------------------------
# home 初始化 / 重置
# ----------------------------------------------------------------------------

def ensure_layout(home: Path) -> None:
    for sub in ("config", "data", "raw/sessions", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def init_home(home: Path, logger: logging.Logger, reset_raw: bool = True) -> None:
    """初始化全新 memory.db（含维度注册表）并按重建口径重置 raw_files。

    幂等：memory.db 已存在且非空时仅告警（不覆盖——已有重建成果不可静默丢弃）；
    raw_files 重置在每次 --init 时执行（全部 status=new / 游标清零 / 失败计数清零）。
    """
    from sgme import config
    from sgme.data import db as db_mod, memory_dao, session_dao

    data_dir = home / "data"
    cfg = config.load_config()
    mem, session, wiki = db_mod.init_databases(data_dir)
    try:
        n_mem = mem.execute("SELECT COUNT(*) AS c FROM memories").fetchone()[0]
        if n_mem:
            logger.warning("memory.db 已有 %d 条记忆（保留，不重置）: %s", n_mem, data_dir / "memory.db")
        memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
        logger.info("维度注册表就绪: %d 维 / %d 别名",
                    len(cfg["dimensions"]), len(cfg["aliases"]))

        if reset_raw:
            session.execute(
                "UPDATE raw_files SET status='new', last_refined_seq=0, refined_at=NULL, "
                "content_hash=NULL, fail_count=0, last_error=NULL"
            )
            session.commit()
        session.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rows = list(session.execute(
            "SELECT status, COUNT(*) FROM raw_files GROUP BY status ORDER BY status"))
        logger.info("raw_files 重置后状态: %s", {r[0]: r[1] for r in rows})
        logger.info("文件数=%d；data 目录=%s", sum(r[1] for r in rows), data_dir)
    finally:
        for conn in (mem, session, wiki):
            try:
                db_mod.close(conn)
            except Exception:
                pass


# ----------------------------------------------------------------------------
# 网关自检
# ----------------------------------------------------------------------------

def probe_gateway(cfg: dict, logger: logging.Logger, timeout_s: float = 180.0) -> None:
    """对首链节点发一次最小 chat 请求；非 200 / 无 choices 直接抛 SystemExit(2)。"""
    import httpx

    node = ((cfg.get("llm") or {}).get("chains") or {}).get("refinement") or []
    node = node[0] if node else {}
    base_url = (node.get("base_url") or "").rstrip("/")
    model = node.get("model")
    api_key_env = node.get("api_key_env")
    api_key = os.environ.get(api_key_env, "local-gateway") if api_key_env else "local-gateway"
    if not base_url or not model:
        logger.error("LLM 首链节点缺 base_url/model：%s", node)
        raise SystemExit(2)
    # 自检请求沿用节点采样参数（尤其 extra_body.reasoning_effort）：思考型模型
    # 若走默认思考档，16 tokens 预算会被 reasoning 吃光 → content=null、
    # finish_reason=length（2026-11-06 实测），自检会误报网关不可用。
    payload = {"model": model,
               "messages": [{"role": "user", "content": "只回复两个字：就绪"}],
               "max_tokens": 256}
    payload.update(node.get("extra_body") or {})
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s, trust_env=False,
        )
    except Exception as e:
        logger.error("网关自检失败（%s）：%s", base_url, e)
        raise SystemExit(2) from e
    elapsed = time.time() - t0
    if resp.status_code != 200:
        logger.error("网关自检非 200: %s → %s", resp.status_code, resp.text[:300])
        raise SystemExit(2)
    try:
        data = resp.json()
        choices = data.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    except Exception as e:
        logger.error("网关自检响应非 JSON: %s", e)
        raise SystemExit(2) from e
    if not choices or not content.strip():
        logger.error("网关自检响应为空 content：%s", json.dumps(data, ensure_ascii=False)[:300])
        raise SystemExit(2)
    logger.info("网关自检通过: model=%s base=%s %.2fs content=%r",
                model, base_url, elapsed, content.strip()[:40])


# ----------------------------------------------------------------------------
# 选文件 / 单文件执行
# ----------------------------------------------------------------------------

_ORDER_SQL = {
    "size": "size DESC, file_id",                 # 大文件优先（先验证最难的）
    "size_asc": "size ASC, file_id",              # 小文件优先（窗口内尽量多覆盖会话）
    "time": "COALESCE(started_at, '') ASC, file_id",   # 旧会话优先
    "time_desc": "COALESCE(started_at, '') DESC, file_id",  # 新会话优先
}


def list_pending(session_conn: sqlite3.Connection, order: str, limit: int | None) -> list[dict]:
    order_sql = _ORDER_SQL.get(order, _ORDER_SQL["size"])
    sql = f"SELECT file_id, size, started_at FROM raw_files WHERE status='new' ORDER BY {order_sql}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in session_conn.execute(sql).fetchall()]


#: 网关级冷却截止时刻（monotonic）。任一 worker 撞到 405/限流即全局暂停，
#: 避免多线程同时重试把临时 IP 封禁拖长（2026-11-06 实测：3 并发触发 429 →
#: 405「firewall blocked this IP」约 3 分钟后才恢复）。
_COOLDOWN_LOCK = threading.Lock()
_gateway_cooldown_until: float = 0.0


def set_gateway_cooldown(seconds: float = 180.0) -> None:
    global _gateway_cooldown_until
    with _COOLDOWN_LOCK:
        _gateway_cooldown_until = max(_gateway_cooldown_until, time.monotonic() + seconds)


def wait_gateway_cooldown(logger: logging.Logger) -> None:
    """所有 worker 在网关冷却期内统一等待（分批 sleep，便于中断）。"""
    while True:
        with _COOLDOWN_LOCK:
            remain = _gateway_cooldown_until - time.monotonic()
        if remain <= 0:
            return
        logger.info("网关冷却中，%.0fs 后继续", remain)
        time.sleep(min(remain, 30.0))


_TLS = threading.local()


def _worker_conns(data_dir: Path):
    """每线程独立 sqlite 连接（禁止跨线程共享；init_databases 幂等建表）。"""
    conns = getattr(_TLS, "conns", None)
    if conns is None:
        from sgme.data import db as db_mod

        conns = db_mod.init_databases(data_dir)
        _TLS.conns = conns
    return conns


def _latest_run(mem_conn: sqlite3.Connection, file_id: str) -> dict:
    """最近一条 L1 提炼 run（provider/status/tokens）+ 本文件截断块合计。

    优先取 stage='l1_extraction'（真正调用 GLM 的批次）；该文件若走空池/灰区
    短路，则回退到最近任意 run（如 empty_pool / near_dup），避免 provider 显示为空。
    """
    try:
        sql = ("SELECT provider, status, memories_count, total_tokens, stage "
               "FROM refine_runs WHERE file_id=?%s ORDER BY started_at DESC LIMIT 1")
        row = mem_conn.execute(sql % " AND stage='l1_extraction'", (file_id,)).fetchone()
        if row is None:
            row = mem_conn.execute(sql % "", (file_id,)).fetchone()
        run = dict(row) if row else {}
        if run:
            try:
                run["truncated"] = int(mem_conn.execute(
                    "SELECT COALESCE(SUM(CAST(json_extract(action_counts, "
                    "'$.l1_truncated_chunks') AS INTEGER)), 0) FROM refine_runs "
                    "WHERE file_id=? AND stage='l1_extraction'", (file_id,),
                ).fetchone()[0] or 0)
            except Exception:
                run["truncated"] = 0
        return run
    except Exception:
        return {}


def _raw_status(session_conn: sqlite3.Connection, file_id: str) -> str:
    row = session_conn.execute(
        "SELECT status FROM raw_files WHERE file_id=?", (file_id,)).fetchone()
    return row[0] if row else "MISSING"


def _transient_error(error: str | None) -> bool:
    """网关瞬态错误判定（用于进程内重试；常驻 runner 不依赖人工再启动）。

    405 = ZCode 免费档临时 IP 限流（message 明确「wait a while」）；
    429/超时/5xx/连接错误同属可等错误。非瞬态错误（如解析失败/程序 bug）不重试。
    """
    if not error:
        return False
    msg = str(error).lower()
    return any(k in msg for k in ("429", "405", "rate_limit", "timeout", "timed out",
                                  "5xx", "connection", "start plan", "concurrency limit"))


def run_one(file_id: str, size: int | None, cfg: dict, data_dir: Path,
            max_attempts: int, state: StateStore) -> dict:
    """在当前线程执行单个文件（独立 conns，瞬态错误进程内退避重试）。"""
    from sgme.engine import pipeline as pipeline_mod

    logger = logging.getLogger("rebuild")
    mem_conn, session_conn, _ = _worker_conns(data_dir)
    attempts = state.attempts(file_id)
    t0 = time.perf_counter()
    while True:
        wait_gateway_cooldown(logger)
        attempts += 1
        error = None
        result = None
        try:
            result, _l3_stats = pipeline_mod.refine_one(file_id, mem_conn, session_conn, cfg)
            status = result.status
            memories = len(result.memories)
        except Exception as e:  # 单文件异常不中断整批
            status = "exception"
            memories = 0
            error = f"{type(e).__name__}: {e}"
        raw_status = _raw_status(session_conn, file_id)
        # 瞬态网关错误（405/429/超时）→ 当场退避重试（最多 max_attempts 次），
        # 避免一个临时限流窗口把文件误判为永久失败。
        if raw_status == "new" and _transient_error(error):
            # 网关临时封禁/限流：全局冷却（其他 worker 也一起等），别把次数浪费在
            # 封禁窗口里；冷却后仍按 max_attempts 判定是否继续。
            set_gateway_cooldown(180.0)
        if (raw_status == "new" and _transient_error(error)
                and attempts < max_attempts):
            wait_s = min(300, 30 * attempts)
            logger.warning("file=%s 瞬态错误，%.0fs 后重试（attempt %d/%d）: %s",
                           file_id, wait_s, attempts, max_attempts, str(error)[:160])
            time.sleep(wait_s)
            continue
        break
    elapsed = time.perf_counter() - t0
    run = _latest_run(mem_conn, file_id)

    ok = raw_status == "refined"
    if not ok and raw_status == "new" and attempts >= max_attempts:
        # 重建口径的重试上限（与 E3 的 fail_count 熔断互补）：达上限标 error 防无限重刷
        try:
            session_conn.execute(
                "UPDATE raw_files SET status='error', last_error=? WHERE file_id=?",
                (f"rebuild_max_attempts({attempts})", file_id),
            )
            session_conn.commit()
        except Exception:
            pass
        raw_status = _raw_status(session_conn, file_id)

    info = {
        "file_id": file_id,
        "size": size,
        "elapsed_s": round(elapsed, 2),
        "status": status,
        "raw_status": raw_status,
        "memories": memories,
        "provider": run.get("provider"),
        "run_status": run.get("status"),
        "tokens": run.get("total_tokens"),
        "truncated": run.get("truncated", 0),
        "attempts": attempts,
        "error": error or (result.error if result is not None else None),
        "ts": _now(),
    }
    state.record(file_id, ok, attempts, info)
    return info


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SGME 全量重建 runner（隔离 home）")
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="重建 home（默认 rebuild-20261106/home）")
    parser.add_argument("--workers", type=int, default=3, help="并发线程数（默认 3）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 个文件（冒烟用）")
    parser.add_argument("--max-attempts", type=int, default=2, help="单文件重试上限（默认 2）")
    parser.add_argument("--order", choices=("size", "size_asc", "time", "time_desc"),
                        default="size",
                        help="size=体积降序（默认）/size_asc=体积升序/time=会话旧→新/time_desc=新→旧")
    parser.add_argument("--files", default=None,
                        help="显式指定 file_id 列表（逗号分隔；冒烟/补跑用）")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理文件，不调 LLM")
    parser.add_argument("--skip-gateway-check", action="store_true",
                        help="跳过网关自检（网关临时封禁/限流期使用；正式跑不建议跳过）")
    parser.add_argument("--init", action="store_true", help="初始化 home（memory.db + 重置 raw_files）后退出")
    args = parser.parse_args(argv)

    home = Path(args.home).resolve()
    bootstrap_env(home)
    ensure_layout(home)
    logger = setup_logging(home)

    from sgme import config
    from sgme.data import db as db_mod

    if args.init:
        init_home(home, logger)
        return 0

    cfg = config.load_config()
    session_conn = db_mod.connect_session(home / "data")
    try:
        total_new = session_conn.execute(
            "SELECT COUNT(*) FROM raw_files WHERE status='new'").fetchone()[0]
        if args.files:
            wanted = [x.strip() for x in args.files.split(",") if x.strip()]
            rows = session_conn.execute(
                "SELECT file_id, size, started_at FROM raw_files "
                "WHERE status='new' AND file_id IN (%s)"
                % ",".join("?" * len(wanted)), wanted).fetchall()
            by_id = {r["file_id"]: dict(r) for r in rows}
            pending = [by_id[x] for x in wanted if x in by_id]
        else:
            pending = list_pending(session_conn, args.order, args.limit)
    finally:
        db_mod.close(session_conn)

    logger.info("重建 home=%s workers=%s order=%s limit=%s max_attempts=%s",
                home, args.workers, args.order, args.limit, args.max_attempts)
    logger.info("待处理 status=new: %d（本次选中 %d）", total_new, len(pending))
    if args.dry_run:
        for i, rf in enumerate(pending, 1):
            logger.info("[dry-run %d/%d] %s size=%s started_at=%s",
                        i, len(pending), rf["file_id"], rf["size"], rf["started_at"])
        return 0
    if not pending:
        logger.info("无待处理文件（全部已 refined/error），退出")
        return 0

    if args.skip_gateway_check:
        logger.warning("已跳过网关自检（--skip-gateway-check）")
    else:
        probe_gateway(cfg, logger)

    state = StateStore(home)
    data_dir = home / "data"
    stats = {"selected": len(pending), "ok": 0, "failed": 0, "memories": 0}
    stats_lock = threading.Lock()
    t_start = time.perf_counter()
    failure_examples: list[dict] = []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_one, rf["file_id"], rf["size"], cfg, data_dir,
                               args.max_attempts, state): rf for rf in pending}
        done_n = 0
        for fut in as_completed(futures):
            rf = futures[fut]
            try:
                info = fut.result()
            except Exception as e:  # 理论上 run_one 已兜底；双保险
                info = {"file_id": rf["file_id"], "size": rf["size"], "status": "fatal",
                        "raw_status": "?", "memories": 0, "provider": None,
                        "tokens": None, "elapsed_s": 0, "attempts": state.attempts(rf["file_id"]),
                        "error": f"{type(e).__name__}: {e}"}
            done_n += 1
            with stats_lock:
                if info.get("raw_status") == "refined":
                    stats["ok"] += 1
                else:
                    stats["failed"] += 1
                    if len(failure_examples) < 20:
                        failure_examples.append(info)
                stats["memories"] += int(info.get("memories") or 0)
                elapsed_total = time.perf_counter() - t_start
                rate = done_n / elapsed_total * 60 if elapsed_total > 0 else 0.0
            logger.info(
                "[%d/%d] file=%s size=%s %.1fs status=%s raw=%s memories=%d provider=%s "
                "tokens=%s truncated=%d attempts=%d%s",
                done_n, len(pending), info.get("file_id"), info.get("size"),
                info.get("elapsed_s") or 0, info.get("status"), info.get("raw_status"),
                info.get("memories") or 0, info.get("provider"), info.get("tokens"),
                info.get("truncated") or 0, info.get("attempts") or 0,
                (" err=" + str(info.get("error"))[:200]) if info.get("error") else "",
            )

    elapsed = time.perf_counter() - t_start
    rate = len(pending) / elapsed * 60 if elapsed > 0 else 0.0
    remaining = max(0, total_new - len(pending))
    eta_min = remaining / rate if rate > 0 else None
    logger.info("本轮完成: selected=%d ok=%d failed=%d memories=%d elapsed=%.0fs rate=%.2f 文件/分钟",
                stats["selected"], stats["ok"], stats["failed"], stats["memories"], elapsed, rate)
    if eta_min is not None:
        logger.info("剩余待办 %d 个，按本轮吞吐预计 %.0f 分钟（%.1f 小时）", remaining, eta_min, eta_min / 60)
    state.bump_stats(ok=stats["ok"], failed=stats["failed"], memories=stats["memories"])
    if failure_examples:
        logger.info("失败样例（最多 20 条）: %s",
                    json.dumps(failure_examples, ensure_ascii=False)[:2000])
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断（state 已逐文件落盘，可直接重跑续跑）", file=sys.stderr)
        raise SystemExit(130)
