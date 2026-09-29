---
name: hermes
description: Hermes 的 SGME（拾光记忆）官方适配器（memory.provider 插件）。当对话涉及「以前/上次/还记得/历史事实」、要求记住某事、需要关怀信号/角色扮演、读写 SGME 记忆/知识/技能、触发提炼或画像注入时使用。接入标记：SGME-ONBOARDING-v2。不适用于与 SGME 无关的通用问答。
---

# SGME 接入纪律（SGME-ONBOARDING-v2 · Hermes）

你（Hermes Agent）通过官方 **memory.provider 插件**接入长期记忆引擎 SGME（ShiGuang Memory Engine）。职责：把会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

> 本技能是 SGME 官方适配器（源码 `adapters/hermes/`）。
> **形态**：Python 插件（`memory.provider` 槽位），无 MCP 握手；工具在插件加载时注册。
> **优先用插件已注册的 SGME 工具**；无插件能力时走 HTTP CLI 兜底（见 `scripts/sgme_client.py`）。

## 获取与安装（自助）

1. `skill_search("adapter hermes")` → `skill_get` 确认版本与纪律
2. `skill_materialize(name="adapter-hermes", dest_dir="<工作区>")` 落盘完整包（**仅同机**——落盘发生在 SGME 服务端文件系统；跨机拿不到产物，改走下方「无 skill 通道」的仓库路径，或 `skill_get` 取正文自行写盘）
3. 运行 `python install.py`（或 `python install.py --home <HERMES_HOME>`）部署到 `$HERMES_HOME/plugins/sgme/`
4. `hermes plugins enable sgme` 后按下方纪律运行

无 skill 通道时：从仓库 `adapters/hermes/` 运行 `install.py`（详见 `references/README.md`）。

## 服务发现（找不到 SGME 时按序）

1. 环境变量 `SGME_HTTP_URL` / `SGME_BASE_URL`、`SGME_MCP_URL`
2. `$HERMES_HOME/sgme/config.json` 或 `sgme.json`（插件本地配置）
3. 本机 `~/.sgme/install.json`（地址/端口/Key 引用）
4. 回环默认 `http://127.0.0.1:9910`（MCP 同主机 +3 `/mcp`）
5. 探测 `GET /v1/health`；仍失败 → 报告「SGME 未发现」

## 接口与鉴权

| 项 | 值 |
|---|---|
| HTTP / MCP | 见上「服务发现」 |
| 请求头 | `X-API-Key` |
| agent 能力面 | `SGME_AGENT_KEY` 或插件配置（`agt_*`） |
| 写侧/管理 | `SGME_ADMIN_KEY`（config/skill 写侧） |
| 自检 | 插件加载后调 `sgme` 相关工具；或仓库侧 `scripts/selfcheck.py` |

密钥只读、不硬编码、不回显、不写入对话。

## 使用纪律（五条铁律）

1. **每轮对话结束 `append` 当前轮次**（插件 `sync_turn` 自动入库；勿手写 L0）。
2. **会话结束触发提炼**（插件 hook；永远 async）。
3. **对话开始 `inject` 取画像 / `search` 检索相关记忆**。
4. **主动关怀靠消费信号**：`signal_pull` → `signal_claim` → 关怀 → `signal_ack`。
5. **对话开始（或用户指定角色）`role_list` → `role_assemble`**——换皮不换芯。

## 强制查询

涉及用户/项目历史事实（之前/以前/上次/还记得…），必须先 `search` 再回答；查不到如实说「记忆库中未找到」。

## 批量提炼纪律

≥20 文件分批（每批 ≤20）+ 批间 30–60 秒；429 不立即重试；永远 async。

## 三池主动登记

用户要办的事 → `demand_create --project-id <大写英文>`；创意 → `idea_add`；立项 → `project_register`（`project_id` **一律大写**）。

## 通信渠道（兜底铁律）

任何主动消息（关怀/提醒/告警）无论是否发到其它通道，**必须在当前会话也发一条**，直到用户明确取消。

## 事件对接

事件三类：`care_*` / `memory_updated` / `anomaly_warn`。接法：SSE `GET /v1/events/stream`、游标 `events/pull`、`signal_pull`。

## 能力面（Hermes 插件已注册工具）

插件侧暴露记忆检索/注入/信号/三池/角色/技能等工具（对齐 MCP 基准；`append` 由 sync_turn 自动完成、`agent_onboarding` 因无 MCP 握手豁免）。完整清单见 `references/README.md` 与 `plugin.yaml`。

## 自检清单（接入完成标准，八项）

1. 发现：`GET /v1/health` 200  
2. 连通与身份：工具可调；密钥为专属 `agt_*`  
3. 写入：会话入库（或 `append`）  
4. 检索：`search` 命中  
5. 提炼：`refine_trigger` async 可用  
6. 技能：`skill_search` 有结果  
7. 自我配置：身份文件含 `SGME-ONBOARDING-v2`  
8. 适配器：已安装本适配器（或显式声明走 MCP 通用接入并写入纪律模板）
