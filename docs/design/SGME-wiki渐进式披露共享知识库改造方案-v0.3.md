# 拾光记忆引擎（SGME）— wiki 渐进式披露共享知识库改造方案

> 版本：v0.3（修订稿：v0.2 已吸收审查意见；v0.3 修正 W1 迁移机制实现偏差）
> 日期：2026-08-16 ｜ 审查人：用户（对照 wiki_dao.py / wiki/fts.py / operations/wiki.py / operations/search.py / migrations/0001 / mcp_server.py 逐条核对）
> 地位：改造方案，审查修订后待确认；确认后按任务清单实施。
> 关联：docs/requirements/SGME-Backlog-v0.2.md（需求锚）、docs/design/SGME-架构设计-v0.9.md（架构依据）、docs/design/SGME-实施变更记录-v0.9.md（实施记录）、docs/research/wiki-kb-benchmark/（调研，AIRDT 仓库）
> 决策背景：wiki 设计决策已入 wiki（NAS 9910，page_id wiki渐进式披露共享知识库-设计决策-v0-1-45810444）

---

## 0. 审查修订记录（v0.1 → v0.2）

| 审查项 | 级别 | 修订落点 |
|---|---|---|
| FTS 重建 SQL 与现状矛盾、破坏中文检索 | P0 | §5.1 重写：保留 content_seg 分词列模式，新增 description_seg，扩展现有 DDL 重建 |
| 自进化管线归属与游标未交代 | P0 | §5.4 重写：独立第三条管线 + 独立 wiki_evolve 游标，明确不复用 refinery.refine |
| tags LIKE 过滤脆弱 | P1 | §5.2 改 Python 层 _parse_tags 精确判断 |
| append 幂等缺落点 | P1 | §5.3/5.4：entry hash 内嵌标记 + 查重 |
| 溯源与架构 §10.1 冲突 | P1 | §4 D6/B：显式澄清只对 skill 经验写回溯源 |
| W6 违反项目文件管理铁律 | P1 | §5.6/W6：源放 adapters/dsh/skills/，install 部署 |
| W1 迁移机制偏差（migrations/0002 撞 registry「不补列」红线） | P0 | §5.1 重写：弃 migrations/0002，列新增走 _migrate_*、FTS 走 init_wiki_fts（项目既有惯例） |
| supersession 锚点与 page_id 策略冲突 | P1 | §5.1/5.3：锚点定义（page_id 稳定 + title 判等取代） |
| 「不做兼容妥协」表述自相矛盾 | P1 | §5.1 澄清语义 |
| 候选区落点未定义 | P1 | §5.4：一期审计兜底，二期候选表 |
| 费用门禁启发式模糊 | P2 | §5.4 简化为 ≥N 轮 |
| superseded 过滤漏 LIKE 兜底 | P2 | §5.2 覆盖两条路径 |
| bridge 跨项目归属模糊 | P2 | §9 明确登记归属 |
| PATCH description 默认行为 | P2 | §5.3 默认不动 |

---

## 1. 背景与问题

1. **技能/知识碎片化**：Hermes（417+82 技能）与 DSH（29 技能）各维护独立技能库，布局不兼容（Hermes 三级分类 vs DSH 两级发现），无法直接共享目录；双维护、版本漂移。
2. **上下文膨胀**：技能全量注入 system prompt 是固定开销（Hermes ~6000 tok/轮），渐进式披露是业界共识解法（调研印证：Anthropic Skills / MOC / TiddlyWiki 四方同构）。
3. **多 agent 共享需求**：SGME 已服务 DSH/Hermes/reasonix/trae/workbuddy 五类 agent，需要统一的知识/技能/经验共享层。
4. **自进化需求**：Hermes 有 hindsight（会话后总结经验写回技能）；SGME 侧应提供同等能力且写入共享知识库。

## 2. 设计目标（5 个，调研逐一对齐）

| # | 目标 | 调研印证 |
|---|---|---|
| ① | md 内容为主体：技能手册 = 知识 = wiki_pages 条目 | md 唯一事实源，SQLite 仅索引（Joplin/Outline） |
| ② | 渐进式披露：L1 常驻元数据 + L2 按需加载 | SKILL.md frontmatter 常驻 ~24 tok/条（Anthropic） |
| ③ | 多 agent 共享同一知识库（记忆/知识/经验三层） | 检索即服务：「答案+引用」接口（Notion Q&A/Mem0） |
| ④ | 自进化：会话后自动总结写回 | 事件触发+限速+规则闸门（Voyager 反例） |
| ⑤ | 缓存友好：知识修改不影响对话前缀缓存 | 静态在前、动态在后、更新走追加（Claude Code） |

## 3. 架构总览

    L1 常驻（catalog 前缀，稳定）              L2 按需（工具调用，尾部）
    ┌─────────────────────────┐        ┌──────────────────────────┐
    │ wiki-skill-discovery     │        │ wiki_pages（手册全文）    │
    │ （索引 skill，搜索引导）   │───────▶│ wiki_search / wiki_pages │
    │ + L1 精选条目（title+     │  拉取   │ / wiki_page 工具         │
    │  description，稳定排序）  │        │ （FTS5 + category/tags） │
    └─────────────────────────┘        └───────────┬──────────────┘
                                                   │ 自进化写回（第三条管线）
            ┌──────────────────────────────────────┘
            ▼
      session/end → 费用门禁 → LLM 提炼 → 规则闸门 → ADD-only 追加/新建
      （SGME 侧独立 evolve 管线；DSH/Hermes 仅触发）

**分工**：wiki_pages 是唯一事实源（技能手册 + 知识 + 经验统一入库）；本地文件系统技能保留为 L1 常驻与索引引导；自进化写回 wiki（L2），L1 冻结（铁律）。

## 4. 决策记录

### D1. skill 当知识存 wiki_pages（已定）
tags 加 `skill` 标记、category 分类（`skill/<domain>`）；content 存 SKILL.md 全文（frontmatter 保留）；FTS5/wiki_links 现成。

### D2. raw 不混入（已定）
raw/ 是 L0 会话原件域，技能手册是 L2 知识域，职责分离。

### D3. wiki_pages 加 description 字段（已定）
「描述即索引」——L1 常驻条目的价值全在 description。入库时 LLM 辅助生成/校准。

### D4. 统一搜索过滤 skill（已定）
`/v1/search` 的 wiki_pages 层默认排除 skill 标记记录（回忆通道不见手册）；`wiki_search`/wiki_pages 工具不过滤（执行通道专找）。过滤用精确判断，不用 LIKE（见 §5.2）。

### D5. 索引 skill 只写搜索引导（已定）
1 个通用 `wiki-skill-discovery`；数据库检索（FTS5）快准，索引 skill 不做内容搬运。

### D6. 自进化写回 wiki（已定，含规则闸门）
触发：session/end + 费用门禁（首期=会话 ≥N 轮，N 默认 5 可配）；提炼：LLM 结构化输出；写入：ADD-only 追加（entry hash 去重）；审计：来源会话+时间戳。
**闸门（采纳规则闸门）**：schema 校验 → 来源必填 → entry hash 去重 → 通过则写；可疑拒绝并记审计（一期不建候选表，见 §5.4）。
**溯源范围澄清（P1-5）**：author/status/supersedes 溯源字段**仅适用于 skill 类「经验写回」条目**；ingest/refinery 类知识页面维持架构 §10.1「wiki 知识不需要溯源」语义，互不冲突。

### D7. skills_hub 保留禁用（已定）
wiki 链路打通后 skills_hub 的"文件系统分发"被 wiki API 绕过；`enabled=false`，代码保留（原件永不删）。

### D8. 官方 skill 插件先不动（已定）
DSH skills 是服务注册表，L2 手册不进 catalog，官方插件非拦路虎；触发"干掉"条件（catalog 膨胀/渲染控制不足）时再评估。

### 调研修正（全部定稿）
- **B（采纳）**：schema 补 `author` / `status`（active/superseded）/ `supersedes`；supersession 用确定性规则（同 category+同 title），锚点定义见 §5.1（P1-7 一并解决）
- C（采纳）：检索主路径 FTS5+确定性过滤，向量检索一期不做，留扩展位
- D（采纳）：L1 枚举确定性稳定排序（category+更新时间）

## 5. 详细设计

### 5.1 数据模型与迁移（任务 W1）

**迁移机制（W1 实现偏差修正，2026-08-16 勘察确认）**：项目有两套迁移机制，职责边界明确——`migrations/` 目录**只做存量数据搬运**（`_registry.py` docstring：不建表、不补列）；**列新增**走 `db.py` 的 `_migrate_*` 函数（PRAGMA 检测缺列 → ALTER，连接时幂等自动跑，十几个先例）；**FTS 虚拟表**走 `wiki/fts.py` 的 `init_wiki_fts`（B2 原则：wiki_fts 不进 WIKI_DDL）。**本方案不新增 migrations/0002**，照既有惯例落地：

新增 5 列（description / description_seg / author / status / supersedes），由 `_migrate_wiki_page_columns()` 用 PRAGMA 检测缺列后逐列 ALTER（幂等自愈，connect_wiki() 连接时自动跑）：

**FTS 扩展（P0-1 修订）**：

    -- 1) wiki_pages 新增列（可空，向后兼容）
    ALTER TABLE wiki_pages ADD COLUMN description TEXT;      -- L1 摘要（描述即索引）
    ALTER TABLE wiki_pages ADD COLUMN description_seg TEXT;  -- jieba 预分词（照 content_seg）
    ALTER TABLE wiki_pages ADD COLUMN author TEXT;           -- 写入 agent/会话（B）
    ALTER TABLE wiki_pages ADD COLUMN status TEXT DEFAULT 'active';  -- active|superseded（B）
    ALTER TABLE wiki_pages ADD COLUMN supersedes TEXT;       -- 被取代的 page_id（B）

    -- 2) FTS 升级重建：保留 content_seg 中文分词模式，新增 description_seg
    --    （FTS5 外部内容表加索引列无法 ALTER，必须重建；
    --     重建 = 升级现有 WIKI_FTS_DDL 常量后 DROP+CREATE+回填，非另起炉灶）
    DROP TABLE IF EXISTS wiki_fts;
    CREATE VIRTUAL TABLE wiki_fts USING fts5(
        content_seg, description_seg, page_id UNINDEXED,
        content='wiki_pages', content_rowid='rowid'
    );
    -- 3) 触发器同步字段扩展（wiki_ai/wiki_ad/wiki_au 均加 description_seg）
    -- 4) 存量回填：description_seg = _seg(description)（复用 wiki_dao._seg）

实现：①`sgme/data/db.py`——WIKI_DDL 的 wiki_pages 追加 5 列（status 带默认值）+ 新增 `_migrate_wiki_page_columns()` + `connect_wiki()` 末尾调用；②`sgme/wiki/fts.py`——`WIKI_FTS_DDL` 扩为 `fts5(content_seg, description_seg, page_id UNINDEXED, content='wiki_pages', content_rowid='rowid')`，三个触发器（wiki_ai/wiki_ad/wiki_au）同步 description_seg，`init_wiki_fts()` 加结构检测（缺 description_seg 列 → DROP 重建 + 回填，FTS5 无法 ALTER 加列）；③`sgme/data/wiki_dao.py`——insert_page/update_page_content 增 description 等参数 + 计算 `description_seg=_seg(description)`；④`sgme/operations/wiki.py`——`_LIST_SKIP_FIELDS` 加 `description_seg`（防响应臃肿，同 content_seg）；⑤tests——「老库缺列 → 连接自动补列」+「FTS 命中 description」用例。

**验收**：服务重启即自动迁移（无需手动跑脚本）；老数据 status 默认 active；`/v1/wiki/search` 命中 description。**中文检索能力不降级**（content_seg 保留，description_seg 同 jieba 方案）。

**兼容性澄清（P1-8）**："不做兼容妥协"指**不为旧客户端保留旧行为**（统一搜索默认过滤 skill、superseded 过滤、新增 PATCH 端点均属行为变更，不因旧客户端而省略）；**数据层仍自然兼容**（新列可空、status 默认 active、既有端点不感知新列）。实施记录 BXX 登记 + 更新说明。

**supersession 锚点定义（P1-7）**：
- `page_id` = 唯一稳定锚点：append/PATCH 按 page_id 原地更新（不产生新版本）
- 判等取代：create 写入时若存在「同 category + 同 title」的 active 旧页且 content 不同 → 新页为新版本（新 page_id），旧页置 `status='superseded'` + `supersedes=新 page_id`
- 两条写路径统一：append=原地追加（无版本语义）；create=新版本取代旧版本（确定性规则，不靠 embedding）

### 5.2 检索语义（任务 W2）

- `operations/search.py` `_search_wiki_pages`：默认 `exclude_skill=True` + 过滤 `status='superseded'`
- **skill 过滤实现（P1-3）**：不用 `tags LIKE`（JSON 字符串 + 双重编码脏数据前科）；Python 层复用 `wiki_dao._parse_tags` 解析后精确判断 `'skill' in tags`（list_pages 已解析，改造即可）
- **superseded 过滤覆盖两条路径（P2）**：`wiki/fts.py` `search_wiki_fts` 内 BM25 MATCH 路径与 LIKE 兜底路径**两处**都加 `status='active'`（兜底路径直接查 wiki_pages，漏加会漏出 superseded 页面）；`wiki_dao.list_pages` 同步过滤
- `wiki_search` / `wiki_pages`：不过滤 skill、过滤 superseded
- 检索结果带 page_id + title + description + category + tags + snippet

### 5.3 API 契约（任务 W3）

新增：
- `PATCH /v1/wiki/pages/{page_id}` — 按 id 精确更新/追加（自进化写回主通道）
  - 请求：`{"content": "...", "append": true|false, "title"?:, "category"?:, "tags"?:, "description"?:}`
  - `append=true`（默认）：content 追加到现有正文末尾（ADD-only），追加片段自带标记：`> 来源: <session_id> | hash: <sha256前8>`
  - **description 默认行为（P2）**：PATCH **默认不动 description**（追加经验不动页级摘要）；仅显式传 `description` 才更新
  - **append 幂等（P1-4）**：入口先查重——在现有 content 中检索 entry hash 标记，已存在则 no-op（幂等达成）；hash 内嵌 content 标记为一期落点，不建独立 entry 表（条目量大时二期再升表）
  - 后端：`operations/wiki.py` 补 `update_page`（调 `wiki_dao.update_page_content`，签名已含 title/content/category/tags），FTS 触发器自动同步
- 复用：`POST /v1/wiki/pages`（create/upsert，含 supersession 检测）、`GET /v1/wiki/pages?category=`、`GET /v1/wiki/search`

MCP（`sgme/mcp_server.py`）补：`wiki_page_update(page_id, content, append?)`。

### 5.4 自进化管线（任务 W4，SGME 侧独立 evolve 管线）

**管线归属（P0-2 修订）**：自进化是**第三条独立管线**（会话 → wiki 手册），与两条既有管线并行不交叉：

| 管线 | 输入 → 输出 | 归属 |
|---|---|---|
| engine（现有） | 会话 → memory.db（L1/L1.5/L2） | sgme/engine/ |
| refinery（现有） | 文件/URL/图片/视频 → wiki_pages | sgme/refinery/ |
| **evolve（本次新增）** | 会话 → wiki 手册（经验回写） | **sgme/operations/evolve.py**（新建） |

- **复用**：`sgme/llm` 降级链 + `prompts` 版本管理 + `operations/wiki.py` 写入操作；**不接 `refinery.refine`**（其入口只吃文件/URL/图片/视频，不接会话）
- **独立游标**：新增 `wiki_evolve` 进度表（session_id PK / processed_at / status：queued|done|skipped|rejected），与 memory 管线的 `refine_cursor` 完全分离——**不复用同一水位**（避免与 memory 提炼抢消费进度/重复提炼）

流程：

    触发：POST /v1/wiki/evolve/trigger（Agent Key；body: session_id 可选；缺省扫描 wiki_evolve 表中未处理会话）
      ▼
    费用门禁（首期简化，P2）：仅「session/end 且会话 ≥N 轮」（N 默认 5 可配）；其余信号（tool 失败/用户纠正等）二期再补
      ▼
    LLM 提炼（复用 sgme/llm 降级链 + prompts）：
      输入：会话内容 → 输出结构化 JSON：
        {type: append|create, category, title, entry, source_session}
      entry 格式：断言式（现象→原因→正确做法），≤200 字/条
      ▼
    规则闸门（已定，P1-9）：schema 校验 → 来源必填 → entry hash 去重
      → 通过：写入；可疑：拒绝 + 记审计（一期落点=审计日志/审计表，不建候选区表；
        二期如需人工复核再引入 status='pending' 或候选表）
      ▼
    写入：append → PATCH（目标手册「踩坑记录」章节，author=触发 agent，内嵌 hash）
         create → POST（新手册页，description 由提炼一并生成，触发 supersession 检测）
      ▼
    审计：wiki_evolve 表记录 {time, agent, session_id, action, entry_hash}

关键纪律：**回写与注入分离**——自进化只写 L2 手册，L1 常驻块冻结不自我改写（业界共识 + 缓存铁律）。

### 5.5 bridge 补工具（任务 W5，SGME adapters/dsh/sgme-bridge）

`src/tools.ts` 按现有 defineTool 模式补三个工具（对照 operations/wiki.py 已有操作）：
- `wiki_pages`：按 category 列手册（轻量字段：page_id/title/description/category/tags，不含正文）
- `wiki_page`：按 page_id 取全文
- `wiki_page_update`：按 page_id 追加/更新（自进化或人工维护用）

### 5.6 索引 skill 规范（任务 W6）

**源位置（P1-6 修订）**：源文件放 `adapters/dsh/skills/wiki-skill-discovery/SKILL.md`（随 SGME git 管理）；由 install 脚本（`adapters/dsh/install.py` 扩展）部署到消费端 skills 目录（`~/.agents/skills/` 等，可重建的部署副本）——**不直接写外部目录**（项目产物随 git 铁律）。

内容：
- frontmatter：name / description（触发式描述：任务需要 SGME/GitHub 等操作手册时加载）
- 正文三件事：①用 `wiki_pages` 按 `category=skill/<domain>` 列手册 ②读 title+description 判断加载哪本 ③用 `wiki_page` 拉全文执行
- L1 精选条目（若未来 catalog 从 wiki 拉）：title+description，确定性排序（category+updated_at DESC）

### 5.7 手册规范（任务 W7）

- category 命名：`skill/<domain>`（skill/sgme、skill/github、skill/airdt…）；设计/方案类 `design`
- 手册 frontmatter：name / description（LLM 辅助生成，触发式描述）
- 手册结构：功能 / 简介 / 详细步骤 + 末尾「踩坑记录」章节（自进化追加目标，只增不改）

## 6. 实施任务清单

| # | 任务 | 涉及文件 | 依赖 | 验收 |
|---|---|---|---|---|
| W1 | 迁移：db.py 加 5 列（_migrate_wiki_page_columns）+ fts.py FTS 扩展重建 + wiki_dao 参数 + operations 跳过列 | sgme/data/db.py、sgme/wiki/fts.py、sgme/data/wiki_dao.py、sgme/operations/wiki.py、tests | — | 重启即自动迁移；中文检索不降级（content_seg 保留）；老数据 status 默认 active；/v1/wiki/search 命中 description |
| W2 | 统一搜索过滤（skill 精确判断 + superseded 两路径） | operations/search.py、wiki/fts.py、wiki_dao.py、tests | W1 | pytest；/v1/search 不见 skill、wiki_search 可见；LIKE 兜底也过滤 superseded |
| W3 | PATCH 端点 + MCP wiki_page_update（含 append 幂等） | wiki/routes.py、operations/wiki.py、mcp_server.py、tests | W1 | PATCH 追加生效；hash 去重幂等；description 默认不动 |
| W4 | evolve 管线（触发/门禁/提炼/闸门/写入/审计 + wiki_evolve 游标） | operations/evolve.py（新）、llm、prompts、tests | W3 | 试点会话 → 手册追加经验条目；≥N 轮门禁生效；与 memory 提炼互不干扰 |
| W5 | bridge 补三工具 | adapters/dsh/sgme-bridge/src/tools.ts、tests | W1 | DSH 会话可调 wiki_pages/wiki_page/wiki_page_update |
| W6 | 索引 skill（源 + install 部署） | adapters/dsh/skills/wiki-skill-discovery/、install.py | W5 | DSH 走通「索引→拉手册→执行」；源在 git 内 |
| W7 | SGME 操作手册入库 | wiki（NAS 9910） | W1 | category=skill/sgme 手册可检索、description 达标 |

**顺序**：W1 → W2/W3/W5 并行 → W4（依赖 W3）→ W6/W7（消费端验证）。

## 7. 验证与试点方案

1. **迁移验证**：pytest（W1 新增「老库缺列自动补列」与「FTS 命中 description」用例）+ 服务重启即自动迁移（无需手动脚本）+ 中文检索回归（对照组：迁移前查询词命中行为一致）
2. **检索验证**：`/v1/search` 与 `wiki_search` 对照（回忆不见手册、执行可见；superseded 两路径均过滤）
3. **自进化试点（控费）**：手动触发 5-10 次（`POST /v1/wiki/evolve/trigger`）→ 检查提炼质量与费用 → 质量达标开自动（session/end + ≥N 轮）→ 观察一周费用 → 全速
4. **缓存验证**：DSH 会话中手册修改后，前缀 catalog 无变化（digest 不变），仅尾部追加
5. **多 agent 验证**：Hermes 侧调 SGME wiki 读同一手册（复用 adapters/hermes）

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| 自进化写坏共享手册 | 规则闸门（hash 去重/来源必填）+ ADD-only + 每日备份（wiki.db 在备份口径内）+ superseded 纠错 |
| 提炼质量不稳 | 试点期手动触发观察；entry ≤200 字断言式约束；可疑拒绝记审计 |
| LLM 费用超限 | ≥N 轮门禁（过滤大部分会话）+ 单会话一次提炼 + 每日上限可配 |
| FTS 重建影响既有检索 | 重建后中文回归测试；迁移前备份 wiki.db；三连败查文档 |
| 与 memory 提炼抢游标 | 独立 wiki_evolve 进度表，物理分离 |
| 与 Hermes 侧衔接不畅 | adapters/hermes 已有；Hermes 写回需其侧适配（二期） |

## 9. 待确认项（修订后收敛为 2 个）

1. **A 已定**：自进化规则闸门（自动：schema+来源+hash 去重；可疑拒绝记审计；二期再评估候选区表）
2. **B 已定**：schema 补 author/status/supersedes，锚点定义见 §5.1（P1-7 已解决）
3. **任务登记归属（P2）**：代码在 SGME 仓的任务（W1-W7 主线）登记 **SGME Backlog**；消费端联动（索引 skill 部署、bridge 工具在 DSH 侧的使用）登记 **AIRDT Backlog**；调研文档留在 AIRDT docs/research 仅作引用资产
4. **试点范围**：W1 + W6 + W7 先行（wiki 侧闭环、零 LLM 成本、风险最低），W4 自进化放到质量验证之后再开

## 10. 调研参照

- D:\Projects\AIRDT\docs\research\wiki-kb-benchmark\00-summary.md（综合）／01-tools-survey.md／02-methodology.md／03-industry-practice.md
- 已入 wiki：wiki渐进式披露共享知识库-设计决策-v0.1（NAS 9910）
