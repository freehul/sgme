"""engine/dream.py：Dream 夜间整理（0.8 ST-10）——四步编排 + 定时器 + 执行锁。

定位（`SGME-Dream夜间整理设计-v0.1.md` §1）：夜间低峰的一次性兜底批处理
（补提炼 + 生命周期 + 日报），**不是主链路的替代**。与已禁用的 refine.batch_scan
（10 分钟轮询常驻）的区别：Dream 每日一次、可配、可手动触发、带日报产物。

四步执行（§2，单次运行内顺序执行）
----------------------------------
① 抽取   扫描 session.db raw_files status='new' → 逐文件 pipeline.refine_one
          （复用提炼管线唯一出口；单文件独立 try/except，崩溃只丢当前文件）
② 判决   refine_one 内置（L1.5 冲突裁决 + L2 场景聚合），无单独步骤
③ 生命周期
          A. TTL 主动标记：memories 中超 TTL 的 active 动态维度记忆 → expired
             （rejected_at=now、reject_reason='dream_ttl_expired'；
              ttl_days IS NULL / status != active 一律跳过——保守边界）
          B. 冷归档：raw_files status='refined' 且 started_at 距今 > archive_days
             → status='archived'（文件不删、DB 行不删——原件永不删铁律）
④ 日报   汇总本次运行 → report_dir/dream-YYYYMMDD.md 落盘（阮一峰风格紧凑）
          + dream_reports 表写入（memory.db）+ error 级事件进 signal_events

TTL 边界一致性（§2.2 A）：查询层过滤条件为 `julianday(updated_at) <=
julianday('now') - ttl_days`（过期即退出注入），落库标记采用同一边界
（`updated_at <= now - ttl`），保证「落库标记不改变查询语义」。

调度（§3）：Gateway 内定时器（线程模式，复用 batch_scan 的定时思想，
不引入 APScheduler）。``ensure_scheduler`` 幂等启动常驻 daemon 线程，
按 dream.schedule（本地时区 HH:MM）到点执行 run_dream，循环复查配置；
schedule 为空 = 不自动只手动。⚠️ 接线说明：ST-10 铁律只允许改 routes_admin.py，
故定时器由手动触发端点（POST /v1/admin/dream/trigger）在首次触发时顺带
``ensure_scheduler`` 拉起；生产 Gateway 触发一次后定时器即常驻。

执行锁（§3）：模块级 ``_run_lock`` 单实例互斥——run_dream 非阻塞获取，
获取失败即视为「执行中」（防重入，触发端点据此返回 409 ERR_CONFLICT）。
设计文档要求「与 refine 批量提炼共用同一把锁」（dream 与 trigger_async 不得
并发跑提炼），但 ST-10 铁律禁止改动 pipeline/operations/refine，本锁暂为
Dream 自持；后续可在 pipeline.py 引入同锁并让 async_refine_worker 获取，
本模块已把锁暴露为 ``RUN_LOCK`` 供平滑接线。

失败预案（§4）：
1. 单文件失败：独立 try/except 继续，记入日报错误列表（最多 10 条）
2. 阶段失败（如生命周期 SQL 异常）：该阶段中止，其余阶段继续，日报标注
3. 整体崩溃：status=new 幂等语义天然可重入；绝不半途标记已处理
4. 静默失败防线：运行结束必有日报产物；日报为空 = 没运行（T-12 水位交叉验证）

⚠️ ST-10 铁律说明：dream_reports 的读写 SQL 本应下沉 data 层独立 DAO 文件，
但任务约束只允许新增本文件（engine 侧），SQL 以最小面内聚于此，后续可平滑迁出。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sgme import config
from sgme.data import session_dao
from sgme.engine import pipeline as pipeline_mod

logger = logging.getLogger("sgme.engine.dream")

#: Dream 执行锁（单实例互斥；防重入 + 与 refine 批量提炼共用的接线点，见模块 docstring）
RUN_LOCK = threading.Lock()

#: 定时器线程守卫（ensure_scheduler 幂等）
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop: threading.Event | None = None

#: 日报错误列表上限（设计文档 §2.3：最多列 10 条）
_MAX_ERRORS_IN_REPORT = 10


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_date_label() -> str:
    """日报日期标签 YYYYMMDD（本地日期——与 schedule 本地时区语义一致）。"""
    return datetime.now().strftime("%Y%m%d")


# ======================================================================
# ① + ② 抽取与判决（pipeline.refine_one 串联，单文件独立容错）
# ======================================================================

def _step_extract(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
    max_files: int,
) -> tuple[int, int, int, list[dict]]:
    """抽取阶段：扫 status=new → 逐文件 refine_one（含判决）。返回统计。

    Returns:
        (refined_count, error_count, memory_count, errors)
        - refined_count：status='refined' 的文件数
        - error_count：失败文件数（业务失败或异常）
        - memory_count：新增记忆数（L1.5 stored 累计；降级直存同样计入）
        - errors：错误列表 [{file_id, error}]（最多 _MAX_ERRORS_IN_REPORT 条）
    """
    new_files = session_dao.list_by_status(session_conn, "new", limit=max_files)
    refined_count = 0
    error_count = 0
    memory_count = 0
    errors: list[dict] = []
    for rf in new_files:
        file_id = rf["file_id"]
        try:
            # refine_one = refine_file（L1 抽取）→ persist_memories（L1.5 判决落库 + L2）
            result, l15_stats = pipeline_mod.refine_one(file_id, mem_conn, session_conn, cfg)
        except Exception as e:
            logger.warning("Dream 提炼文件 %s 异常（继续下一文件）: %s", file_id, e)
            error_count += 1
            if len(errors) < _MAX_ERRORS_IN_REPORT:
                errors.append({"file_id": file_id, "error": f"异常: {e}"})
            continue
        if result.status == "refined":
            refined_count += 1
            memory_count += int((l15_stats or {}).get("stored", 0) or 0)
        else:
            error_count += 1
            if len(errors) < _MAX_ERRORS_IN_REPORT:
                errors.append({
                    "file_id": file_id,
                    "error": result.error or f"提炼失败（status={result.status}）",
                })
    return refined_count, error_count, memory_count, errors


# ======================================================================
# ③ 生命周期：TTL 主动标记 + 冷归档
# ======================================================================

def _mark_expired_ttl(mem_conn: sqlite3.Connection) -> int:
    """TTL 主动标记：超 TTL 的 active 动态维度记忆 → expired。

    边界与查询层一致（§2.2 A 一致性保证）：
      ``julianday(updated_at) <= julianday('now') - ttl_days``
    保守边界：ttl_days IS NULL（静态维度/记忆级覆盖为不过期）不标记；
    status != 'active'（已 rejected/expired/archived）跳过。
    rejected_at 复用既有列做标记时间（语义扩展，见数据模型文档），
    reject_reason 固定 'dream_ttl_expired'。
    """
    cur = mem_conn.execute(
        """
        UPDATE memories
        SET status='expired', rejected_at=?, reject_reason='dream_ttl_expired'
        WHERE status='active' AND ttl_days IS NOT NULL
          AND julianday(updated_at) <= julianday('now') - ttl_days
        """,
        (_now_iso(),),
    )
    mem_conn.commit()
    return cur.rowcount


def _archive_old_raw_files(session_conn: sqlite3.Connection, archive_days: int) -> int:
    """冷归档：raw_files refined 且 started_at 距今 > archive_days → archived。

    文件不删、DB 行不删（原件永不删铁律）；仅状态流转，天然可重入。
    """
    cur = session_conn.execute(
        """
        UPDATE raw_files SET status='archived'
        WHERE status='refined' AND started_at IS NOT NULL
          AND julianday('now') - julianday(started_at) > ?
        """,
        (archive_days,),
    )
    session_conn.commit()
    return cur.rowcount


# ======================================================================
# ④ 日报：MD 落盘 + dream_reports 表 + signal_events
# ======================================================================

def _report_dir(cfg: dict[str, Any]) -> Path:
    """解析日报目录：相对路径按用户根定位（默认 data/reports/，T-23 跟随 SGME_HOME）。"""
    dream_cfg = cfg.get("dream", {}) or {}
    rd = (dream_cfg.get("report_dir") or "data/reports/").strip()
    p = Path(rd)
    if not p.is_absolute():
        p = config.USER_ROOT / p
    return p


def _count_scenes_since(mem_conn: sqlite3.Connection, since_ts: str) -> int:
    """本次运行新增场景数（scenes.created_at >= 运行起点）。"""
    row = mem_conn.execute(
        "SELECT COUNT(*) AS c FROM scenes WHERE created_at >= ?", (since_ts,)
    ).fetchone()
    return int(row["c"]) if row else 0


def _tokens_since(mem_conn: sqlite3.Connection, since_ts: str) -> int:
    """当日 refine_runs token 消耗合计（运行起点之后，含各阶段）。"""
    row = mem_conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM refine_runs WHERE started_at >= ?",
        (since_ts,),
    ).fetchone()
    return int(row["t"]) if row else 0


def _pool_totals(mem_conn: sqlite3.Connection) -> dict:
    """记忆池总量 / 场景总量（查询失败不阻塞日报，回退 0）。"""
    try:
        total_memories = mem_conn.execute(
            "SELECT COUNT(*) AS c FROM memories"
        ).fetchone()["c"]
        total_scenes = mem_conn.execute(
            "SELECT COUNT(*) AS c FROM scenes"
        ).fetchone()["c"]
        return {"total_memories": int(total_memories), "total_scenes": int(total_scenes)}
    except Exception as e:
        logger.warning("Dream 池总量统计失败（回退 0）: %s", e)
        return {"total_memories": 0, "total_scenes": 0}


def _render_report_md(
    date_label: str,
    stats: dict[str, Any],
    errors: list[dict],
    stage_errors: list[str],
    pool: dict[str, Any],
) -> str:
    """渲染阮一峰风格紧凑日报（设计文档 §2.3）。"""
    lines = [f"# Dream 日报 {date_label}", ""]
    lines.append("## 提炼")
    lines.append(f"- 处理文件：{stats['refined_count'] + stats['error_count']}"
                 f"（成功 {stats['refined_count']} / 失败 {stats['error_count']}）")
    lines.append(f"- 新增记忆：{stats['memory_count']}")
    lines.append(f"- 新增场景：{stats['scene_count']}")
    lines.append("")
    lines.append("## 生命周期")
    lines.append(f"- TTL 过期标记：{stats['expired_count']}")
    lines.append(f"- 冷归档：{stats['archived_count']}")
    lines.append(f"- 信号 TTL 归档：{stats['signal_purged_count']}")
    lines.append(f"- 关怀信号：{stats['care_signal_count']}")
    lines.append("")
    lines.append("## 异常")
    if errors:
        for e in errors:
            lines.append(f"- {e['file_id']}：{e['error']}")
    else:
        lines.append("- 无")
    if stage_errors:
        lines.append("")
        lines.append("### 阶段异常（该阶段中止，其余继续）")
        for s in stage_errors:
            lines.append(f"- {s}")
    lines.append("")
    lines.append("## 汇总")
    lines.append(f"- 记忆池总量：{pool['total_memories']}")
    lines.append(f"- 场景总量：{pool['total_scenes']}")
    lines.append(f"- Token 消耗：{stats['tokens']}")
    lines.append("")
    return "\n".join(lines)


def _write_report_md(cfg: dict[str, Any], date_label: str, md: str) -> str:
    """日报 MD 落盘（不入 git）。返回相对项目根路径（dream_reports.path 用）。"""
    d = _report_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"dream-{date_label}.md"
    path.write_text(md, encoding="utf-8")
    try:
        return str(path.relative_to(config.USER_ROOT))
    except ValueError:
        return str(path)


def _upsert_report_row(
    mem_conn: sqlite3.Connection,
    date_label: str,
    rel_path: str,
    stats: dict[str, Any],
    summary: str,
) -> None:
    """写入 dream_reports（同日期重复运行 = upsert 更新，按日一行）。"""
    mem_conn.execute(
        """
        INSERT INTO dream_reports
          (date, path, refined_count, memory_count, scene_count, error_count,
           expired_count, archived_count, summary, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
          path=excluded.path,
          refined_count=excluded.refined_count,
          memory_count=excluded.memory_count,
          scene_count=excluded.scene_count,
          error_count=excluded.error_count,
          expired_count=excluded.expired_count,
          archived_count=excluded.archived_count,
          summary=excluded.summary,
          created_at=excluded.created_at
        """,
        (date_label, rel_path, stats["refined_count"], stats["memory_count"],
         stats["scene_count"], stats["error_count"], stats["expired_count"],
         stats["archived_count"], summary, _now_iso()),
    )
    mem_conn.commit()


def _publish_dream_error(
    mem_conn: sqlite3.Connection,
    date_label: str,
    errors: list[dict],
    stage_errors: list[str],
) -> None:
    """error 级事件进 signal_events（{type:'dream_error', ...}，SCSM pull 可感知）。"""
    try:
        from sgme.signal import engine as signal_engine
        signal_engine.publish(
            event_type="dream_error",
            source="dream",
            payload={
                "date": date_label,
                "error_count": len(errors),
                "stage_error_count": len(stage_errors),
                "errors": errors[: _MAX_ERRORS_IN_REPORT],
                "stage_errors": stage_errors,
            },
            mem_conn=mem_conn,
        )
    except Exception as e:
        logger.warning("Dream 错误信号发布失败（不阻塞）: %s", e)


# ======================================================================
# 编排入口
# ======================================================================

def is_running() -> bool:
    """Dream 是否正在执行（触发端点防重入用）。"""
    return RUN_LOCK.locked()


def run_dream(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """四步编排（阻塞执行）。执行锁被占用 → 直接返回 running（防重入）。

    Args:
        mem_conn: memory.db 连接（L1.5/L2/日报/信号）。
        session_conn: session.db 连接（raw_files 队列）。
        cfg: 运行时配置（dream 段 + 提炼链路）。

    Returns:
        运行摘要 dict：date / status / refined_count / memory_count / scene_count /
        error_count / expired_count / archived_count / report_path / errors /
        stage_errors / total_memories / total_scenes / tokens。
    """
    if not RUN_LOCK.acquire(blocking=False):
        return {"status": "running", "message": "Dream 正在执行中（防重入）"}
    try:
        return _run_dream_locked(mem_conn, session_conn, cfg)
    finally:
        RUN_LOCK.release()


def run_dream_safe(
    data_dir: str | Path,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """后台线程执行体：自建连接 + 异常不抛出（日志 + 返回 error 摘要）。

    连接隔离（2026-08-14）：线程自建独立连接（init_databases(data_dir)），
    不共享宿主 app.state 连接——Windows 多线程共享 sqlite 连接存在 access
    violation 竞态（与 backup_scheduler 同款）。线程结束关闭自建连接。

    整体崩溃防线（§4.3）：异常只丢本次运行，status=new 幂等语义保证下次重扫。
    """
    from sgme.data import db as db_mod

    d = Path(data_dir) if data_dir else config.DATA_DIR
    mem_conn, session_conn, _ = db_mod.init_databases(d)
    try:
        return run_dream(mem_conn, session_conn, cfg)
    except Exception as e:
        logger.exception("Dream 运行异常（后台）: %s", e)
        return {"status": "error", "error": str(e)}
    finally:
        try:
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
        except Exception:
            pass


def _run_dream_locked(
    mem_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """执行锁已持有的四步编排体（run_dream 内部使用，勿直接调用）。"""
    dream_cfg = cfg.get("dream", {}) or {}
    max_files = int(dream_cfg.get("max_files", 200) or 200)
    archive_days = int(dream_cfg.get("archive_days", 90) or 90)
    ttl_mark = bool(dream_cfg.get("ttl_mark", True))
    start_ts = _now_iso()
    date_label = _local_date_label()
    errors: list[dict] = []
    stage_errors: list[str] = []

    # ① 抽取 + ② 判决（refine_one 内置）
    refined_count, error_count, memory_count, errors = _step_extract(
        mem_conn, session_conn, cfg, max_files,
    )
    try:
        scene_count = _count_scenes_since(mem_conn, start_ts)
    except Exception as e:
        logger.warning("Dream 场景计数失败（回退 0）: %s", e)
        scene_count = 0

    # ③ 生命周期（每阶段独立容错：该阶段失败中止，其余继续）
    expired_count = 0
    archived_count = 0
    signal_purged_count = 0
    if ttl_mark:
        try:
            expired_count = _mark_expired_ttl(mem_conn)
        except Exception as e:
            logger.exception("Dream TTL 主动标记失败（该阶段中止）: %s", e)
            stage_errors.append(f"TTL 主动标记失败: {e}")
    try:
        archived_count = _archive_old_raw_files(session_conn, archive_days)
    except Exception as e:
        logger.exception("Dream 冷归档失败（该阶段中止）: %s", e)
        stage_errors.append(f"冷归档失败: {e}")
    try:
        # 信号 TTL 归档（ST-27 T-62 收尾：信号是衍生数据非「原件」，超期物理删除）
        from sgme.data import signal_dao
        purged = signal_dao.purge_expired_signals(mem_conn)
        signal_purged_count = sum(purged.values())
    except Exception as e:
        logger.exception("Dream 信号 TTL 归档失败（该阶段中止）: %s", e)
        stage_errors.append(f"信号 TTL 归档失败: {e}")

    # 关怀信号扫描（ST-28：主动关怀闭环——信号自动产生，零 LLM 幂等去重）。
    # 受 care.enabled 控制（与 routes_care 挂载同开关）；消费仍由 agent 会话开始
    # signal_pull 拉取——SGME 只发信号不做决策（架构铁律）。
    care_signal_count = 0
    if (cfg.get("care") or {}).get("enabled", True):
        try:
            from sgme.care import signals as care_signals_mod
            care_stats = care_signals_mod.scan_care_signals(mem_conn, cfg)
            care_signal_count = sum(care_stats.values())
        except Exception as e:
            logger.exception("Dream 关怀信号扫描失败（该阶段中止）: %s", e)
            stage_errors.append(f"关怀信号扫描失败: {e}")

    # ④ 日报：汇总 → MD 落盘 → dream_reports → signal_events
    tokens = _tokens_since(mem_conn, start_ts)
    pool = _pool_totals(mem_conn)
    stats = {
        "refined_count": refined_count,
        "memory_count": memory_count,
        "scene_count": scene_count,
        "error_count": error_count,
        "expired_count": expired_count,
        "archived_count": archived_count,
        "signal_purged_count": signal_purged_count,
        "care_signal_count": care_signal_count,
        "tokens": tokens,
    }
    summary = (
        f"提炼 {refined_count} 文件 / 新增记忆 {memory_count} / 新增场景 {scene_count}"
        f" / TTL 过期 {expired_count} / 冷归档 {archived_count}"
        f" / 信号归档 {signal_purged_count} / 关怀信号 {care_signal_count} / 失败 {error_count}"
    )
    rel_path = _write_report_md(cfg, date_label, _render_report_md(
        date_label, stats, errors, stage_errors, pool,
    ))
    try:
        _upsert_report_row(mem_conn, date_label, rel_path, stats, summary)
    except Exception as e:
        logger.exception("Dream 日报落库失败（不阻塞）: %s", e)
        stage_errors.append(f"日报落库失败: {e}")
    if error_count > 0 or stage_errors:
        _publish_dream_error(mem_conn, date_label, errors, stage_errors)

    logger.info(
        "Dream 运行完成 date=%s refined=%d failed=%d memories=%d scenes=%d "
        "expired=%d archived=%d signal_purged=%d care_signal=%d report=%s",
        date_label, refined_count, error_count, memory_count, scene_count,
        expired_count, archived_count, signal_purged_count, care_signal_count, rel_path,
    )
    return {
        "date": date_label,
        "status": "done",
        "refined_count": refined_count,
        "memory_count": memory_count,
        "scene_count": scene_count,
        "error_count": error_count,
        "expired_count": expired_count,
        "archived_count": archived_count,
        "signal_purged_count": signal_purged_count,
        "care_signal_count": care_signal_count,
        "report_path": rel_path,
        "errors": errors,
        "stage_errors": stage_errors,
        "total_memories": pool["total_memories"],
        "total_scenes": pool["total_scenes"],
        "tokens": tokens,
    }


# ======================================================================
# 定时器（daemon 内常驻线程，复用 batch_scan 线程模式，不引入 APScheduler）
# ======================================================================

def _seconds_until(schedule: str) -> float:
    """距下次 HH:MM（本地时区）的秒数；非法格式回退 1 小时后重试。"""
    now = datetime.now()
    try:
        h, m = schedule.strip().split(":", 1)
        target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    except (ValueError, TypeError):
        logger.warning("Dream schedule 格式非法（1 小时后重试）: %r", schedule)
        return 3600.0
    delta = (target - now).total_seconds()
    if delta <= 0:
        delta += 86400.0
    return delta


def _scheduler_loop(
    cfg: dict[str, Any],
    stop_event: threading.Event | None = None,
    data_dir: str | Path | None = None,
) -> None:
    """定时器线程体：按 dream.schedule 到点执行 run_dream，循环。

    - schedule 为空（不自动只手动）：长眠 1 小时后复查（配置可被 API 修改）
    - enabled=false：到点跳过执行（开关可运行时切换）
    - stop_event：测试用（None = 生产常驻）

    v1.0 连接隔离修复（2026-08-11）：线程**自建独立连接**（init_databases），
    不再共享宿主连接——多调度器线程共享同一 sqlite 连接在 Windows 上有
    access violation 竞态（执行中连接被宿主关闭即原生崩溃）。线程退出时
    关闭自建连接；宿主关闭连接与本线程无关（不再需要 SELECT 1 探测宿主）。
    """
    from sgme.data import db as db_mod

    d = Path(data_dir) if data_dir else config.DATA_DIR
    mem_conn, session_conn, _ = db_mod.init_databases(d)
    try:
        while stop_event is None or not stop_event.is_set():
            dream_cfg = cfg.get("dream", {}) or {}
            schedule = dream_cfg.get("schedule", "03:00")
            if not schedule:
                if stop_event is not None:
                    if stop_event.wait(3600):
                        return
                else:
                    time.sleep(3600)
                continue
            wait = _seconds_until(schedule)
            if stop_event is not None:
                if stop_event.wait(wait):
                    return
            else:
                time.sleep(wait)
            if not (cfg.get("dream", {}) or {}).get("enabled", True):
                continue
            try:
                run_dream(mem_conn, session_conn, cfg)
            except Exception as e:
                logger.exception("Dream 定时执行异常（下次到点重试）: %s", e)
    finally:
        try:
            db_mod.close(mem_conn)
            db_mod.close(session_conn)
        except Exception:
            pass


def ensure_scheduler(
    cfg: dict[str, Any],
    data_dir: str | Path | None = None,
) -> bool:
    """幂等启动 Dream 定时器线程（daemon）。已启动返回 False。

    手动触发端点接线时调用；生产 Gateway 首次触发后定时器即常驻。
    cfg 为共享可变字典（app.state.cfg），配置改动下个周期生效。
    data_dir：线程自建连接的数据库目录（缺省 config.DATA_DIR）。
    """
    global _scheduler_thread, _scheduler_stop
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _scheduler_stop = threading.Event()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            args=(cfg, _scheduler_stop, data_dir),
            daemon=True,
            name="sgme-dream-scheduler",
        )
        _scheduler_thread.start()
        logger.info("Dream 定时器已启动（schedule=%s）", cfg.get("dream", {}).get("schedule"))
        return True


def stop_scheduler(timeout: float = 5.0) -> bool:
    """停止 Dream 定时器线程（幂等；测试/关停用，生产常驻可不调）。

    置位 stop_event 并 join 等待线程退出——线程当前可能在 sleep/wait
    长周期，join 超时返回 False（不强制杀，daemon 线程随进程退出）。
    """
    global _scheduler_thread, _scheduler_stop
    with _scheduler_lock:
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            _scheduler_thread = None
            return True
        if _scheduler_stop is not None:
            _scheduler_stop.set()
        _scheduler_thread.join(timeout)
        if not _scheduler_thread.is_alive():
            _scheduler_thread = None
            return True
        return False


# ======================================================================
# dream_reports 查询（路由层协议翻译用；ST-10 铁律下 SQL 内聚于此）
# ======================================================================

def list_reports(
    mem_conn: sqlite3.Connection,
    *,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """日报分页列表（date 倒序）。返回 (rows, total)。"""
    page = max(1, int(page))
    limit = min(200, max(1, int(limit)))
    total = mem_conn.execute("SELECT COUNT(*) AS c FROM dream_reports").fetchone()["c"]
    rows = mem_conn.execute(
        """
        SELECT date, path, refined_count, memory_count, scene_count, error_count,
               expired_count, archived_count, summary, created_at
        FROM dream_reports
        ORDER BY date DESC
        LIMIT ? OFFSET ?
        """,
        (limit, (page - 1) * limit),
    ).fetchall()
    return [dict(r) for r in rows], int(total)


def get_report(mem_conn: sqlite3.Connection, date: str) -> dict | None:
    """单日日报：DB 行 + MD 正文（文件缺失时 content 为空串，不报错）。"""
    row = mem_conn.execute(
        "SELECT * FROM dream_reports WHERE date=?", (date,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    content = ""
    try:
        p = Path(data["path"])
        if not p.is_absolute():
            p = config.USER_ROOT / p
        if p.exists():
            content = p.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Dream 日报正文读取失败（content 置空）: %s", e)
    data["content"] = content
    return data
