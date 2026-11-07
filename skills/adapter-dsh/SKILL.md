---
name: adapter-dsh
description: DeepSeek Harness（dsh）的 SGME（拾光记忆）官方适配器（Cordis TS 原生插件）。当对话涉及「以前/上次/还记得/历史事实」、要求记住某事、需要关怀信号/角色扮演、读写 SGME 记忆/知识/技能、触发提炼或画像注入时使用。接入标记：SGME-ONBOARDING-v4。不适用于与 SGME 无关的通用问答。
category: adapter
tags: [skill, adapter, sgme]

---
# SGME 接入纪律（SGME-ONBOARDING-v4 · DSH）

你（DeepSeek Harness）通过官方 **Cordis TS 原生插件**接入长期记忆引擎 SGME（ShiGuang Memory Engine）。职责：把会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

> 本技能是 SGME 官方适配器（源码 `adapters/dsh/`，运行时主体 `sgme-bridge/`）。
> **形态**：TS 原生插件，运行时零 Python；Python 侧仅 `install.py` / `import_history.py`。
> **优先用插件已注册工具**（`memory_search` / `wiki_search` 等）与 `/sgme` 命令。

## 获取与安装（自助）

1. `skill_search("adapter dsh")` → `skill_get` 确认版本与纪律
2. `skill_materialize(name="adapter-dsh", dest_dir="<工作区>")` 落盘完整包到**本地** `<工作区>/adapter-dsh/`（走技能库包通道 + 两级缓存：目录版本短路 + `package_sha256` 比对，命中零下载；旧版服务端自动回退服务端物化）。也可直接用包通道 `GET /v1/skills/{name}/package`（或 `skill_fetch.py --name adapter-dsh --dest <工作区>` 一键下载校验解包，脚本经 `GET /v1/onboarding/docs` 在线获取；失败不留残件）
3. 运行 `python scripts/install.py --dir <项目根>`（注册 agent、写 `.env`、打印加载命令）
4. `dsh plugin add "link:<包路径>/package/sgme-bridge"`（或仓库路径）
5. 在含 `.env` 的目录启动 dsh

无 skill 通道时：从仓库 `adapters/dsh/` 安装（详见 `references/README.md`）。

## 服务发现（找不到 SGME 时按序）

1. 环境变量 `SGME_HTTP_URL` / `SGME_BASE_URL`、`SGME_MCP_URL`
2. 项目 `.env`（`SGME_AGENT_KEY` / `SGME_ADMIN_KEY`）
3. 本机 `~/.sgme/install.json`
4. 回环默认 `http://127.0.0.1:9910`
5. 探测 `GET /v1/health`；仍失败 → 报告「SGME 未发现」

## 接口与鉴权

| 项 | 值 |
|---|---|
| HTTP / MCP | 见上「服务发现」 |
| 请求头 | `X-API-Key` |
| agent 能力面 | `SGME_AGENT_KEY`（`agt_*`） |
| 写侧/管理 | `SGME_ADMIN_KEY` |
| 历史导入 | `python scripts/import_history.py`（幂等可重跑） |

密钥只读、不硬编码、不回显、不写入对话。

## 使用纪律（五条铁律）

1. **每轮自动入库**（插件 `turn/end` → `append`；勿手写 L0）。
2. **会话结束触发提炼**（插件；永远 async）。
3. **对话开始注入画像 / `memory_search` 检索**。
4. **主动关怀靠消费信号**：`signal_pull` → `signal_claim` → 关怀 → `signal_ack`。
5. **对话开始（或用户指定角色）`role_list` → `role_assemble`**——换皮不换芯。

## 重要信息存放处（凭据库）

SGME 第四模块 credentials 是跨机共享的 API Key / token / 密码的集中存放处（NAS 唯一真源，本机 .env 只是缓存）。

- 看有哪些：`credential_list()` / `sgme-cred list`（只回键名与元数据，永不回值）；
- 程序取用：`sgme-cred run -- <命令>`（注入环境变量，值不落盘、不进对话）；
- 必须显式查看：`credential_get(name)`——值不得写入对话正文或记忆；
- 写 / 删 / 恢复只走 admin（WebUI「凭证管理」或 `POST /v1/credentials`）。
- 文件 / 二进制重要数据同样放凭据库（`cred_type=file`）：`POST /v1/credentials/{key}/file` 上传（admin）、`GET` 同路径下载（agent，受 scope / 过期约束）；MCP 文本面只给元数据，不下发二进制。

## 强制查询

涉及用户/项目历史事实（之前/以前/上次/还记得…），必须先检索再回答；查不到如实说「记忆库中未找到」。

## 批量提炼纪律

≥20 文件分批（每批 ≤20）+ 批间 30–60 秒；429 不立即重试；永远 async。

## 三池主动登记

用户要办的事 → `demand_create`（`project_id` **一律大写**）；创意 → `idea_add`；立项 → `project_register`。

## 通信渠道（兜底铁律）

任何主动消息（关怀/提醒/告警）无论是否发到其它通道，**必须在当前会话也发一条**，直到用户明确取消。

## 事件对接

事件三类：`care_*` / `memory_updated` / `anomaly_warn`。接法：SSE 长连 / 游标拉取 / `signal_pull`。

## 能力面

插件工具面对齐 SGME MCP 基准（`append` 刻意不暴露——session-sync 自动入库）。清单见 `references/README.md` 与 `package/sgme-bridge/README.md`。

## 自检清单（接入完成标准，八项）

1. 发现：`GET /v1/health` 200  
2. 连通与身份：工具可调；密钥为专属 `agt_*`  
3. 写入：会话入库  
4. 检索：`memory_search` 命中  
5. 提炼：async 提炼可达  
6. 技能：`skill_search` 有结果  
7. 自我配置：身份文件含 `SGME-ONBOARDING-v4`  
8. 适配器：已安装本适配器（或显式声明走 MCP 通用接入并写入纪律模板）

## 汇报规范（v3 新增）
按 SGME 汇报规范输出：结论先行（一句话）+ 关键数据（实测并附来源/命令）+ 要点列表（≤7 条）+ 遗留风险/未验证项 + 下一步选项（不替用户决定）。
本宿主渲染档位：R（富 UI：dsh-ui 围栏，插件渲染）。
完整规范见 SGME 仓库 AI-INSTALL/report-style.md，或技能 report-format。

