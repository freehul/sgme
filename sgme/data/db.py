"""storage/db.py：三库连接 + 建表 DDL + 迁移框架。

唯一操作 SQLite 数据库的层入口。所有 DAO 通过本模块获取连接。
- memory.db：memories/archive/tags/sources/registry/alias + refine_runs
  + memory_vectors/signal_events/signal_subscribers
  + **v0.7 迁入**：scenes/scene_vectors/scene_memories/scene_versions + memory_stats
- session.db（**v0.7 新建**）：raw_files + refine_cursor
- wiki.db（**v0.7 收缩**，扩展模块）：wiki_pages + wiki_links + **ingest_tasks（0.8 T-13）**
- 每库各自持有一份 schema_versions 登记表

DDL 累积式扩展（IF NOT EXISTS 幂等）；schema_versions 为迁移版本表。
- v1（minimal-closure）：基础六表 + raw_files
- v2（v0.4-completion T9）：scenes/scene_memories/scene_versions/memory_vectors/signal_events/signal_subscribers
- v3（#33 提示词版本管理）：refine_runs 审计表 + memories/memory_archive 补 prompt_version 列
- v4（中文检索分词 v0.3）：memories 补 content_seg 列（storage 只管列存在，内容由 search 回填）
- v0.7 三库拆分：raw_files → session.db；scenes 系列 → memory.db；新增 refine_cursor / memory_stats
  （存量数据搬运由项目根 `migrations/0001_split_three_dbs.py` 一次性完成，本模块只管表结构就绪）

分层铁律（勿越界）：本模块**只建普通表、只管「列的存在」**。
FTS5 虚拟表（memories_fts / scenes_fts / wiki_fts）及其同步触发器、分词回填、rebuild
一律归 search / wiki 模块（`sgme/search/init_fts`、`sgme/search/init_scenes_fts`），
db.py 保持零 FTS、零 search 依赖。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sgme import config

# 当前 schema 版本（每次 DDL 变更 +1）
SCHEMA_VERSION = 4

# memory.db 建表 DDL（v1 基础六表 + v2 向量/信号表 + v3 refine_runs/prompt_version + v4 content_seg）
MEMORY_DDL = """
CREATE TABLE IF NOT EXISTS dimension_registry (
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, category TEXT NOT NULL,
  time_velocity TEXT NOT NULL DEFAULT 'static', ttl_days INTEGER,
  description TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  boundaries TEXT);  -- T-11：维度边界（vs 对照消歧说明），YAML→DB 不再静默丢弃
CREATE TABLE IF NOT EXISTS dimension_alias (
  alias TEXT PRIMARY KEY, dimension_id TEXT NOT NULL REFERENCES dimension_registry(id));
CREATE TABLE IF NOT EXISTS memories (
  memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL,
  priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, ttl_days INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, agent_tag TEXT,
  prompt_version TEXT, content_seg TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  rejected_at TEXT, reject_reason TEXT,
  occurred_at TEXT);   -- v0.5：记忆对应会话事件的真实发生时刻（vs created_at=提炼落库时刻）
CREATE TABLE IF NOT EXISTS memory_archive (
  memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL,
  priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, ttl_days INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, agent_tag TEXT,
  prompt_version TEXT, archived_at TEXT NOT NULL, superseded_by TEXT,
  occurred_at TEXT);
CREATE TABLE IF NOT EXISTS memory_tags (
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  dimension_id TEXT NOT NULL REFERENCES dimension_registry(id),
  PRIMARY KEY (memory_id, dimension_id));
CREATE TABLE IF NOT EXISTS memory_sources (
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  source_ref TEXT NOT NULL,
  source_type TEXT NOT NULL,
  PRIMARY KEY (memory_id, source_ref));  -- 2026-08-16 T-69：同记忆同源唯一（幂等写入防御）
CREATE INDEX IF NOT EXISTS idx_tags_dim ON memory_tags(dimension_id, memory_id);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_priority ON memories(priority DESC);
CREATE INDEX IF NOT EXISTS idx_sources_mem ON memory_sources(memory_id);

-- v2 新增：记忆向量索引（/search 向量检索 + RRF 融合用，T13 接入）
CREATE TABLE IF NOT EXISTS memory_vectors (
  memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
  embedding BLOB NOT NULL,
  model TEXT NOT NULL,
  dims INTEGER NOT NULL,
  embedded_at TEXT NOT NULL);

-- v2 新增：信号引擎事件持久化（T11 接入；push/pull 重连补偿用）
CREATE TABLE IF NOT EXISTS signal_events (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  source TEXT NOT NULL,
  payload TEXT NOT NULL,
  ts TEXT NOT NULL,
  consumed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_signal_events_ts ON signal_events(ts);
CREATE INDEX IF NOT EXISTS idx_signal_events_type ON signal_events(type, ts);

-- v2 新增：订阅者持久游标（pull 模式重连补偿，T11 接入）
CREATE TABLE IF NOT EXISTS signal_subscribers (
  subscriber_id TEXT PRIMARY KEY,
  last_signal_id TEXT,
  last_consumed_ts TEXT);

-- v3 新增：提炼批次审计（#33 提示词版本管理，逐批记录 stage/version/variant/provider/counts/status）
CREATE TABLE IF NOT EXISTS refine_runs (
  run_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  version TEXT NOT NULL,
  variant TEXT,
  provider TEXT NOT NULL,
  bucket_key TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  memories_count INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER,       -- v0.5：LLM 用量记账（DeepSeek 统计）
  completion_tokens INTEGER,
  total_tokens INTEGER,
  action_counts TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'running',
  error TEXT);
CREATE INDEX IF NOT EXISTS idx_refine_runs_stage ON refine_runs(stage, version, started_at);
CREATE INDEX IF NOT EXISTS idx_refine_runs_file ON refine_runs(file_id, started_at);

-- ============ v0.7 三库拆分（B2）：L2 场景系列由 wiki.db 迁入 memory.db ============
-- ⚠️ 此处**只追加普通表**。scenes_fts 虚拟表与 scenes_ai/ad/au 触发器【不在此处】——
--    归 search 层 `sgme/search/init_scenes_fts()`，与 memories_fts 对称。
--    若在此抢先建 scenes_fts，init_scenes_fts 会立刻 DROP 重建（重复建表 + 职责错位）。

-- L2 场景叙事文档（精炼层标准形态，T10 接入）
-- content_seg：jieba 分词列，storage 只管列存在，内容由 search 回填
CREATE TABLE IF NOT EXISTS scenes (
  scene_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  heat INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_memory_added_at TEXT,
  content_seg TEXT);
CREATE INDEX IF NOT EXISTS idx_scenes_status ON scenes(status, updated_at DESC);

-- 场景向量（L2 语义检索，对称 memory_vectors；普通表，embedding 存 BLOB，非 vec0 虚拟表）
CREATE TABLE IF NOT EXISTS scene_vectors (
  scene_id TEXT PRIMARY KEY REFERENCES scenes(scene_id),
  embedding BLOB NOT NULL,
  model TEXT NOT NULL,
  dims INTEGER NOT NULL,
  embedded_at TEXT NOT NULL);

-- 场景-记忆双向溯源（D8 决策 4）
CREATE TABLE IF NOT EXISTS scene_memories (
  scene_id TEXT NOT NULL REFERENCES scenes(scene_id),
  memory_id TEXT NOT NULL,
  PRIMARY KEY (scene_id, memory_id));
CREATE INDEX IF NOT EXISTS idx_scene_memories_memory ON scene_memories(memory_id);

-- 场景历史快照（L2 UPDATE/MERGE 前置归档，提供场景级 diff 审计）
CREATE TABLE IF NOT EXISTS scene_versions (
  version_id TEXT PRIMARY KEY,
  scene_id TEXT NOT NULL REFERENCES scenes(scene_id),
  content TEXT NOT NULL,
  version_at TEXT NOT NULL,
  reason TEXT);

-- v0.7 新增：记忆使用统计 sidecar（A2 决策）
-- 只在 inject 链路写 last_injected_at / recall_count（best-effort，不阻塞主流程）；
-- last_recalled_at 为**预留字段（v0.8 待定）**——search 命中链路禁止挂载任何写操作。
CREATE TABLE IF NOT EXISTS memory_stats (
  memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
  last_recalled_at TEXT,
  recall_count INTEGER DEFAULT 0,
  last_injected_at TEXT);
"""

# session.db 建表 DDL（v0.7 新建，B1）：引擎调度状态库
# raw_files 由 wiki.db 迁入；refine_cursor 为 v0.7 新增（A3：阶段 1 只建表，不写 DAO、不接调度）
SESSION_DDL = """
CREATE TABLE IF NOT EXISTS raw_files (
  file_id TEXT PRIMARY KEY, path TEXT NOT NULL, session_key TEXT NOT NULL,
  agent_id TEXT, agent_model TEXT, started_at TEXT, ended_at TEXT, refined_at TEXT,
  last_refined_seq INTEGER, status TEXT NOT NULL DEFAULT 'new', size INTEGER,
  content_hash TEXT);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_files(status, refined_at);

-- 按日批次调度游标（D6：与 raw_files.last_refined_seq 并存互补，二者不是同一套东西）
-- A3：阶段 1 **仅建表**，初始为空；DAO 与调度接入见阶段 3 P3-T5。
CREATE TABLE IF NOT EXISTS refine_cursor (
  namespace TEXT NOT NULL,
  date_label TEXT NOT NULL,
  cursor_at TEXT,
  status TEXT DEFAULT 'pending',
  retry_count INTEGER DEFAULT 0,
  last_error TEXT,
  updated_at TEXT,
  PRIMARY KEY (namespace, date_label));
"""

# wiki.db 建表 DDL（v0.7 收缩，B3）：仅 wiki 扩展模块自有普通表
# raw_files → session.db；scenes 系列 → memory.db（见 SESSION_DDL / MEMORY_DDL）。
# ⚠️ wiki_fts 虚拟表【不进此处】：由阶段 3 wiki 模块自建（对称 init_scenes_fts）。
WIKI_DDL = """
CREATE TABLE IF NOT EXISTS wiki_pages (
  page_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  category TEXT,
  tags TEXT,
  source_type TEXT,
  source_url TEXT,
  source_file TEXT,
  ingested_at TEXT,
  updated_at TEXT,
  content_seg TEXT,
  description TEXT,
  description_seg TEXT,
  author TEXT,
  status TEXT DEFAULT 'active',
  supersedes TEXT);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_updated ON wiki_pages(updated_at DESC);

CREATE TABLE IF NOT EXISTS wiki_links (
  source_id TEXT,
  target_id TEXT,
  rel_type TEXT,
  confidence REAL,
  source TEXT,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_wiki_links_source ON wiki_links(source_id);
"""

# schema 版本表（迁移框架）
SCHEMA_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_versions (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL);
"""

# ============ 0.8 ST-15：需求池 demands（memory.db，独立 DDL 常量） ============
# 状态流转：pending（未立项）→ planned（已立项）→ partial（部分解决）→ done（已解决）。
# ⚠️ 刻意**不加外键**：
#   - project_id → project_meta 由 ST-16 并行创建；连接开了 PRAGMA foreign_keys=ON，
#     声明指向尚不存在的表会让 demands 的任何 DML 直接抛 "no such table"；
#   - origin_idea_id → memories 的记忆会被 Supersession 归档/替换，外键会阻塞归档
#     或级联抹掉溯源（设计文档 §2 ④「归档 FK 崩溃」教训）。
#   存在性改由 operations 层软校验（project_meta 就绪则校验，未就绪则放行）。
DEMANDS_DDL = """
CREATE TABLE IF NOT EXISTS demands (
  demand_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER NOT NULL DEFAULT 50,
  project_id TEXT,
  origin_idea_id TEXT,
  source_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT);
CREATE INDEX IF NOT EXISTS idx_demands_status_priority ON demands(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_demands_updated ON demands(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_demands_project ON demands(project_id);
CREATE INDEX IF NOT EXISTS idx_demands_origin_idea ON demands(origin_idea_id);
"""

# ---------- 0.8 ST-16：项目注册表（memory.db，独立 DDL 常量） ----------
# 图纸：`SGME-数据模型设计-v0.1.md` §二 project_meta + `SGME-创意池与需求池设计-v0.1.md` §3 ③。
# 轻量元数据（项目名 | 路径 | git 仓库 | 最近活跃 | 当前里程碑），登记入口
# `scripts/project_init.py` 六步之④ + `POST /v1/admin/projects`。
# 索引：仅主键 project_id（数据模型 §三索引清单亦只列 PK）——注册表基数 = 项目数（~10²），
# 过滤/排序全表扫描代价可忽略，加二级索引只会放大写入成本，属过度设计。
PROJECT_META_DDL = """
CREATE TABLE IF NOT EXISTS project_meta (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  git_repo TEXT,
  last_active_at TEXT,
  milestone TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
"""

# ---------- T-56：创意池独立表 ideas（memory.db，独立 DDL 常量） ----------
# 2026-08-14「维度独立日」：创意从 memories 打标独立为 ideas 表——完全由
# 用户/接入 agent 掌控（idea_add / /v1/admin/ideas*），LLM 提炼不再写创意。
# 无 content_seg/FTS：创意不进 /v1/search（人工管理资产，ideas API 专属浏览），
# 列表 q 过滤走 LIKE 子串。origin_memory_id 记录迁移溯源（原 memories.memory_id）。
IDEAS_DDL = """
CREATE TABLE IF NOT EXISTS ideas (
  idea_id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  custom_flag TEXT,
  reject_reason TEXT,
  rejected_at TEXT,
  source_ref TEXT,
  origin_memory_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ideas_updated ON ideas(updated_at DESC);
"""

# ---------- ST-27 T-57：信号消费回执表（memory.db，独立 DDL 常量） ----------
# 三层消费模型（广播 pull → 认领原子抢 → 回执 signal_acks）：
# - signal_events.consumed_at 仍为「已认领」标记（原子认领 WHERE consumed_at IS NULL）
# - signal_acks 记录「谁认领 + 结果」——claimed 认领未处理完 / acked 成功 / failed 失败
# - (event_id, agent_id) 复合主键：同一信号可被多 agent 认领（广播语义），各留回执
SIGNAL_ACKS_DDL = """
CREATE TABLE IF NOT EXISTS signal_acks (
  event_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,
  claimed_at TEXT NOT NULL,
  acked_at TEXT,
  PRIMARY KEY (event_id, agent_id));
CREATE INDEX IF NOT EXISTS idx_signal_acks_agent ON signal_acks(agent_id, claimed_at DESC);
"""

# ---------- 0.8 T-13：ingest 任务持久化（wiki.db，独立 DDL 常量） ----------
# 图纸：`SGME-数据模型设计-v0.1.md` §二 wiki.db → ingest_tasks。
# 原 `_TASKS` 进程内内存字典 → SQLite 表：任务创建/状态流转/查询全落库，
# 服务重启后 queued/running 任务按数据模型语义恢复（queued 可重跑 / running 标记中断）。
# 索引：status + updated_at（守护重试策略按状态扫描 + 时间排序）。
INGEST_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS ingest_tasks (
  task_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  title TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  result_page_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT);
CREATE INDEX IF NOT EXISTS idx_ingest_tasks_status ON ingest_tasks(status, updated_at DESC);
"""

# ---------- 0.8 ST-10：Dream 日报（memory.db，独立 DDL 常量） ----------
# 图纸：`SGME-数据模型设计-v0.1.md` §二 dream_reports（ST-10）。
# date 即 PK（YYYYMMDD，本地日期）——数据模型 §三索引清单只列 `dream_reports(date)` PK，
# 日报倒序直接走 PK 扫描，不另建二级索引（按日一行，基数极小）。
# summary 为紧凑一行摘要（阮一峰风格日报正文落盘在 path 指向的 MD 文件）。
DREAM_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS dream_reports (
  date TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  refined_count INTEGER NOT NULL DEFAULT 0,
  memory_count INTEGER NOT NULL DEFAULT 0,
  scene_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  expired_count INTEGER NOT NULL DEFAULT 0,
  archived_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT,
  created_at TEXT NOT NULL);
"""


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: Path) -> sqlite3.Connection:
    """打开 SQLite 连接：启用 WAL、外键、row_factory。

    check_same_thread=False：FastAPI 把同步端点丢到 threadpool 执行，
    连接需跨线程复用。SQLite WAL 模式 + 单用户 Server 场景下安全
   （读不阻塞、写由 SQLite 文件锁串行）。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_schema(conn: sqlite3.Connection, ddl: str, version: int, name: str) -> None:
    """执行 DDL 并记录 schema 版本（幂等：版本已存在则跳过 DDL 写入版本行）。"""
    conn.executescript(ddl)
    conn.executescript(SCHEMA_VERSIONS_DDL)
    cur = conn.execute(
        "SELECT version FROM schema_versions WHERE version=?", (version,)
    )
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO schema_versions (version, name, applied_at) VALUES (?,?,?)",
            (version, name, _now_iso()),
        )
        conn.commit()


def connect_memory(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """打开 memory.db 连接并确保 schema 就绪。"""
    d = Path(data_dir) if data_dir else config.DATA_DIR
    conn = _connect(d / "memory.db")
    _ensure_schema(conn, MEMORY_DDL, SCHEMA_VERSION, "memory_v4")
    _migrate_mem_prompt_version(conn)
    _migrate_mem_content_seg(conn)
    _migrate_mem_status(conn)
    _migrate_mem_occurred_at(conn)
    _migrate_refine_runs_tokens(conn)
    _migrate_mem_idea_columns(conn)
    _migrate_dim_boundaries(conn)
    _migrate_demands_table(conn)
    _migrate_project_meta_table(conn)
    _migrate_dream_reports_table(conn)
    _migrate_ideas_table(conn)
    _migrate_signal_consumed_by(conn)
    _migrate_signal_acks_table(conn)
    return conn


def connect_session(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """打开 session.db 连接并确保 schema 就绪（v0.7 新建，B1）。

    session.db 承载引擎调度状态：raw_files（会话原文索引）+ refine_cursor（按日批次游标）。
    与 connect_memory 同模式：`_ensure_schema` 幂等建表 + 登记 schema 版本。
    版本策略（§2.5.1）：沿用全局 `SCHEMA_VERSION` 整数，label 独立为 `session_v1`，
    与 memory_v4 / wiki_v4 在各自库的 schema_versions 中互不覆盖。
    """
    d = Path(data_dir) if data_dir else config.DATA_DIR
    conn = _connect(d / "session.db")
    _ensure_schema(conn, SESSION_DDL, SCHEMA_VERSION, "session_v1")
    _migrate_session_agent_model(conn)
    return conn


def connect_wiki(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """打开 wiki.db 连接并确保 schema 就绪（v0.7 收缩为 wiki_pages/wiki_links；0.8 T-13 增 ingest_tasks）。

    B4：不再调用 `_migrate_wiki_raw_files` / `_migrate_wiki_scene_seg`——
    raw_files 已迁至 session.db（content_hash 已含在 SESSION_DDL），
    scenes 系列已迁至 memory.db；旧 wiki.db 内的同名表由 migrations/0001 一次性搬运，
    并按 D5 保留为归档（不 DROP）。
    """
    d = Path(data_dir) if data_dir else config.DATA_DIR
    conn = _connect(d / "wiki.db")
    _ensure_schema(conn, WIKI_DDL, SCHEMA_VERSION, "wiki_v4")
    _migrate_ingest_tasks_table(conn)
    _migrate_wiki_page_columns(conn)
    _migrate_wiki_evolve_table(conn)
    return conn


def _migrate_wiki_page_columns(conn: sqlite3.Connection) -> None:
    """老库迁移：wiki_pages 补 description/description_seg/author/status/supersedes 列
    （wiki 渐进式披露 W1，2026-08-16，方案 v0.3 §5.1）。

    分层职责（照 _migrate_mem_content_seg 先例）：本函数只 ALTER 补空列；
    列内容（description_seg 分词）由 wiki_dao 写入时计算，FTS 重建由
    wiki/fts.py init_wiki_fts 负责。status 默认 'active'（多 agent 共享
    supersession 基础）。幂等：列已存在则无操作；表不存在时 no-op。
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_pages'"
    ).fetchone()
    if not has_table:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
    for col, ddl in (
        ("description", "ALTER TABLE wiki_pages ADD COLUMN description TEXT"),
        ("description_seg", "ALTER TABLE wiki_pages ADD COLUMN description_seg TEXT"),
        ("author", "ALTER TABLE wiki_pages ADD COLUMN author TEXT"),
        ("status", "ALTER TABLE wiki_pages ADD COLUMN status TEXT DEFAULT 'active'"),
        ("supersedes", "ALTER TABLE wiki_pages ADD COLUMN supersedes TEXT"),
    ):
        if col not in cols:
            conn.execute(ddl)
    conn.commit()


def _migrate_wiki_evolve_table(conn: sqlite3.Connection) -> None:
    """老库迁移：wiki.db 建 wiki_evolve 表（W4 自进化独立游标，2026-08-16）。

    照 _migrate_ingest_tasks_table 先例：CREATE TABLE IF NOT EXISTS 幂等；
    与 memory 提炼的 refine_cursor 完全分离（P0-2 修正：不复用同一水位）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_evolve (
          session_key TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'queued',
          action TEXT,
          entry_hash TEXT,
          page_id TEXT,
          error TEXT,
          created_at TEXT,
          processed_at TEXT)
        """
    )
    conn.commit()


def _migrate_wiki_scene_seg(conn: sqlite3.Connection) -> None:
    """【已废弃 v0.7】老库迁移：scenes 补 content_seg 列（场景检索升级 v5，2026-08-07）。

    废弃原因（B4）：scenes 已迁入 memory.db，且 MEMORY_DDL 建表即含 content_seg 列，
    不存在「memory.db 有 scenes 但缺 content_seg」的历史库形态。
    保留函数体仅为兼容旧 wiki.db 的手工运维场景，**连接链路不再调用**。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(scenes)").fetchall()]
    if "content_seg" not in cols:
        conn.execute("ALTER TABLE scenes ADD COLUMN content_seg TEXT")
        conn.commit()


def _migrate_mem_content_seg(conn: sqlite3.Connection) -> None:
    """老库迁移：memories 补 content_seg 列（中文检索分词 v0.3，2026-08-06）。

    分层职责（缺口 C）：storage 只管**列的存在**——本函数只 ALTER 补空列，
    不计算 `content_seg` 内容、不 import `segment()`（仍零 import search）。
    列的内容（分词回填 + FTS 重建）由 search 侧 `init_fts._ensure_fts_ready` 负责。
    幂等：列已存在则无操作；重复执行安全。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "content_seg" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN content_seg TEXT")
        conn.commit()


def _migrate_session_agent_model(conn: sqlite3.Connection) -> None:
    """老库迁移：raw_files 补 agent_model 列（T-43 提炼动态链跟随 agent LLM）。

    幂等：列已存在则无操作；重复执行安全。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_files)").fetchall()]
    if "agent_model" not in cols:
        conn.execute("ALTER TABLE raw_files ADD COLUMN agent_model TEXT")
        conn.commit()


def _migrate_mem_prompt_version(conn: sqlite3.Connection) -> None:
    """老库迁移：memories/memory_archive 补 prompt_version 列（#33，2026-08-05）。

    仿 _migrate_wiki_raw_files 先例：已有表（无该列）时 ALTER TABLE ADD COLUMN；
    新库 DDL 已含，跳过。幂等：列已存在则无操作；重复执行安全。
    """
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "prompt_version" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN prompt_version TEXT")
            conn.commit()


def _migrate_mem_status(conn: sqlite3.Connection) -> None:
    """老库迁移：memories 补 status/rejected_at/reject_reason 列（用户纠错「不采用」标记，2026-08-06）。

    用户反馈"这条记忆是错的"时打 rejected 标记（不删除、可恢复）；
    查询/加载/搜索一律过滤 status='rejected'。幂等：列已存在则无操作。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        conn.execute("ALTER TABLE memories ADD COLUMN rejected_at TEXT")
        conn.execute("ALTER TABLE memories ADD COLUMN reject_reason TEXT")
        conn.commit()


def _migrate_mem_occurred_at(conn: sqlite3.Connection) -> None:
    """老库迁移：memories/memory_archive 补 occurred_at 列（v0.5，2026-08-06）。

    记忆对应会话事件的真实发生时刻（vs created_at=提炼落库时刻）。
    幂等：列已存在则无操作。
    """
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "occurred_at" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN occurred_at TEXT")
            conn.commit()


def _migrate_refine_runs_tokens(conn: sqlite3.Connection) -> None:
    """老库迁移：refine_runs 补 token 记账列（v0.5，2026-08-06）。幂等。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    for col in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if col not in cols:
            conn.execute(f"ALTER TABLE refine_runs ADD COLUMN {col} INTEGER")
    conn.commit()


def _migrate_mem_idea_columns(conn: sqlite3.Connection) -> None:
    """老库迁移：memories 补 notes / custom_flag 列（创意池 ST-14，2026-08-08）。

    创意池不建独立表——「创意 = 带 `ideas` 维度、`ttl_days IS NULL` 的记忆」，
    人工修正只需在 memories 上补两列：
    - notes       TEXT：追加式人工备注，JSON 数组 `[{"ts": ISO, "text": ...}]`；
    - custom_flag TEXT：用户自由文本标记（无固定枚举；升格用 `promoted`）。

    仿 `_migrate_mem_status` 先例：**只补 memories，不补 memory_archive**。
    依据（读基线归档 INSERT 得证）：`memory_dao.archive_memory` 用**显式列名**
    落归档，列表为 (memory_id, content, memory_type, priority, time_velocity,
    ttl_days, created_at, updated_at, agent_tag, prompt_version, archived_at,
    superseded_by, occurred_at)——**不含 notes / custom_flag**（正如它也不含
    status / content_seg，故那两列同样只加在 memories）。归档面是「被取代记忆的
    冷备」，不承载人工修正态；即便给 memory_archive 加了这两列，归档 INSERT
    也不会写入它们（且 archive_memory 在非白名单的 memory_dao.py，不可改），
    徒增无值空列。故按证据只加 memories。

    幂等：列已存在则无操作；重复执行安全。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "notes" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN notes TEXT")
    if "custom_flag" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN custom_flag TEXT")
    conn.commit()


def _migrate_wiki_raw_files(conn: sqlite3.Connection) -> None:
    """【已废弃 v0.7】老库迁移：raw_files 补 content_hash 列（Task 6，2026-08-04）。

    废弃原因（B4）：raw_files 已迁至 session.db，`SESSION_DDL` 建表即含 content_hash 列。
    保留函数体仅为 migrations/0001 读取旧 wiki.db 时的兼容兜底，**连接链路不再调用**。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_files)").fetchall()]
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE raw_files ADD COLUMN content_hash TEXT")
        conn.commit()


def _migrate_dim_boundaries(conn: sqlite3.Connection) -> None:
    """老库迁移：dimension_registry 补 boundaries 列（T-11，维度边界保留）。

    幂等：列已存在则无操作。boundaries = 维度间语义消歧说明（vs 对照），
    registry/dimensions.yaml 定义、import 时随行写入——此前无列导致静默丢弃，
    L1 提示词只拿到 id：display_name（审计 D8）。
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dimension_registry'"
    ).fetchone()
    if not has_table:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(dimension_registry)").fetchall()]
    if "boundaries" not in cols:
        conn.execute("ALTER TABLE dimension_registry ADD COLUMN boundaries TEXT")
        conn.commit()


def _migrate_demands_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建 demands 需求池表 + 索引（0.8 ST-15，2026-08-09）。幂等。

    demands 不进 MEMORY_DDL 而独立成 `DEMANDS_DDL` + 本迁移函数，与
    `_migrate_mem_*` 系列同模式：老库（schema_versions 已登记 memory_v4）不会重跑
    MEMORY_DDL 之外的变更，必须走迁移函数才能补齐新表——`_ensure_schema` 虽每次
    executescript，但把 0.8 新表塞进 v4 的 DDL 常量会让「版本 ↔ 表集合」的对应关系失真。

    幂等性：CREATE TABLE / CREATE INDEX 全部 IF NOT EXISTS，重复调用无副作用。
    """
    conn.executescript(DEMANDS_DDL)
    conn.commit()


def _migrate_dream_reports_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建 dream_reports 日报表（0.8 ST-10，2026-08-10）。幂等。

    与 `_migrate_demands_table` 同模式：dream_reports 不进 MEMORY_DDL 而独立成
    `DREAM_REPORTS_DDL` + 本迁移函数——老库（schema_versions 已登记 memory_v4）
    不会重跑 MEMORY_DDL 之外的变更，必须走迁移函数才能补齐新表。

    幂等性：CREATE TABLE IF NOT EXISTS，重复调用无副作用。
    """
    conn.executescript(DREAM_REPORTS_DDL)
    conn.commit()


def _migrate_ideas_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建创意池独立表 ideas（T-56，2026-08-14）。幂等。

    与 `_migrate_demands_table` 同模式：ideas 不进 MEMORY_DDL 而独立成
    `IDEAS_DDL` + 本迁移函数——老库（schema_versions 已登记 memory_v4）
    不会重跑 MEMORY_DDL 之外的变更，必须走迁移函数才能补齐新表。

    幂等性：CREATE TABLE IF NOT EXISTS，重复调用无副作用。
    """
    conn.executescript(IDEAS_DDL)
    conn.commit()


def _migrate_signal_consumed_by(conn: sqlite3.Connection) -> None:
    """老库迁移：signal_events 补 consumed_by 列（ST-27 T-57，原子认领溯源）。

    幂等：列已存在则无操作。consumed_by 记录「谁认领消费了这条信号」，
    配合 signal_acks 回执表可完整溯源（认领人 + 处理结果）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signal_events)").fetchall()]
    if "consumed_by" not in cols:
        conn.execute("ALTER TABLE signal_events ADD COLUMN consumed_by TEXT")
        conn.commit()


def _migrate_signal_acks_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建信号消费回执表 signal_acks（ST-27 T-57）。幂等。

    与 _migrate_ideas_table 同模式：signal_acks 不进 MEMORY_DDL 而独立成
    SIGNAL_ACKS_DDL + 本迁移函数——老库（schema_versions 已登记 memory_v4）
    不会重跑 MEMORY_DDL 之外的变更，必须走迁移函数才能补齐新表。
    """
    conn.executescript(SIGNAL_ACKS_DDL)
    conn.commit()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """返回该库所有用户表名（不含 sqlite_ 前缀）。"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r["name"] for r in cur.fetchall()]


def schema_version(conn: sqlite3.Connection) -> int | None:
    """返回当前 schema 版本号。"""
    cur = conn.execute("SELECT MAX(version) AS v FROM schema_versions")
    row = cur.fetchone()
    return row["v"] if row else None


def close(conn: sqlite3.Connection) -> None:
    """安全关闭连接。"""
    try:
        conn.commit()
    finally:
        conn.close()


def init_databases(
    data_dir: str | Path | None = None,
) -> tuple[sqlite3.Connection, sqlite3.Connection, sqlite3.Connection]:
    """初始化三库（建表 + 版本登记）。返回 `(mem_conn, session_conn, wiki_conn)`。

    ⚠️ v0.7 破坏性变更：返回值由 2 元组 `(mem, wiki)` 改为 3 元组。
    所有解包点必须同步改造，否则运行时 `ValueError: too many values to unpack`。
    """
    mem = connect_memory(data_dir)
    session = connect_session(data_dir)
    wiki = connect_wiki(data_dir)
    return mem, session, wiki


# ---------- D1：三库连接的对外统一命名（connect_* 保留为等价别名） ----------

def get_memory_conn(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """memory.db：记忆池 + L2 场景系列 + 向量 + 信号 + 提炼审计 + memory_stats。"""
    return connect_memory(data_dir)


def get_session_conn(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """session.db：raw_files + refine_cursor（连接即建表；A3 阶段 1 只建表不接调度）。"""
    return connect_session(data_dir)


def get_wiki_conn(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """wiki.db：wiki_pages + wiki_links（wiki 扩展模块）。

    D4（`wiki.enabled=false` 时返回 None）依赖 `config.yaml` 的 `wiki` 段，
    该配置段随阶段 4 §5.2 落地；阶段 1 保持始终返回真实连接，行为与改造前一致。
    """
    return connect_wiki(data_dir)


# ---------- 0.8 ST-16：project_meta 迁移 ----------

def _migrate_project_meta_table(conn: sqlite3.Connection) -> None:
    """新库/老库迁移：memory.db 建 project_meta 项目注册表（0.8 ST-16，2026-08-09）。

    幂等：DDL 全部 `CREATE TABLE IF NOT EXISTS`，重复调用无副作用。
    独立于 MEMORY_DDL 单独执行（而非并进 MEMORY_DDL 字符串），
    是为压缩 0.8 三个并行任务（ST-14/ST-15/ST-16）在本文件的冲突面。

    不 bump `SCHEMA_VERSION`：与既有 `_migrate_mem_*` 系列一致——
    版本号是三库共享的全局常量，并行任务各自 +1 必然互相覆盖；
    表结构就绪与否由 IF NOT EXISTS 自证，不依赖版本号。
    """
    conn.executescript(PROJECT_META_DDL)
    conn.commit()


# ---------- 0.8 T-13：ingest_tasks 迁移 ----------

def _migrate_ingest_tasks_table(conn: sqlite3.Connection) -> None:
    """新库/老库迁移：wiki.db 建 ingest_tasks 任务表（0.8 T-13，2026-08-10）。

    ingest_tasks 不进 WIKI_DDL 而独立成 `INGEST_TASKS_DDL` + 本迁移函数，与
    `_migrate_demands_table` / `_migrate_project_meta_table` 同模式：老库
    （schema_versions 已登记 wiki_v4）不会重跑 WIKI_DDL 之外的变更，必须走迁移
    函数才能补齐新表——`_ensure_schema` 虽每次 executescript，但把 0.8 新表塞进
    v4 的 DDL 常量会让「版本 ↔ 表集合」的对应关系失真。

    幂等性：CREATE TABLE / CREATE INDEX 全部 IF NOT EXISTS，重复调用无副作用。
    """
    conn.executescript(INGEST_TASKS_DDL)
    conn.commit()
