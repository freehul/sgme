# SGME Agent Onboarding 指引

> 写给 AI Agent 读的接入指南。人类运维请移步 [运维手册](../docs/runbook.md)。
> 你（Agent）读完本文件，应该能独立完成：找到 SGME → 连上 → 开始写记忆 → 触发提炼 → 查进度。

## TL;DR（30 秒速览）

1. **发现**：探测 `http://<NAS_IP>:9910/v1/health`（SGME 现部署于 NAS）；失败读 `~/.sgme/install.json`；仍失败就向用户报告「SGME 未发现」。
2. **连接**：MCP 端点 `http://<NAS_IP>:9913/mcp`（HTTP API 为 `:9910`），带 `X-API-Key`。
3. **接入**：先调 `agent_onboarding` 工具（连接即发现，版本/能力/上手一条龙）。本指引全部文档可在线读取：`GET {base}/v1/onboarding/docs`（索引）/ `GET {base}/v1/onboarding/docs/<文件名>`（全文，免 Key，v1.7.2+ 服务端内置）。
4. **写入**：每轮对话结束 `append` 当前轮次；会话结束 `refine_trigger(async_mode=true)`。
5. **提炼**：批量 ≥20 文件必须分批 + 批间 30–60s；429 不立即重试，交 `batch_scan` 兜底。
6. **技能获取（按需检索，MUST）**：需要 SGME 未内置的专业能力时，先 `skill_search(query)` 检索、命中后 `skill_get(name)` 拉全文注入再执行——**禁止未检索就声称具备技能或硬凑步骤**。冷启动包只含《技能检索协议》一个 skill、不预载全量技能，按需检索才是正路。
   **⚠️ 前提：协议必须常驻上下文（2026-11-03 实测教训）**。「需要时再检索」这句话本身不成立——Agent 不知道自己「现在需要」，就不会去检索。接入方必须二选一：
   - **宿主有 system prompt 注入能力**（Hermes 适配器 / dsh 插件）→ 会话开始时把协议正文（`GET /v1/skills/coldstart` 的 `index.items[].content`）注入静态块，常驻开头；
   - **宿主无注入能力**（doubao / mimo / workbuddy / zcode / codex / claude-code）→ 会话开始调一次 `skill_coldstart`，把返回的协议放进**工作记忆 / 常驻清单**，让它在此后每轮都可见。

   判据（唯一能证伪的方式）：**新会话里 Agent 在没人提醒的情况下，能否说出「SGME 有技能库，开发任务该先 skill_search」**。
7. **管理面（2.0）**：待审队列 / 运维看板 / 角色演化见 §8.5；凭证管理见 §4.3；技能包两级缓存与技能编写见 §6.3 / §6.4。

### SGME 能力平面（核心三模块）

接入 SGME 后，Agent 不需要批量安装很多 skill。只安装当前宿主的 SGME 适配器；其余能力留在 SGME，按需调用：

| 需求 | 模块 | 入口 |
|---|---|---|
| 历史事实、用户偏好、项目上下文、画像和会话提炼 | `memory` | `append`、`inject`、`search`、`memory_get`、`refine_*` |
| 持久手册、设计、经验、踩坑和知识沉淀 | `wiki` | `wiki_search`、`wiki_pages`、`wiki_page`、`wiki_page_add/update` |
| 框架、工具链、专业流程和领域技能 | `skills` | `skill_search` → `skill_digest` → `skill_get` |
| 跨机共享的重要 KEY（可选模块） | `credentials` | `credential_list` / `credential_get`；程序用 KEY 走 `sgme-cred run`（详见 §4.3） |

统一路由：**memory 取上下文 → skills 取方法 → 执行 → wiki 沉淀稳定经验**。普通 skill 不物化；宿主适配器或确需本地资产的工具包按部署形态取件——同机用 `skill_materialize`（落盘在 SGME 服务端）；跨机用**包通道**取完整包：`GET /v1/skills/{name}/package`（或 `skill_fetch.py` 一键下载校验解包，见 §6.1），字节保真、可在本地直接执行。

SGME 技能库已提供统一开发能力入口：`coding-workflow`（TDD/调试/审查/验证/CodeGraph/提交）、`sgme-development`（引擎开发）、`sgme-adapter-development`（适配器）、`sgme-docs-authoring`（文档）、`skill-governance`（技能治理）和 `sgme-operations`（记忆/技能/wiki/凭据运维与手册）。需要时按 `skill_search` 检索，不要再批量安装重复技能。

### 为什么协议要常驻（而不是「需要时再想」）

`skill_search` 只是工具箱里的一个工具，**工具不会自己触发**。Agent 只有在上下文里同时看见「有技能库」和「什么时候该用」，才可能在接到开发任务时想起去检索。

实测（2026-11-03）：Hermes 会话的 system prompt 当时只有「角色提示词 + 画像摘要」，没有协议。结果整轮开发改了 10+ 个文件、修了十几个 bug，`coding-discipline` / `git-pr-workflow` **一次都没被加载**——不是拿不到（`sgme_skill_search` 一直可用），是压根不知道有。补上协议注入后，新会话开头就带着那张「任务信号 → 模块 → 动作」路由表。

同理，**不要用手抄的方式把技能复制到宿主本地**：手抄既会漏内容（实测漏掉真源整节），又会造出第二个真源持续漂移。要本地可用就走包通道（`skill_materialize` / `GET /v1/skills/{name}/package`），且副本必须是可重建的。

---

## 1. 服务发现三步（找不到 SGME 怎么办）

SGME 装在哪、端口多少、用什么 Key——三步定位，零人工依赖：

| 步骤 | 动作 | 成功 → | 失败 → |
|---|---|---|---|
| ① | 探测 `GET http://<NAS_IP>:9910/v1/health`（NAS） | 拿到了，直接进 §2 | 下一步 |
| ② | 读安装清单 `~/.sgme/install.json`（地址/端口/Key 引用） | 按清单连接 | 下一步 |
| ③ | 就绪报告「SGME 未发现」，提醒用户安装/启动 | — | — |

**项目目录语义**：用户以 CLI `--dir` 指定或 Desktop 打开项目文件夹时，`install.json` 就在该目录的 `.sgme/` 下——由你（Agent）负责读，不要问用户要路径。

## 2. 连接与鉴权

| 项 | 值 |
|---|---|
| MCP 端点 | `http://<NAS_IP>:9913/mcp`（streamable HTTP） |
| HTTP API | `http://<NAS_IP>:9910`（功能等价，两协议共享同一引擎） |
| Agent Key | 请求头 `X-API-Key`（HTTP）；MCP 按客户端配置注入 |
| 默认开发 Key | `dev-agent-key-change-me` / `dev-admin-key-change-me`——**仅限本机回环**（127.0.0.1/::1/localhost），远程调用一律 403 |

**多 Agent 共存的正确姿势**：**接入时每个 Agent 必须申请自己的专属 Key**——调用 `POST /v1/admin/agents/register`（需 admin key；`agent_id` 按宿主名命名，小写字母 / 数字 / 连字符，如 `hermes` / `dsh`），返回 `agt_*` 明文仅此一次；AI-INSTALL 任务卡 ② 此为规定步骤。推荐把 admin key 随接入一并同步到接入侧环境文件（`SGME_ADMIN_KEY`，仅环境文件，不进对话/文档）。见 runbook §9.1。用默认 Key 做远程接入会撞 403，别试。

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
        "http://<NAS_IP>:9913/mcp", headers={"X-API-Key": key}
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

连上后第一件事：调 `agent_onboarding()`。它返回 SGME 版本、**全部 43 个工具的清单与用途**、快速上手三条主线（注册/提炼/回忆）、**自助配置段（self_config）**。信息以它为准，本文件只是它的展开版。

**自助配置（2026-08-13，接入完成标准之一）**：`agent_onboarding` 返回 `self_config` 段——要求接入的 agent **自己修改自己的身份文件**（SOUL.md / AGENTS.md / CLAUDE.md 等，按工具机制自选），把返回的模板（版本标记 `SGME-ONBOARDING-v2`）写入。SGME 不替 agent 改文件，只要求结果、不限制过程：

1. 自查身份文件是否已有 `SGME-ONBOARDING` 标记（版本 ≥ **v2** 即完成，跳过；仍写 v1 的请升级到 v2 模板）；
2. 缺失或版本旧 → 按模板写入自己的身份文件（写入位置/方式按工具的文件组织与加载机制自定）；
3. 写入后**读回验证**内容完整，并向主人汇报（写了哪个文件、哪个位置）；
4. 无法写入（权限/沙箱/审批限制）→ **如实报告主人并给出建议，禁止谎称已完成**（Trae 的 user_rules 目录有沙箱写保护实锤）。

> 目的：接入从「人复制 README 提示词」变为「agent 连接后自助配置」——换 Agent 零成本：不管工具的文件机制差异，只要求结果（下个会话起 SGME 纪律生效）。
>
> **完整「接入完成」定义见 §6.1（八项，与 `selfcheck.md` 同口径）**——自我配置只是其中第 ⑦ 项，不是全部。

**MCP 43 工具清单与用途**（与 `ONBOARDING_TOOLS` 清单程序化对账一致；PR#7 起逐步扩充，含三池/信号/角色/技能九工具）：

| 工具 | 用途 | 一句话用法 |
|---|---|---|
| `agent_onboarding` | 连接即发现：版本/能力清单/快速上手 | 接入后第一个调它 |
| `append` | L0 捕获：写入原始会话（幂等） | `append(session_key, started_at, content)`，content 首行须 `# {ISO时间戳} {role}` |
| `inject` | 记忆注入：按模式模板返回画像块 | `inject(mode='daily')` 拿当日画像 |
| `search` | 混合检索：BM25 + 向量 + RRF，带溯源（记忆池 + 技能，默认 scopes=["memory","skills"]） | `search(query, limit)` |
| `answer` | 聚合型提问（计数/列举/时序推理），服务端多一步 LLM 合成后直接返回答案 | `answer("我一共提过几次 X")` |
| `wiki_search` | 检索 wiki 知识库（wiki_pages 知识文档，FTS5 BM25 + 兜底） | `wiki_search(query, limit)` |
| `wiki_pages` | wiki 页面列表（category 过滤；不含正文） | `wiki_pages(category='ops')` 先列表定位 |
| `wiki_page` | wiki 页面详情（标题/正文全文/分类/来源） | `wiki_page(page_id)` 按 id 取正文 |
| `wiki_page_add` | wiki 页面直接写入（幂等 upsert，可带 description） | `wiki_page_add(title, content, category)` 建手册页 |
| `wiki_page_update` | 按 page_id 更新/追加（append 默认 ADD-only + entry hash 去重幂等） | `wiki_page_update(page_id, content)` 追加踩坑记录（W3） |
| `wiki_evolve_trigger` | 自进化触发：会话→经验→写回手册（费用门禁 + 规则闸门） | 会话后触发，经验自动沉淀（W4） |
| `memory_get` | 单条记忆详情（内容/维度/TTL + 溯源 + 归档链） | `memory_get(memory_id)` |
| `memory_reject` | 标记记忆「不采用」（不删除、可恢复） | `memory_reject(memory_id, reason)` |
| `memory_unreject` | 撤销「不采用」标记，恢复 active（误操作恢复） | `memory_unreject(memory_id)` |
| `refine_trigger` | 触发提炼：单文件或扫 status=new 批量 | `refine_trigger(async_mode=true)` 异步排队即返 |
| `refine_batch` | 批量提炼：显式文件列表或扫全部未提炼 | `refine_batch(file_ids=[...], async_mode=true)` |
| `refine_status` | 提炼进度：待提炼/已完成/失败计数 + 水位 + 最近失败 | 异步触发后轮询它 |
| `stats` | 统计：记忆数/维度分布/原始文件状态/水位 | 了解数据规模 |
| `health` | 健康检查：LLM 可用性/提炼水位/心跳/向量 | 就绪检查必调 |
| `config_get` | 读取运行时配置（l1/l2/refine/search/backup） | `config_get(section='refine')` |
| `config_update` | 更新配置段（热生效 + 落盘） | 跨机部署远程设配置 |
| `idea_add` | 创意池写入（用户主动提出的创意） | `idea_add(title, content)` |
| `demand_create` | 待办池写入（跨项目统一待办，可带 project_id——**一律大写**，如 DHVS；支持 origin_idea_id 升格） | `demand_create(title, project_id?)` |
| `project_register` | 项目池登记（用户主动立项的项目元数据；project_id 纯英文且一律大写） | `project_register(project_id, path, ...)` |
| `signal_pull` | 拉取未消费信号（care_*/memory_updated/anomaly_warn） | 对话开始 `signal_pull()` |
| `signal_claim` | 原子认领信号（谁消费谁标记，防重复打扰） | `signal_claim(event_id)` |
| `signal_ack` | 信号消费回执（claimed/acked/failed） | 关怀完成后 `signal_ack(event_id)` |
| `signal_clear` | 清理已消费信号游标 | `signal_clear(subscriber_id)` |
| `role_list` | 列出可用角色（换皮不换芯） | 对话开始 `role_list()` |
| `role_assemble` | 组装角色人设并按其说话 | `role_assemble(role_id)` |
| `role_active_get` / `role_active_set` | 查看/切换当前生效角色 | `role_active_get()` |
| `skill_*` | 技能管理九工具：skill_list/skill_coldstart/skill_search/skill_digest/skill_get/skill_materialize/skill_put/skill_delete/skill_rename（B114 补齐 list/coldstart/写侧） | `skill_search(query)` / `skill_get(name)` / `skill_materialize(name, dest_dir)`（落盘在服务端；跨机改用包通道 GET /v1/skills/{name}/package，见 §6.1） |
| `credential_list` / `credential_get` | 凭据模块（EP-11）：键名列表（不含值）/ 取单值（需 `credentials.enabled`；EP-11，2026-10-15） | 值不得进对话正文；标准姿势 `sgme-cred run` 注入（见 §4.3） |

**自进化（W4，2026-08-16；B114 后双通道）**：会话经验回写按「归属口诀」选通道——世界知识/通用经验走 `wiki_evolve_trigger`（LLM 提炼后追加到 wiki 手册页，多 agent 共享）；**跟技能本体走的坑直接 `skill_put` 更新该技能的 SKILL.md**（技能真源在 skills 模块 git 仓，已不寄居 wiki）。

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

### 4.2 通信渠道（兜底铁律）

**当前会话是兜底通信渠道**：任何主动消息（关怀/提醒/告警等），无论是否已发送到其它通道（微信/飞书/Telegram 等），都必须在当前会话也发一条；此兜底持续生效，直到用户明确取消。

- 有其它通道时：先发其它通道，再在当前会话补一条（兜底，不遗漏）；
- 无其它通道时：就在当前会话发（当前会话是唯一通道）；
- 用户明确说「以后不用在当前会话发了」才停止兜底，否则默认一直兜底。

### 4.3 凭据取用与管理（EP-11，可选，2.0 管理面补齐）

SGME 凭据模块把「重要 KEY」集中到 NAS 统一真源（PC 本地 .env 降级为缓存），任何已注册 agent 可平级读取；写 / 删 / 恢复仅 admin。模块未启用（`credentials.enabled=false`）时相关端点/工具返回友好提示或 404，属正常。

> **红线：列表与管理视图永不返回明文值。** 明文只有两个出口——单值读取 `GET /v1/credentials/{key}` 与批量导出 `GET /v1/credentials/export`；后者是**危险操作**（一次拿到多把 KEY 的明文），只在用户明确要求且目的清楚时执行，导出内容不得进对话正文、记忆或日志。

| 你要做的事 | 姿势 |
|---|---|
| 程序需要 KEY 运行 | **标准姿势**：`sgme-cred run -- python app.py`（拉取 → 注入环境变量 → 执行子进程；值不落盘、不进对话） |
| 看有哪些 KEY | MCP `credential_list()` / `sgme-cred list` / `GET /v1/credentials`——键名 + 元数据（`description` / `created_at` / `updated_at` / `updated_by`），**不含值** |
| 必须显式查看某值 | MCP `credential_get(name)` / `sgme-cred get KEY` / `GET /v1/credentials/{key}`——**仅限必须**；值不得写入对话正文或记忆 |
| 落一份本地缓存 | `sgme-cred export > .env`（显式操作；缓存只读不手改，真源始终在 SGME） |
| 新增 / 轮换 | `POST /v1/credentials`（admin）body `{key_name, value, description?}`；同名写入即轮换 |
| 下线某 KEY | `DELETE /v1/credentials/{key}`（admin，软删幂等；不存在 404） |
| 恢复误删 | `POST /v1/credentials/{key}/restore`（admin，幂等）；已删键名直接重写会 409，必须先 restore（不自动复活，防误覆盖） |
| 批量导出 | `GET /v1/credentials/export?only=K1,K2` 或 `?prefix=...`（互斥）→ `{"credentials": {KEY: value}, "count": n}`；**危险**，逐键记审计 + SecurityLog |

> ⚠️ 值一旦进入对话正文，会经 append 进 L0 原始层并随对话出境云端模型（不可回收）。客户端 `scripts/sgme_cred.py` 单文件零依赖、可裸拷任意机器；认证读 `~/.sgme/install.json`（零明文）。**运维细节**（备份/恢复/轮换/部署/迁移）见技能 `sgme-operations`（`skill_search("SGME 运维")` → `skill_get` 取手册）。

## 5. 提炼与限流策略（2026-08-11 Trae 全量提炼实测）

**背景**：Trae 全量提炼 600+ 文件时连环撞 429，提炼直接失败。SGME 已有三道防线（LLM 调用层节流器 0.5 rps + 429 入退避 + batch_scan 兜底），但**你仍然要自律**：

| 规则 | 内容 |
|---|---|
| 批量上限 | **≥20 文件必须分批**，每批 ≤20 |
| 批间间隔 | **30–60 秒**（节流器会再平滑，但别赌） |
| 429 处理 | **失败不立即重试**——文件留在 status=new，交 `batch_scan` 兜底（服务端定时自动扫；间隔 `refine.batch_scan.interval_min`，2026-08-20 起默认 60 分钟） |
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
| **hooks 型** | 检测会话结束事件（SessionEnd/Stop），自动触发提炼 | Hermes / Claude Code（官方适配器已内置 hooks 自动化） | 配自动提炼 hook，无需每会话手动收尾 |
| **自律型** | 无 hooks → 会话收尾主动调提炼 | Trae / ZCode | 指引要求：结束前 `refine_trigger(async_mode=true)`，别指望别人 |
| **保底型** | 服务端兜底，不依赖 agent 自觉 | 任何 agent（最后防线） | 服务端 `batch_scan` 常驻定时器扫 status=new；`refine_on_append=true` 时 append 后自动单文件提炼 |

> **官方适配器登记**：Hermes（memory.provider 插件）、DSH（Cordis 插件）、Doubao Work（豆包工作，`adapters/doubao`，Skill 形态）、MiMo Desktop（`adapters/mimo`，Skill + 原生 MCP 优先，2026-09-22 登记）、WorkBuddy（`adapters/workbuddy`，Skill + 原生 MCP 优先，从 `~/.workbuddy/mcp.json` 零配置继承，2026-09-22 登记）、ZCode（`adapters/zcode`，Skill 形态 + 自律接入 + `import_history.py` 历史会话补导入，install.py 可注册 ZCode 原生 MCP server，2026-09-26 登记）、Codex（`adapters/codex`，stdio MCP 代理动态透传 43 工具 + 会话生命周期 CLI，install.py 可注册 Codex MCP server，2026-09-30 登记）、Claude Code（`adapters/claude-code`，stdio MCP 代理动态透传 43 工具 + 原生 hooks 会话生命周期：SessionStart 注入 / Stop 落盘 / SessionEnd 提炼，install.py 注册 Claude Code MCP server 与 hooks，2026-10-01 登记）为官方维护适配器；其余 agent 走 MCP 通用接入（`agent_onboarding` 自助配置），有 hook 能力者按《SGME-接口契约》自研适配器。

**判断方法**：有 SessionEnd/Stop 事件机制 = hooks 型；没有 = 自律型；两者都失效还有保底型兜底——记忆不会丢，只会晚提炼。

## 6.1 官方适配器获取与安装（技能库自助，2026-09-28）

> 解决「适配器只在源码仓库、运行实例拿不到」的断点（WorkBuddy 接入排查 P2）。
> 通道：`skill_search` → `skill_get`（读指引）→ **取完整包**：同机 `skill_materialize`（落盘在服务端文件系统）；跨机用包通道 `GET /v1/skills/{name}/package` / `skill_fetch.py` 一键拉取（下载→校验→解包→本地落盘）。

**四步自助：**

1. **发现**：`skill_search("adapter")` 或 `skill_search("<宿主名> 适配器")`——命中 `adapter-hermes` / `adapter-dsh` / `adapter-doubao` / `adapter-mimo` / `adapter-workbuddy` / `adapter-zcode` / `adapter-codex` / `adapter-claude-code`
2. **确认**：`skill_get("adapter-<host>")` 读纪律与安装说明
3. **取完整包**：**同机**（agent 与 SGME 同一台机器）——`skill_materialize(name="adapter-<host>", dest_dir="<工作区>")`，除 `SKILL.md` 外 `scripts/` / `install.py` / `locales/` / `package/` 等随附文件一并落盘（落盘在 SGME 服务端文件系统）；**跨机**——`skill_fetch.py --name adapter-<host> --dest <工作区>`（脚本经在线文档获取：`GET /v1/onboarding/docs/skill_fetch.py`；等价手工：下载 `GET /v1/skills/adapter-<host>/package` → 按包内 `MANIFEST.sha256` 校验 → 在空目录解包；失败不留残件）
4. **安装**：按包内 `SKILL.md` / `references/README.md` 说明执行（通常是 `python install.py`；dsh 为 `install.py` + `dsh plugin add`）

| 技能名 | 宿主 | 形态 | 安装要点 |
|---|---|---|---|
| `adapter-hermes` | Hermes | memory.provider Python 插件 | `install.py` → `$HERMES_HOME/plugins/sgme/` |
| `adapter-dsh` | DeepSeek Harness | Cordis TS 原生插件 | `scripts/install.py` + `dsh plugin add` |
| `adapter-doubao` | 豆包工作 | Skill + CLI | `install.py` 部署技能目录 |
| `adapter-mimo` | MiMo Desktop | Skill + 原生 MCP 优先 | `install.py` → `~/.config/mimocode/skills/mimo/` |
| `adapter-workbuddy` | WorkBuddy | Skill + mcp.json 零配置继承 | `install.py` → `~/.workbuddy/skills/sgme/` |
| `adapter-zcode` | ZCode | Skill + 自律接入 | `install.py` + 可选 `import_history.py` |
| `adapter-codex` | Codex | stdio MCP 动态透传 + 生命周期 CLI | `install.py --register` |
| `adapter-claude-code` | Claude Code | stdio MCP 动态透传 + 原生 hooks | `install.py`（注册 `~/.claude.json` MCP server 与 `~/.claude/settings.json` hooks） |

**无宿主适配器**（或 skill 库未含 adapter）：走 MCP 通用接入——写入 `self_config.template`（第 ⑦ 项）并按 §6 档位自律运行。**不因缺适配器阻塞接入**。

## 6.2 「接入完成」八项标准（与 `selfcheck.md` 同口径）

| # | 项 | 一句话 |
|---|---|---|
| ① | 发现 | `GET /v1/health` 200，版本可见 |
| ② | 连通与身份 | 工具可调；专属 `agt_*` key |
| ③ | 写入 | `append` / 会话入库 `status:new` |
| ④ | 检索 | `search` 命中带溯源 |
| ⑤ | 提炼 | `refine_trigger` async 可达 |
| ⑥ | 技能 | 协议已常驻上下文（或已调 `skill_coldstart`）+ `skill_search` 有结果 |
| ⑦ | 自我配置 | 身份文件含 `SGME-ONBOARDING-v2` |
| ⑧ | 适配器 | 已装 `adapter-<host>`，或显式走通用 MCP 且 ⑦ 完成 |

> 两套「完成」定义曾互相打架（selfcheck 六项 vs self_config 模板）——自 2026-09-28 起统一为上表，`selfcheck.md` / `agent_onboarding.self_config.requirement` / 本节三处同源。

## 6.3 技能包通道与两级缓存（2.0 / D-59）

> 目标：技能库到几百条后，不逐个请求、不整库重拉。先比目录版本，再比单技能包指纹；命中零传输，未命中才拉包并逐文件校验。
> 四个端点均需 `X-API-Key`（Agent Key），且仅在 `skills.enabled=true` 时挂载。

| 端点 | 返回 | 用途 |
|---|---|---|
| `GET /v1/skills/catalog-version` | `{"catalog_version": "<16 位十六进制>"}`；空库 `"empty"`，技能库连接不可用 `"disabled"`（模块未挂载则端点 404） | 一级短路（响应 <100 字节） |
| `GET /v1/skills/{name}/manifest` | `{name, version, package_sha256, content_len, updated_at, files:[{path,sha256,size}]}`；`files` 不含 `SKILL.md` | 二级比对（响应 <200 字节） |
| `GET /v1/skills/{name}/package` | `application/gzip` 流：归档根 `MANIFEST.sha256` + `<name>/` 下逐文件（`SKILL.md` + `references/`/`scripts/`/`assets/` 等）；响应头 `X-Skill-Sha256`、`X-Skill-Files`、`ETag` | 完整包下载 |
| `GET /v1/skills/{name}/files/{relpath}` | 原始字节 `application/octet-stream` + `X-File-Sha256` | 单文件精确取用 |

**包通道细节**：

- `package` 只支持 `format=tar.gz`（默认）；`format=zip` 是保留参数 → 400。`If-None-Match: <ETag>` 命中 → 304（不重复计一次下载）。
- 包内 `MANIFEST.sha256` 每行 `<sha256><两个空格><相对路径>`（相对技能根，不含 `<name>/` 前缀）；`X-Skill-Sha256` = 该清单文本的 sha256，`X-Skill-Files` = 清单行数（不含 MANIFEST 自身）。
- wiki 源 / 源文件缺失 / 内置《技能检索协议》→ 退化为**单文件包**（仅 `SKILL.md`），属合法。
- `files/{relpath}` 必须命中包文件集：绝对路径 / 盘符 / `..` → 400；技能或成员不存在（含 junk、符号链接）→ 404。读宽写严，顶层 `install.py` / `locales/**` / `package/**` 可读。
- 配额：`skills.package_quota.*` 单文件 / 整包 / 文件数三维，超限 → 400（独立于写侧 asset_quota）。

**两级缓存**（官方适配器已内置；真源 `adapters/_shared/skill_cache.py`，各适配器随分发包带副本）：

1. **目录级短路**：`GET /v1/skills/catalog-version` 与本地 `<state_dir>/.sgme-catalog.json` 比对；相同 → 本会话不再逐个比对（进程内只拉一次）。
2. **单技能比对**：`GET /v1/skills/{name}/manifest` 的 `package_sha256` 与本地 `<dest>/<name>/.sgme-manifest.json` 的 `content_hash` 比对；相同且清单内文件 sha256 全部完好 → 直接用本地副本，**零包传输**。
3. **未命中才拉包**：下载 `package` → 包内 `MANIFEST.sha256` 逐文件校验 + 文件集双向对照（缺一个/多一个都拒收）+ 响应头 `X-Skill-*` 对账 → 在 `<dest>/.<name>.fetch-*` 临时目录组装 → `os.replace` **原子替换**（失败回滚、不留残件）。

**本地落盘**：

- 落点由调用方给：`<dest>/<name>/`，布局与包内一致；副本清单 `<dest>/<name>/.sgme-manifest.json` = `{name, version, content_hash, files:[{path,sha256}], fetched_at, catalog_version}`。
- 本地文件被外部改动/删除 → 下次 `ensure_skill` 检出并重拉（清单即自检依据）；只有「同一 `catalog_version` 下取的副本」才允许一级短路，防下载失败后旧副本被误判为最新。
- **不建**全局技能缓存目录、**不做**预热；落点永远由调用方给。`skill_materialize` 的 `dest_dir` 是**服务端**路径（`landing_side="server"`），跨机不要混用。
- 降级：catalog/manifest 端点不可用（旧服务端 404/405、网络抖动）→ 直接拉包，行为与 1.x 一致；只有包下载/校验失败才报错。

## 6.4 技能编写：本地工作副本（2.0 §5.2，仓库内工具）

`scripts/skill_workspace.py` 把「改技能」变成普通目录编辑：pull 下来 → 任意编辑器改 → status 看差异 → push 过 lint 入库。直接读写 `skills.db`，不碰 git、不需要服务进程。

> **分发边界**：该脚本在仓库 `scripts/` 下，**不随镜像分发**（镜像只内置 `AI-INSTALL/`、`sgme/`、`roles/`、`skills/` 等运行资产，不含 `scripts/`）。只有源码检出 / 仓库内可用；容器部署要用，需先把脚本临时拷入容器。

```bash
# 1) 拉一个技能成可编辑目录（已有未入库改动/非本技能会拒绝；--force 才整目录原子替换）
python scripts/skill_workspace.py pull <skill> --dir <工作区>/<skill>

# 2) 编辑 SKILL.md 与 references/ scripts/ assets/；新增文件直接放进目录
#    不要动 .sgme-manifest.json

# 3) 看与库的差异（有差异退出码 1，无差异 0，可进 CI）
python scripts/skill_workspace.py status --dir <工作区>/<skill>

# 4) 提交入库：先跑格式 lint，error 即拒绝并逐条列出；通过则写 skills + skill_files + 修订史
python scripts/skill_workspace.py push --dir <工作区>/<skill>

# 5) 批量盘点工作副本
python scripts/skill_workspace.py list --dir <工作区>
```

- 库定位：默认 `sgme.config.DATA_DIR/skills.db`（`SGME_HOME` 可重定向）；显式 `--data-dir <SGME_DATA_DIR>` 或 `--db <.../skills.db>`（二选一，`--db` 优先）。
- 冲突：pull 后库内被别处改过，push 默认拒绝（先重新 pull 合并）；`--force` 以本地为准覆盖。
- 迁移期边界：只写 2.0 的 `skills.db`，**不写** 1.x 的 git 工作区（`skills.source_dirs`）；两套真源迁移完成前不互通。
- 库表只承载六类 frontmatter（`description` / `category` / `tags` / `version` / `pattern` / `uses`）；`SKILL.md` 其它自定义字段在 pull 重建时不保留，正文与附属文件原样保留。

## 7. 就绪检查与主动提醒

连接后第一件事不是写记忆，是 **`health()`**：

- `llm.available`：LLM 是否可用（false → 提炼会降级直存，不丢但不出标签）
- `vector.available`：向量检索是否可用（false → 纯 BM25）
- `refinement.queue_depth / stalled`：提炼水位是否健康
- 数据源：`stats()` 看记忆/原始文件是否为空

> **两个模型**：SGME 提炼靠 LLM（主链 agnes-2.5-flash，免费，`AGNESAI_API_KEY`；备用硅基流动 THUDM/GLM-4-9B-0414，免费档，`SILICONFLOW_API_KEY`；zhipu 已移出降级链 B121、DeepSeek-V4-Flash 转付费移出 B144），语义检索靠向量 embedding（硅基流动 BAAI/bge-m3，`SILICONFLOW_API_KEY`）——缺了分别降级为「直存」/「纯 BM25」，`health` 里 `llm.available` / `vector.available` / `vector.connectivity` 会如实反映，哪个 false 补哪个。**Key 缺失时 `health` 的 `model_config.missing_keys` 会列出缺哪些**，按 [免费模型Key申请指南.md](免费模型Key申请指南.md) 申请免费 Key（两平台各约 10 分钟、零充值）。

**LLM 或数据源缺失时，主动提醒用户，表述提供商无关**（不要报任何品牌）：

> LLM 未配置——请配置你所用提供商的 key（见 `config/providers.yaml`）。

环境清单同理：教用户**配置方法与查找路径**（`providers.yaml` 的 `api_key_env` 字段、`AGNESAI_API_KEY` / `SILICONFLOW_API_KEY` 等；免费 Key 申请见本目录《免费模型Key申请指南.md》）。你可以诊断「缺什么」（health 的 `model_config.missing_keys` 即诊断结果），免费托底场景可直接建议 Agnes / 硅基流动（免费），付费场景品牌由用户自己选。

## 7.5 向量引擎接入流程（本地优先，云端降级）

> **策略（2026-08 定案）**：向量 embedding 采用「本地优先、云端免费降级备用」——主 provider = 本地 Ollama（`bge-m3`，1024 维），fallback = 硅基流动 siliconflow 云端（`BAAI/bge-m3`，同样 1024 维，免费）。本地与云端都用同款 bge-m3（1024 维），**切换不重灌索引**。

**触发条件**：`health()` 返回 `vector.available=false` 或 `vector.connectivity=false` 时，执行本流程。

**Step 1｜诊断**：调 `health()`，看三项字段定位问题：

| 字段 | 含义 |
|---|---|
| `vector.available` | 向量检索是否可用（false → 当前纯 BM25） |
| `vector.connectivity` | embedding provider 连通性（false → 没配上或连不上） |
| `model_config.missing_keys` | 缺哪些模型 Key（如 `SILICONFLOW_API_KEY`） |

**Step 2｜探测本机向量引擎**（按序执行，任一命中即用）：

```bash
# Ollama（默认端口 11434）
curl http://127.0.0.1:11434/api/tags
# LM Studio（默认端口 1234）
curl http://127.0.0.1:1234/v1/models
```

期望输出：JSON 响应中出现 `bge-m3` 即本机引擎可用 → 跳到 Step 4。两个请求都失败 → 本机未装任何向量引擎，进 Step 3。

> **不推荐 llama.cpp**：模型状态无人维护，用户清理显存后即失联，排查麻烦——不在推荐列表。

**Step 3｜引导部署**（二选一分支）：

- 已装 Ollama / LM Studio 但模型列表里没有 `bge-m3`：
  - Ollama：`ollama pull bge-m3`（一次拉取）
  - LM Studio：搜索栏输入 `bge-m3` → 下载
- 都没装：推荐二选一——**Ollama**（跨平台一键安装，命令行友好）或 **LM Studio**（有 GUI，可视化下载模型）。

**Step 4｜配置写入**：直接改 `config/sgme.yaml` 的 `search.vector` 段（agent 可改文件；**改后需重启 SGME 生效**）。主 provider 指本地，`fallbacks` 列表指云端免费降级（**同用 bge-m3 1024 维，切换不重灌索引**）：

```yaml
search:
  vector:
    enabled: true
    provider: local          # 本地优先
    model: bge-m3
    base_url: http://127.0.0.1:11434/v1   # LM Studio 用 http://127.0.0.1:1234/v1
    # api_key_env 留空——本地 Ollama/LM Studio 无需鉴权
    fallbacks:
      - provider: siliconflow   # 云端免费降级备用
        model: BAAI/bge-m3
        base_url: https://api.siliconflow.cn/v1
        api_key_env: SILICONFLOW_API_KEY
```

> 说明：`fallbacks` 降级链只在 `config/sgme.yaml` 的 `search.vector` 段生效（本地失败自动切云端）。`config/providers.yaml` 的顶层 `embedding` 段是 WebUI 向量模型管理的供应商清单（`vector_capable: true` 声明），**不含 fallbacks 语义**——单供应商配置可放那里，但需要降级链请用 `search.vector.fallbacks`。

**Step 5｜验证闭环**：再次调 `health()` → `vector.connectivity=true` → 报告「✅ 本地向量引擎已就绪」；仍失败按 **端口 / 模型名 / 代理劫持** 顺序排障（SGME 铁律：httpx 调用等价 `trust_env=False`，确认 localhost 未被 Clash 等代理劫持），再失败**如实上报**，不得谎称已完成。

**云端 agent 边界**：本流程要求 agent 能操作宿主（curl / 装软件 / 写配置文件）。宿主机 agent 可直接执行；云端 agent（无宿主权限）应把上述操作建议交给用户执行，**禁止谎称已完成**。

## 8. 日常使用范式（四条主线）

- **注册**：无独立注册步骤——`append` 写入第一条会话即完成接入（同一 `session_key` 延续会话）。
- **提炼**：`refine_trigger` / `refine_batch` 触发，`refine_status` 查进度。
- **回忆**：`search(query)` 混合检索带溯源；`inject(mode='daily')` 注入当日画像；`memory_get` 看细节。
- **关怀**：`signal_pull` 拉未消费关怀信号 → `signal_claim` 原子认领 → 关怀用户 → `signal_ack` 回执（谁消费谁标记，防多 agent 重复打扰）。
- **管理**：`memory_reject` 纠错（不删除、可恢复）；`stats` / `health` 看状态；`config_get` / `config_update` 读写配置。
- **技能获取**：`skill_search(query)` 检索专业能力 → `skill_get(name)` 拉全文注入上下文 → 按技能执行；无命中如实告知用户（详见冷启动包中的《技能检索协议》）。

## 8.5 管理与运维面（2.0）

> 以下端点用于「人 / 管理 agent 看状态、做裁决」，不是日常记忆写入链路。地址同 `{base}`，均带 `X-API-Key`。

### 8.5.1 待审记忆队列（D-30）

L2 提炼对低置信度产物打 `needs_review=1`：**不进主注入块、不进人格证据**，只在本队列可见，等人工放行/拒绝；超期（默认 7 天）未处理的按**保守直存**消化——放行进库并在 `notes` 追加「未经人工确认」标记（去重），不丢弃、可溯源。

| 动作 | 端点 | 要点 |
|---|---|---|
| 看待审 | `GET /v1/admin/review/pending?limit=50&offset=0&dimension=<维度>` | `limit` 1–200（默认 50），`offset` ≥0；返回 `{items, total}`，items 为完整记忆行 |
| 放行 | `POST /v1/admin/review/{memory_id}/approve` | body 可选 `{"note": "..."}`；幂等；放行后回到正常注入/检索可见 |
| 拒绝 | `POST /v1/admin/review/{memory_id}/reject` | body `{"reason": "..."}` 必填；置 `status='rejected'`（软删可恢复）；幂等 |
| 超期消化 | `POST /v1/admin/review/auto-resolve` | body 可选 `{"days": 7}`（≥1）；返回 `{"resolved": n}`；已有标记不重复追加 |

鉴权：`require_agent_key`——Agent Key 或 Admin Key 均可（与 `memory_reject` 同档）；WebUI 管理面用 Admin Key。待审放行/拒绝属**用户裁决**，agent 代用户操作前先确认。

### 8.5.2 运维看板（只读，Admin Key）

| 端点 | 参数 | 返回 |
|---|---|---|
| `GET /v1/admin/ops/ttl-disposition` | `days` 1–365，默认 30；非法回退默认 | `{items: [{day, valid_expired, renewed, anchor, sleeping, archived}], total: {...}, days}` |
| `GET /v1/admin/ops/llm-cost` | 同上 | `{items: [{day, provider, model, stage, prompt_tokens, completion_tokens, calls}], totals: {...}, days}` |

- 两张表懒建；表不存在 / 无数据时返回空 `items` + 零汇总 + `note`，**不是 404/500**——看板显示「暂无数据」即正常。
- `ttl-disposition` 五档固定枚举：`valid_expired` / `renewed` / `anchor` / `sleeping` / `archived`。
- `llm-cost` 按 `(day, provider, model, stage)` 聚合，用于看提炼/检索/对话各阶段的 token 与调用量。

### 8.5.3 角色演化（设计 4.2）

角色分两层：

- **基线层**（静态人设，写在角色卡里）：`data.extensions.sgme_profile`，白名单五键——`biography` / `values` / `hobbies` / `voice` / `personality_slots`；`personality_slots` 六个中文槽位：语言风格 / 情绪反应 / 价值观取舍 / 社交姿态 / 冲突处理 / 亲密表达。改基线 = `POST /v1/admin/roles/{role_id}` 整体 upsert 角色卡（body `{"data": {...}}`，`name`/`description` 必填）。
- **演化层**（运行时数据，不入 git）：`data/role_evolution/<role_id>.json`，记录共同经历 / 心情 / 性格与三观变化。

| 动作 | 端点 | 要点 |
|---|---|---|
| 读演化层 | `GET /v1/admin/roles/{role_id}/evolution` | 文件不存在返回空结构 200（`{timeline:[], feelings:[], personality_changes:[], values_changes:[]}`），不 404 |
| 追加一条 | `POST /v1/admin/roles/{role_id}/evolution` | 扁平 body：`{"kind": "...", ...字段}`；`at` 由服务端生成；角色不存在 → 404；kind/字段非法 → 400 |

`kind` 与字段白名单：

| kind | 必填 | 可选 |
|---|---|---|
| `timeline` | `event` | `source` |
| `feelings` | `thought` | `trigger` |
| `personality_changes` | `slot`, `to` | `from`, `reason` |
| `values_changes` | `change` | `reason` |

鉴权：`require_agent_key`（角色卡是用户级资产，与记忆同权限级）。演化层是「角色经历」，不是记忆池——不要用 `append` 混写。

## 9. 常见坑

| 坑 | 症状 | 对策 |
|---|---|---|
| 默认 Key 远程调用 | 403 ERR_FORBIDDEN | 用 register 签发的 `agt_*` 或 env 自定义 Key |
| 批量撞限流 | 429，批量提炼失败 | 分批 ≤20 + 批间 30–60s；429 不重试，交 batch_scan |
| content 格式错 | 422 ERR_INVALID_ARGS | 首行必须是 `# {ISO时间戳} {role}` |
| 同步模式跑大批量 | 会话长时间阻塞 | 永远 async_mode=true |
| 只 append 不 refine | 记忆永远停在 status=new | 会话结束触发提炼；或开 `refine_on_append` / 靠 batch_scan |
| 改配置不落盘 | 重启丢失 | `config_update` 自动落盘；手改文件需重启生效 |
| 待审队列长期积压 | `review/pending` 一直有数据 | 人工 `approve` / `reject`；超 7 天可 `auto-resolve` 保守直存，但会带「未经人工确认」标记 |
| 以为列表能看凭据值 | `GET /v1/credentials` 无 `value` | 设计如此（列表永不含明文）；取单值用 `GET /v1/credentials/{key}`，批量用 `export`（危险） |
| 技能每次都全量重拉 | 包流量不降 | 旧服务端无 `catalog-version`/`manifest`（404）会降级直拉；新版先比目录版本与 `package_sha256` |
| 容器里找不到 `skill_workspace.py` | `scripts/` 不在镜像 | 在仓库检出内运行，或临时把脚本拷入容器（不随镜像分发） |
| 角色演化写入 404 | `POST .../evolution` 返回角色不存在 | 先建角色卡（`POST /v1/admin/roles/{role_id}`）；演化层是角色运行数据，不允许孤儿 |
