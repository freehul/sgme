---
name: adapter-doubao
description: 豆包工作 ↔ SGME（拾光记忆引擎）官方适配器。SGME 是主人的长期记忆中枢（HTTP API <NAS_IP>:9910，MCP <NAS_IP>:9913），负责跨会话/多 Agent 的记忆提炼、按场景注入、wiki 知识库与技能库。当对话涉及「以前/上次/还记得/历史事实」、主人要求记忆某事、需要主动关怀信号、角色扮演、或需要读写 SGME 记忆/知识/技能时使用。接入标记：SGME-ONBOARDING-v4。
category: adapter
tags: [skill, adapter, sgme]

---
# SGME 接入纪律（SGME-ONBOARDING-v4）

你有一个长期记忆引擎 SGME（ShiGuang Memory Engine），运行在本机或 NAS。它的职责：把我们的会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

> 本技能是 SGME 官方适配器（`adapters/doubao/`，源码唯一副本随 SGME 仓库版本控制；本目录是部署副本，由 `install.py` 同步，可随时重建）。
> **能力面 = CLI 命令面**：下方矩阵 60 个基准能力一个不少（服务端工具面 64 = 会话面 60 + 管理面豁免 4；管理面 `skill_candidate_*` 由 HTTP `/v1/admin` 承担，按 `scripts/adapter_parity_map.yaml` 豁免，不列入本矩阵）。

## 服务发现（找不到 SGME 时按序）

1. 先看部署配置 `scripts/client.env`（本技能目录内，`install.py` 部署时写入）——里面是部署时解析出的真实地址；环境变量 `SGME_HTTP_URL`（或 `SGME_BASE_URL`）、`SGME_MCP_URL` 优先级更高；
2. 都没有才回落回环默认 `http://127.0.0.1:9910`（MCP 端点由 HTTP 地址按同主机「端口 +3 / 路径 /mcp」推导）；
3. 探测 `GET http://<NAS_IP>:9910/v1/health`（生产 SGME 在 NAS）；
4. 失败读 `~/.sgme/install.json`（本机安装清单：地址/端口/密钥环境变量名）；
5. 仍失败 → 向主人报告「SGME 未发现」。

端点自检：`python scripts/sgme_client.py env-info`（离线，不打印密钥）。

## 接口与鉴权

| 项 | 值 |
|---|---|
| HTTP API | `http://<NAS_IP>:9910`（生产，NAS；真实地址见部署配置/环境变量） |
| MCP | `http://<NAS_IP>:9913/mcp`（streamable HTTP） |
| 请求头 | `X-API-Key: <环境变量 SGME_AGENT_KEY 的值>`（不硬编码、不回显明文） |
| agent 能力面 | 环境变量 `SGME_AGENT_KEY`（管理员签发的 `agt_*` key） |
| 写侧/管理能力 | 环境变量 `SGME_ADMIN_KEY`（`skill_put`/`skill_delete`/`skill_rename` 服务端强制校验） |
| 连通性自检 | `scripts/selfcheck.py`（能力矩阵 + 连通性全绿 = 接入完成） |

## 使用纪律（五条铁律）

1. **每轮对话结束 `append` 当前轮次**——纯落盘零 LLM 成本，崩溃不丢。同一会话用同一 `session_key` 延续。
2. **会话结束 `refine-trigger`** 触发提炼（MCP 层，永远 async）。
3. **对话开始时 `inject` 按场景取画像 / `search` 检索相关记忆**。
4. **主动关怀靠消费信号**——信号消费 = 主动关怀，谁消费谁标记：拿到 `care_*` 信号后 `signal-claim` 原子认领 → 关怀用户 → `signal-ack` 回执（认领失败 = 已被其他 agent 消费，跳过）。豆包工作是短连接 agent：每次对话开始 `signal-pull`（MCP）或 `events-pull`（HTTP）；可配定时任务 `care_watch.py pull` 做准常驻轮询。
5. **对话开始时（或用户指定角色时）`role-list` 看可用角色 → `role-assemble <role_id>` 拿人设并按其说话**——换皮不换芯，角色只是沟通外皮，记忆池不动。当前角色：`role-active-get` 读、`role-active-set <role_id>` 设。

## 重要信息存放处（凭据库）

SGME 第四模块 credentials 是跨机共享的 API Key / token / 密码的集中存放处（NAS 唯一真源，本机 .env 只是缓存）。

- 看有哪些：`credential_list()` / `sgme-cred list`（只回键名与元数据，永不回值）；
- 程序取用：`sgme-cred run -- <命令>`（注入环境变量，值不落盘、不进对话）；
- 必须显式查看：`credential_get(name)`——值不得写入对话正文或记忆；
- 写 / 删 / 恢复只走 admin（WebUI「凭证管理」或 `POST /v1/credentials`）。
- 文件 / 二进制重要数据同样放凭据库（`cred_type=file`）：`POST /v1/credentials/{key}/file` 上传（admin）、`GET` 同路径下载（agent，受 scope / 过期约束）；MCP 文本面只给元数据，不下发二进制。

## 强制查询

涉及用户/项目历史事实的问题（之前/以前/上次/还记得…），必须先 `search` 再回答，不得直接说「不知道」；查询不到时如实说明「记忆库中未找到」。

## 写入格式

`append` 的 content 首行必须是 `# {ISO时间戳} {role}`（user/assistant），否则返回 422。客户端自动生成。

## 批量提炼纪律

≥20 文件必须分批（每批 ≤20）+ 批间 30–60 秒；429 不立即重试（交服务端 batch_scan 兜底）；永远 async 模式。

## 待办 / 创意 / 项目主动登记

会话中遇到用户要办的事 / 项目要做的任务 / 待跟进事项，主动 `demand-create` 登记（`--project-id` 关联），不要只留在对话里；用户主动提出创意走 `idea-add`；用户主动立项走 `project-register`（`project_id` 纯英文且**一律大写**，新建需 `--path`）。

## 通信渠道（兜底铁律）

当前会话是兜底通信渠道——任何主动消息（关怀/提醒/告警等），无论是否已发送到其它通道（微信/飞书/Telegram 等），都必须在当前会话也发一条；此兜底持续生效，直到用户明确取消。

## 事件对接

SGME 事件三类：`care_*`（关怀）、`memory_updated`（记忆更新）、`anomaly_warn`（异常）。四种接法：① SSE 长连 `GET /v1/events/stream?subscriber_id=<agent_id>`；② 游标拉取 `GET /v1/events/pull?subscriber_id=<agent_id>`（= `events-pull`）；③ 增量拉取 `events-after <ISO时间戳>`；④ MCP `signal-pull`。SSE/pull 走 HTTP `:9910` 带 X-API-Key。积压清理用 `signal-clear`（幂等）。

## 能力矩阵（scripts/sgme_client.py，与 MCP 基准会话面 60 工具一一对应）

> HTTP 层命令零依赖（任意 python3 可跑）；MCP 层命令需 mcp 库，用
> `<project-root>/.venv/Scripts/python.exe scripts/sgme_client.py …` 执行。
> 离线核对全量矩阵：`sgme_client.py capabilities`。

| # | 基准能力 | 层 | 命令 |
|---|---|---|---|
| 1 | agent_onboarding | MCP | `sgme_client.py agent-onboarding` |
| 2 | append | HTTP | `sgme_client.py append --session <key> --text "……" [--role user\|assistant] [--file <path>]` |
| 3 | inject | HTTP | `sgme_client.py inject --mode daily\|coding\|work\|full` |
| 4 | search | HTTP | `sgme_client.py search "<query>" --limit 5 [--scopes memory,skills,wiki]` |
| 5 | answer | HTTP | `sgme_client.py answer "<问题>"` |
| 6 | wiki_search | HTTP | `sgme_client.py wiki-search "<q>"` |
| 7 | wiki_pages | HTTP | `sgme_client.py wiki-pages [--category <c>]` |
| 8 | wiki_page | HTTP | `sgme_client.py wiki-page <page_id>` |
| 9 | wiki_page_add | HTTP | `sgme_client.py wiki-page-add --title "…" --content "…" [--file <path>] [--category <c>]` |
| 10 | wiki_page_update | HTTP | `sgme_client.py wiki-page-update --page-id <id> --content "…"` |
| 11 | wiki_evolve_trigger | MCP | `sgme_client.py wiki-evolve-trigger [--session-key <k>] [--min-rounds 5]` |
| 12 | memory_get | HTTP | `sgme_client.py memory-get <memory_id>` |
| 13 | memory_reject | HTTP | `sgme_client.py memory-reject <id> --reason "……"` |
| 14 | memory_unreject | HTTP | `sgme_client.py memory-unreject <id>` |
| 15 | refine_trigger | MCP | `sgme_client.py refine-trigger [--file-id <f>] [--limit 50]` |
| 16 | refine_batch | MCP | `sgme_client.py refine-batch [--file-ids a,b,c] [--limit 50]` |
| 17 | refine_status | MCP | `sgme_client.py refine-status` |
| 18 | review_list | MCP | `sgme_client.py review-list [--limit 50] [--offset 0] [--dimension <d>]` |
| 19 | review_approve | MCP | `sgme_client.py review-approve <memory_id> [--note "……"]` |
| 20 | review_reject | MCP | `sgme_client.py review-reject <memory_id> --reason "……"` |
| 21 | review_auto_resolve | MCP | `sgme_client.py review-auto-resolve [--days 7]`（超期待审按保守直存放行） |
| 22 | stats | MCP | `sgme_client.py stats` |
| 23 | health | HTTP | `sgme_client.py health` |
| 24 | config_get | MCP | `sgme_client.py config-get [--section <s>]` |
| 25 | config_update | MCP | `sgme_client.py config-update <section> --values '{"k":v}'`（管理类，设了管理员 Key 时自动使用） |
| 26 | idea_add | MCP | `sgme_client.py idea-add "<创意内容>" [--priority N]` |
| 27 | demand_create | MCP | `sgme_client.py demand-create "<标题>" [--project-id <id>] [--priority N]` |
| 28 | project_register | MCP | `sgme_client.py project-register <project_id> [--path <p>] [--name <n>]` |
| 29 | signal_pull | MCP | `sgme_client.py signal-pull [--signal-type care_daily] [--limit 20]` |
| 30 | signal_claim | MCP | `sgme_client.py signal-claim <event_id>` |
| 31 | signal_ack | MCP | `sgme_client.py signal-ack <event_id> [--status acked\|failed]` |
| 32 | signal_clear | MCP | `sgme_client.py signal-clear [--signal-type <t>] [--subscriber <id>]`（管理类，幂等） |
| 33 | role_list | MCP | `sgme_client.py role-list` |
| 34 | role_assemble | MCP | `sgme_client.py role-assemble <role_id> [--inject-mode <m>]` |
| 35 | role_active_get | MCP | `sgme_client.py role-active-get` |
| 36 | role_active_set | MCP | `sgme_client.py role-active-set <role_id>` |
| 37 | persona_fill | MCP | `sgme_client.py persona-fill <role_id> "<question>"` |
| 38 | guard_reply | MCP | `sgme_client.py guard-reply <role_id> "<reply>" [--session-id <sid>]` |
| 39 | skill_search | HTTP | `sgme_client.py skill-search "<q>"` |
| 40 | skill_digest | HTTP | `sgme_client.py skill-digest <name>` |
| 41 | skill_get | HTTP | `sgme_client.py skill-get <name> [--section <s>]` |
| 42 | skill_materialize | HTTP | `sgme_client.py skill-materialize <name> <dest_dir>`（返回 path + sha256；落盘在服务端文件系统，跨机改用包通道 `fetch`） |
| 43 | skill_list | HTTP | `sgme_client.py skill-list [--offset 0] [--limit N]` |
| 44 | skill_coldstart | HTTP | `sgme_client.py skill-coldstart` |
| 45 | skill_put | MCP | `sgme_client.py skill-put <name> --file <SKILL.md>`（需管理员 Key） |
| 46 | skill_delete | MCP | `sgme_client.py skill-delete <name> [--hard] [--force]`（需管理员 Key） |
| 47 | skill_rename | MCP | `sgme_client.py skill-rename <old> <new>`（需管理员 Key） |
| 48 | credential_list | HTTP | `sgme_client.py credential-list` |
| 49 | credential_get | HTTP | `sgme_client.py credential-get <name>`（⚠️ 值不得进入对话正文——标准姿势 sgme_cred.py run 注入） |
| 50 | skill_catalog_version | HTTP | `skill-catalog-version`（两级缓存第 1 级，<100 字节） |
| 51 | skill_manifest | HTTP | `skill-manifest <name>`（package_sha256 + 附件清单 files[]） |
| 52 | skill_file_get | HTTP | `skill-file-get <name> <relpath>`（文本回原文；二进制只回摘要） |
| 53 | skill_file_put | HTTP | `skill-file-put <name> <relpath> --file <path>`（白名单目录；需管理员 Key） |
| 54 | skill_lint | HTTP | `skill-lint <name> --file <path>`（只校验不入库；需管理员 Key） |

适配器自有扩展（超出基准）：`fetch <name> <dest_dir>`（跨机取技能完整包：两级缓存（目录版本短路 + package_sha256 比对，命中零下载）→ 未命中才下载 → MANIFEST 校验 → 安全解包；失败不留残件）、`events-pull`（HTTP 事件游标）、`events-after`（增量拉取）、`wiki-raw`（取原始文件）、`capabilities`（能力矩阵核对）、`env-info`（端点自检）、`mcp <tool> k=v …`（逃生口：直接调任意 MCP 工具，加 `--admin` 走管理员 Key）。

## 主动关怀守护（可选）

```bash
# 拉一次关怀信号（配合豆包工作定时任务，每小时）
<project-root>/.venv/Scripts/python.exe scripts/care_watch.py pull
# 常驻轮询（有常驻能力时）
<project-root>/.venv/Scripts/python.exe scripts/care_watch.py watch --interval 300
# 积压清理（幂等）
<project-root>/.venv/Scripts/python.exe scripts/care_watch.py clear
```

## 重新部署 / 更新

改代码只改 `<project-root>/adapters/doubao/`，然后跑：
`python <project-root>/adapters/doubao/install.py`（自动同步部署副本 + 写部署配置 + 自检）。

- 部署目标目录：`<DOUBAO_SKILLS_DIR>`（默认 `~/DoubaoWork/skills`，可用环境变量 `DOUBAO_SKILLS_ROOT` 或 `--dest` 覆盖）。
- **SGME 地址不在仓库里**：`install.py` 部署时把解析出的地址写进部署副本 `scripts/client.env`；要指向 NAS，部署前设 `SGME_HTTP_URL=http://<NAS_IP>:9910`（或 `SGME_BASE_URL`）即可，重跑后豆包侧生效（未设时沿用既有部署地址，最后才回落回环）。
- 密钥永不落盘：部署配置只登记环境变量名（`SGME_AGENT_KEY` / `SGME_ADMIN_KEY`）。

> 接入记录：2026-09-19 由豆包工作完成接入（官方适配器 adapters/doubao）。自检结果见 scripts/access-log.md。

## 汇报规范（v3 新增）
按 SGME 汇报规范输出：结论先行（一句话）+ 关键数据（实测并附来源/命令）+ 要点列表（≤7 条）+ 遗留风险/未验证项 + 下一步选项（不替用户决定）。
本宿主渲染档位：M（Markdown：小标题+列表+粗体，表格 ≤4 列）。
完整规范见 SGME 仓库 AI-INSTALL/report-style.md，或技能 report-format。

