# SGME × Hermes 适配插件

SGME 作为 Hermes **原生 memory.provider** 的桥接插件（架构 §18）。

## 目录说明（项目文件管理原则）

| 位置 | 角色 |
|---|---|
| `adapters/hermes/`（本项目） | **源码唯一副本**，随 SGME 版本控制与分发 |
| `$HERMES_HOME/plugins/sgme/`（Hermes 运行时） | 部署副本，由 `install.py` 生成，可随时重建 |

> 修改插件代码只改 `adapters/hermes/`，然后重跑 `install.py` 同步部署副本。

## 功能

- `system_prompt_block()` — SGME 画像摘要注入 system prompt
- `prefetch(query)` — 每轮 LLM 前召回相关记忆
- `sync_turn()` — 每轮对话写入 SGME 原始层（后台异步）
- `on_session_end()` — 会话结束触发异步提炼
- 工具：`sgme_memory_search` / `sgme_conversation_search`

## 安装

```bash
# 1. 部署插件（自动探测 HERMES_HOME）
python adapters/hermes/install.py

# 2. 指定目录（可选）
python adapters/hermes/install.py --home %LOCALAPPDATA%/hermes

# 3. 确认配置启用（memory.provider: sgme）
# 4. 重启 Hermes
```

## 前提

- SGME Server 常驻运行（HTTP 9910 / MCP 9913）
- Key 配置：环境变量 `SGME_ADMIN_KEY` / `SGME_AGENT_KEY`（或 plugin.yaml config 段默认值）

## 卸载

删除 `$HERMES_HOME/plugins/sgme/`，config.yaml 改回 `memory.provider: holographic`（或其他），重启。
