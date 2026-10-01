# Claude Code × SGME 适配器

把 Claude Code 接入 [SGME 拾光记忆引擎](http://<SGME_HOST>:9910)（本机/NAS）的宿主适配器：
**MCP stdio 动态透传 + 原生 hooks 会话生命周期**。

> SGME 三档适配模式中，Claude Code 属 **hooks 型**宿主：有 SessionStart / Stop / SessionEnd
> 事件机制，因此可以自研适配器实现「自动注入画像、每轮落盘、收尾提炼」零手工操作。

## 形态

| 组件 | 载体 | 作用 |
|---|---|---|
| MCP 透传 | `claude_sgme.mcp_proxy`（stdio） | 把 Claude Code 的工具面动态转发到远端 SGME MCP（41+ 工具，新增工具零改动） |
| 会话启动 | hook `SessionStart` → `cli hook session-start` | `inject` 画像 + `signal_pull` 未消费关怀信号 → additionalContext 注入 |
| 每轮落盘 | hook `Stop` → `cli hook stop` | 解析 transcript JSONL，增量 append 到 SGME L0（游标对账，服务端幂等） |
| 收尾提炼 | hook `SessionEnd` → `cli hook session-end` | 补一次追加 + `refine_trigger(async_mode=true)` |

## 安装

```powershell
# 0. 前置：已安装 SGME 实例，且本机已签发专属 key（claude-code）
python scripts\bootstrap_key.py      # 签发 agt_* key → ~/.sgme/claude-agent.json + 用户环境变量

# 1. 依赖（uv 或 pip 均可）
uv venv .venv
uv pip install httpx --python .venv\Scripts\python.exe
uv pip install -e . --python .venv\Scripts\python.exe

# 2. 冒烟
.venv\Scripts\python.exe -m claude_sgme.cli health

# 3. 注册 MCP server + hooks（自动备份，可重复运行）
.venv\Scripts\python.exe install.py

# 4. 重启 Claude Code 生效
```

## 配置（只走环境变量 / 部署配置，密钥不落代码）

| 变量 | 用途 |
|---|---|
| `SGME_BASE_URL` / `SGME_HTTP_URL` | 例如 `http://<SGME_HOST>:9910`（读不到时回退 `~/.sgme/claude-agent.json`） |
| `SGME_CLAUDE_KEY` | 本适配器专属 `agt_*` key（回退部署状态文件 `~/.sgme/claude-agent.json`） |
| `SGME_MCP_URL` | MCP 端点，缺省按 HTTP 端口 +3 推导（9910→9913/mcp） |
| `SGME_AGENT_ID` | 缺省 `claude-code`（以部署状态文件为准） |

所有 HTTP 调用等价 `trust_env=False`，内网流量不经系统代理。

## 运行时文件（都在本机，内容不含凭据）

- `~/.claude-sgme/state/cursor.json` — 每会话导出游标（条数 + 锚点，不存对话内容）
- `~/.claude-sgme/logs/hook.log` — hooks 运行日志（512KB 自动轮转）
- `~/.sgme/claude-agent.json` — 部署状态（agent_id / 地址 / key）

## 与官方适配器的关系

结构参照官方 `adapters/codex`（stdio 动态透传 + 生命周期 CLI），按 Claude Code
宿主特性替换为原生 hooks。官方适配器登记表暂无 claude——本适配器为宿主侧自研，
后续可按 `sgme-adapter-development` 流程上架技能库（`adapter-claude`）。

## 卸载 / 回退

`install.py` 每次写入前会生成 `*.bak-sgme-<时间戳>` 备份；恢复对应备份即可回退
MCP 注册与 hooks 配置。游标与日志目录可直接删除。
