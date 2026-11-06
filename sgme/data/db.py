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

import logging
import sqlite3
from datetime import UTC
from pathlib import Path

from sgme import config

logger = logging.getLogger("sgme.data.db")

# 当前 schema 版本（每次 DDL 变更 +1）
# v5（2026-11-04 · SGME 2.0 迁移阶段 1「加列」）：memories/memory_archive 补
# lifecycle / ttl_renew_count / ttl_renewed_at / inject_tier / meaning_anchor 五列；
# dimension_registry 补 priority（D-53 R1 主维度选取）；新增 ttl_disposition（正式表，
# 含 reason）/ deletion_audit / rule_sync_ledger / rule_sync_daily 四表。
# 口径见 docs/design/SGME2.0-设计方案-V1.0.md §9.2 阶段 1。
SCHEMA_VERSION = 6

# memory.db 建表 DDL（v1 基础六表 + v2 向量/信号表 + v3 refine_runs/prompt_version + v4 content_seg）
MEMORY_DDL = """
CREATE TABLE IF NOT EXISTS dimension_registry (
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, category TEXT NOT NULL,
  time_velocity TEXT NOT NULL DEFAULT 'static', ttl_days INTEGER,
  description TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  boundaries TEXT,  -- T-11：维度边界（vs 对照消歧说明），YAML→DB 不再静默丢弃
  priority INTEGER);  -- D-53 R1（2026-11-04）：主维度选取用，数值越小越靠近身份锚
                      -- （身份 10 / 家庭 20 / ... / 创意 130；种子值见 resources/registry/dimensions.yaml）
CREATE TABLE IF NOT EXISTS dimension_alias (
  alias TEXT PRIMARY KEY, dimension_id TEXT NOT NULL REFERENCES dimension_registry(id));
CREATE TABLE IF NOT EXISTS memories (
  memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL,
  priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, ttl_days INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, agent_tag TEXT,
  prompt_version TEXT, content_seg TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  rejected_at TEXT, reject_reason TEXT,
  occurred_at TEXT,   -- v0.5：记忆对应会话事件的真实发生时刻（vs created_at=提炼落库时刻）
  -- ---- 2.0 三轴分离（D-46）与时效五档（D-52）载体（迁移阶段 1，2026-11-04）----
  lifecycle TEXT,                       -- active / sleeping / archived；NULL 按注入谓词兜底视为 active
  ttl_renew_count INTEGER NOT NULL DEFAULT 0,  -- 续期次数（D-52 上限 2 次）
  ttl_renewed_at TEXT,                  -- 上次续期时间锚（旧到期日）
  inject_tier TEXT,                     -- full / summary / anchor / none
  meaning_anchor REAL,                  -- D-50 意义锚 M（NULL = 未计算，存量不编造分数）
  superseded_by TEXT);                  -- D-56（schema v6）：替代指针；2.0 起 supersession 走 lifecycle='archived' + 本列，不再写 status='rejected'
CREATE TABLE IF NOT EXISTS memory_archive (
  memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL,
  priority INTEGER NOT NULL, time_velocity TEXT NOT NULL, ttl_days INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, agent_tag TEXT,
  prompt_version TEXT, archived_at TEXT NOT NULL, superseded_by TEXT,
  occurred_at TEXT,
  -- 归档表同步补齐 2.0 五列（归档链要能还原全字段，口径同 memories）
  lifecycle TEXT, ttl_renew_count INTEGER NOT NULL DEFAULT 0, ttl_renewed_at TEXT,
  inject_tier TEXT, meaning_anchor REAL);
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
  error TEXT,
  candidate_count INTEGER,       -- L1.5 F2（2026-10-09）：批内候选数（仅批路径承载）
  candidate_ids_hash TEXT,       -- L1.5 F2：候选 id 集 sha256[:16]（同候选集反查，仅批路径）
  short_circuit_reason TEXT,     -- L1.5 F2：短路记录原因（仅非批路径独立记录承载）
  elapsed_ms INTEGER,            -- ST-58/T-289（2026-10-17）：批次真实耗时（finish 依 started_at 计算）
  chunk_sha TEXT,                -- D-80（2026-11-06）：块内容指纹（同块重复率）
  cache_hit INTEGER NOT NULL DEFAULT 0,  -- D-80：1=块级缓存命中（未调 LLM）
  reused_tokens INTEGER NOT NULL DEFAULT 0); -- D-80：命中时复用的 token 量
CREATE INDEX IF NOT EXISTS idx_refine_runs_stage ON refine_runs(stage, version, started_at);
CREATE INDEX IF NOT EXISTS idx_refine_runs_file ON refine_runs(file_id, started_at);

-- D-80（2026-11-06）：块级提炼结果缓存。同一 (prompt 全文 + 模型指纹) 的块只算一次钱——
-- 失败重跑 / 同内容重复出现时直接复用，不再向 LLM 重发。
CREATE TABLE IF NOT EXISTS refine_chunk_cache (
  cache_key TEXT PRIMARY KEY,      -- sha256(prompt + NUL + model_fp)
  prompt_version TEXT,             -- 提示词版本（观测用，不参与 key 语义）
  model_fp TEXT NOT NULL,          -- 模型链指纹（链头 provider/model/max_tokens 归一化哈希）
  chunk_sha TEXT NOT NULL,         -- sha256(chunk 文本)[:16]（观测「同块」用）
  memories_json TEXT NOT NULL,     -- 解析后的记忆数组（含空数组：空产出同样要缓存）
  provider TEXT,                   -- 实际命中的 provider（缓存写回时记录）
  total_tokens INTEGER,            -- 该块首跑的真实 token 消耗（命中时计入 reused_tokens）
  created_at TEXT NOT NULL,
  last_hit_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  reused_tokens INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_refine_chunk_cache_created ON refine_chunk_cache(created_at);

-- D-80（2026-11-06）：块级失败补跑队列。成功块照常提交并推游标；失败块单独登记，
-- 由 chunk_replay 逐块补跑（不再靠「整份文件不提交、下一轮全量重发」兜底）。
CREATE TABLE IF NOT EXISTS refine_chunk_failures (
  failure_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  chunk_sha TEXT NOT NULL,         -- sha256(chunk 文本)[:16]
  prompt_version TEXT,
  chunk_text TEXT NOT NULL,        -- 失败块原文（剪枝+分块后的最终形态，补跑直接复用）
  sunk_refs_text TEXT,             -- 随块送去的沉淀清单（补跑保持同上下文）
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_at TEXT NOT NULL,
  UNIQUE(file_id, chunk_sha));
CREATE INDEX IF NOT EXISTS idx_refine_chunk_failures_file ON refine_chunk_failures(file_id, last_at);

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

-- ============ L1.5 F1：近似查重隔离表（T-167 词法扩展，2026-10-09） ============
-- 命中近似重复（向量 cos ≥ threshold）的新记忆不落 memories、内容暂存本表；
-- 人工复核后丢弃（mark_discarded）或旁路补录（near_dup_dao.restore）。
-- 去重键 UNIQUE(matched_memory_id, content_hash)：同事实跨批反复触发只占一行
-- （写入侧 ON CONFLICT 只更新分数，见 near_dup_dao.upsert_hit）。
-- created_at 秒精度（与 refine_dao._now_iso 项目惯例对齐）。
CREATE TABLE IF NOT EXISTS near_dup_quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,            -- sha256(content)[:16]（去重键一半）
  matched_memory_id TEXT NOT NULL,       -- 命中的既有记忆
  score REAL NOT NULL,
  source_ref TEXT,
  dimension_ids TEXT,                    -- json.dumps(ensure_ascii=False)，读取容错返回 []
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','discarded','restored')),
  restored_memory_id TEXT,               -- 旁路补录后的新记忆 id（追溯）
  restore_reason TEXT,                   -- 补录理由
  discard_reason TEXT,                   -- 丢弃理由（人工批复留痕，2026-11-05 两侧对称）
  UNIQUE(matched_memory_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_quarantine_matched ON near_dup_quarantine(matched_memory_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_status ON near_dup_quarantine(status);
"""

# ============ SGME 2.0 迁移阶段 1：正式表与审计表 DDL（2026-11-04）============
# 独立常量（照 DEMANDS_DDL 先例）：由 connect_memory 的 _migrate_2_0_tables 显式
# executescript 建表，既能覆盖新库、也能覆盖「schema_versions 已有记录但表缺失」的半迁移库。
MEMORY_2_0_TABLES_DDL = """
-- ttl_disposition：D-52 五档处置审计（0 valid_expired / 1 renewed / 2 anchor / 3 降档 /
-- 4 sleeping→archived）。只有**产生处置**才写行（未到期不记账——3 万+ 条全量记账
-- 会把审计表撑成噪声）；reason 承载 D-73 召回加固来源（recall_reinforced）。
CREATE TABLE IF NOT EXISTS ttl_disposition (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT,
  disposition TEXT,
  reason TEXT,
  m_score REAL,
  renew_count INTEGER,
  old_expire_at TEXT,
  new_expire_at TEXT,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_ttl_disp_mem ON ttl_disposition(memory_id, created_at DESC);

-- deletion_audit：D-54 两态删除审计（logical 可恢复 / purge 彻底删除）。
-- snapshot_json 为 purge 前的完整行快照 —— 与 D-44「增量快照保留 30 天」配套，
-- 保留期内可人工恢复；agent key 只能 logical，purge 必须 admin + 用户在 WebUI 现场确认。
CREATE TABLE IF NOT EXISTS deletion_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('logical','purge')),
  triggered_by TEXT NOT NULL,
  agent_id TEXT,
  reason TEXT,
  content_sha256 TEXT,
  snapshot_json TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_deletion_audit_mem ON deletion_audit(memory_id, created_at DESC);

-- rule_sync_ledger / rule_sync_daily：规则版本账本（3.5）——只检测版本一致性，
-- 不读 agent 本地文件内容；日表保留 90 天（与 security_log_keep_days 同口径）。
CREATE TABLE IF NOT EXISTS rule_sync_ledger (
  agent_id TEXT PRIMARY KEY,
  rule_version_seen TEXT,
  last_receipt_at TEXT,
  lag_sessions INTEGER DEFAULT 0,
  updated_at TEXT);
CREATE TABLE IF NOT EXISTS rule_sync_daily (
  date_label TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  rule_version_seen TEXT,
  current_version TEXT,
  lag_sessions INTEGER DEFAULT 0,
  status TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (date_label, agent_id));
CREATE INDEX IF NOT EXISTS idx_rule_sync_daily_date ON rule_sync_daily(date_label, agent_id);
"""

# LLM 用量按天账本（D-63 / §7.2）。2026-11-04 起纳入迁移链（§9.2 阶段 1 表清单第 5 张），
# 定义从 llm_usage_dao 上移到本模块作**唯一真源**，DAO 的 ensure_table 直接复用——
# 避免「同一张表两处 DDL」漂移（§9.2 实际进度：llm_usage_daily 此前为懒建、不在迁移链）。
LLM_USAGE_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS llm_usage_daily (
  day TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  stage TEXT NOT NULL,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  calls INTEGER DEFAULT 0,
  PRIMARY KEY (day, provider, model, stage));
"""

# session.db 建表 DDL（v0.7 新建，B1）：引擎调度状态库
# raw_files 由 wiki.db 迁入；refine_cursor 为 v0.7 新增（A3：阶段 1 只建表，不写 DAO、不接调度）
SESSION_DDL = """
CREATE TABLE IF NOT EXISTS raw_files (
  file_id TEXT PRIMARY KEY, path TEXT NOT NULL, session_key TEXT NOT NULL,
  agent_id TEXT, agent_model TEXT, started_at TEXT, ended_at TEXT, refined_at TEXT,
  last_refined_seq INTEGER, status TEXT NOT NULL DEFAULT 'new', size INTEGER,
  content_hash TEXT,
  -- E3 失败熔断（2026-11-05）：连续提炼失败次数（成功路径清零）+ 最近失败原因。
  -- 达 refine.max_file_attempts → status='error' 终止 batch_scan 重扫；
  -- 体积闸（E4）写 last_error='file_too_large'。不新增 status 取值。
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT);
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
  supersedes TEXT,
  -- ---- 知识库 2.0 九列（D-62 / §5.1 · 迁移阶段 1，2026-11-04）----
  page_type TEXT DEFAULT 'topic',        -- 页型：entity / topic / log / index
  category_class TEXT,                    -- D-62 自动分类结果（7 类之一）；category 保留原始路径
                                          -- （兼容期不改 1.x 语义；阶段 3 可用 --migrate-category 切换）
  headline TEXT,                          -- 标题行 ≤30 字（空时确定性回退）
  content_hash TEXT,                      -- 内容指纹（去重 / 变更检测）
  heat_score REAL DEFAULT 0,              -- 热度三件：heat_score + last_recalled_at + view_count
  last_recalled_at TEXT,
  view_count INTEGER NOT NULL DEFAULT 0,
  dirty INTEGER NOT NULL DEFAULT 0,       -- 实体页聚合脏标记（梦境重生成驱动）
  updated_by TEXT,                        -- user / agent_<id> / system（与草稿态挂钩）
  my_notes TEXT);                         -- 我的笔记列：重生成永不触碰
CREATE INDEX IF NOT EXISTS idx_wiki_pages_updated ON wiki_pages(updated_at DESC);
-- 注意：page_type / content_hash 的索引不放这里——老库 executescript 先于迁移执行，
-- 建索引会撞「no such column」；改由 _migrate_wiki_2_0_columns 在 ALTER 之后建。

-- 修订史（append-only）：每次变更留内容快照 + 变更类型 + 作者 + 一行摘要。
-- 与技能库 skill_revisions 同款思路；不引入 git（单人自用，库内修订史更轻）。
CREATE TABLE IF NOT EXISTS wiki_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL,
  title TEXT,
  content TEXT NOT NULL,
  change_kind TEXT NOT NULL DEFAULT 'update',   -- create / update / merge / rollback
  author TEXT,
  summary TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_wiki_rev_page ON wiki_revisions(page_id, revision_no DESC);

-- 分类注册表（≤10 一级类；未命中回退「未分类」并告警，D-62 不许静默）
CREATE TABLE IF NOT EXISTS wiki_categories (
  name TEXT PRIMARY KEY,
  display_name TEXT,
  sort_order INTEGER DEFAULT 100,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_wiki_cat_order ON wiki_categories(sort_order);

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

# ---------- T-163：接口调用统计（memory.db，独立 DDL 常量） ----------
# 背景：服务端此前无调用统计——「哪些 HTTP 端点 / MCP 工具从未被调用、谁在调用」
# 只能靠容器 stdout 日志（只覆盖单容器生命周期、MCP 工具名不落地）回答。
# 埋点：9910 HTTP 纯 ASGI 中间件（UsageMiddleware）+ 9913 MCP ApiKeyMiddleware
# body 窥探（解析 tools/call 的 params.name）；两层共写本表。
# 行粒度 = (day, kind, name, caller) 日聚合 upsert；写入方全静默（旁路统计，
# 失败不影响请求）；保留策略见 usage_dao.prune_usage（默认 400 天）。
API_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS api_usage_daily (
  day      TEXT NOT NULL,      -- UTC 日 YYYY-MM-DD
  kind     TEXT NOT NULL,      -- http | mcp
  name     TEXT NOT NULL,      -- HTTP 路由模板 / MCP 工具名
  caller   TEXT NOT NULL,      -- agent_id / default / anonymous / unknown
  calls    INTEGER NOT NULL DEFAULT 0,
  last_ts  TEXT NOT NULL,      -- 最近调用时刻（ISO UTC）
  last_ip  TEXT,               -- 最近来源 IP
  PRIMARY KEY (day, kind, name, caller));
CREATE INDEX IF NOT EXISTS idx_api_usage_name ON api_usage_daily(name, day DESC);
"""

# ---------- T-174：技能消费统计（memory.db，独立 DDL 常量） ----------
# 背景：api_usage_daily（T-163）把技能名归一化掉了（name = /v1/skills/{name}），
# 无法回答「四级披露哪一层真在被用、哪个技能被取用、谁在用」。补救而非替代：
# 两表正交并存，本表保留 skill 列。
# 埋点：digest/get 由 HTTP/MCP 中间件按路由模板 + 路径参数推导；search/materialize
# 需响应结果或请求体语义，由业务侧（端点/工具）记录；package（T-281 跨机包通道：
# 完整包下载 / 单文件读）由 HTTP 端点记录（见 operations/skill_usage.py）。
# 行粒度 = (day, layer, skill, caller) 日聚合 upsert；写入方全静默；
# note 是「最近一次」诊断摘要（检索摘要 / 目标目录类型 / 包格式与文件数 / 单文件扩展名），
# 真实路径不入库。
SKILL_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS skill_usage_daily (
  day      TEXT NOT NULL,      -- UTC 日 YYYY-MM-DD
  layer    TEXT NOT NULL,      -- search | digest | get | materialize（四级披露）| package（跨机包通道）
  skill    TEXT NOT NULL,      -- 技能名；无单一技能名的调用（检索）记 '-'
  caller   TEXT NOT NULL,      -- agent_id / default / anonymous / unknown
  calls    INTEGER NOT NULL DEFAULT 0,
  last_ts  TEXT NOT NULL,      -- 最近调用时刻（ISO UTC）
  last_ip  TEXT,               -- 最近来源 IP
  note     TEXT,               -- 最近一次诊断摘要（检索摘要 / 目标目录类型 / 包摘要）
  PRIMARY KEY (day, layer, skill, caller));
CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage_daily(skill, day DESC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_layer ON skill_usage_daily(layer, day DESC);
"""

# ---------- T-251：记忆调用统计（memory.db，独立 DDL 常量） ----------
# 第三层调用统计（api_usage_daily=端点/工具、skill_usage_daily=技能消费之后）：
# 对象维度下探到**单条记忆**——双源埋点（检索命中 search + 画像注入 inject），
# 回答「哪些记忆常被用 / 从未被用过」的活跃度分析。
# A2 政策修订（2026-10-10 用户批）：**仅限本表**——检索链路可写 memory_usage_daily
# （单次批量 upsert + 日聚合，控写放大）；memory_stats / last_recalled_at 链路
# 照旧禁止（不受本次放宽影响，见 memory_stats_dao docstring）。
# day = UTC（对齐 usage_dao）；行粒度 = (day, memory_id, source, caller) 日聚合
# upsert；保留 400 天由 memory_usage_dao.prune_memory_usage 控制。
MEMORY_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS memory_usage_daily (
  day       TEXT NOT NULL,     -- UTC 日 YYYY-MM-DD
  memory_id TEXT NOT NULL,     -- 记忆 id（对象维度）
  source    TEXT NOT NULL,     -- search | inject（双源）
  caller    TEXT NOT NULL,     -- agent_id / default / anonymous / unknown
  hits      INTEGER NOT NULL DEFAULT 0,
  last_ts   TEXT,              -- 最近调用时刻（ISO UTC）
  PRIMARY KEY (day, memory_id, source, caller));
CREATE INDEX IF NOT EXISTS idx_mem_usage_memory ON memory_usage_daily(memory_id, day DESC);
CREATE INDEX IF NOT EXISTS idx_mem_usage_day ON memory_usage_daily(day DESC);
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


# ---------- ST-38 T-133：记忆关系边 memory_edges（memory.db，独立 DDL 常量） ----------
# 图纸：`SGME-记忆系统进化方案-v0.2.md` §T2-1（v0.2 新增 Phase 0 结构边）。
# 关系集 ≤6：similar / causes / supersedes / belongs_to / contradicts / evolves_from。
# source：'llm' | 'cooccur' | 'scene' | 'system'（v0.2 新增 system=结构性边，backfill 产物）。
# valid_from / valid_to 预留（阶段三启用）。edge_id 由 DAO 确定性生成 `{from}::{to}::{relation}`（幂等可重跑）。
# ⚠️ 刻意不加外键：记忆会被 Supersession 归档/软删（memory_archive），外键会阻塞归档
#    或级联抹掉溯源（同 demands 先例，见 DEMANDS_DDL 注释）；存在性由 DAO/调用方软校验。
MEMORY_EDGES_DDL = """
CREATE TABLE IF NOT EXISTS memory_edges (
  edge_id    TEXT PRIMARY KEY,
  from_id    TEXT NOT NULL,
  to_id      TEXT NOT NULL,
  relation   TEXT NOT NULL,
  weight     REAL NOT NULL DEFAULT 1.0,
  valid_from TEXT,
  valid_to   TEXT,
  created_at TEXT NOT NULL,
  source     TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_edges_from ON memory_edges(from_id, relation);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON memory_edges(to_id, relation);
"""

# ---------- 2026-11-02 任务 B：角色离线生活独立表（memory.db，独立 DDL 常量） ----------
# 硬边界（交接文档 §3.4）：
# 1. **与 memories 无任何关系**：角色人生是虚构设定，进事实池会污染用户真实记忆
#    （与 ideas/demands 同款独立表模式，无外键、无 route、不进 /v1/search）
# 2. 内容只到类型级（上游保证，DAO 不自作主张清洗）
# 3. is_secret=1 永不进注入；shared_at 记「已经说出口」，同一段只说一次
# injected_at 是注入频率闸的记账列（每装配最多 1 条 / 每日上限按它统计）
# ============ core 记忆卡（D-79，2026-11-05）============
# 三层记忆分工的 core 层：只装「稳定的人属性」，每次注入必带。
# 与 memories（recall 层）分离：卡片一槽一值、覆盖式更新、有硬上限（400 字/卡、120 字/槽）。
# 不参与自动批量提炼——写入只走「用户对话授权」或「WebUI 编辑」，来源记在 updated_by。
MEMORY_CARDS_DDL = """
CREATE TABLE IF NOT EXISTS memory_cards (
  card_id TEXT PRIMARY KEY,             -- identity/family/routine/preferences/principles/goals
  title TEXT NOT NULL,                  -- 显示名（渲染用）
  slots TEXT NOT NULL,                  -- JSON {slot_key: value | [values]}
  updated_at TEXT NOT NULL,
  updated_by TEXT,                      -- user / agent / webui
  version INTEGER NOT NULL DEFAULT 1
);

-- 修改留痕（用户 2026-11-05 定：每次修改必须可查）
CREATE TABLE IF NOT EXISTS memory_card_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL,
  slot_key TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  changed_at TEXT NOT NULL,
  changed_by TEXT,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_card_history_card ON memory_card_history(card_id, changed_at);
"""

# 槽位 schema（唯一的槽位真源；数据库只存值，不存结构）
# 一槽一值；family 是多值槽（值可为字符串数组）
CARD_SCHEMA: dict[str, dict] = {
    "identity": {"title": "基本身份", "slots": ["称呼", "语言", "城市", "时区"]},
    "family": {"title": "家庭", "slots": ["成员"], "multi": ["成员"]},
    "routine": {"title": "作息", "slots": ["起床", "睡觉", "固定日程"]},
    "preferences": {"title": "偏好", "slots": ["喜欢", "不喜欢", "忌讳"]},
    "principles": {"title": "协作准则", "slots": ["诚实", "计划", "报备", "原件", "检索", "安装"]},
    "goals": {"title": "目标", "slots": ["长期", "中期", "短期"]},
}

# 硬上限（防退化成段落；超限拒绝写入并记 anomaly_warn）
CARD_MAX_CHARS = 400
SLOT_MAX_CHARS = 120


ROLE_LIFE_DDL = """
CREATE TABLE IF NOT EXISTS role_life_events (
  event_id    TEXT PRIMARY KEY,
  role_id     TEXT NOT NULL,
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'see',
  content     TEXT NOT NULL,
  mood        TEXT,
  why_secret  TEXT,
  is_secret   INTEGER NOT NULL DEFAULT 0,
  injected_at TEXT,
  shared_at   TEXT,
  created_at  TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_role_life_role_at ON role_life_events(role_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_role_life_pick ON role_life_events(role_id, is_secret, injected_at);
"""


def _now_iso() -> str:
    """UTC ISO 8601 时间戳。"""
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: Path) -> sqlite3.Connection:
    """打开 SQLite 连接：启用 WAL、外键、忙碌等待、row_factory。

     check_same_thread=False：FastAPI 把同步端点丢到 threadpool 执行，
     连接需跨线程复用。SQLite WAL 模式 + 单用户 Server 场景下安全
    （读不阻塞、写由 SQLite 文件锁串行）。

    ``busy_timeout``（2026-11-01 补）：SQLite 默认值为 0——写锁被占用时**立即**
    抛 ``database is locked``，不等待。生产日志实测累计 948 条锁错误、
    并出现「未处理异常: database is locked」的 500 响应；提炼任务持锁期间
    任何并发写（信号、用量统计、管理端点）都会瞬时失败。设 5 秒等待后，
    写操作会在锁释放后自动重试，代价只是极少数请求慢几十毫秒。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
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
    _migrate_refine_runs_candidate_cols(conn)
    _migrate_mem_idea_columns(conn)
    _migrate_dim_boundaries(conn)
    _migrate_demands_table(conn)
    _migrate_project_meta_table(conn)
    _migrate_dream_reports_table(conn)
    _migrate_ideas_table(conn)
    _migrate_signal_consumed_by(conn)
    _migrate_signal_acks_table(conn)
    _migrate_api_usage_table(conn)
    _migrate_skill_usage_table(conn)
    _migrate_memory_usage_table(conn)
    _migrate_persona_tables(conn)
    _migrate_memory_edges_table(conn)
    _migrate_mem_facts_json(conn)
    _migrate_mem_valid_period(conn)
    _migrate_refine_runs_elapsed_ms(conn)
    _migrate_refine_runs_chunk_columns(conn)
    _migrate_memory_2_0_columns(conn)
    # 2.0 迁移阶段 1（2026-11-04）：生命周期五列 + 维度优先级 + 四张正式表
    _migrate_memory_lifecycle_columns(conn)
    _migrate_dim_priority(conn)
    _migrate_2_0_tables(conn)
    _migrate_memory_pages_table(conn)
    _migrate_role_life_table(conn)
    _migrate_quarantine_discard_reason(conn)
    _migrate_memory_cards_table(conn)
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
    _migrate_raw_files_session_uniq(conn)
    # E3（2026-11-05）：失败熔断两列（fail_count / last_error），老库幂等补列
    _migrate_raw_files_fail_columns(conn)
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
    _migrate_wiki_2_0_columns(conn)
    _migrate_wiki_evolve_table(conn)
    return conn


def connect_skills(data_dir: str | Path | None = None) -> sqlite3.Connection:
    """打开 skills.db 连接并确保 schema 就绪（T-112：技能索引持久化）。

    落盘位置 ``config.DATA_DIR / "skills.db"``（生产即 ``/data/data/skills.db``，
    与 memory.db / wiki.db / session.db 同层级，随 SGME_HOME 卷持久化）。

    设计取舍（2026-08-29 定）：
    - **向量用普通表 + BLOB**（对称 scene_vectors），**不用 vec0 虚拟表**——
      项目既有 memory_vectors/scene_vectors 均为该形态，且
      ``data/search/vector.py`` 已封装 sqlite-vec 加速 + numpy 降级双路，
      引入 vec0 属范式分裂且需固定维度、迁移成本高。
    - **全文用 FTS5 外部内容表**（对称 wiki_fts），索引列为 jieba 分词后的
      ``*_seg``，由触发器随主表同步；分词内容由 skills_dao 写入时计算。
    - 技能模块禁用时**不调用本函数**（核心零影响，对称 wiki 扩展模块）。
    """
    d = Path(data_dir) if data_dir else config.DATA_DIR
    conn = _connect(d / "skills.db")
    _ensure_schema(conn, SKILLS_DDL, SCHEMA_VERSION, "skills_v1")
    # 老库迁移（B156）：先补 name_seg 列并回填，再建 FTS/触发器
    # （外部内容表与触发器都引用 name_seg，顺序不能反）
    migrated = _migrate_skills_name_seg(conn)
    # T-287：skill_vectors 补内容指纹列（存量行 NULL → 判待重嵌；不涉 FTS）
    _migrate_skill_vectors_content_sha(conn)
    # T-401 / T-402 合并：两个迁移都幂等，按序执行——先补 strength（T-401），
    # 再补 skills.status（T-402 的函数会先查列是否存在，已补则跳过，不重复 ALTER）
    _migrate_skill_uses_strength(conn)
    _migrate_skills_uses_gate(conn)
    # FTS5 虚表 + 同步触发器（幂等；与 wiki 的 init_wiki_fts 同职责，
    # 但技能库体量小且无需历史重建，直接在连接时就绪，避免调用方漏调）
    conn.executescript(SKILLS_FTS_DDL)
    conn.executescript(SKILLS_FTS_TRIGGERS)
    if migrated:
        # 刚补列/回填过 → 旧 FTS 索引可能被坏触发器污染过（name_seg 位塞的是
        # 未分词原名），全量重建一次外部内容索引（407 条毫秒级，非热路径）
        conn.execute("INSERT INTO skills_fts(skills_fts) VALUES('rebuild')")
    conn.commit()
    return conn


# ============ T-112：skills.db（技能索引持久化，独立 DDL 常量） ============
# 图纸：`docs/design/SGME-Skills管理模块设计-v0.2.md` §二「skills.db 两步门」——
# 原设计「M1 不建库」基于内存 BM25 达标（全量重建 3.15s / 单次打分 0.5ms），
# 但用户从**产品演进**角度拍板建库（结构化查询/依赖图/增量/对账是迟早要补的能力）。
#
# 表职责：
#   skills         主表（name PK + sha256 增量锚点 + frontmatter 字段 + 全文）
#   skills_fts     FTS5 全文索引（外部内容表，索引 jieba 分词列）
#   skill_vectors  向量（BLOB，对称 scene_vectors；sqlite-vec 加速 + numpy 降级）
#   skill_uses     uses 依赖图（入向引用一级信号源，写侧门禁用）+ strength 强/弱依赖
#   skill_sync_meta 同步水位（对账 cron / 增量嵌入用）
#   skill_files    技能附属文件（references/scripts/assets，T-401 正式建表）
#   skill_revisions 技能修订史（append-only，T-401 正式建表）
#   skill_meta     生命周期状态（active/deprecated/tombstone + superseded_by，T-401）
#
# ⚠️ 刻意不加外键到 skills(name)：技能删除走墓碑制（store.py），
#    外键会阻塞软删与重命名；存在性由 operations 层软校验。
SKILLS_DDL = """
CREATE TABLE IF NOT EXISTS skills (
  name TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  name_seg TEXT,
  description TEXT NOT NULL DEFAULT '',
  description_seg TEXT,
  category TEXT,
  tags TEXT,
  version TEXT,
  pattern TEXT,
  source TEXT,
  origin_path TEXT,
  content TEXT NOT NULL,
  content_seg TEXT,
  content_len INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  synced_at TEXT,
  status TEXT NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_updated ON skills(updated_at DESC);

CREATE TABLE IF NOT EXISTS skill_vectors (
  name TEXT PRIMARY KEY,
  embedding BLOB NOT NULL,
  model TEXT NOT NULL,
  dims INTEGER NOT NULL,
  embedded_at TEXT NOT NULL,
  content_sha256 TEXT);

CREATE TABLE IF NOT EXISTS skill_uses (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  strength TEXT NOT NULL DEFAULT 'weak',
  PRIMARY KEY (src, dst));
CREATE INDEX IF NOT EXISTS idx_skill_uses_dst ON skill_uses(dst);

CREATE TABLE IF NOT EXISTS skill_sync_meta (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT);

-- T-401：技能包附属文件与修订史（原只在 skill_package_dao.ensure_tables 懒建，
-- 生产库从未建过；此处正式并入建表段，结构以 skill_package_dao 的 CREATE 为准，
-- 两处列定义必须一致——tests/test_skills_db.py 有结构对账用例钉死）。
CREATE TABLE IF NOT EXISTS skill_files (
  skill_name TEXT NOT NULL,
  rel_path   TEXT NOT NULL,
  content    BLOB NOT NULL,
  size       INTEGER NOT NULL,
  sha256     TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (skill_name, rel_path));
CREATE TABLE IF NOT EXISTS skill_revisions (
  revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_name  TEXT NOT NULL,
  version     TEXT NOT NULL,
  package_sha256 TEXT NOT NULL,
  file_count  INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  note        TEXT,
  created_at  TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_skill_rev_name ON skill_revisions(skill_name, created_at);

-- T-401：技能生命周期状态独立成表（不动 skills 主表——状态需在增量同步/改名
-- 墓碑清理技能行后仍然存续；superseded_by 语义同 store.rename_skill 的墓碑 frontmatter）。
-- 未登记行按 active 处理；白名单校验在 data/skill_meta_dao.py（DAOs 是唯一写入口）。
CREATE TABLE IF NOT EXISTS skill_meta (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'active'
         CHECK (status IN ('active', 'deprecated', 'tombstone')),
  superseded_by TEXT,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_skill_meta_status ON skill_meta(status);

-- D-67：route=skill_candidate 记忆的消费台账（技能候选池）。
-- 刻意**不写 skills 主表**：
--   1) sync_index 的 delete 分支会把「源目录里没有」的技能行清掉，候选行会被误删；
--   2) 候选正文进 skills_fts 会污染技能检索（还没审核的草稿不该被 skill_search 命中）。
-- memory_id 唯一 = 幂等锚点：一条记忆只能消费一次；ignored 即「人工明确不要」。
CREATE TABLE IF NOT EXISTS skill_candidates (
  candidate_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id       TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  content         TEXT NOT NULL DEFAULT '',
  category        TEXT,
  tags            TEXT,
  confidence      REAL NOT NULL DEFAULT 0.0,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'accepted', 'ignored', 'promoted')),
  reason          TEXT,
  note            TEXT,
  reviewer        TEXT,
  source          TEXT NOT NULL DEFAULT 'memory_route',
  content_sha256  TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  reviewed_at     TEXT);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_status
  ON skill_candidates(status, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_sha
  ON skill_candidates(content_sha256);
"""

# FTS5 外部内容表 + 同步触发器（对称 wiki/fts.py 的 WIKI_FTS_DDL / TRIGGERS）
SKILLS_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name_seg,
    description_seg,
    content_seg,
    name UNINDEXED,
    content='skills',
    content_rowid='rowid'
);
"""

SKILLS_FTS_TRIGGERS = """
DROP TRIGGER IF EXISTS skills_ai;
DROP TRIGGER IF EXISTS skills_ad;
DROP TRIGGER IF EXISTS skills_au;
CREATE TRIGGER skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid, name_seg, description_seg, content_seg, name)
    VALUES (new.rowid, new.name_seg, new.description_seg, new.content_seg, new.name);
END;
CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name_seg, description_seg, content_seg, name)
    VALUES ('delete', old.rowid, old.name_seg, old.description_seg, old.content_seg, old.name);
END;
CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name_seg, description_seg, content_seg, name)
    VALUES ('delete', old.rowid, old.name_seg, old.description_seg, old.content_seg, old.name);
    INSERT INTO skills_fts(rowid, name_seg, description_seg, content_seg, name)
    VALUES (new.rowid, new.name_seg, new.description_seg, new.content_seg, new.name);
END;
"""


def _migrate_skills_name_seg(conn: sqlite3.Connection) -> bool:
    """老库迁移（B156，2026-09-08）：skills 表补 name_seg 分词列并回填存量。

    Returns:
        True = 本次发生了补列/回填（调用方据此决定是否重建 FTS 索引）；
        False = 无需迁移（新库或已迁移过）。

    背景（生产实证 NAS v1.1.8）：T-112 建表时漏建 name_seg 列，而 skills_fts
    外部内容表与三个同步触发器都引用它 → 技能 UPDATE/DELETE 触发器与任何
    FTS 全表扫描报 ``no such column: T.name_seg``；触发器还将未分词的
    ``new.name`` 塞进 name_seg 位，BM25 的 10× 名字加权形同虚设。

    迁移职责（照 _migrate_wiki_page_columns 先例）：
    1. ALTER 补 name_seg 列（幂等：已有列则跳过）；
    2. 回填：name_seg = segment(name)、缺分词的 description_seg/content_seg
       一并按当前分词口径补齐（只处理 NULL/空，不重算已有值）；
    3. 不在此处重建 FTS——connect_skills 后续 executescript 会重建触发器，
       上层启动预热 sync_index 走增量路径补 FTS 行（删旧写新由触发器完成）。

    分词在 data 层内联导入 sgme.segment（模块唯一职责是分词，无反向依赖，
    不违反依赖方向：data → segment 与 data → config 同级）。
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skills'"
    ).fetchone()
    if not has_table:
        return False
    cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)").fetchall()]
    added = False
    if "name_seg" not in cols:
        conn.execute("ALTER TABLE skills ADD COLUMN name_seg TEXT")
        cols.append("name_seg")
        added = True

    # 回填缺分词的存量行（幂等：只补 NULL/空，已分词的不动）
    from sgme.segment import segment

    rows = conn.execute(
        "SELECT rowid, name, description, content, name_seg, description_seg, "
        "content_seg FROM skills WHERE name_seg IS NULL OR name_seg=''"
    ).fetchall()
    for r in rows:
        name_seg = segment(r["name"])
        desc_seg = r["description_seg"] or (
            segment(r["description"]) if r["description"] else ""
        )
        content_seg = r["content_seg"] or (
            segment(r["content"]) if r["content"] else ""
        )
        conn.execute(
            "UPDATE skills SET name_seg=?, description_seg=?, content_seg=? WHERE rowid=?",
            (name_seg, desc_seg, content_seg, r["rowid"]),
        )
    conn.commit()
    return added or bool(rows)


def _migrate_skill_vectors_content_sha(conn: sqlite3.Connection) -> bool:
    """老库迁移（T-287，2026-10-16）：skill_vectors 补 content_sha256 内容指纹列。

    Returns:
        True = 本次发生了补列；False = 无需迁移（新库或已迁移过）。

    背景：旧判据「todo = 全表 − vector_covered」只查向量行存在性——已存在行的
    技能内容更新后不重嵌（生产实测 190/541 技能 updated_at > embedded_at）。
    修复本体在 operations.skills::sync_index 的版本化判据；本迁移只负责让
    **存量行**补出该列（值为 NULL）——NULL ≠ 当前指纹 → 自然判「待重嵌」，
    欠账随下次同步/启动预热逐轮补完。不触碰 FTS（不涉分词列，无需 rebuild）。
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_vectors'"
    ).fetchone()
    if not has_table:
        return False
    cols = [r[1] for r in conn.execute("PRAGMA table_info(skill_vectors)").fetchall()]
    if "content_sha256" not in cols:
        conn.execute("ALTER TABLE skill_vectors ADD COLUMN content_sha256 TEXT")
        conn.commit()
        return True
    return False


def _migrate_skill_uses_strength(conn: sqlite3.Connection) -> bool:
    """老库迁移（T-401，2026-11-02）：skill_uses 补 strength 依赖强度列。

    Returns:
        True = 本次补了列；False = 无需迁移（新库或已迁移过）。

    背景：依赖图原先只有 (src,dst) 两列，「强依赖（缺了就跑不通）」与
    「弱依赖（顺带一提）」无法区分，写侧门禁无法分级。迁移给列默认 'weak'：
    存量边一律按最宽松语义处理（不阻塞任何既有删除/改名），强依赖由上游
    按 frontmatter 显式声明后逐步收紧。

    幂等：列已存在则跳过 ALTER；随后的回填只命中 NULL/空串（正常情况下零行，
    兜底老库手工加过可空 strength 列的形态）。skill_uses 无 FTS 依赖，无需 rebuild。
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_uses'"
    ).fetchone()
    if not has_table:
        return False
    cols = [r[1] for r in conn.execute("PRAGMA table_info(skill_uses)").fetchall()]
    added = False
    if "strength" not in cols:
        # SQLite 的 ALTER ADD COLUMN 带常量默认值时，存量行自动取 'weak'
        conn.execute(
            "ALTER TABLE skill_uses ADD COLUMN strength TEXT NOT NULL DEFAULT 'weak'"
        )
        added = True
    conn.execute("UPDATE skill_uses SET strength='weak' WHERE strength IS NULL OR strength=''")
    conn.commit()
    return added
def _migrate_skills_uses_gate(conn: sqlite3.Connection) -> bool:
    """老库迁移（T-402）：skills 补 status、skill_uses 补 strength 列。

    Returns:
        True = 本次发生了补列；False = 无需迁移（新库或已迁移过）。

    仅 ALTER ADD COLUMN（带默认值），**不动存量行**：现有技能默认 active、
    现有 uses 边默认 weak（强依赖需显式标记，避免升级后突然阻断删除）。
    """
    changed = False
    has_skills = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skills'"
    ).fetchone()
    if has_skills:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)").fetchall()]
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE skills ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
            changed = True
    has_uses = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_uses'"
    ).fetchone()
    if has_uses:
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(skill_uses)").fetchall()]
        if "strength" not in ucols:
            conn.execute(
                "ALTER TABLE skill_uses ADD COLUMN strength TEXT NOT NULL DEFAULT 'weak'"
            )
            changed = True
    if changed:
        conn.commit()
    return changed


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_evolve (
          session_key TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'queued',
          action TEXT,
          entry_hash TEXT,
          page_id TEXT,
          error TEXT,
          created_at TEXT,
          processed_at TEXT)
        """)
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


def _migrate_raw_files_fail_columns(conn: sqlite3.Connection) -> None:
    """老库迁移：raw_files 补 fail_count / last_error 列（E3 失败熔断，2026-11-05）。

    背景（B312 待办 1）：块级失败的文件此前保持 status=new 无限重扫——实测同一批
    超大文件空转 5 天 / 4.9 万次提炼。新增两列承载「连续失败次数 + 最后一次失败
    原因」，由 engine/refine.py 与 engine/batch_scan.py 的失败路径递增，达
    refine.max_file_attempts → status='error' 终止重扫；成功路径清零。

    幂等：PRAGMA table_info 检查后逐列 ALTER；列已存在则跳过。新库由 SESSION_DDL
    建全列。不更新 SCHEMA_VERSION——照 _migrate_refine_runs_elapsed_ms 先例：
    _migrate_* 序列本就是启动时无条件串行执行的幂等函数，与版本号无依赖。
    fail_count 带 NOT NULL DEFAULT 0 → 存量行自动回填 0（SQLite ALTER 语义）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_files)").fetchall()]
    if not cols:  # 表不存在 → 交给 SESSION_DDL 建表负责（防御，正常链路不会发生）
        return
    if "fail_count" not in cols:
        conn.execute(
            "ALTER TABLE raw_files ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0"
        )
    if "last_error" not in cols:
        conn.execute("ALTER TABLE raw_files ADD COLUMN last_error TEXT")
    conn.commit()


def _migrate_raw_files_session_uniq(conn: sqlite3.Connection) -> None:
    """raw_files (session_key, started_at) 唯一索引（F-4，2026-09-24 深度审查）。

    目的：为 append_l0 的「同会话同起时刻幂等」提供数据库级兜底（跨进程防线；
    进程内锁见 engine/pipeline.py::_APPEND_L0_LOCK）。

    幂等：索引已存在则无操作。存量库可能已有竞态产生的重复行——此时**跳过建索引
    并告警**（不动存量数据；人工处置后重启自动补建）。排查 SQL：
      SELECT session_key, started_at, COUNT(*) c FROM raw_files
      WHERE started_at IS NOT NULL GROUP BY 1,2 HAVING c>1;
    """
    dup = conn.execute(
        "SELECT session_key, started_at, COUNT(*) AS c FROM raw_files "
        "WHERE started_at IS NOT NULL GROUP BY session_key, started_at "
        "HAVING c > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        logger.warning(
            "raw_files 存在重复 (session_key, started_at)（如 %s / %s，共 %d 行）——"
            "跳过唯一索引创建（不动存量数据）；人工去重后重启自动补建",
            dup[0], dup[1], dup[2],
        )
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_files_session_started "
        "ON raw_files(session_key, started_at)"
    )
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
        conn.execute(
            "ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
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


def _migrate_refine_runs_candidate_cols(conn: sqlite3.Connection) -> None:
    """老库迁移：refine_runs 补候选池留痕三列（L1.5 F2，2026-10-09）。幂等。

    三列语义（v4 §二.4）：``candidate_count`` / ``candidate_ids_hash`` 仅批路径承载；
    ``short_circuit_reason`` 仅非批路径独立短路记录承载——语义区分不依赖脆弱组合约定。
    新库走 MEMORY_DDL 建全列，本函数只为老库补列（照 _migrate_refine_runs_tokens 先例：
    PRAGMA 检查 + 条件 ALTER + commit）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    if "candidate_count" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN candidate_count INTEGER")
    if "candidate_ids_hash" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN candidate_ids_hash TEXT")
    if "short_circuit_reason" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN short_circuit_reason TEXT")
    conn.commit()


def _migrate_refine_runs_elapsed_ms(conn: sqlite3.Connection) -> None:
    """老库迁移：refine_runs 补 `elapsed_ms` 列（ST-58/T-289，2026-10-17）。幂等。

    背景：三处 run 记账原先都在 LLM 调用**之后**才 `start()`，导致全表
    `started_at == finished_at`（21,857 行无一例外），效率完全不可度量。
    T-289 把 `start()` 前移到 LLM 调用之前，本列为随之新增的耗时承载列。

    口径：``elapsed_ms`` = 批次真实墙钟耗时，由 ``RefineRunRecorder.finish``
    依 ``started_at`` 计算写入（调用点不传值，防各处口径漂移）。**可空**——
    老行无值不影响旧代码读取。

    新库走 MEMORY_DDL 建全列，本函数只为老库补列（照
    `_migrate_refine_runs_candidate_cols` 先例：PRAGMA 检查 + 条件 ALTER + commit）。
    不更新 SCHEMA_VERSION——`_migrate_*` 序列本就是启动时无条件串行执行的幂等
    函数，与版本号无依赖（版本号仅由 `_ensure_schema` 用于 MEMORY_DDL 登记）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    if "elapsed_ms" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN elapsed_ms INTEGER")
    conn.commit()


def _migrate_refine_runs_chunk_columns(conn: sqlite3.Connection) -> None:
    """老库迁移：refine_runs 补块级观测三列（D-80，2026-11-06）。幂等。

    - chunk_sha：本批次对应块的内容指纹（sha256[:16]），用于算「同块重复率」——
      重复率 = 总批次 / 不同 (file_id, chunk_sha) 组合数，是本次成本问题的核心指标。
    - cache_hit：1 = 本次未调 LLM（命中块级缓存），0 = 真实调用。
    - reused_tokens：命中缓存时复用的 token 量（首跑真实消耗），用于回算省了多少。

    新库走 MEMORY_DDL 建全列；本函数只为老库补列（照
    _migrate_refine_runs_elapsed_ms 先例：PRAGMA 检查 + 条件 ALTER + commit）。
    不更新 SCHEMA_VERSION——_migrate_* 序列本就启动时无条件幂等执行。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refine_runs)").fetchall()]
    if not cols:
        return
    if "chunk_sha" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN chunk_sha TEXT")
    if "cache_hit" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0")
    if "reused_tokens" not in cols:
        conn.execute("ALTER TABLE refine_runs ADD COLUMN reused_tokens INTEGER NOT NULL DEFAULT 0")
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
    cols = [
        r[1] for r in conn.execute("PRAGMA table_info(dimension_registry)").fetchall()
    ]
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


# ---------- ST-35 T-98：人格洞察（memory.db，独立 DDL 常量） ----------
# persona_traits：特质累积表——dimension/value/confidence/evidence_refs/scene_context。
# 设计原则（2026-08-25 用户定案）：产出是「倾向」不是标签判决；单条记忆不改写
# 整体画像（confidence 随证据数累积）；Supersession 复用画像 v2 规则（非累积覆盖）。
# user_mbti：用户自报 MBTI 锚点（娱乐向展示皮），记录轨迹（INTJ→INFJ 变化可视化），
# 与 traits 累积模型互为校验，不强制换算。
PERSONA_TRAITS_DDL = """
CREATE TABLE IF NOT EXISTS persona_traits (
  trait_id TEXT PRIMARY KEY,
  dimension TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.0,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  evidence_refs TEXT,
  scene_context TEXT NOT NULL DEFAULT 'general',
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT,
  source TEXT NOT NULL DEFAULT 'rule',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_persona_traits_dim ON persona_traits(dimension, status, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_persona_traits_updated ON persona_traits(updated_at DESC);

CREATE TABLE IF NOT EXISTS user_mbti (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mbti_type TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'self_reported',
  note TEXT,
  recorded_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_user_mbti_time ON user_mbti(recorded_at DESC);

CREATE TABLE IF NOT EXISTS persona_reports (
  report_id TEXT PRIMARY KEY,
  period TEXT NOT NULL,
  report TEXT NOT NULL,
  mbti_result TEXT,
  trait_changes TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_persona_reports_period ON persona_reports(period DESC);
"""

PERSONA_STATE_DDL = """
CREATE TABLE IF NOT EXISTS persona_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL);
"""


def _migrate_persona_tables(conn: sqlite3.Connection) -> None:
    """ST-35 人格洞察三表（persona_traits/user_mbti/persona_reports）+ 计时状态表。

    与 _migrate_ideas_table 同模式：不进 MEMORY_DDL、不 bump SCHEMA_VERSION，
    CREATE TABLE IF NOT EXISTS 幂等，老库重启自动补建。
    """
    conn.executescript(PERSONA_TRAITS_DDL)
    conn.executescript(PERSONA_STATE_DDL)
    conn.commit()


def _migrate_memory_edges_table(conn: sqlite3.Connection) -> None:
    """老库迁移：memory.db 建 memory_edges 关系边表（ST-38 T-133，2026-08-31）。

    与 _migrate_demands_table 同模式：不进 MEMORY_DDL、不 bump SCHEMA_VERSION，
    CREATE TABLE / CREATE INDEX 全部 IF NOT EXISTS 幂等，老库重启自动补建。
    表内容（边）由 scripts/backfill_edges.py 一次性/周期 backfill 写入（source='system'）。
    """
    conn.executescript(MEMORY_EDGES_DDL)
    conn.commit()


def _migrate_mem_facts_json(conn: sqlite3.Connection) -> None:
    """老库迁移：memories/memory_archive 补 facts_json 列（ST-38 T-136，2026-08-31）。

    D4 MVP：原子事实三元组以 JSON 数组列落库（免建表迁移），后续验证价值后再迁
    memory_facts 独立表（可索引、可 SQL 精确过滤）。JSON1 json_each 展开查询见
    sgme/data/facts_dao.py。幂等：列已存在则无操作。
    """
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "facts_json" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN facts_json TEXT")
    conn.commit()


def _migrate_mem_valid_period(conn: sqlite3.Connection) -> None:
    """老库迁移：memories/memory_archive 补 valid_from/valid_to 列（ST-39 T-138，2026-08-31）。

    有效期间语义：事实何时开始/失效（occurred_at 已覆盖事件时刻）。NULL=永久有效，
    天然向后兼容（存量记忆全部 NULL → 过滤零影响）。检索侧过滤见
    sgme/data/search/_filter_expired。幂等：列已存在则无操作。
    """
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for col in ("valid_from", "valid_to"):
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    conn.commit()


def _migrate_memory_2_0_columns(conn: sqlite3.Connection) -> None:
    """老库迁移：memories/memory_archive 补 2.0 L2 提取层新增列（2026-10-31）。

    依据 `SGME2.0-设计方案-V1.0.md` D-23 / D-30 / D-53：

    - ``feeling``：主事件的感受字段（D-23 main_event{content/occurred_at/feeling}）
    - ``trait``：特质原文，≤50 字，只给原文不给域（D-23）
    - ``scene_key``：场景标签单列，替代 1.x scenes 表（D-53 取消场景聚合）；
      由 L2 落库钩子在同一事务内计算，不建场景表、不存场景正文
    - ``needs_review``：待审闸门（D-23 二轮修订 P1-2）——1 = 不进主注入块、
      不进人格证据，只在待审候选可见；默认 0 保证存量记忆零影响

    幂等：列已存在则无操作；memory_archive 同步补，保证归档链能还原全字段。
    """
    new_cols = (
        ("feeling", "TEXT"),
        ("trait", "TEXT"),
        ("scene_key", "TEXT"),
        ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
        # 未拆事实 + 因果边（JSON）：2.0 字段落地前的过渡落点，
        # 结构 {"facts": [...未拆事实...], "causes": [{"from","to"}...]}
        ("l2_facts_json", "TEXT"),
        # 三轴 + 路由 + 七值类型（D-66/D-67）：1.x 解析层已算出但**落库时被丢弃**，
        # 2.0 要能按这些字段做注入过滤与统计，必须先持久化
        ("type", "TEXT"),
        ("subject", "TEXT"),
        ("certainty", "TEXT"),
        ("derivation", "TEXT"),
        ("modality", "TEXT"),
        ("route", "TEXT"),
        # D-78 惊讶度先验（0-1）：NULL = 未计算（存量记忆不编造分数，
        # 填满等于给存量凭空加强惊讶度——与 D-52 意义锚回填同款纪律）
        ("surprise", "REAL"),
        # D-68 软遗忘：正文刻意不带 topic（防被忘内容在检索/注入时重新浮现），
        # 「忘了什么」只能靠这两列回溯；NULL = 非遗忘请求
        ("forget_topic", "TEXT"),
        ("forget_kind", "TEXT"),
    )
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if not cols:  # 表不存在 → 交给 DDL 建表负责
            continue
        for col, ddl_type in new_cols:
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type}")
    # 场景标签索引（D-53：只有一个标签，但检索/聚合要按它过滤）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_scene ON memories(scene_key, memory_id)")
    conn.commit()


def _migrate_memory_lifecycle_columns(conn: sqlite3.Connection) -> None:
    """老库迁移（2026-11-04 · 迁移阶段 1）：memories/memory_archive 补 2.0 生命周期五列。

    依据 `SGME2.0-设计方案-V1.0.md` D-46（三轴分离）/ D-50（意义锚 M）/ D-52（时效五档）：

    - ``lifecycle``：生命周期轴（active / sleeping / archived），与质量轴 ``status``、
      来源轴 ``derivation/modality`` 正交——2.0 起 TTL 到期**不再**写 ``status='expired'``
    - ``ttl_renew_count`` / ``ttl_renewed_at``：D-52 档 1 续期（上限 2 次）的计数与时间锚
    - ``inject_tier``：注入档位（full / summary / anchor / none），档 2 锚点块用它
    - ``meaning_anchor``：D-50 意义锚 M（首轮梦境回填；NULL = 未计算，存量不编造分数）

    回填按 §9.3「专则优先于通则」：``status='expired'`` → sleeping + inject_tier='none'
    （1.x 把 TTL 到期错塞进质量轴，故 status 原值不动）；归档表存量 → archived；
    其余 → active。只在 NULL 上回填，重复调用幂等。
    """
    new_cols = (
        ("lifecycle", "TEXT"),
        ("ttl_renew_count", "INTEGER NOT NULL DEFAULT 0"),
        ("ttl_renewed_at", "TEXT"),
        ("inject_tier", "TEXT"),
        ("meaning_anchor", "REAL"),
        # D-56（schema v6，2026-11-04）：supersession 收口指针（归档表早已有，memories 补上）
        ("superseded_by", "TEXT"),
    )
    added = False
    for table in ("memories", "memory_archive"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if not cols:  # 表不存在 → 交给 DDL 建表负责
            continue
        for col, ddl_type in new_cols:
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type}")
                added = True
    # 回填只在「本次真的补了列」时跑（否则每次启动都全表 UPDATE 3 万行）；
    # 专则优先（§9.3）：① expired 专则 ② 归档表 ③ 通用默认。
    # 新库由 DDL 带列、不走本分支：写入侧未判定时 lifecycle 留 NULL，
    # 注入谓词 (lifecycle IS NULL OR lifecycle='active') 视为 active（§3.6）。
    if added:
        conn.execute(
            "UPDATE memories SET lifecycle='sleeping', inject_tier='none' "
            "WHERE lifecycle IS NULL AND status='expired'"
        )
        conn.execute("UPDATE memories SET lifecycle='active' WHERE lifecycle IS NULL")
        conn.execute(
            "UPDATE memory_archive SET lifecycle='archived' WHERE lifecycle IS NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mem_lifecycle ON memories(lifecycle, memory_id)"
    )
    conn.commit()


def _migrate_dim_priority(conn: sqlite3.Connection) -> None:
    """老库迁移（2026-11-04）：dimension_registry 补 priority 列（D-53 R1 主维度选取）。

    种子值写在 ``sgme/resources/registry/dimensions.yaml``（D-8 可配），启动时由
    ``memory_dao.import_registry`` 幂等 upsert；此处只保证列存在。未登记优先级的
    维度留 NULL，scene_key 规则把它当最低优先级（与 R3 兜底一致，不虚增信息）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(dimension_registry)").fetchall()]
    if cols and "priority" not in cols:
        conn.execute("ALTER TABLE dimension_registry ADD COLUMN priority INTEGER")
        conn.commit()


def _migrate_2_0_tables(conn: sqlite3.Connection) -> None:
    """老库迁移（2026-11-04）：显式建 2.0 四表（ttl_disposition / deletion_audit / rule_sync_*）。

    MEMORY_2_0_TABLES_DDL 独立成常量（照 DEMANDS_DDL 先例），由本函数 executescript；
    覆盖「schema_versions 已记录但表缺失」的半迁移库。幂等（IF NOT EXISTS）。
    """
    conn.executescript(MEMORY_2_0_TABLES_DDL)
    conn.executescript(LLM_USAGE_DAILY_DDL)
    # 旧懒建表（engine/recall_aging.ensure_ttl_disposition_table）没有 reason 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ttl_disposition)").fetchall()]
    if cols and "reason" not in cols:
        conn.execute("ALTER TABLE ttl_disposition ADD COLUMN reason TEXT")
    conn.commit()


def _migrate_wiki_2_0_columns(conn: sqlite3.Connection) -> None:
    """老库迁移（2026-11-04 · 迁移阶段 1）：知识库 2.0 九列 + 修订史/分类两表（§5.1 / D-62）。

    九列：page_type（页型）/ headline（标题行）/ content_hash（指纹）/ 热度三件
    （heat_score + last_recalled_at + view_count）/ dirty（脏标记）/ updated_by /
    my_notes（我的笔记列，重生成永不触碰）。两表：wiki_revisions（append-only 修订史）
    与 wiki_categories（≤10 一级类注册表）。

    只补结构不动存量内容：page_type 用 DDL 默认 'topic'（回填与 306 页 skill/* 划界
    属阶段 3，见 §9.2 阶段 3）；content_hash 留 NULL 由写入侧首次落库时补。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
    if cols:
        new_cols = (
            ("page_type", "TEXT DEFAULT 'topic'"),
            ("category_class", "TEXT"),
            ("headline", "TEXT"),
            ("content_hash", "TEXT"),
            ("heat_score", "REAL DEFAULT 0"),
            ("last_recalled_at", "TEXT"),
            ("view_count", "INTEGER NOT NULL DEFAULT 0"),
            ("dirty", "INTEGER NOT NULL DEFAULT 0"),
            ("updated_by", "TEXT"),
            ("my_notes", "TEXT"),
        )
        for col, ddl_type in new_cols:
            if col not in cols:
                conn.execute(f"ALTER TABLE wiki_pages ADD COLUMN {col} {ddl_type}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_pages_type ON wiki_pages(page_type, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_pages_hash ON wiki_pages(content_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_pages_class "
            "ON wiki_pages(category_class, updated_at DESC)"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wiki_revisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          page_id TEXT NOT NULL,
          revision_no INTEGER NOT NULL,
          title TEXT,
          content TEXT NOT NULL,
          change_kind TEXT NOT NULL DEFAULT 'update',
          author TEXT,
          summary TEXT,
          created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_wiki_rev_page ON wiki_revisions(page_id, revision_no DESC);
        CREATE TABLE IF NOT EXISTS wiki_categories (
          name TEXT PRIMARY KEY,
          display_name TEXT,
          sort_order INTEGER DEFAULT 100,
          is_default INTEGER NOT NULL DEFAULT 0,
          created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_wiki_cat_order ON wiki_categories(sort_order);
        """
    )
    conn.commit()


def _migrate_quarantine_discard_reason(conn: sqlite3.Connection) -> None:
    """老库迁移：near_dup_quarantine 补 discard_reason 列（2026-11-05，幂等）。

    背景：灰区人工批复**只落实了「放行理由」**（restore_reason），丢弃侧
    `mark_discarded` 连参数都没有——同一条意见只兑现了一半。
    用户 2026-11-05 拍板：不设必填，但两侧都要留痕。理由是调阈值时要知道
    「0.88 这一刀砍掉的是什么」，没有理由两周后就没人记得为什么丢。

    与 `_migrate_skills_name_seg` 同模式：PRAGMA 查列 → 缺才 ALTER
    （CREATE TABLE IF NOT EXISTS 对已存在的表不会补列，故必须走迁移）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(near_dup_quarantine)").fetchall()]
    if "discard_reason" in cols:
        return
    conn.execute("ALTER TABLE near_dup_quarantine ADD COLUMN discard_reason TEXT")
    conn.commit()


def _migrate_memory_cards_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建 core 记忆卡两表（D-79，2026-11-05）。幂等。

    与 _migrate_ideas_table 同模式：独立 DDL 常量 + 本迁移函数——老库
    （schema_versions 已登记 memory_v4）不会重跑 MEMORY_DDL 之外的变更，
    必须走迁移函数才能补齐新表。
    """
    conn.executescript(MEMORY_CARDS_DDL)
    conn.commit()


def _migrate_role_life_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建角色离线生活独立表 role_life_events（2026-11-02 任务 B）。幂等。

    与 `_migrate_ideas_table` 同模式：独立 DDL 常量 + 本迁移函数，老库
    （schema_versions 已登记 memory_v4）连上时才补得出这张表。
    """
    conn.executescript(ROLE_LIFE_DDL)
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


def _migrate_api_usage_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建接口调用统计表 api_usage_daily（T-163，2026-09-13）。幂等。

    与 _migrate_signal_acks_table 同模式：独立成 API_USAGE_DDL + 本迁移函数——
    老库（schema_versions 已登记）不会重跑新表的 DDL，必须走迁移函数补齐。
    """
    conn.executescript(API_USAGE_DDL)
    conn.commit()


def _migrate_skill_usage_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建技能消费统计表 skill_usage_daily（T-174，2026-09-19）。幂等。

    与 ``_migrate_api_usage_table`` 同模式：独立成 SKILL_USAGE_DDL + 本迁移函数——
    老库（schema_versions 已登记）不会重跑新表的 DDL，必须走迁移函数补齐。
    """
    conn.executescript(SKILL_USAGE_DDL)
    conn.commit()


def _migrate_memory_usage_table(conn: sqlite3.Connection) -> None:
    """老库迁移：建记忆调用统计表 memory_usage_daily（T-251，2026-10-10）。幂等。

    与 ``_migrate_skill_usage_table`` 同模式：独立成 MEMORY_USAGE_DDL + 本迁移函数——
    老库（schema_versions 已登记 memory_v4）不会重跑 MEMORY_DDL 之外的变更，
    必须走迁移函数才能补齐新表（第三层调用统计；A2 放宽仅限本表）。
    """
    conn.executescript(MEMORY_USAGE_DDL)
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
    # ST-58/T-289：收敛超期悬挂的 running 行（幂等；无残留时零开销、零日志）。
    # 失败不阻断初始化——收敛是旁路，主流程不得因它起不来。
    try:
        from sgme.data.refine_dao import RefineRunRecorder

        RefineRunRecorder.reap_stale_running(mem)
    except Exception as exc:  # noqa: BLE001
        print(f"[SGME refine_runs] 超期 running 收敛失败（不影响启动）: {exc}")
    return mem, session, wiki


def resolve_data_dir(conn: sqlite3.Connection) -> Path | None:
    """从既有连接推导数据库目录（T-222：后台线程自建独立连接用）。

    取 ``PRAGMA database_list`` 中 main 库的文件路径 → 父目录。v0.7 三库拆分后
    memory.db / session.db / wiki.db 同层级（同 data_dir），故任一连接的父目录
    即 data_dir。内存库（``:memory:``）无文件路径 → 返回 None，由调用方自行回退。

    背景（T-222，2026-09-30 深度审查 P0-1）：提炼后台线程不得复用宿主连接
    （跨线程共享 sqlite 连接在显式事务下互踩：BEGIN 冲突 + rollback 连带回滚，
    记忆静默丢失），改为线程内 ``init_databases(data_dir)`` 自建；data_dir 由
    本函数从宿主连接直接推导——operations / pipeline 的对外签名零变化，
    入口层（routes/mcp_server）零波及。
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:
        return None
    for row in rows:
        # row = (seq, name, file)；sqlite3.Row 与普通元组均支持整数下标
        if row[1] == "main" and row[2]:
            return Path(row[2]).resolve().parent
    return None


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


# ---------- memory_pages（记忆 → 知识页/技能 关联，2026-10-31）----------
#
# 用户定：技术细节与踩坑经验沉淀到技能库/知识库，**记忆只留摘要 + 指向详情页的索引**。
# 刻意不加外键（同 memory_edges 先例）：知识页会归档/合并，记忆也会走 Supersession 归档。
# kind: wiki（ref_id=page_id）| skill（ref_id=技能名）
# relation: details（详情在该页）| derived_from（该页由本条记忆生成）
MEMORY_PAGES_DDL = """
CREATE TABLE IF NOT EXISTS memory_pages (
  memory_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  ref_id     TEXT NOT NULL,
  relation   TEXT NOT NULL DEFAULT 'details',
  created_at TEXT NOT NULL,
  PRIMARY KEY (memory_id, kind, ref_id));
CREATE INDEX IF NOT EXISTS idx_mpage_ref ON memory_pages(kind, ref_id);
CREATE INDEX IF NOT EXISTS idx_mpage_mem ON memory_pages(memory_id);
"""


def _migrate_memory_pages_table(conn: sqlite3.Connection) -> None:
    """建/补 memory_pages（幂等）。多语句必须用 executescript。"""
    conn.executescript(MEMORY_PAGES_DDL)
    conn.commit()
