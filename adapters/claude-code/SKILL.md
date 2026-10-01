---
name: sgme-claude-code
description: Claude Code 的 SGME 官方适配器（MCP stdio 动态透传 + 原生 hooks：SessionStart 注入 / Stop 落盘 / SessionEnd 提炼）。当对话涉及历史事实、用户偏好、记忆读写、技能检索、Wiki 查询或提炼触发时使用。接入标记：SGME-ONBOARDING-v2。
---

# SGME × Claude Code 官方适配器

本适配器把 Claude Code 接入 SGME 统一能力平面。它不维护静态工具清单，通过本地
stdio 代理动态转发远端 SGME MCP 工具面（当前 41 项），并以 Claude Code 原生
hooks 实现会话生命周期自动化。

## 形态

- MCP：`python -m claude_sgme.mcp_proxy`（stdio 动态透传；UTF-8 固定，Windows
  GBK 环境不崩 `tools/list`）；`install.py` 注册 `~/.claude.json` 的 MCP server `sgme`。
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
