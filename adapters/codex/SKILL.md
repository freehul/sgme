---
name: sgme-codex
description: Codex 的 SGME 官方适配器（动态 MCP 代理 + 会话生命周期 CLI）。当对话涉及历史事实、用户偏好、记忆读写、技能检索、Wiki 查询或提炼触发时使用。接入标记：SGME-ONBOARDING-v2。
---

# SGME × Codex 官方适配器

本适配器把 Codex 接入 SGME 统一能力平面。它不维护静态工具清单，而是通过
本地 stdio 代理动态转发远端 SGME MCP 工具面（当前 41 项）。

## 形态

- `install.py --register` 写入 Codex MCP server `sgme`，密钥只按名称继承
  `SGME_CODEX_KEY`，不落盘。
- MCP 透传覆盖 memory / skills / wiki / care signals / roles / config /
  refine 全部工具；新增 SGME MCP 工具无需逐项改适配器。
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

