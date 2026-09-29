---
name: sgme
description: SGME 能力平面总手册：memory、skills、wiki 三大核心模块的接入、路由、按需调用与排障。
tags:
  - skill
  - memory
  - skills
  - wiki
category: sgme
version: 2.0.0
pattern: auto
uses:
  - skill-registry-protocol
---

# SGME 操作手册

> 拾光记忆引擎（Single-user Agent Memory Engine）——多 agent 共享的记忆/知识/经验中枢。
> 服务：NAS <NAS_IP>（HTTP :9910 / MCP :9913）。密钥走环境变量，不落明文。

## 一、能力平面：接入后不再堆装 skill

SGME 是 Agent 的长期能力平面。接入后，宿主只需要一个适配器（或通用 MCP）；
专业能力默认留在 SGME 技能库中，按需检索和注入，不要把几十个技能全文复制到本地。

| 核心模块 | 负责什么 | 先用什么 | 典型入口 |
|---|---|---|---|
| **memory** | 用户/项目历史事实、偏好、会话捕获、画像注入、提炼与溯源 | `inject` / `search` | `append`、`inject`、`search(scopes=["memory"])`、`memory_get`、`refine_*` |
| **skills** | 可复用的专业流程、工具链和领域能力；按需加载全文 | `skill_search` → `skill_digest` → `skill_get` | `skill_list`、`skill_search`、`skill_get`、`skill_materialize` |
| **wiki** | 持久知识、手册、设计文档、经验、踩坑和自进化写回 | `wiki_search` / `wiki_pages` | `wiki_search`、`wiki_pages`、`wiki_page`、`wiki_page_add/update` |

### 任务路由规则

1. 用户问「以前、上次、记得、我的偏好、项目历史」：先查 **memory**，查不到就明确说记忆库未找到。
2. 需要长期保存的手册、方案、经验、踩坑：写入 **wiki**；写前先 `wiki_search` 避免重复。
3. 需要某个框架、工具链、专业流程：先搜 **skills**，看摘要后再取全文；不要凭空声称拥有该能力。
4. 任务同时涉及三类信息时：先 memory 取上下文，再 skills 取执行方法，最后把稳定经验沉淀到 wiki。
5. 找不到合适 skill 时，不安装一堆替代品，也不编造步骤；报告 SGME 技能库缺口，必要时再请主人补充或纳管。

### Agent 日常闭环

`health → inject/search → 执行任务 → append → refine_trigger(async) → wiki/skill 按需沉淀`

`skill_materialize` 只用于宿主适配器或确实需要本地文件的工具包；普通专业 skill 保持在 SGME 中按需读取。

### 能力技能索引

SGME 已把常用开发能力纳入统一技能库，Agent 不需要另外批量安装：

| 任务 | 技能 | 检索词 |
|---|---|---|
| Gateway 健康、记忆、提炼、wiki 和技能索引运维 | `sgme-operations` | `SGME operations` |
| 编码、TDD、调试、审查、验证、CodeGraph、提交 | `coding-workflow` | `coding development` |
| SGME 引擎、数据库、提炼管线 | `sgme-development` | `SGME engine development` |
| 六宿主适配器开发与发布 | `sgme-adapter-development` | `SGME adapter development` |
| 设计、Backlog、onboarding、runbook | `sgme-docs-authoring` | `SGME documentation` |
| 技能命名、去重、合并、删除和生命周期 | `skill-governance` | `skill governance` |

## 二、功能总览

| 域 | 能力 | 入口 |
|---|---|---|
| 记忆 | L1.5 标签化记忆池：写入/检索/注入画像 | HTTP /v1/* + MCP 9913 |
| 技能库 | 专业流程与工具能力（渐进式披露，支持整包物化） | /v1/skills/* + MCP skill_* |
| 知识库 | wiki_pages 知识页面（md 内容，FTS5 检索，category/tags 分类） | /v1/wiki/* |
| 信号 | 关怀信号（待办到期/情绪/过劳/每日） | DSH 桥接 signal_* |
| 提炼 | 会话→记忆 自动提炼管线（L1/L1.5/L2） | refine_* |
| 运维 | 健康/统计/备份/看门狗自愈 | /v1/health /v1/admin/* |

## 三、接入方式

### 1. HTTP API（:9910）
- 鉴权头：X-API-Key: <Agent Key>（Agent Key 调非 admin 端点；Admin Key 调 /v1/admin/*）
- 健康检查：GET /v1/health（无需 X-API-Key，Bearer 可选）
- 关键端点：
  - POST /v1/append：写入会话（session_key/started_at/content/agent_id/ended_at）→ 触发提炼
  - POST /v1/inject：注入画像（mode=daily 等，max_tokens 控制）
  - POST /v1/search：统一检索（scopes=[memory, wiki/scenes, wiki_pages]）
  - GET /v1/wiki/search?q=：知识库检索（FTS5 BM25 + LIKE 兜底）
  - GET /v1/wiki/pages?category=：按分类列页面
  - GET /v1/wiki/pages/{page_id}：页面详情
  - POST /v1/wiki/pages：直接写入页面（title+content，幂等 upsert，可带 description）
  - PATCH /v1/wiki/pages/{page_id}：按 id 精确更新/追加（append 默认追加 ADD-only + hash 去重幂等，description 默认不动）
  - POST /v1/wiki/ingest：提交提炼任务（text/file/url → refinery → wiki_pages）
  - POST /v1/wiki/evolve/trigger：自进化触发（会话→经验→写回手册）

### 2. MCP（:9913，同进程）
工具集：append / inject / search / memory_get / memory_reject / refine_trigger / refine_batch / refine_status / stats / health / wiki_search / wiki_pages / wiki_page / wiki_page_add / wiki_page_update / wiki_evolve_trigger / config_get / agent_onboarding

### 3. DSH 桥接（dsh-sgme 插件，会话内工具）
memory_search（L1.5 记忆池检索）/ wiki_search（知识库检索）/ wiki_pages / wiki_page / signal_pull / signal_claim / signal_ack（关怀信号闭环）

## 四、核心操作步骤

### 1. 查记忆（"之前/以前/还记得"类问题必用）
1. 调 memory_search（DSH）或 POST /v1/search scopes=["memory"]（HTTP）
2. 按维度过滤（identity/projects/status/focus/tasks/goals/ideas）
3. 查不到如实说"记忆库未找到"，不编造

### 2. 写知识库 wiki
1. 判断归属分类：技能/手册 → category=skill/<domain>；设计方案 → category=design
2. 调 wiki_search 确认是否已存在同类页面（避免重复）
3. 写入：POST /v1/wiki/pages（title/content/category/tags/description），重复提交幂等
4. 验证：wiki_search 能检索到

### 3. 会话入库与提炼
1. DSH 侧 session-sync 自动把 turn 累积成会话 POST /v1/append（幂等）
2. 提炼自动跑：L0 → L1 → L1.5 冲突合并 → L2 场景
3. 手动触发：MCP refine_trigger（file_id 可选，limit=50，async_mode）

### 4. 关怀信号（DSH）
1. signal_pull 拉取未消费信号
2. signal_claim 原子认领（防多 agent 重复关怀）
3. 处理完 signal_ack 写回执（claimed/acked/failed）

### 5. 自进化（经验回写）
1. 会话后自动/手动触发：POST /v1/wiki/evolve/trigger 或 MCP wiki_evolve_trigger
2. 费用门禁（消息块 ≥ min_rounds）→ LLM 提炼 → 规则闸门 → 写入手册踩坑记录
3. 审计：wiki_evolve 表记录每次运行

### 6. 运维
- 健康：GET /v1/health 或 MCP health
- 统计：MCP stats
- 备份：/v1/admin/backup（每日自动 + 三库口径 memory/session/wiki）
- 重启：SSH NAS 重启容器（看门狗自愈 + 每日备份兜底）

## 五、配置与密钥

| 变量 | 用途 |
|---|---|
| SGME_BASE_URL | 服务地址（http://<NAS_IP>:9910） |
| SGME_AGENT_KEY | Agent Key（非 admin 端点） |
| SGME_ADMIN_KEY | Admin Key（/v1/admin/*） |
| `providers.yaml` 中声明的 `api_key_env` | 提炼/向量服务密钥；以 `health.model_config.missing_keys` 为准 |

规则：密钥只读环境变量，代码/配置禁止硬编码；不在对话中贴明文。

## 六、踩坑记录

（本章节由自进化追加，只增不改。格式：现象 → 原因 → 正确做法，带来源与时间戳）

> 来源: DeepSeek Agent | hash: 7d30b1ab

---

## 2026-08-17 工具更新（B77）

DSH 桥接（sgme-bridge 0.2.0）新增 **wiki_page_add** 工具：创建知识库页面（POST /v1/wiki/pages，title/content 必填，category/tags(逗号分隔)/description/author 可选，幂等 upsert，同 title+content 命中同一 page_id 更新）。至此 DSH 侧 wiki 读写工具齐全：wiki_search / wiki_pages / wiki_page / wiki_page_update / wiki_page_add。L1 skill sgme-operations 描述已补「写wiki/建知识库页面/记录经验」触发词，建页任务可直接触发加载本手册。

> 来源: dsh-agent | hash: 0f4cbc07

---

## 模型配置（2026-08-29 更新：agnes 主链，zhipu 移出）

**密钥表**（现状）：

| 变量 | 用途 |
|---|---|
| AGNESAI_API_KEY | 提炼主链 agnes-2.5-flash（免费，$0/1M token；key 缺失时 health 的 model_config.missing_keys 会提示） |
| SILICONFLOW_API_KEY | 提炼备用 THUDM/GLM-4-9B-0414（免费档；V4-Flash 转付费已移出 B144）+ 向量检索硅基流动 BAAI/bge-m3（1024 维，免费；实名认证后零费用） |

**提炼降级链**（2026-08-22 agnes 主位；2026-08-29 zhipu 免费 Key 失效移出链 B121；2026-09-01 DeepSeek-V4-Flash 转付费移出链 B144）：agnes(agnes-2.5-flash, 免费主) → siliconflow(THUDM/GLM-4-9B-0414, 免费档备) → rule drop_batch。

**向量健康检查**：health 的 vector.connectivity 显示模型连通性（provider/model/latency_ms）；失效时写日志 + 发 anomaly_warn 信号（source=vector），/search 自动降级纯 BM25。

**免费 Key 申请**：Agnes https://agnes-ai.cn（邮箱注册，agnes-2.5-flash 免费）→ AGNESAI_API_KEY；硅基流动 https://cloud.siliconflow.cn（注册 + 实名认证解锁免费模型，BAAI/bge-m3 调用零费用）→ SILICONFLOW_API_KEY。完整流程见 AI-INSTALL/免费模型Key申请指南.md。

> 来源: dsh-agent | hash: e1d4200d

---

## 踩坑：生产 WebUI 角色管理页无内置角色模板（2026-08-18）

> 来源: DeepSeek Agent | 2026-08-18

- **现象**：生产（容器化）WebUI 角色管理页无内置角色（管家/伴侣/朋友/导师）
- **原因**：ROLES_DIR = $SGME_HOME/roles（容器 = /data/roles，挂载卷）；内置角色在镜像 /app/roles（Dockerfile 有 COPY）。entrypoint 首次启动只物化 sgme.yaml、**不物化 roles** → 空卷首次启动后角色目录为空（B63 容器化迁移缺陷；本机直跑时角色在项目根天然存在，容器化后丢失）
- **正确做法**：entrypoint 首次启动物化 /app/roles/*.json → $SGME_HOME/roles/（T-55/B80 已修）；已上线容器手工修复：`docker exec sgme sh -c 'mkdir -p /data/roles && cp /app/roles/*.json /data/roles/'`
- **经验**：容器化迁移时，所有"程序资源默认值"类数据（配置模板/内置角色/内置模板）都要检查是否有首次启动物化机制；SGME_HOME 指向空卷后，镜像内程序资源不会自动出现在用户数据目录
