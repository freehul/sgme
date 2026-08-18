# SGME 接口契约 v0.1

> **读者**：要「自己开发 SGME 适配器」的 AI Agent。读完本文件，你应该能独立完成：选接入方式 → 调对端点 → 实现最小动作集 → 自验合格。
> 本文件是**自包含的技术契约**，不再要求你通读《架构设计 v0.9》。人类运维请看 docs/runbook.md，接入策略决策树见《SGME-接入契约-v0.1》。

## 0. 一句话定位

SGME 是一个单用户 Agent 记忆引擎 Server：把你的会话提炼成标签化记忆，按场景注入回来。**你（接入的 Agent）负责两件事——把会话写进去（append），把记忆读出来（inject/search）**；提炼由服务端引擎做，你只负责触发。

## 0.5 部署前置：两个模型

SGME 的提炼与检索各依赖一个外部模型，**缺了会静默降级，不是故障**：

| 依赖 | 用途 | 配置 | 缺失后果 |
|---|---|---|---|
| 提炼 LLM | L1 提取记忆 | `DEEPSEEK_API_KEY`（或 providers.yaml 其它 LLM） | 降级直存（不丢但不出标签） |
| 向量 embedding | 语义检索 | `SILICONFLOW_API_KEY`（`search.vector` 段） | 检索降级纯 BM25（关键词仍可用） |

接入后先调 `health` 看 `llm.available` / `vector.available`，哪个 false 就补哪个的 key。

## 1. 端口与传输

| 服务 | 端口 | 说明 |
|---|---|---|
| HTTP API | **9910** | 本契约全部端点走这里 |
| MCP | **9913** | 与 HTTP 同进程、功能等价，无 hook 的 Agent 首选（连上自动发现工具） |
| SCSM（预留） | 9911 | 独立进程，与本契约无关 |

- 传输：HTTP/1.1 + JSON（UTF-8），无 gRPC / 消息队列
- 契约前缀 `/v1/`，破坏性变更升 `/v2/`
- 时间：ISO 8601（统一 UTC 存储）

## 2. 鉴权

请求头 `X-API-Key`（契约层）+ 可选 `Authorization: Bearer`（传输层，默认本机旁路关闭）。

| Key 类型 | 权限 |
|---|---|
| **Agent Key** | 只能调非 Admin 端点（append / inject / search / memory / events / health / sessions） |
| **Admin Key** | 全部端点（含 refine 触发、agent 注册） |

关键约定：

1. **Agent Key 从哪来**：管理员调 `POST /v1/admin/agents/register` 签发（`agt_*` 前缀，明文仅返回一次）；或环境变量 `SGME_AGENT_KEY` / `SGME_ADMIN_KEY` 自定义。
2. **默认开发 Key 仅限本机回环**：未设置 env 时的内置兜底 Key（`dev-agent-key-change-me` / `dev-admin-key-change-me`）只允许 127.0.0.1/::1/localhost，远程调用一律 403。
3. **错误码**：401 = Bearer 缺失/无效；403 = API Key 缺失/无效/无权限（agent key 调 admin 端点）。
4. 维度参数一律收注册表 id（`projects`/`tech_stack`…），**不收中文名**。

## 3. 通用错误结构

```json
{ "error": { "code": "ERR_INVALID_ARGS", "message": "dimensions 必须引用已注册维度 id", "details": {} } }
```

`ERR_INVALID_ARGS`(400) / `ERR_UNAUTHORIZED`(401) / `ERR_FORBIDDEN`(403) / `ERR_NOT_FOUND`(404) / `ERR_CONFLICT`(409) / `ERR_RATE_LIMITED`(429) / `ERR_INTERNAL`(500) / `ERR_LLM_UNAVAILABLE`(503)。

## 4. 核心端点（自研适配器必用）

### 4.1 POST /v1/append — 写入会话（Agent Key）

请求（content 字段格式见下方「L0 格式」）：

```json
{
  "session_key": "你的agent-id-<会话id>",
  "agent_id": "你的agent标识",
  "started_at": "2026-08-03T11:18:06Z",
  "ended_at": "2026-08-03T12:30:00Z",
  "source_type": "session",
  "content": "<L0 消息块文本，见下>"
}
```

响应 201：`{"file_id": "…", "path": "raw/sessions/….md", "status": "new"}`

**L0 content 格式（最易错，务必记牢）**：

```text
# 2026-08-03T11:18:06Z user
用户说了什么
# 2026-08-03T11:20:00Z assistant
你回了什么
```

- 首行必须是 `# {ISO时间戳} {role}`，role ∈ user/assistant/tool
- user 块用 `# ` 单井号；assistant/tool 块用 `## ` 双井号
- tool 块首行 `**tool**: {工具名}`
- 解析出 0 条消息 → 400（错误信息会提示需要 `# {ISO} {role}` 格式）

**幂等语义**：同 `session_key` + 同 `started_at` → 幂等丢弃（返回既有 file_id）；同 `session_key` + 不同 `started_at` → 追加（status 重置 new 重新提炼）。所以**每次写入用导出时刻作 started_at**，别用会话开始时刻（否则每轮被幂等丢弃）。

### 4.2 POST /v1/inject — 取画像（Agent Key）

请求：`{"mode": "daily", "max_tokens": 700}`（mode ∈ daily/coding/work/full，或 custom_filter）

响应 200：`{"tier0": {...}, "blocks": [{"title", "items": [...]}], "stats": {...}}`

- 纯结构化 SQL 查询，零 LLM 成本
- 空结果时 `stats.note` 会附可行动提示（"暂无相关记忆…请先 append"），**空不等于故障**

### 4.3 POST /v1/search — 三层检索（Agent Key）

请求：`{"query": "…", "scopes": ["memory","wiki","wiki_pages"], "limit": 10, "include_sources": true}`

- scope：`memory`（记忆池）/ `wiki`（或 `scenes`，L2 场景）/ `wiki_pages`（知识库页面）
- 响应结果带 `trace[]` 溯源链（记忆 → 场景 → 原始文件）
- 混合检索 BM25 + 向量 + RRF，`meta.routes` 显示实际命中的检索通道

### 4.4 GET /v1/events — 信号拉取（Agent Key，主动关怀用）

- `GET /v1/events/pull?subscriber_id=<你的agent_id>`：持久游标拉取（适合定时轮询）
- `GET /v1/events/stream?subscriber_id=<你的agent_id>`：SSE 长连实时推送（有常驻能力的首选）
- 事件三类：`care_*`（关怀）、`memory_updated`、`anomaly_warn`

### 4.5 GET /v1/health — 就绪检查（Bearer 即可）

```json
{ "status": "ok", "version": "…",
  "llm": { "provider": "deepseek", "available": true },
  "refinement": { "queue_depth": 0 },
  "vector": { "available": true } }
```

接入后第一件事调它。`llm.available=false` → 提炼降级直存（不丢但不出标签）；`vector.available=false` → 纯 BM25。

### 4.6 记忆纠错（Agent Key，可选）

- `GET /v1/memory/{memory_id}`：单条记忆 + 溯源 + 归档链
- `POST /v1/memory/{memory_id}/reject`：标记「不采用」（不删、可恢复）

### 4.7 Admin 端点（自研适配器的提炼/注册）

| 端点 | 用途 |
|---|---|
| `POST /v1/admin/refine/trigger_async` | 异步触发提炼（后台立即返回，**适配器收尾用它**） |
| `POST /v1/admin/agents/register` | 签发 Agent Key |
| `GET /v1/admin/stats` | 数据规模/水位 |

## 5. 自研适配器必须实现的动作（最小合格集）

一个「合格」的 hooks 型适配器，至少要覆盖这 4 个动作：

| # | 动作 | 端点 | 时机 |
|---|---|---|---|
| 1 | **捕获** | `POST /v1/append` | 会话结束（有每轮 hook 则每轮） |
| 2 | **提炼** | `POST /v1/admin/refine/trigger_async` | 捕获后异步触发 |
| 3 | **注入** | `POST /v1/inject` | 会话开始 |
| 4 | **检索** | `POST /v1/search` | 会话中按需 |

**可选加分项**（最大化发挥，非必做）：

- 信号闭环：`GET /v1/events` 消费 `care_*` → 主动关怀
- 记忆纠错：`reject` / `unreject`
- 历史补导入：幂等重跑存量会话

## 6. 验收标准（装完怎么算合格）

按序自检，全过即合格：

1. `GET /v1/health` → `status=ok` 且 `llm.available=true`
2. 写一条测试会话 → `append` 返回 201 + `file_id`
3. `POST /v1/admin/refine/trigger_async` → 稍后 `health` 的 `queue_depth` 归零
4. `search` 命中刚写入的记忆（带 trace 溯源）
5. `inject` 返回含内容的画像块

## 7. 参考实现

- 官方适配器源码：`adapters/hermes/`（memory.provider 槽位）、`adapters/dsh/`（Cordis 插件）——自研时照抄其「捕获→提炼→注入→检索」映射即可
- L0 幂等/增量导出/游标推进的完整配方：见项目 `docs/design/` 下 L0 格式与适配器开发约定

> 本契约对应架构 v0.9 §22，以源码实现为准；发现本文件与实现不符，以实现为准并回报修正。