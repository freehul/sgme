# SGME Agent Onboarding 指引

> 写给 AI Agent 读的接入指南。人类运维请移步 [docs/runbook.md](runbook.md)。
> 你（Agent）读完本文件，应该能独立完成：找到 SGME → 连上 → 开始写记忆 → 触发提炼 → 查进度。

## TL;DR（30 秒速览）

1. **发现**：探测 `http://localhost:9910/v1/health`；失败读 `~/.sgme/install.json`；仍失败就向用户报告「SGME 未发现」。
2. **连接**：MCP 端点 `http://localhost:9913/mcp`（HTTP API 为 `:9910`），带 `X-API-Key`。
3. **接入**：先调 `agent_onboarding` 工具（连接即发现，版本/能力/上手一条龙）。
4. **写入**：每轮对话结束 `append` 当前轮次；会话结束 `refine_trigger(async_mode=true)`。
5. **提炼**：批量 ≥20 文件必须分批 + 批间 30–60s；429 不立即重试，交 `batch_scan` 兜底。

---

## 1. 服务发现三步（找不到 SGME 怎么办）

SGME 装在哪、端口多少、用什么 Key——三步定位，零人工依赖：

| 步骤 | 动作 | 成功 → | 失败 → |
|---|---|---|---|
| ① | 探测 `GET http://localhost:9910/v1/health` | 拿到了，直接进 §2 | 下一步 |
| ② | 读安装清单 `~/.sgme/install.json`（地址/端口/Key 引用） | 按清单连接 | 下一步 |
| ③ | 就绪报告「SGME 未发现」，提醒用户安装/启动 | — | — |

**项目目录语义**：用户以 CLI `--dir` 指定或 Desktop 打开项目文件夹时，`install.json` 就在该目录的 `.sgme/` 下——由你（Agent）负责读，不要问用户要路径。

## 2. 连接与鉴权

| 项 | 值 |
|---|---|
| MCP 端点 | `http://localhost:9913/mcp`（streamable HTTP） |
| HTTP API | `http://localhost:9910`（功能等价，两协议共享同一引擎） |
| Agent Key | 请求头 `X-API-Key`（HTTP）；MCP 按客户端配置注入 |
| 默认开发 Key | `dev-agent-key-change-me` / `dev-admin-key-change-me`——**仅限本机回环**（127.0.0.1/::1/localhost），远程调用一律 403 |

**多 Agent 共存的正确姿势**：管理员先为每个 Agent 签发独立 Key（`POST /v1/admin/agents/register`，返回 `agt_*` 明文仅此一次），见 runbook §9.1。用默认 Key 做远程接入会撞 403，别试。

### MCP 连接示例（Python，官方 mcp 库，2026-08-14 实测跑通）

> ⚠️ **别裸调 streamable-http**：手动 HTTP 按 `initialize → notifications/initialized → tools/call` 三步走仍可能报 `Session not found`（FastMCP 1.28 会话管理依赖官方 SDK 的握手/SSE 读取语义）。**推荐直接用官方 `mcp` 客户端**（本项目 venv 已装 `mcp 1.28.1`），以下为最小可跑示例：

```python
import asyncio, os
# 防 Clash 代理劫持 localhost（SGME 铁律：httpx 调用必须等价 trust_env=False）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    key = os.environ.get("SGME_AGENT_KEY", "dev-agent-key-change-me")
    async with streamablehttp_client(
        "http://127.0.0.1:9913/mcp", headers={"X-API-Key": key}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("agent_onboarding", {})
            for c in res.content:
                print(c.text)

asyncio.run(main())
```

连接成功后第一件事就是调 `agent_onboarding()`（见 §3），拿 `self_config.template` 写入自己的身份文件。

## 3. 连接即发现：agent_onboarding 工具

连上后第一件事：调 `agent_onboarding()`。它返回 SGME 版本、**全部 16 个工具的清单与用途**、快速上手三条主线（注册/提炼/回忆）、**自助配置段（self_config）**。信息以它为准，本文件只是它的展开版。

**自助配置（2026-08-13，接入完成标准）**：`agent_onboarding` 返回 `self_config` 段——要求接入的 agent **自己修改自己的身份文件**（SOUL.md / AGENTS.md / CLAUDE.md 等，按工具机制自选），把返回的模板（版本标记 `SGME-ONBOARDING-v1`）写入。SGME 不替 agent 改文件，只要求结果、不限制过程：

1. 自查身份文件是否已有 `SGME-ONBOARDING` 标记（版本 ≥ v1 即完成，跳过）；
2. 缺失或版本旧 → 按模板写入自己的身份文件（写入位置/方式按工具的文件组织与加载机制自定）；
3. 写入后**读回验证**内容完整，并向主人汇报（写了哪个文件、哪个位置）；
4. 无法写入（权限/沙箱/审批限制）→ **如实报告主人并给出建议，禁止谎称已完成**（Trae 的 user_rules 目录有沙箱写保护实锤）。

> 目的：接入从「人复制 README 提示词」变为「agent 连接后自助配置」——换 Agent 零成本：不管工具的文件机制差异，只要求结果（下个会话起 SGME 纪律生效）。

**MCP 16 工具清单与用途**（PR#7 + T-22 补 wiki 三工具）：

| 工具 | 用途 | 一句话用法 |
|---|---|---|
| `agent_onboarding` | 连接即发现：版本/能力清单/快速上手 | 接入后第一个调它 |
| `append` | L0 捕获：写入原始会话（幂等） | `append(session_key, started_at, content)`，content 首行须 `# {ISO时间戳} {role}` |
| `inject` | 记忆注入：按模式模板返回画像块 | `inject(mode='daily')` 拿当日画像 |
| `search` | 混合检索：BM25 + 向量 + RRF，带溯源（记忆池） | `search(query, limit)` |
| `wiki_search` | 检索 wiki 知识库（wiki_pages 知识文档，FTS5 BM25 + 兜底） | `wiki_search(query, limit)` |
| `wiki_pages` | wiki 页面列表（category 过滤；不含正文） | `wiki_pages(category='ops')` 先列表定位 |
| `wiki_page` | wiki 页面详情（标题/正文全文/分类/来源） | `wiki_page(page_id)` 按 id 取正文 |
| `memory_get` | 单条记忆详情（内容/维度/TTL + 溯源 + 归档链） | `memory_get(memory_id)` |
| `memory_reject` | 标记记忆「不采用」（不删除、可恢复） | `memory_reject(memory_id, reason)` |
| `refine_trigger` | 触发提炼：单文件或扫 status=new 批量 | `refine_trigger(async_mode=true)` 异步排队即返 |
| `refine_batch` | 批量提炼：显式文件列表或扫全部未提炼 | `refine_batch(file_ids=[...], async_mode=true)` |
| `refine_status` | 提炼进度：待提炼/已完成/失败计数 + 水位 + 最近失败 | 异步触发后轮询它 |
| `stats` | 统计：记忆数/维度分布/原始文件状态/水位 | 了解数据规模 |
| `health` | 健康检查：LLM 可用性/提炼水位/心跳/向量 | 就绪检查必调 |
| `config_get` | 读取运行时配置（l1/l2/refine/search/backup） | `config_get(section='refine')` |
| `config_update` | 更新配置段（热生效 + 落盘） | 跨机部署远程设配置 |

## 4. 记忆写入与消费节奏（五条铁律）

记忆是你自己的责任。五条铁律：

1. **每轮对话结束 `append` 当前轮次**——纯落盘、零 LLM 成本，崩溃不丢。同一会话用同一 `session_key` 延续。
2. **会话结束 `refine_trigger(async_mode=true)`**——带着完整上下文提炼，产出标签化记忆。
3. **对话开始时 `inject` 按场景取画像 / `search` 检索相关记忆**。
4. **主动关怀靠消费信号**——信号消费 = 主动关怀，谁消费谁标记：拿到 `care_*` 信号后 `signal_claim` 原子认领 → 关怀用户 → `signal_ack` 回执（认领失败 = 已被其他 agent 消费，跳过）。获取信号两条路：
   - 短连接（无常驻进程）：每次对话开始 `signal_pull` 拉未消费信号；
   - 长连接（有常驻能力，**主动关怀首选**）：挂 SSE 事件流 `GET /v1/events/stream?subscriber_id=<你的agent_id>`（带 X-API-Key），`care_*`/`memory_updated`/`anomaly_warn` 一产生即实时推送 → 立即 claim→关怀→ack；断线重连带 Last-Event-ID 头补偿，不丢事件。
5. **对话开始时（或用户指定角色时）`role_list` 看可用角色 → `role_assemble(role_id)` 拿人设并按其说话**——换皮不换芯，角色只是沟通外皮，记忆池不动。

content 格式（首行必须）：

```
# 2026-08-04T10:00:00Z user
这一轮用户说了什么
# 2026-08-04T10:00:01Z assistant
这一轮你回了什么
```

格式错误 → 422 `ERR_INVALID_ARGS`，先自查首行。

### 4.1 事件对接（主动关怀的触发源，常驻 agent 必读）

主动关怀能不能「主动」，取决于你**不在场时**能否被事件唤醒。SGME 事件三类：

| 事件 | 含义 | 触发时机 |
|---|---|---|
| `care_*` | 关怀信号（情绪低落/待办到期/过劳/每日问候） | Dream 每日扫描产生 |
| `memory_updated` | 记忆更新 | 提炼落库时 |
| `anomaly_warn` | 异常告警 | 提炼停摆/错误 |

**三种接法任选**（按你的驻留能力）：

1. **SSE 长连接**（常驻 agent 首选）：`GET /v1/events/stream?subscriber_id=<你的agent_id>`，带 `X-API-Key` 头。事件实时推送，帧格式 `id:`/`event:`/`data:`；断线重连时带 `Last-Event-ID` 头即可从断点续传，不丢事件。适合 Hermes 等有常驻进程/后台循环的 agent。
2. **游标拉取**（定时任务）：`GET /v1/events/pull?subscriber_id=<agent_id>`，持久游标自动推进，适合 cron 定期轮询。
3. **MCP `signal_pull`**（会话内短连接）：无驻留能力时，每次对话开始拉一次。

> SSE/pull 走 HTTP :9910；`signal_pull` 走 MCP :9913。三条路最终都回到 `signal_claim` → 关怀 → `signal_ack` 同一闭环。

## 5. 提炼与限流策略（2026-08-11 Trae 全量提炼实测）

**背景**：Trae 全量提炼 600+ 文件时连环撞 429，提炼直接失败。SGME 已有三道防线（LLM 调用层节流器 0.5 rps + 429 入退避 + batch_scan 兜底），但**你仍然要自律**：

| 规则 | 内容 |
|---|---|
| 批量上限 | **≥20 文件必须分批**，每批 ≤20 |
| 批间间隔 | **30–60 秒**（节流器会再平滑，但别赌） |
| 429 处理 | **失败不立即重试**——文件留在 status=new，交 `batch_scan` 兜底（默认 10 分钟一轮自动扫） |
| 触发方式 | **永远异步**：`refine_trigger(async_mode=true)` / `refine_batch(async_mode=true)`，排队即返，不阻塞你的会话 |
| 进度查询 | `refine_status()` 轮询：待提炼/已完成/失败计数 + 水位 + 最近失败 |
| 预期管理 | 大批量提炼是**分钟级**任务，不是秒级。触发后去干别的，隔一会儿再查 |

**正确流程**（全量提炼）：

```
1. refine_batch(async_mode=true)          # 或分批 file_ids，每批 ≤20
2. 等待 30–60s（批间）
3. refine_status() → 查待提炼数是否归零
4. 有失败？看最近失败原因；429 类直接等 batch_scan 下一轮
```

**禁止**：同步模式跑大批量；429 后立即重试；一次触发 100 个文件。

## 6. 三档适配模式（你属于哪一档）

| 档位 | 机制 | 适用 | 你要做的事 |
|---|---|---|---|
| **hooks 型** | 检测会话结束事件（SessionEnd/Stop），自动触发提炼 | Hermes / Reasonix / Claude Code | 配自动提炼 hook，无需每会话手动收尾 |
| **自律型** | 无 hooks → 会话收尾主动调提炼 | Trae / ZCode | 指引要求：结束前 `refine_trigger(async_mode=true)`，别指望别人 |
| **保底型** | 服务端兜底，不依赖 agent 自觉 | 任何 agent（最后防线） | 服务端 `batch_scan` 常驻定时器扫 status=new；`refine_on_append=true` 时 append 后自动单文件提炼 |

> **已接入工具登记（2026-08-13）**：Reasonix（本机 Desktop + CLI）——走 **hooks 适配层**（`adapters/reasonix/`）：SessionStart 注入画像/相关记忆 + SessionEnd 导出会话触发提炼（`.reasonix/settings.json` 两条 hooks，已装），会话中 `/sgme <关键词>` 命令检索（bridge.py 调 `/v1/search`，memory+wiki 两层）；全局规则 `%APPDATA%/reasonix/AGENTS.md`（用户级 standing instructions：SGME 纪律 + 项目开发流程约束 + 五步查证），项目级规则随项目根 AGENTS.md 自动加载（install 已追加 SGME 记忆系统声明段）；key 在 `adapters/reasonix/.env`（agent_id=reasonix）。**不走 MCP**（hooks 专用适配是既定设计，MCP 留给无 hooks 的 Agent）。

**判断方法**：有 SessionEnd/Stop 事件机制 = hooks 型；没有 = 自律型；两者都失效还有保底型兜底——记忆不会丢，只会晚提炼。

## 7. 就绪检查与主动提醒

连接后第一件事不是写记忆，是 **`health()`**：

- `llm.available`：LLM 是否可用（false → 提炼会降级直存，不丢但不出标签）
- `vector.available`：向量检索是否可用（false → 纯 BM25）
- `refinement.queue_depth / stalled`：提炼水位是否健康
- 数据源：`stats()` 看记忆/原始文件是否为空

**LLM 或数据源缺失时，主动提醒用户，表述提供商无关**（不要报任何品牌）：

> LLM 未配置——请配置你所用提供商的 key（见 `config/providers.yaml`）。

环境清单同理：教用户**配置方法与查找路径**（`providers.yaml` 的 `api_key_env` 字段、`VOLC_API_KEY` 等），品牌由用户自己选。你可以诊断「缺什么」，但不要说「去装某某」。

## 8. 日常使用范式（四条主线）

- **注册**：无独立注册步骤——`append` 写入第一条会话即完成接入（同一 `session_key` 延续会话）。
- **提炼**：`refine_trigger` / `refine_batch` 触发，`refine_status` 查进度。
- **回忆**：`search(query)` 混合检索带溯源；`inject(mode='daily')` 注入当日画像；`memory_get` 看细节。
- **关怀**：`signal_pull` 拉未消费关怀信号 → `signal_claim` 原子认领 → 关怀用户 → `signal_ack` 回执（谁消费谁标记，防多 agent 重复打扰）。
- **管理**：`memory_reject` 纠错（不删除、可恢复）；`stats` / `health` 看状态；`config_get` / `config_update` 读写配置。

## 9. 常见坑

| 坑 | 症状 | 对策 |
|---|---|---|
| 默认 Key 远程调用 | 403 ERR_FORBIDDEN | 用 register 签发的 `agt_*` 或 env 自定义 Key |
| 批量撞限流 | 429，批量提炼失败 | 分批 ≤20 + 批间 30–60s；429 不重试，交 batch_scan |
| content 格式错 | 422 ERR_INVALID_ARGS | 首行必须是 `# {ISO时间戳} {role}` |
| 同步模式跑大批量 | 会话长时间阻塞 | 永远 async_mode=true |
| 只 append 不 refine | 记忆永远停在 status=new | 会话结束触发提炼；或开 `refine_on_append` / 靠 batch_scan |
| 改配置不落盘 | 重启丢失 | `config_update` 自动落盘；手改文件需重启生效 |
