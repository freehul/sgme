---
name: sgme
description: SGME 拾光记忆引擎操作手册：服务发现、接入纪律、常用操作与排障。
tags:
  - skill
category: sgme
---

---
name: sgme-operations
description: SGME（拾光记忆引擎）操作手册——记忆查询/写入、知识库 wiki、信号、提炼、运维全流程。需要操作 SGME（查记忆、写知识库、管提炼、看健康）时加载本手册按步骤执行。
type: skill
---

# SGME 操作手册

> 拾光记忆引擎（Single-user Agent Memory Engine）——多 agent 共享的记忆/知识/经验中枢。
> 服务：NAS 192.168.10.10（HTTP :9910 / MCP :9913）。密钥走环境变量，不落明文。

## 一、功能总览

| 域 | 能力 | 入口 |
|---|---|---|
| 记忆 | L1.5 标签化记忆池：写入/检索/注入画像 | HTTP /v1/* + MCP 9913 |
| 知识库 | wiki_pages 知识页面（md 内容，FTS5 检索，category/tags 分类） | /v1/wiki/* |
| 信号 | 关怀信号（待办到期/情绪/过劳/每日） | DSH 桥接 signal_* |
| 提炼 | 会话→记忆 自动提炼管线（L1/L1.5/L2） | refine_* |
| 运维 | 健康/统计/备份/看门狗自愈 | /v1/health /v1/admin/* |

## 二、接入方式

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

## 三、核心操作步骤

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

## 四、配置与密钥

| 变量 | 用途 |
|---|---|
| SGME_BASE_URL | 服务地址（http://192.168.10.10:9910） |
| SGME_AGENT_KEY | Agent Key（非 admin 端点） |
| SGME_ADMIN_KEY | Admin Key（/v1/admin/*） |
| DEEPSEEK_API_KEY_SGME | 提炼用 LLM 密钥（降级链） |

规则：密钥只读环境变量，代码/配置禁止硬编码；不在对话中贴明文。

## 五、踩坑记录

（本章节由自进化追加，只增不改。格式：现象 → 原因 → 正确做法，带来源与时间戳）

> 来源: DeepSeek Agent | hash: 7d30b1ab

---

## 2026-08-17 工具更新（B77）

DSH 桥接（sgme-bridge 0.2.0）新增 **wiki_page_add** 工具：创建知识库页面（POST /v1/wiki/pages，title/content 必填，category/tags(逗号分隔)/description/author 可选，幂等 upsert，同 title+content 命中同一 page_id 更新）。至此 DSH 侧 wiki 读写工具齐全：wiki_search / wiki_pages / wiki_page / wiki_page_update / wiki_page_add。L1 skill sgme-operations 描述已补「写wiki/建知识库页面/记录经验」触发词，建页任务可直接触发加载本手册。

> 来源: dsh-agent | hash: 0f4cbc07

---

## 2026-08-18 模型配置更新（T-55 免费托底 + 主链切换）

**密钥表更正**（2026-08-18 主链与向量已切换）：

| 变量 | 用途 |
|---|---|
| ZHIPU_API_KEY | 提炼主链智谱 GLM-4.7-Flash（永久免费；key 缺失时 health 的 model_config.missing_keys 会提示） |
| DEEPSEEK_API_KEY_SGME | 提炼备用 deepseek（付费，主链限流/故障时兜底） |
| SILICONFLOW_API_KEY | 向量检索硅基流动 BAAI/bge-m3（1024 维，免费；实名认证后零费用） |

**提炼降级链**（2026-08-18 用户定）：zhipu(glm-4.7-flash, 免费主) → deepseek(deepseek-v4-flash, 付费备) → rule drop_batch。

**向量健康检查**：health 的 vector.connectivity 显示模型连通性（provider/model/latency_ms）；失效时写日志 + 发 anomaly_warn 信号（source=vector），/search 自动降级纯 BM25。

**免费 Key 申请**：智谱 https://open.bigmodel.cn（手机号注册，GLM-4.7-Flash 永久免费）→ ZHIPU_API_KEY；硅基流动 https://cloud.siliconflow.cn（注册 + 实名认证解锁免费模型，BAAI/bge-m3 调用零费用）→ SILICONFLOW_API_KEY。完整流程见 docs/guide/免费模型Key申请指南.md。

> 来源: dsh-agent | hash: e1d4200d

---

## 踩坑：生产 WebUI 角色管理页无内置角色模板（2026-08-18）

> 来源: DeepSeek Agent | 2026-08-18

- **现象**：生产（容器化）WebUI 角色管理页无内置角色（管家/伴侣/朋友/导师）
- **原因**：ROLES_DIR = $SGME_HOME/roles（容器 = /data/roles，挂载卷）；内置角色在镜像 /app/roles（Dockerfile 有 COPY）。entrypoint 首次启动只物化 sgme.yaml、**不物化 roles** → 空卷首次启动后角色目录为空（B63 容器化迁移缺陷；本机直跑时角色在项目根天然存在，容器化后丢失）
- **正确做法**：entrypoint 首次启动物化 /app/roles/*.json → $SGME_HOME/roles/（T-55/B80 已修）；已上线容器手工修复：`docker exec sgme sh -c 'mkdir -p /data/roles && cp /app/roles/*.json /data/roles/'`
- **经验**：容器化迁移时，所有"程序资源默认值"类数据（配置模板/内置角色/内置模板）都要检查是否有首次启动物化机制；SGME_HOME 指向空卷后，镜像内程序资源不会自动出现在用户数据目录
