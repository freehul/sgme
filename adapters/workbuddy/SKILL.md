---
name: sgme
description: WorkBuddy 的 SGME（拾光记忆）官方适配器。当对话涉及「以前/上次/还记得/历史事实」、要求记住某事、需要关怀信号/角色扮演、读写 SGME 记忆/知识/技能、触发提炼或画像注入时使用。接入标记：SGME-ONBOARDING-v2。不适用于与 SGME 无关的通用问答。
---

# SGME 接入纪律（SGME-ONBOARDING-v2 · WorkBuddy）

你（WorkBuddy）已接入长期记忆引擎 SGME（ShiGuang Memory Engine）。职责：把会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

> 本技能是 SGME 官方适配器（源码 `adapters/workbuddy/`）。两种分发形态：① WorkBuddy 插件市场安装（本目录即插件包，由市场物化到插件缓存，更新走 `/plugin marketplace update`）；② 手动部署副本（`install.py` 同步，可随时重建）。
> **能力面 = CLI 命令面**：下方矩阵 60 个基准能力一个不少（服务端工具面 64 = 会话面 60 + 管理面豁免 4；管理面 `skill_candidate_*` 由 HTTP `/v1/admin` 承担，按 `scripts/adapter_parity_map.yaml` 豁免，不列入本矩阵）。
> **优先用本会话已挂载的 MCP 工具**（同名能力）；无 MCP 或要脚本化时再走 CLI。
> 本机 agent_id **默认 `workbuddy`**（服务端已注册）——可被本机覆盖：环境变量 `SGME_WORKBUDDY_AGENT_ID` → 身份文件 `~/.sgme/workbuddy-agent.json` 的 `agent_id` → 默认。**多设备接入时必须覆盖为服务端为本机签发的注册 id**（占位符示例 `<注册id>-<设备名>`）；用 `env-info` 查看生效值与来源。不要借用其它 agent 的 key。

## 服务发现（找不到 SGME 时按序）

1. 环境变量 `SGME_HTTP_URL` / `SGME_BASE_URL`、`SGME_MCP_URL`
2. 同目录部署配置 `scripts/client.env`（`install.py` 写入）
3. 本机身份文件 `~/.sgme/workbuddy-agent.json`（`http`/`mcp`/`agent_id`）
4. **WorkBuddy 自己的 MCP 配置 `~/.workbuddy/mcp.json` 的 `sgme` server**——本机通常已配好，直接继承地址与密钥
5. 回环默认 `http://127.0.0.1:9910`（MCP 同主机端口 +3 `/mcp`）
6. 探测 `GET /v1/health`；再读 `~/.sgme/install.json`；仍失败 → 报告「SGME 未发现」

端点自检：`python scripts/sgme_client.py env-info`（不打印密钥）。

## 接口与鉴权

| 项 | 值 |
|---|---|
| HTTP / MCP | 见上「服务发现」 |
| 请求头 | `X-API-Key` |
| agent_id | 默认 `workbuddy`（服务端已注册，溯源打标用）；本机可覆盖——环境变量 `SGME_WORKBUDDY_AGENT_ID` 或身份文件 `agent_id`，多设备接入用服务端为本机签发的注册 id（占位符示例 `<注册id>-<设备名>`）；`env-info` 查看生效值与来源 |
| agent 能力面 | 密钥按序解析：`SGME_WORKBUDDY_KEY` → `~/.sgme/workbuddy-agent.json` → `~/.workbuddy/mcp.json` 的 `sgme` server → `SGME_AGENT_KEY`（兜底） |
| 写侧/管理 | `SGME_ADMIN_KEY`（`skill_put`/`skill_delete`/`skill_rename` 服务端强制） |
| 自检 | `python scripts/selfcheck.py` |

密钥只读、不硬编码、不回显、不写入对话。

> ⚠️ **本机已知坑**：`SGME_AGENT_KEY` 在本机环境里可能是**其它 agent 的 key**
> （实测绑定 `agent_id=dsh`）。因此它被降为**最后兜底**；正常路径走
> `~/.workbuddy/mcp.json`（本机该处 key 已绑定 `workbuddy`）。
> 若发现写入的记忆 agent_tag 不对，先跑 `env-info` 看密钥来源。

## 使用纪律（五条铁律）

1. **每轮对话结束 `append` 当前轮次**——同一会话同一 `session_key`。
2. **会话结束 `refine-trigger`**（永远 async）。
3. **对话开始 `inject` 取画像 / `search` 检索相关记忆**。
4. **主动关怀靠消费信号**：`signal-pull` → `signal-claim` → 关怀 → `signal-ack`。可选 `care_watch.py pull` 轮询。
5. **对话开始（或用户指定角色）`role-list` → `role-assemble`**——换皮不换芯。

## 强制查询

涉及用户/项目历史事实（之前/以前/上次/还记得…），必须先 `search` 再回答；查不到如实说「记忆库中未找到」。

## 写入格式

`append` content 首行必须是 `# {ISO时间戳} {role}`（客户端自动生成）。

## 批量提炼纪律

≥20 文件分批（每批 ≤20）+ 批间 30–60 秒；429 不立即重试；永远 async。

## 三池主动登记

用户要办的事 → `demand-create --project-id <大写英文>`；创意 → `idea-add`；立项 → `project-register`（`project_id` **一律大写**）。

## 通信渠道（兜底铁律）

任何主动消息（关怀/提醒/告警）无论是否发到其它通道，**必须在当前会话也发一条**，直到用户明确取消。

## 事件对接

事件三类：`care_*` / `memory_updated` / `anomaly_warn`。接法：SSE `GET /v1/events/stream`、游标 `events-pull`、`signal-pull`。

## 能力矩阵（优先 MCP；CLI 兜底）

HTTP 层零依赖；MCP 层需 mcp 库，用 `<project-root>/.venv/Scripts/python.exe scripts/sgme_client.py …`。
核对：`sgme_client.py capabilities`。

| # | 能力 | 层 | 命令（或 MCP 同名工具） |
|---|---|---|---|
| 1 | agent_onboarding | MCP | `agent-onboarding` |
| 2 | append | HTTP | `append --session <key> --text "……"` |
| 3 | inject | HTTP | `inject --mode daily\|coding\|work\|full` |
| 4 | search | HTTP | `search "<query>" --scopes memory,skills,wiki` |
| 5 | answer | HTTP | `answer "<问题>"` |
| 6 | wiki_search | HTTP | `wiki-search "<q>"` |
| 7 | wiki_pages | HTTP | `wiki-pages` |
| 8 | wiki_page | HTTP | `wiki-page <page_id>` |
| 9 | wiki_page_add | HTTP | `wiki-page-add --title … --content …` |
| 10 | wiki_page_update | HTTP | `wiki-page-update --page-id … --content …` |
| 11 | wiki_evolve_trigger | MCP | `wiki-evolve-trigger` |
| 12 | memory_get | HTTP | `memory-get <id>` |
| 13 | memory_reject | HTTP | `memory-reject <id> --reason …` |
| 14 | memory_unreject | HTTP | `memory-unreject <id>` |
| 15 | refine_trigger | MCP | `refine-trigger` |
| 16 | refine_batch | MCP | `refine-batch` |
| 17 | refine_status | MCP | `refine-status` |
| 18 | review_list | MCP | `review-list [--limit N] [--offset N] [--dimension <id>]` |
| 19 | review_approve | MCP | `review-approve <id> [--note …]` |
| 20 | review_reject | MCP | `review-reject <id> --reason …` |
| 21 | review_auto_resolve | MCP | `review-auto-resolve [--days 7]`（管理类，需 SGME_ADMIN_KEY） |
| 22 | stats | MCP | `stats` |
| 23 | health | HTTP | `health` |
| 24 | config_get | MCP | `config-get` |
| 25 | config_update | MCP | `config-update` |
| 26 | idea_add | MCP | `idea-add` |
| 27 | demand_create | MCP | `demand-create` |
| 28 | project_register | MCP | `project-register` |
| 29 | signal_pull | MCP | `signal-pull` |
| 30 | signal_claim | MCP | `signal-claim` |
| 31 | signal_ack | MCP | `signal-ack` |
| 32 | signal_clear | MCP | `signal-clear` |
| 33 | role_list | MCP | `role-list` |
| 34 | role_assemble | MCP | `role-assemble` |
| 35 | role_active_get | MCP | `role-active-get` |
| 36 | role_active_set | MCP | `role-active-set` |
| 37 | persona_fill | MCP | `persona-fill` |
| 38 | guard_reply | MCP | `guard-reply <role_id> "<reply>" [--session-id <sid>]` |
| 39 | skill_search | HTTP | `skill-search` |
| 40 | skill_digest | HTTP | `skill-digest` |
| 41 | skill_get | HTTP | `skill-get` |
| 42 | skill_materialize | HTTP | `skill-materialize`（返回 path + sha256；落盘在服务端文件系统，跨机改用包通道 `fetch <name> <dest_dir>`） |
| 43 | skill_list | HTTP | `skill-list` |
| 44 | skill_coldstart | HTTP | `skill-coldstart` |
| 45 | skill_put | MCP | `skill-put` |
| 46 | skill_delete | MCP | `skill-delete` |
| 47 | skill_rename | MCP | `skill-rename` |
| 48 | credential_list | HTTP | `credential-list` |
| 49 | credential_get | HTTP | `credential-get <name>`（⚠️ 值不得进入对话正文——标准姿势 sgme_cred.py run 注入） |
| 50 | skill_catalog_version | HTTP | `skill-catalog-version`（两级缓存第 1 级，<100 字节） |
| 51 | skill_manifest | HTTP | `skill-manifest <name>`（package_sha256 + 附件清单 files[]） |
| 52 | skill_file_get | HTTP | `skill-file-get <name> <relpath>`（文本回原文；二进制只回摘要） |
| 53 | skill_file_put | HTTP | `skill-file-put <name> <relpath> --file <path>`（白名单目录；需管理员 Key） |
| 54 | skill_lint | HTTP | `skill-lint <name> --file <path>`（只校验不入库；需管理员 Key） |
| 55 | near_dup_list | MCP | `near-dup-list`（近似重复隔离区列表，灰区待人工复核） |
| 56 | near_dup_restore | MCP | `near-dup-restore`（放行一条近似重复，补录进记忆池 + 向量回填） |
| 57 | near_dup_discard | MCP | `near-dup-discard`（丢弃一条近似重复，不写进记忆池） |
| 58 | card_list | MCP | `card-list`（core 记忆卡列表：六张卡 + 槽位定义 + 字数上限） |
| 59 | card_update | MCP | `card-update`（写 core 卡一个槽位，旧值进历史；需用户授权） |
| 60 | card_history | MCP | `card-history`（core 记忆卡修改历史） |

扩展命令：`fetch <name> <dest_dir>`（跨机取技能完整包：两级缓存（目录版本短路 + package_sha256 比对，命中零下载）→ 未命中才下载 → MANIFEST 校验 → 安全解包；失败不留残件） / `events-pull` / `events-after` / `wiki-raw` / `capabilities` / `env-info` / `mcp <tool> k=v …`。

## 主动关怀守护（可选，默认不启用）

主人偏好低噪声（明确拒绝自动关怀推送），**本机不配置定时任务**；需要时手动跑一次：

```bash
python scripts/care_watch.py pull
```

仅当主人明确要求「主动提醒」时，才把它挂成定时轮询。

## 常见坑

- 代理环境变量可能劫持内网请求——客户端已忽略代理。
- 远程调用禁止 `dev-agent-key-change-me`（仅本机回环）。
- 测 API 用 Python（git-bash curl 破坏中文 UTF-8）。
- 本技能更新后需**新开 WorkBuddy 对话**才重新加载；若同名技能重复出现，在技能列表停用旧的一份，避免双触发。
- 密钥疑似用错（记忆被打上别的 agent_tag）→ 先 `env-info` 看「agent key 来源」，确认不是 `SGME_AGENT_KEY` 兜底路径。
- 记忆的 **agent_tag 不对** → 先看 **agent_id 来源**（`env-info`）：多设备接入时本机必须覆盖 `SGME_WORKBUDDY_AGENT_ID` 或身份文件 `agent_id`，否则会按默认值错标。
