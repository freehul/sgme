# SGME 架构改造方案 v0.7

> 日期：2026-08-08
> 基于：本会话 + 项目内现有设计文档（架构 0.4~0.6 / 模块化重构 B30 / 数据模型 / 接口契约 / 产品化设计）
> 状态：草案，待确认后实施

---

## 一、改造范围总览

本次改造涵盖：模块拆分、数据库重组、新增模块、现有模块优化。分三个阶段实施。

### 阶段 1：数据库重组 + 模块分类（破坏性变更，需迁移）

### 阶段 2：新增模块（独立开发，不影响现有功能）

### 阶段 3：现有模块优化（非破坏性重构）

---

## 二、数据库重组

### 当前状态

```
memory.db: memories + archive + tags + sources + vectors + signals + refine_runs + dim_registry
wiki.db:   raw_files + scenes + scene_vectors + scene_memories + scene_versions
```

### 问题

1. `raw_files`（会话索引）放在 wiki.db——但它是核心管线的基础设施，wiki 关闭时不能用
2. `scenes`（L2 场景）放在 wiki.db——但 L2 是核心提炼管线产出，不是 wiki 知识
3. wiki.db 的"wiki"含义模糊——实际存的是"场景 + 会话索引"，没有真正的 wiki 页面

### 改造后

```
memory.db    # 核心 · 记忆池（不变，加 scenes 表）
  ├── memories / memory_archive / memory_tags / memory_sources / memory_vectors
  ├── signal_events / signal_subscribers / refine_runs
  ├── dimension_registry / dimension_alias
  ├── scenes              ← 从 wiki.db 迁入
  ├── scene_vectors       ← 从 wiki.db 迁入
  ├── scene_memories      ← 从 wiki.db 迁入
  └── scene_versions      ← 从 wiki.db 迁入

session.db   # 核心 · 会话索引 + 提炼状态（新库）
  ├── raw_files           ← 从 wiki.db 迁入
  └── refine_cursor       ← 新建

wiki.db      # 扩展 · wiki 知识库（wiki 模块启用时创建）
  ├── wiki_pages          ← 新建（全文 + FTS5 + 向量）
  └── wiki_links          ← 新建（页面间关系）
```

### 迁移 SQL

```sql
-- raw_files: wiki.db → session.db
ATTACH 'data/session.db' AS session;
CREATE TABLE session.raw_files AS SELECT * FROM wiki_db.raw_files;

-- scenes: wiki.db → memory.db  
ATTACH 'data/memory.db' AS memory;
CREATE TABLE memory.scenes AS SELECT * FROM wiki_db.scenes;
CREATE TABLE memory.scene_vectors AS SELECT * FROM wiki_db.scene_vectors;
CREATE TABLE memory.scene_memories AS SELECT * FROM wiki_db.scene_memories;
CREATE TABLE memory.scene_versions AS SELECT * FROM wiki_db.scene_versions;
```

### 新增表 DDL

#### refine_cursor（session.db）

```sql
CREATE TABLE refine_cursor (
    namespace   TEXT NOT NULL,
    date_label  TEXT NOT NULL,       -- YYYY-MM-DD
    cursor_at   TEXT,                -- 推进到的消息 created_at
    status      TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT,
    PRIMARY KEY (namespace, date_label)
);
```

#### wiki_pages（wiki.db，wiki 模块启用时）

```sql
CREATE TABLE wiki_pages (
    page_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,      -- 全文（AI 检索用）
    category     TEXT,
    tags         TEXT,               -- JSON 数组
    source_type  TEXT,               -- file / url / image / video
    source_url   TEXT,
    source_file  TEXT,               -- raw/ 中的原件路径
    ingested_at  TEXT,
    updated_at   TEXT,
    content_seg  TEXT                -- jieba 分词
);
CREATE VIRTUAL TABLE wiki_fts USING fts5(title, content, content=wiki_pages);
CREATE TABLE wiki_links (
    source_id   TEXT,
    target_id   TEXT,
    rel_type    TEXT,                -- similar / extends / references / contradicts
    confidence  REAL,
    source      TEXT,                -- auto / manual
    created_at  TEXT
);
```

### 记忆运营统计（memories 表加列）

```sql
ALTER TABLE memories ADD COLUMN last_recalled_at TEXT;
ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN last_injected_at TEXT;
```

> ⚠️ 待确认：加在 memories 本体还是 sidecar 表？本体简单但改 DDL，sidecar 不改 DDL 但多一次 JOIN。

---

## 三、模块拆分

### 3.1 核心模块（不可删除）

| 模块 | 当前 | 改后 | 职责 |
|------|------|------|------|
| **data/** | storage/ | data/ | 数据库操作 + 检索（CRUD + search 合一） |
| **engine/** | engine/ | engine/ | 提炼管线（L1/L1.5/L2/pipeline/health） |
| **config/** | config.py | config/ | 配置加载 + 读写 + 落盘 |
| **llm/** | llm/ | llm/ | LLM 适配 + 降级链 + provider 管理 |
| **profile/** | profile/ | profile/ | 画像注入（模板 + Tier0） |
| **prompts/** | prompts/ | prompts/ | 提示词版本管理 |
| **log/** | — | log/ | 统一日志（新增） |
| **server/** | server/ | server/ | HTTP 入口 |
| **signal/** | signal/ | signal/ | 事件信号 |
| **raw/** | raw/ | raw/ | L0 文件格式 I/O |
| **segment.py** | segment.py | segment.py | 分段 |
| **backup/** | backup/ | backup/ | 数据库备份 |
| **operations/** | — | operations/ | 统一操作层（新增，收敛 HTTP+MCP 参数校验+调用） |
| **refinery/** | — | refinery/ | wiki 知识提炼（新增，ingest+extract+validate） |

### 3.2 扩展模块（可禁用/不安装）

| 模块 | 说明 | 开关 |
|------|------|------|
| **wiki/** | 知识库管理（wiki.db + 实时 HTML 渲染 + 按需导出） | `wiki.enabled: true/false` |
| **adapters/** | Hermes/Reasonix 等 Agent 适配 | 各 adapter 独立 install.py |
| **skills-hub/** | 用户自有技能仓库（map/copy 双模式） | `skills_hub.enabled: true/false` |
| **mcp_server.py** | MCP 协议入口（无适配 Agent 的通用接入） | 端口 9913，`SGME_MCP_DISABLED=1` 关闭 |

### 3.3 新增模块详述

#### operations/ — 统一操作层

```
sgme/operations/
├── __init__.py
├── errors.py          # OperationError / InvalidArgs
├── append.py          # L0 捕获
├── inject.py          # 记忆注入
├── search.py          # 混合检索
├── memory.py          # 单条记忆 CRUD
├── refine.py          # 提炼触发
├── stats.py           # 统计
├── health.py          # 健康检查
└── config.py          # 配置读写
```

HTTP 路由和 MCP 工具变成薄包装，各自只做协议翻译。

#### refinery/ — wiki 知识提炼引擎

```
sgme/refinery/
├── __init__.py        # refine(source) → RefineryResult
├── ingest.py          # 输入处理（文件/URL/图片/视频 → 纯文本）
├── extract.py         # LLM 提取（调模型 + schema 校验 + 失败重试）
├── validate.py        # 质量门（可插拔验证步骤）
└── output.py          # 统一产出格式
```

Refinery **仅服务 wiki 知识提炼**。会话→记忆管线保留在 engine/。蒸馏套装可调用 refinery 的 API，但不属于 refinery。

#### log/ — 统一日志

```
sgme/log/
├── __init__.py        # get_logger(name) + setup()
├── formatter.py       # 控制台/JSON 双格式
└── config.py          # 日志配置解析
```

全项目统一入口 `from sgme.log import get_logger`，避 stdlib `logging` 同名。

---

## 四、与现有设计文档的冲突

### 4.1 架构 0.4 §3 — Wiki Store 定义

**现有定义**：
> Wiki Store：文档知识库：精炼层（提炼后知识点 / 报告 / 文档 / 代码，带引用）+ 原始层（原始会话 / 喂入资料）

**冲突**：我们将 Wiki 拆为扩展模块，只存用户提交的知识。L2 场景（精炼层）归 memory.db，会话索引（原始层）归 session.db。

**处理**：架构文档下一版需重写 §3 中 Wiki Store 的职责描述，改为"可选扩展模块，管理用户主动提交的知识库内容"。

### 4.2 架构 0.4 — L2 场景存储位置

**现有定义**：L2 场景存在 wiki.db 精炼层。

**冲突**：我们决定 scenes 迁入 memory.db（L2 是核心提炼管线产出，不等于 wiki 知识）。

**处理**：架构文档需明确"L2 场景是记忆的聚合视图，属于记忆池的一部分，不是 wiki 知识"。

### 4.3 模块化重构 B30 — 存储层命名

**现有**：`sgme/storage/`（db.py + memory_dao + wiki_dao + stats_dao + signal_dao + refine_dao）

**改后**：`sgme/data/`（db + crud/ + search/）

**处理**：B30 的 step 3 "config 模块写能力"已部分完成，存储→data 的重命名可与本次改造一并进行。

---

## 五、接口变更

### 新增端点

```
POST /v1/refinery/analyze      # 输入分析（检测内容类型、结构）
POST /v1/refinery/extract      # LLM 提取（指定 schema）
GET  /v1/wiki/pages            # wiki 页面列表
GET  /v1/wiki/pages/{id}       # 单页详情（JSON）
GET  /v1/wiki/pages/{id}?view=html  # 实时渲染 HTML
GET  /v1/wiki/pages/{id}/export     # 导出自包含 HTML
POST /v1/wiki/ingest           # 提交 wiki 处理任务
GET  /v1/wiki/ingest/{id}      # 查询处理进度
GET  /v1/wiki/search           # wiki 搜索
GET  /v1/wiki/raw/{hash}       # 下载原件
GET  /v1/skills                # skills-hub 列表
POST /v1/skills/sync           # 触发 skills-hub 同步
```

### 不变端点

现有 `/v1/append` / `/v1/inject` / `/v1/search` / `/v1/memory/*` / `/v1/events` / `/v1/health` / Admin 端点全部保留，路径不变。

### 端点归属

```
HTTP /v1/* (9910)           → 核心 + 扩展端点均可调
MCP (9913)                  → 核心端点（append/inject/search/memory_get/refine/stats/health/config）
wiki / refinery / skills     → 仅 HTTP（没有 MCP 客户端需要这些高级功能）
```

---

## 六、Wiki 模块设计要点（已定案）

- wiki.db 存全文（AI 检索用），不存 pages/ 目录，不存 MD/HTML 文件
- 浏览时 API 返回 JSON，前端渲染；导出时实时生成自包含 HTML（base64 嵌图）
- raw/ 目录存原件归档（text/image/video/audio，hash 命名）
- wiki 知识不需要溯源（和记忆不同）
- 四种输入统一处理：文本/图片/链接/视频 → ingest → extract → wiki_pages
- 知识关系：标签 + 向量相似推荐 + wiki_links 表（auto/manual）
- 图谱视图 + 时间线视图

---

## 七、待确认事项

1. **记忆运营统计列**：加在 memories 本体还是 sidecar 表？
2. **refinery/ 目录名**：`refinery/` 还是 `extraction/`？你倾向哪个？
3. **operations/ 目录名**：`operations/` 还是 `actions/` 或其他？
4. **实施优先级**：建议先做 数据库重组（阶段 1），再做 operations + data 重命名（阶段 3），最后做 refinery + wiki（阶段 2，它们可以独立开发）。这个顺序可以吗？
5. **架构文档**：改造完成后需要出 `SGME-架构设计-0.7.md`，重写 §3 模块表、§8 数据流、数据库章节。现在出还是改造完了出？

---

## 八、文件索引

本次会话产出的方案文档：

| 文件 | 内容 |
|------|------|
| `docs/plans/operations-layer-extraction.md` | operations 层抽取方案 |
| `docs/research/hermes-skill-loading-mechanism.md` | Hermes skill 加载机制分析 |
| `docs/plans/architecture-renovation-v0.7.md` | 本文档 |
