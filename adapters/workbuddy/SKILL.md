---
name: sgme
description: WorkBuddy 的 SGME（拾光记忆）官方适配器。当对话涉及「以前/上次/还记得/历史事实」、要求记住某事、需要关怀信号/角色扮演、读写 SGME 记忆/知识/技能、触发提炼或画像注入时使用。接入标记：SGME-ONBOARDING-v2。不适用于与 SGME 无关的通用问答。
---

# SGME 接入纪律（SGME-ONBOARDING-v2 · WorkBuddy）

你（WorkBuddy）已接入长期记忆引擎 SGME（ShiGuang Memory Engine）。职责：把会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

> 本技能是 SGME 官方适配器（源码 `adapters/workbuddy/`；本目录为部署副本，由 `install.py` 同步，可随时重建）。
> **能力面 = CLI 命令面**：下方矩阵 41 个基准能力一个不少。
> **优先用本会话已挂载的 MCP 工具**（同名能力）；无 MCP 或要脚本化时再走 CLI。
> 本机 agent_id = `workbuddy`（服务端已注册）——不要借用其它 agent 的 key。

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
| agent_id | `workbuddy`（服务端已注册，溯源打标用） |
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
| 18 | stats | MCP | `stats` |
| 19 | health | HTTP | `health` |
| 20 | config_get | MCP | `config-get` |
| 21 | config_update | MCP | `config-update` |
| 22 | idea_add | MCP | `idea-add` |
| 23 | demand_create | MCP | `demand-create` |
| 24 | project_register | MCP | `project-register` |
| 25 | signal_pull | MCP | `signal-pull` |
| 26 | signal_claim | MCP | `signal-claim` |
| 27 | signal_ack | MCP | `signal-ack` |
| 28 | signal_clear | MCP | `signal-clear` |
| 29 | role_list | MCP | `role-list` |
| 30 | role_assemble | MCP | `role-assemble` |
| 31 | role_active_get | MCP | `role-active-get` |
| 32 | role_active_set | MCP | `role-active-set` |
| 33 | skill_search | HTTP | `skill-search` |
| 34 | skill_digest | HTTP | `skill-digest` |
| 35 | skill_get | HTTP | `skill-get` |
| 36 | skill_materialize | HTTP | `skill-materialize` |
| 37 | skill_list | HTTP | `skill-list` |
| 38 | skill_coldstart | HTTP | `skill-coldstart` |
| 39 | skill_put | MCP | `skill-put` |
| 40 | skill_delete | MCP | `skill-delete` |
| 41 | skill_rename | MCP | `skill-rename` |

扩展命令：`events-pull` / `events-after` / `wiki-raw` / `capabilities` / `env-info` / `mcp <tool> k=v …`。

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
