# SGME × ZCode 官方适配器

ZCode（CLI Agent 平台）的 SGME 记忆引擎官方适配器。**Skill 形态**，与 doubao / mimo / workbuddy 三兄弟同构：SKILL.md 接入纪律 + 标准库零依赖 Python 客户端（HTTP + MCP 双通道），会话型自律接入。

## 文件清单

| 文件 | 职责 |
|---|---|
| `SKILL.md` | 技能本体（`sgme-zcode`）：接入纪律 + 五条铁律 + 41 能力矩阵；部署到 `~/.agents/skills/sgme-zcode/` |
| `scripts/sgme_client.py` | 运行时客户端：41 基准能力方法面 + CLI 命令面（`capabilities` 可离线核对） |
| `scripts/selfcheck.py` | 接入自检：静态能力矩阵 + 在线连通 + 一次 append 心跳（session_key=`zcode-selfcheck`） |
| `scripts/care_watch.py` | 主动关怀守护（可选）：pull / claim / ack / watch |
| `import_history.py` | 历史会话补导入：`~/.zcode/cli/db/db.sqlite` → SGME L0（幂等可重跑） |
| `install.py` | 部署：拷贝技能 + 注册 agent + 写部署配置 + 注册 ZCode MCP server + selfcheck |
| `tests/test_client.py` | 客户端单元测试（零网络，mock 网络层） |

## 装法

```bash
python adapters/zcode/install.py                       # 全默认（注册 + MCP 配置 + 自检）
python adapters/zcode/install.py --no-mcp              # 不动 ~/.zcode/cli/config.json
python adapters/zcode/install.py --base-url http://<SGME_HOST>:9910
python adapters/zcode/install.py --agents-md <AGENTS.md 路径>   # 向工作区 AGENTS.md 追加接入段
```

装完后**新开 ZCode 会话**生效：技能 `sgme-zcode` 自动加载；若未跳过 MCP 配置，`sgme` MCP server（`http://<host>:9913/mcp`）自动连接（Settings → MCP 可查状态）。

## 接入形态（为什么是自律型）

- ZCode 有 hooks 但仅 7 个事件、**无 SessionEnd**、stdin 负载未文档化、config 级 hooks 默认禁用——不适合做捕获主通道（对照：hermes 有 memory.provider 槽位、dsh 有 Cordis session 事件）
- Backlog ST-23② 将 ZCode 归类**自律型**：每轮对话结束 append 当前轮次（零 LLM 落盘）+ 会话结束 refine-trigger（async）；服务端 batch_scan / Dream 定时器兜底漏网提炼
- hooks 自动注入/捕获待「hook stdin 探针」任务实证后再评估（见 Backlog）

## 地址解析（六级）

`--base-url` → 环境变量 `SGME_HTTP_URL`/`SGME_BASE_URL` → 部署配置 `scripts/client.env` → 身份文件 `~/.sgme/zcode-agent.json` → **ZCode MCP 配置 `~/.zcode/cli/config.json` 的 `sgme` server（零配置继承）** → 回环默认。只给 HTTP 地址即可，MCP 端点自动按同主机端口 +3 / 路径 `/mcp` 推导（9910→9913）。

## 密钥链（不落盘进仓库）

`SGME_ZCODE_KEY`（专用，最高优先）→ 部署副本 `scripts/.env`（install.py 注册 `agt_*` key 后写入，**仓库外**）→ `~/.sgme/zcode-agent.json` 的 `api_key` → `~/.zcode/cli/config.json` 的 `sgme` server `X-API-Key` → `SGME_AGENT_KEY`（兜底，附警示——本机该变量可能属其它 agent，T-140）。

写侧/管理能力（`skill_put` / `skill_delete` / `skill_rename`）另需 `SGME_ADMIN_KEY`。

## 与其它官方适配器的差异

| 维度 | zcode | workbuddy / doubao / mimo |
|---|---|---|
| 部署位 | `~/.agents/skills/sgme-zcode/` | 各自平台技能目录 |
| 零配置继承 | `~/.zcode/cli/config.json` 的 `sgme` server | `~/.workbuddy/mcp.json` 等 |
| agent 注册 | install.py 默认注册（dsh 同款） | workbuddy 不注册 |
| 历史导入 | `import_history.py` 读 ZCode `db.sqlite` | dsh 读 session.jsonl.zstd；hermes 读 state.db |
| 原生 MCP | install.py 可注册 `mcp.servers.sgme`（type=http） | 平台已有 mcp.json |

## 常见坑

- **MCP 配置改后需新开会话**才生效；CLI 通道不受影响
- `mcp` 层逃生口需 mcp 库——用 SGME 项目 venv 执行
- `import_history.py` 需 `SGME_ADMIN_KEY`（查已有会话做幂等跳过 + 触发提炼）
- 导入 ≥20 会话后批量提炼分批（每批 ≤20）+ 批间 30–60s；429 不立即重试
- 测试样例全部假值（私网语义 `10.0.0.x`、占位密钥拼接写法防密钥扫描误拦）
