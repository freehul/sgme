# SGME WebUI 设计 v0.1

- **状态**：设计基线 + 实现标注（2026-08-12 基线；2026-08-13 全量页面落地并回标）
- **关联需求**：ST-7（记忆引擎 WebUI 管理面板，4 导航 22 视图，含创意池管理 UI——原 ST-17 并入）、T-26~T-33（全部已解决）
- **前置**：T-26 已接线 `/v1/admin/ideas`（routes_ideas.py + app.py）；`ideas` 维度已注册（registry/dimensions.yaml）

---

## 1. 技术栈与工程布局

- **形态**：前后端分离 SPA（用户已定，2026-08-12）
- **前端**：Vue 3 + Vite + TypeScript（默认选型，可替换；不引入重型 UI 框架，用轻量组件自建以贴合单用户管理面板）
- **后端托管（用户已定）**：源码放项目 `ui/` 目录；开发用 Vite dev server + 代理到 `:9910`；生产构建 `dist/` 由 FastAPI 静态托管
- **数据源**：全部消费 `/v1/*` 与 `/v1/admin/*` HTTP 契约（见架构 v0.9 §22），不直连数据库

### 目录结构（规划）

```
ui/
├── index.html
├── vite.config.ts        # dev proxy: /v1 → http://127.0.0.1:9910
├── src/
│   ├── main.ts
│   ├── router.ts         # 4 导航路由
│   ├── api/              # 端点封装（每模块一个 client）
│   ├── views/            # 22 个页面视图
│   ├── components/       # 通用组件（分页/表格/过滤器/状态徽标）
│   └── stores/           # 轻量状态（当前筛选、分页）
└── dist/                 # 构建产物，由 FastAPI 托管
```

FastAPI 侧：`app.py` 挂载静态目录（`ui/dist`），根路径回 `index.html`（SPA history 路由）；`/v1/*` 保持 API。鉴权 key 由前端存储（localStorage 可接受，单用户；生产建议经 Bearer + 登录界面临时持有）。

## 2. 信息架构（4 导航 / 22 视图）

| 导航 | 视图数 | 分类 | 实现状态 |
|---|---|---|---|
| ① 总览 Dashboard | 5 | 健康 / 概览 / 提炼监控 / Dream 日报 / 事件流 | ✅ 已实现（DashboardView 单页多区块，T-30） |
| ② 记忆与知识 | 6 | 记忆列表 / 记忆详情 / 场景 / 统一检索 / Wiki / 会话原文 | ✅ 已实现（6 独立页，T-31） |
| ③ 创意与需求 | 5 | 创意池 / 创意详情 / 需求池 / 需求状态 / 项目池 | ✅ 已实现（T-29；需求状态流转在 DemandList 内） |
| ④ 配置与管理 | 6 | 模板 / 维度注册表 / Agent / 提示词 / 系统配置 / 备份与技能 | ✅ 已实现（SettingsView 9 标签页 + SkillsView 独立页，T-32；**系统配置并入通用设置/TTL/扩展模块标签页，另增模型供应商与降级链标签页**） |

> **实现偏差说明（2026-08-13）**：① 落地为**扁平侧边栏**（记忆浏览/场景管理/提炼监控/设置 + 扩展功能 Wiki/技能仓库），未做设计稿的二级分组导航；② 原设计 ④ 导航的模板/注册表/Agent/提示词/备份等独立路由统一 redirect 合流到 `/settings` 标签页（router.ts 8 条 redirect）；③ 新增「模型供应商与降级链」标签页（对应后端 T-33 LLM 管理端点）；④ 技能仓库 SkillsView 作为扩展功能独立导航页。行为等价、入口更少，验收以视图功能为准。
>
> **导航调整（2026-08-14）**：③「创意与需求」分组导航顺序为 `创意池 → 项目池 → 待办`；「项目」改名「项目池」（路由 `/projects` + 路由 meta 标题同步）并移动到「待办」上方。

> 22 视图清单、数据源端点、关键交互见 §4 视图明细表。

## 3. 视觉与交互原则

- 中文界面（维度/状态/枚举展示层映射中文，请求侧用注册表 id——对齐架构铁律 #3）
- 状态徽标语义：active=正常 / rejected=用户判错 / expired=过时 / archived=归档（status 型字段，可用语义色）
- 列表统一分页信封 `{items, count, total, page, limit}`；limit 上限 200
- 写入型操作（编辑/标记/删除/签发）均需确认；软删除展示「可恢复」提示
- 溯源：记忆/创意详情提供「来源 → 场景 → 原始文件」链路跳转

## 4. 视图明细表（22 视图）

### ① 总览 Dashboard

| # | 视图 | 数据源 | 关键交互 |
|---|---|---|---|
| 1 | 系统健康 | `GET /v1/health` | 状态徽标（llm/vector/refinement.queue_depth/watermark）；异常引导 |
| 2 | 数据概览 | `GET /v1/admin/stats` | 计数卡片（记忆/场景/会话/Agent）+ 维度分布图 |
| 3 | 提炼监控 | `GET /v1/admin/refine_runs` + `GET /v1/admin/stats/detail` | 提炼记录分页（默认含 error/running）、token 成本图表（daily/weekly/monthly） |
| 4 | Dream 日报 | `GET /v1/admin/dream/reports` + `GET .../reports/{date}` | 日报列表 + MD 渲染；`POST /v1/admin/dream/trigger` 手动触发 |
| 5 | 事件流 | `GET /v1/events` | 游标拉取、type 过滤（memory_updated/anomaly_warn） |

### ② 记忆与知识

| # | 视图 | 数据源 | 关键交互 |
|---|---|---|---|
| 6 | 记忆列表 | `GET /v1/admin/memories` | 维度（**AND 语义**——勾选维度全部命中，2026-08-13 由 OR 改）/状态/排序/时间窗过滤；ttl_filter |
| 7 | 记忆详情 | `GET /v1/memory/{id}`、`POST .../reject`、`POST .../unreject` | 溯源链展开、拒绝/恢复（含 reason） |
| 8 | 场景列表 | `GET /v1/admin/scenes`、`POST /v1/admin/scenes/{id}/status` | 状态标记（active/rejected/expired） |
| 9 | 统一检索 | `POST /v1/search` | scope（memory/wiki）、结果 + routes 展示 + 溯源 trace |
| 10 | Wiki 页面 | `GET /v1/wiki/pages`、`GET .../pages/{id}?view=html`、`/export` | 列表/详情渲染/导出自包含 HTML |
| 11 | 会话原文 | `GET /v1/admin/sessions`、`GET .../sessions/{file_id}` | L0 原文查看（溯源根） |

### ③ 创意与需求

| # | 视图 | 数据源 | 关键交互 |
|---|---|---|---|
| 12 | 创意池 | `GET /v1/admin/ideas` | 列表/检索/标记过滤/状态过滤；路由 `/ideas`（ST-7 AC） |
| 13 | 创意详情 | `GET/PATCH /v1/admin/ideas/{id}`、`POST .../notes`、`PUT .../flag` | 编辑内容/优先级、追加备注、设置标记、**升格** |
| 14 | 需求池 | `GET /v1/admin/demands` | 状态/项目/优先级过滤 |
| 15 | 需求状态 | `PUT /v1/admin/demands/{id}/status` | 状态流转（pending→planned→partial→done） |
| 16 | 项目池 | `GET/PATCH /v1/admin/projects` | 列表/编辑 project_meta（path/git_repo/milestone） |

### ④ 配置与管理

| # | 视图 | 数据源 | 关键交互 |
|---|---|---|---|
| 17 | 模板管理 | `GET/POST/PUT/DELETE /v1/admin/templates` | 可视化编辑模板、YAML 回填、内置模板禁删 |
| 18 | 维度注册表 | `GET /v1/admin/registry` | 维度/别名查看、新增/停用/别名增删 |
| 19 | Agent 管理 | `GET /v1/admin/agents`、`POST .../register`、`DELETE .../{id}` | 签发 Key（明文仅一次展示）、吊销（default 禁吊销） |
| 20 | 提示词管理 | `GET /v1/admin/prompts`、`POST .../publish`、`.../activate`、`.../ab`、`GET .../metrics` | 版本列表/发布/激活/A-B 配置/指标 |
| 21 | 系统配置 | `GET/PUT /v1/admin/config` | 白名单段热改（sgme.yaml 可写段） |
| 22 | 备份与技能 | `POST /v1/admin/backup/create`、`GET .../backup/list`、`POST .../restore`、`POST /v1/admin/skills/sync` | 触发快照、备份列表/恢复、技能同步 |

## 5. 创意池升格联动（用户已定：联动创建需求）

- **语义**：创意「升格」= 置 `custom_flag='promoted'` **并**创建一条需求（`demands`），回填 `origin_idea_id` 闭合溯源链
- **后端**（**已实现 2026-08-12**）：`POST /v1/admin/ideas/{id}/promote`
  - Body：`{"title"(必填), "content", "priority"(缺省 50), "project_id"(可选), "source_ref"(可选)}`
  - operations/idea.py `promote_idea`：置 flag（先置，失败即中止）+ 复用 `demand.create_demand`（传 `origin_idea_id`）
  - data 层已有 `demands.origin_idea_id` 与 `idx_demands_origin_idea` 索引，无需 DDL
  - 测试：`tests/test_routes_ideas.py` 增 2 用例（升格成功闭环 / title 缺失 400 + 不存在 404）
- **前端**：创意卡「升格」按钮 → 弹层填 title/content → 调 promote；成功后跳转需求详情
- 已有 `POST /v1/admin/demands` 支持 `origin_idea_id`（只存不校验），promote 端点复用该字段

## 6. 待办 / 后续

- [x] 实现 `POST /v1/admin/ideas/{id}/promote`（升格联动，§5，2026-08-12 已完成）
- [x] 搭建 `ui/` 工程骨架（Vite + Vue，2026-08-12 已完成）：`package.json`/`vite.config.ts`（dev proxy 9910）/`src`（router + api client + MainLayout + 创意池完整页 IdeaList + 占位页）；`npm run build` 验证通过
- [x] FastAPI 静态托管（2026-08-12 已完成）：`app.py` 挂载 `ui/dist`（存在即挂载）+ SPA catch-all（非 /v1 回 index.html）；`.gitignore` 排除 `/ui/dist/` `/ui/node_modules/`
- [x] 按 §4 视图明细逐页实现（2026-08-12/13 全部完成）：① DashboardView（T-30）② 记忆/场景/检索/会话/Wiki 6 页（T-31）③ 创意/需求/项目 4 页（T-29）④ SettingsView 9 标签页 + SkillsView（T-32）；对应后端 LLM 管理端点（T-33）；`npm run build` 通过（约 50 模块）
- [x] 更新 Backlog 设计文档索引登记本文件（2026-08-12 已登记）
- [ ] ST-7 浏览器逐视图验收（Backlog 标 🔴 待验收）：健康检查 UI、各页 CRUD 实测、鉴权 403 引导、SPA 刷新路由回退

## 7. LLM 供应商与降级链管理（T-33 设计补充，2026-08-13）

- **动机**：原设计 ④ 无 LLM 管理视图；实施中发现设置页需要「模型供应商与降级链」配置能力，且后端无管理端点
- **后端**（已实现）：`sgme/operations/llm.py` + `sgme/server/routes_llm.py`
  - `GET /v1/admin/llm` → 降级链结构（chains）+ 链级规则（rules）+ 供应商连接信息（providers）
  - `GET /v1/admin/llm/health` → 逐供应商健康探测（robust，同步探测每个非 rule 供应商）
  - `POST /v1/admin/llm/providers` → 新增/更新供应商连接信息（写回 providers.yaml）
  - `DELETE /v1/admin/llm/providers/{provider}` → 删除（被链引用时拒绝）
  - `PUT /v1/admin/llm/embedding/active` → 切换当前向量提供商（T-43，2026-08-13 补充；`GET /v1/admin/llm` 响应含 `vector_current`）
  - `PUT /v1/admin/llm/chains` → 整体更新降级链（**T-47**，2026-08-13：增删节点 + 排序，写回 llm.yaml 并刷新运行时）
- **边界**：供应商/链概属 llm.yaml/providers.yaml 程序资源；本模块只读子集 + 供应商连接表增删管理入口 + **降级链可编辑**（T-47 起支持 UI 改链，写回 llm.yaml 保留 rules 段）
- **统一供应商模型（T-47，2026-08-13）**：向量提供商从 providers.yaml 顶层 `embedding` 段并入顶层 `providers` 段，统一以 `vector_capable=true` 标记；providers 段写回只覆盖自身、保留 embedding 等其余段（向后兼容）；`llm_status` 返回统一 providers（各供应商带 vector_capable/models 标记），`llm_embedding_set_active` 从 vector_capable 供应商选向量模型
- **前端**：SettingsView「模型供应商与降级链」标签页（ProvidersView.vue）：**模型供应商**（原「供应商」改名 + 移最上）+ **模型降级链**（原「降级链」改名，可编辑：增删节点/上下排序/选供应商/填模型 + 保存/撤销）+ 供应商增删（表单含「向量模型」checkbox，卡片显示「向量」标签）+ **向量模型区块**（从 vector_capable 供应商卡片中选，含「使用中/设为当前」切换，当前 provider 高亮 + 连通状态——T-47 起「刷新健康」探测范围并入 vector_capable 供应商）；链级规则卡片已删除（用户反馈精简）；保存供应商后顶部横幅反馈

## 7.1 统一检索 wiki_pages scope（T-34 前端同步，2026-08-13）

- **动机**：后端 `/v1/search` 已支持 `wiki_pages` scope（T-34），WebUI 统一检索页需同步——用户可勾选「知识库」纳入检索
- **前端**：SearchView.vue 增加 `wiki_pages` scope 勾选（默认开启）+ 「知识库」标签 + 结果展示 title；点击 wiki_pages 结果跳转 `/wiki?page_id=` 直达 Wiki 详情（WikiView 消费 query 自动打开详情抽屉）；api/memory.ts `SearchResult` 补 `page_id`/`title`

## 与架构约束的兼容

- 请求侧维度一律用注册表 id（§4 各列表过滤），中文仅展示
- 前端只消费 HTTP 契约，不直连 DB / 不绕过 operations 层
- 写入操作全部走 admin key 鉴权（403 缺失）