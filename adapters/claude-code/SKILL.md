---
name: sgme-claude-code
description: Claude Code 的 SGME 官方适配器（MCP stdio 动态透传 + 原生 hooks：SessionStart 注入 / Stop 落盘 / SessionEnd 提炼）。当对话涉及历史事实、用户偏好、记忆读写、技能检索、Wiki 查询或提炼触发时使用。接入标记：SGME-ONBOARDING-v4。
---

# SGME × Claude Code 官方适配器

本适配器把 Claude Code 接入 SGME 统一能力平面。它不维护静态工具清单，通过本地
stdio 代理动态转发远端 SGME MCP 工具面（当前 45 项），并以 Claude Code 原生
hooks 实现会话生命周期自动化。

## 形态

- MCP：`python -m claude_sgme.mcp_proxy`（stdio 动态透传；UTF-8 固定，Windows
  GBK 环境不崩 `tools/list`）；`install.py` 注册 `~/.claude.json` 的 MCP server `sgme`。
- **角色装配（T-303）**：调用 `role_assemble` 时默认传 `render=true`（服务端替换
  `{{char}}/{{user}}` 宏）；`user_name` 只在已知用户显示名时传，未知省略（服务端回落「你」）。
- hooks（注册进 `~/.claude/settings.json`）：
  - `SessionStart` → `inject` 画像 + `signal_pull` 关怀信号 → additionalContext；
  - `Stop` → transcript JSONL 增量 append 到 L0（游标对账；转录重写时全量重放，
    服务端按内容幂等）；
  - `SessionEnd` → 收尾追加 + `refine_trigger(async_mode=true)`。
- 钩子永不阻塞会话：异常吞掉写日志（`~/.claude-sgme/logs/hook.log`），退出码恒 0。

## 安装

```powershell
python scripts\bootstrap_key.py        # 签发专属 agt_* key（需 SGME_ADMIN_KEY）
python install.py                      # 注册 MCP server + hooks（自动备份，幂等）
```

地址与密钥由环境变量或部署状态文件提供：

```text
SGME_BASE_URL=http://<SGME_HOST>:9910
SGME_MCP_URL=http://<SGME_HOST>:9913/mcp
SGME_AGENT_ID=claude-code
SGME_CLAUDE_KEY=<issued-by-sgme-admin>
```

密钥解析优先级：`SGME_CLAUDE_KEY` 环境变量 → `~/.sgme/claude-agent.json` → `SGME_AGENT_KEY`。

## 安全边界

- 不硬编码密钥；`Settings.__repr__` 只输出脱敏来源。
- HTTP/MCP 客户端均使用 `trust_env=False`，避免本机代理劫持内网请求。
- 只读 Claude Code transcript，只写 SGME L0；不删除任何原件。
- 远端 MCP 工具权限仍由 SGME 的 agent key / admin key 规则约束。

## 重要信息存放处（凭据库）

SGME 第四模块 credentials 是跨机共享的 API Key / token / 密码的集中存放处（NAS 唯一真源，本机 .env 只是缓存）。

- 看有哪些：`credential_list()` / `sgme-cred list`（只回键名与元数据，永不回值）；
- 程序取用：`sgme-cred run -- <命令>`（注入环境变量，值不落盘、不进对话）；
- 必须显式查看：`credential_get(name)`——值不得写入对话正文或记忆；
- 写 / 删 / 恢复只走 admin（WebUI「凭证管理」或 `POST /v1/credentials`）。
- 文件 / 二进制重要数据同样放凭据库（`cred_type=file`）：`POST /v1/credentials/{key}/file` 上传（admin）、`GET` 同路径下载（agent，受 scope / 过期约束）；MCP 文本面只给元数据，不下发二进制。

## 获取完整包（技能库）

本适配器是终端 agent：跨机取技能完整包直接走**包通道**——`GET /v1/skills/{name}/package`（tar.gz；按包内 `MANIFEST.sha256` 校验后解包，失败不留残件），或 `skill_fetch.py --name <name> --dest <目录>` 一键下载校验解包（脚本经 `GET /v1/onboarding/docs` 在线获取）；同机可用 `skill_materialize`（落盘在 SGME 服务端文件系统）。

## 汇报规范（v3 新增）
按 SGME 汇报规范输出：结论先行（一句话）+ 关键数据（实测并附来源/命令）+ 要点列表（≤7 条）+ 遗留风险/未验证项 + 下一步选项（不替用户决定）。
本宿主渲染档位：M（Markdown：小标题+列表+粗体，表格 ≤4 列）。
完整规范见 SGME 仓库 AI-INSTALL/report-style.md，或技能 report-format。

