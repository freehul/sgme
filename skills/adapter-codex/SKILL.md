---
name: adapter-codex
description: Codex 的 SGME 官方适配器（动态 MCP 代理 + 会话生命周期 CLI）。当对话涉及历史事实、用户偏好、记忆读写、技能检索、Wiki 查询或提炼触发时使用。接入标记：SGME-ONBOARDING-v4。
category: adapter
tags: [skill, adapter, sgme]

---
# SGME × Codex 官方适配器

本适配器把 Codex 接入 SGME 统一能力平面。它不维护静态工具清单，而是通过
本地 stdio 代理动态转发远端 SGME MCP 工具面（当前 45 项）。

## 形态

- `install.py --register` 写入 Codex MCP server `sgme`，密钥只按名称继承
  `SGME_CODEX_KEY`，不落盘。
- MCP 透传覆盖 memory / skills / wiki / care signals / roles / config /
  refine 全部工具；新增 SGME MCP 工具无需逐项改适配器。
- **角色装配（T-303）**：调用 `role_assemble` 时默认传 `render=true`（服务端替换
  `{{char}}/{{user}}` 宏）；`user_name` 只在已知用户显示名时传，未知省略（服务端回落「你」）。
- `codex_sgme.cli` 提供会话生命周期 `start` / `turn` / `end`，用于每轮
  append 与会话结束异步提炼。
- stdio 使用 UTF-8；Windows 默认 GBK 环境不会再因工具描述中的非 GBK
  字符导致 `tools/list` 崩溃。

## 安装

```powershell
python install.py --register
```

地址与密钥由环境变量提供：

```text
SGME_BASE_URL=http://<SGME_HOST>:9910
SGME_MCP_URL=http://<SGME_HOST>:9913/mcp
SGME_AGENT_ID=codex
SGME_CODEX_KEY=<issued-by-sgme-admin>
```

## 安全边界

- 不硬编码密钥；`Settings.__repr__` 不输出 key。
- HTTP/MCP 客户端均使用 `trust_env=False`，避免本机代理劫持。
- 远端 MCP 工具权限仍由 SGME 的 agent key / admin key 规则约束。

## 重要信息存放处（凭据库）

SGME 第四模块 credentials 是跨机共享的 API Key / token / 密码的集中存放处（NAS 唯一真源，本机 .env 只是缓存）。

- 看有哪些：`credential_list()` / `sgme-cred list`（只回键名与元数据，永不回值）；
- 程序取用：`sgme-cred run -- <命令>`（注入环境变量，值不落盘、不进对话）；
- 必须显式查看：`credential_get(name)`——值不得写入对话正文或记忆；
- 写 / 删 / 恢复只走 admin（WebUI「凭证管理」或 `POST /v1/credentials`）。

## 获取完整包（技能库）

本适配器是终端 agent：跨机取技能完整包直接走**包通道**——`GET /v1/skills/{name}/package`（tar.gz；按包内 `MANIFEST.sha256` 校验后解包，失败不留残件），或 `skill_fetch.py --name <name> --dest <目录>` 一键下载校验解包（脚本经 `GET /v1/onboarding/docs` 在线获取）；同机可用 `skill_materialize`（落盘在 SGME 服务端文件系统）。

## 汇报规范（v3 新增）
按 SGME 汇报规范输出：结论先行（一句话）+ 关键数据（实测并附来源/命令）+ 要点列表（≤7 条）+ 遗留风险/未验证项 + 下一步选项（不替用户决定）。
本宿主渲染档位：M（Markdown：小标题+列表+粗体，表格 ≤4 列）。
完整规范见 SGME 仓库 AI-INSTALL/report-style.md，或技能 report-format。

