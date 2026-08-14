# WorkBuddy × SGME 适配器

WorkBuddy（本机 AI 助手）的 SGME 长期记忆接入适配器，**对标 `adapters/trae` 的 hookless 模式**：
WorkBuddy 通过 MCP 直连 SGME（在 `~/.workbuddy/mcp.json` 配 `X-API-Key`），不依赖 SessionStart/SessionEnd hook。
本适配器只负责「批量 / 历史会话导入」的格式收敛与写入，让任意 WorkBuddy 用户把本地会话沉淀进 SGME 记忆池，记忆可溯源到 `agent_id=workbuddy`。

## 数据来源

WorkBuddy 会话缓存：`~/.workbuddy/projects/<encoded-cwd>/<session-uuid>.jsonl`

- cwd 编码：`D:\Projects\SGME` → `d-Projects-SGME`（与 WorkBuddy 自身一致，见 `bridge.encode_project_dir`）
- 每行一条 JSON：原始对话（`role=user/assistant`、`timestamp=epoch 毫秒`、`content=文本块列表 input_text/output_text`）
- 噪音剔除：`<system-reminder>` 注入、file-history-snapshot 等非消息行在 `parse_workbuddy_jsonl` 中过滤

## 目录

- `bridge.py` — 解析 / 格式化 / 写入 / 检索（纯函数，可复用）
- `import_history.py` — 批量导入 CLI
- `.env` — 注册的 agent key（gitignore，不进仓库）
- `tests/` — 单元测试

## 一键接入（其他 WorkBuddy 用户）

1. 克隆 SGME，启动 Server（默认 `:9910` / `:9913`）
2. 在 `~/.workbuddy/mcp.json` 增加 sgme MCP（type=streamable-http, url=http://127.0.0.1:9913/mcp, headers.X-API-Key=<你的 workbuddy key>）
3. 或用本适配器批量导入历史会话：

   ```bash
   <项目根>/.venv/Scripts/python.exe <项目根>/adapters/workbuddy/import_history.py --oldest 5
   ```

   - `--oldest N` 只导入最早 N 个会话；`--session <uuid>` 单会话；`--dry-run` 只统计；`--no-refine` 导入后不触发提炼
   - 写入的会话 `session_key` 前缀 `workbuddy-`、`agent_id=workbuddy`，记忆可溯源到 WorkBuddy

## 提炼鉴权（重要）

SGME 提炼触发存在**鉴权分叉**，适配器已处理：

- **批量导入用 HTTP 管理端点** `/v1/admin/refine/trigger_async`，强制 `require_admin_key`
  → 需 `SGME_ADMIN_KEY`。`import_history.py` 会自动从项目 `config/.env` 兜底读取；
  若单独调用 `bridge.trigger_refine()`，请显式传 `key=os.environ["SGME_ADMIN_KEY"]`。
- **经 MCP 的 `refine_trigger` 工具**仅经 `ApiKeyMiddleware` 校验 `is_agent`，持有的
  WorkBuddy 注册 key（agent key）即可触发——无需 admin key。

> 即：持有 agent key 的用户可用 MCP 工具提炼；走本适配器 HTTP 批量导入则需 admin key。

## 数据流

```
WorkBuddy 会话(jsonl)
  → parse_workbuddy_jsonl  （过滤噪音、剥离 system-reminder）
  → to_l0                   (# ts user / ## ts assistant)
  → POST /v1/append         (agent_id=workbuddy)
  → trigger_async 提炼（HTTP 管理端点，需 SGME_ADMIN_KEY）
```

## 关键设计

- **故障隔离**：写入失败只记日志，不抛异常阻断（与 trae/reasonix 一致）
- **key 管理**：注册 key 只落 `.env`（`_load_env_file` setdefault 加载），代码零硬编码
- **时间戳**：WorkBuddy `timestamp` 是 epoch 毫秒 int，归一化为 ISO UTC（与 SGME L0 同口径）
- **幂等**：`workbuddy-<uuid>` 已在 `raw_files` 的记录跳过，重跑安全
