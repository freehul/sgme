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

## 验证（装完怎么算成功）

1. `hermes plugins list` → sgme 行状态应为 `enabled`（`not enabled` = 未加载，见常见坑 1）
2. 确认 `config.yaml` 的 `memory.provider: sgme`
3. 重启 Hermes 后，SGME Gateway（:9910）日志应出现 Hermes 的 `/v1/append` 与 `/v1/admin/refine/trigger_async` 调用
4. 会话内验证：问一个历史问题，Hermes 应能通过 `sgme_memory_search` 工具召回 SGME 记忆

## 常见坑

1. **`plugins.enabled` 存成字符串**：`hermes config set plugins.enabled '[...]'` 会写成字符串而非 YAML list，加载器 `isinstance(list)` 校验失败 → 视为无插件加载。须手工把 config.yaml 里该键改回列表块（`- sgme` 形式）。
2. **插件目录位置**：用户级 provider 目录是 `$HERMES_HOME/plugins/sgme/`（**不带 `memory/` 子目录**）；放错位置 → `find_provider_dir` 返回 None，静默不加载。install.py 已按正确路径部署，别手搬到 `plugins/memory/sgme/`。
3. **config.yaml 是安全敏感配置**：部分工具（如 patch）拒绝写 config.yaml，直接改时用 Python 脚本写并备份（`.bak-pre-sgme`）。
4. **改桥接代码要同步部署副本**：改 `adapters/hermes/__init__.py` 后必须重跑 install.py 同步到 `$HERMES_HOME/plugins/sgme/`，否则 Hermes 加载的是旧副本。

## 卸载

删除 `$HERMES_HOME/plugins/sgme/`，config.yaml 改回 `memory.provider: holographic`（或其他），重启。
