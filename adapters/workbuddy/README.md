# WorkBuddy × SGME 适配器

WorkBuddy（腾讯全场景 AI 办公工作台）的 SGME 记忆接入适配器，形态为 **WorkBuddy Skill（SKILL.md）+ Python 客户端 + 原生 MCP 优先**（与豆包工作、MiMo Desktop 同构：会话型自律接入）。

WorkBuddy 原生能力已含 MCP（`~/.workbuddy/mcp.json` 挂 `sgme`）。适配器补全的是**纪律与能力面**：SKILL.md 触发规则、41 基准能力 CLI、selfcheck、关怀守护（可选），以及从 WorkBuddy 自身 MCP 配置**零配置继承**地址与密钥。

## 与其他适配器的差异

| 适配器 | 形态 | 运行时 Python | 常驻能力 |
|---|---|---|---|
| hermes | Python 插件（memory.provider） | 是 | 有 |
| dsh | TS 原生插件（Cordis） | 否 | 有 |
| doubao | Skill + Python 客户端 | 是 | 无（signal-pull / care_watch） |
| mimo | Skill + Python 客户端 + 原生 MCP 优先 | 是 | 无（会话型；care_watch 可选） |
| **workbuddy（本适配器）** | **Skill（`~/.workbuddy/skills/sgme/`）+ Python 客户端 + 原生 MCP 优先** | 是 | 无（会话型；care_watch 可选） |

**WorkBuddy 独有增量**：

1. **从 `~/.workbuddy/mcp.json` 零配置继承**——地址与密钥都不必再填一遍（WorkBuddy 已配好 MCP 的自然结果），不新增一把 key
2. **密钥解析顺序避开 `SGME_AGENT_KEY`**——本机该变量实测绑定 `agent_id=dsh`，若照搬其它适配器的「环境变量优先」会把 WorkBuddy 的 L0 打上 dsh 的 `agent_tag`，污染多 Agent 溯源与隔离（T-140）。故降为最后兜底
3. **install.py 写本机身份文件但不写密钥**（`~/.sgme/workbuddy-agent.json` 只含 `agent_id`/`http`/`mcp`），恪守「密钥不落盘」

## 能力面（与 MCP 基准 41 个工具一一对应）

WorkBuddy 侧没有「工具注册表」——工具面即 CLI 命令面（SKILL.md 指示 agent 调 `scripts/sgme_client.py`）。客户端把 `sgme/mcp_server.py` 的 **41 个基准工具**逐个映射成子命令，缺一不可：

| 层 | 基准能力（41） | 说明 |
|---|---|---|
| HTTP（零依赖，19 个） | `health` `append` `inject` `search` `answer` `memory-get/reject/unreject` `skill-list/coldstart/search/digest/get/materialize` `wiki-search/pages/page/page-add/page-update` | 记忆/检索/技能读/wiki 读写 |
| MCP（需 mcp 库，22 个） | `agent-onboarding` `refine-trigger/batch/status` `stats` `config-get/update` `idea-add` `demand-create` `project-register` `signal-pull/claim/ack/clear` `role-list/assemble/active-get/active-set` `wiki-evolve-trigger` `skill-put/delete/rename` | 提炼/统计/配置/三池/信号闭环/角色/自进化/技能写侧 |
| 适配器扩展（6 个） | `events-pull` `events-after` `wiki-raw` `mcp`（逃生口）`capabilities` `env-info` | 超出基准的自有通道 |

合计 47 个 CLI 子命令（41 基准 + 6 扩展）。

```bash
python scripts/sgme_client.py capabilities   # 离线核对 41/41（方法层 + CLI 层双覆盖）
python scripts/selfcheck.py                  # 连通性 + 能力矩阵 + 身份来源
python scripts/sgme_client.py env-info       # 生效端点与密钥来源（不打印密钥）
```

写侧/管理类（`skill-put` / `skill-delete` / `skill-rename` 服务端强制；`config-update` / `signal-clear` 有管理员 Key 时自动使用）走环境变量 `SGME_ADMIN_KEY`。

## 工作原理

| 能力 | WorkBuddy 侧实现 | SGME 端点 |
|---|---|---|
| 画像 + 相关记忆注入 | SKILL.md 纪律：对话开始 `inject` + `search` | `POST /v1/inject` + `POST /v1/search` |
| 记忆/检索/问答工具 | `sgme_client.py`（HTTP 层 19 个基准命令，零依赖） | `/v1/append` `/v1/search` `/v1/answer` `/v1/memory/*` `/v1/skills/*` `/v1/wiki/*` |
| 会话入库 | SKILL.md 纪律：每轮结束 `append`（`session_key` 延续，agent_id=workbuddy） | `POST /v1/append` |
| 会话结束提炼 | MCP 层 `refine-trigger`（永远 async）；服务端 batch_scan 兜底 | MCP `refine_trigger` |
| 主动关怀 | 短连接 `signal-pull`（对话开始）；`care_watch.py pull`（**默认不启用定时轮询**） | `GET /v1/events/pull` + MCP `signal_claim/ack/clear` |
| 角色扮演 | MCP `role-list` → `role-assemble`（换皮不换芯） | MCP `role_*` |
| 待办/项目/创意登记 | MCP `demand-create` / `project-register` / `idea-add`（`project_id` 一律大写） | MCP（HTTP 端为 admin 端点） |
| 接入自检 | `selfcheck.py`：静态矩阵（41/41 双覆盖）+ 连通性实测 + 身份来源 | 全端点 |

## 目录

```
adapters/workbuddy/
├── SKILL.md            # 技能真源（name=sgme，含 SGME-ONBOARDING-v2 模板 + 41 能力矩阵）
├── locales/            # 显示名（zh-CN / en-US）
├── install.py          # 部署到 ~/.workbuddy/skills/sgme/ + client.env + 身份文件 + 自检
├── .gitignore          # 部署配置与身份文件不入库
├── README.md
├── scripts/
│   ├── sgme_client.py  # HTTP + MCP 双层客户端（41 基准 + 6 扩展）
│   ├── care_watch.py   # 主动关怀守护（可选，默认不启用）
│   └── selfcheck.py    # 接入自检
└── tests/
    └── test_client.py  # 单元测试（mock 网络，离线可跑）
```

## 地址与密钥（仓库干净，部署侧可连）

**地址解析顺序**（客户端构造时求值，无需改代码）：

1. 环境变量 `SGME_HTTP_URL` / `SGME_BASE_URL`（HTTP）、`SGME_MCP_URL`（MCP）
2. 同目录部署配置 `scripts/client.env`（`install.py` 写入部署副本；仓库内只有回环默认）
3. 本机身份文件 `~/.sgme/workbuddy-agent.json`
4. **WorkBuddy 自己的 MCP 配置 `~/.workbuddy/mcp.json` 的 `sgme` server**（本机通常已配好 → 零配置）
5. 回环默认 `http://127.0.0.1:9910`（MCP 按同主机「端口 +3 / 路径 /mcp」推导）

**密钥解析顺序**（同样不落盘、不回显）：

1. `SGME_WORKBUDDY_KEY`（专用变量，推荐）
2. `~/.sgme/workbuddy-agent.json` 的 `api_key`（若你手动加过）
3. `~/.workbuddy/mcp.json` 的 `sgme` server `X-API-Key`（**本机常态路径**）
4. `SGME_AGENT_KEY` —— ⚠️ 兜底；本机该变量可能属其它 agent（实测为 `dsh`）

管理类能力另用 `SGME_ADMIN_KEY`。诊断：`python scripts/sgme_client.py env-info`。

## 前置条件

1. SGME Gateway 运行中（HTTP `<SGME_HOST>:9910`，MCP `:9913`）
2. WorkBuddy 侧已有可用 key（已配好 MCP 时自动继承；否则设 `SGME_WORKBUDDY_KEY`）
3. Python 3.10+（HTTP 层仅标准库；MCP 层需 `mcp` 库——用 SGME 项目 `.venv`）

## 安装

```bash
# 默认：部署到 ~/.workbuddy/skills/sgme/，继承 mcp.json 的地址，写 client.env 与身份文件，跑自检
python adapters/workbuddy/install.py

# 显式指定地址 / 技能根 / 跳过自检 / 不写身份文件
python adapters/workbuddy/install.py --base-url http://<SGME_HOST>:9910
python adapters/workbuddy/install.py --skills-root <WORKBUDDY_SKILLS_DIR>
python adapters/workbuddy/install.py --no-selfcheck
python adapters/workbuddy/install.py --no-seed-identity
```

部署后**新开 WorkBuddy 对话**加载技能 `sgme`。原生 MCP 已配置时优先走 MCP 工具。

## 验证（装完怎么算成功）

1. `python adapters/workbuddy/scripts/selfcheck.py` 全绿（静态 41/41 + 连通性实测 + 身份来源可见）
2. 新会话触发本技能（问「之前 / 上次 / 还记得…」应走 `search`）
3. `search` / `append` / `refine-trigger` 各走通一次
4. 对话后 SGME 出现新 L0：`events-pull --subscriber workbuddy` 或管理端 sessions 可见
5. 离线核对命令面：`python scripts/sgme_client.py capabilities` → `41/41`

## 关键设计

- **能力面 = CLI 命令面**：41 个基准工具全部有对应子命令；`capabilities` 可离线自证覆盖度。
- **双通道**：HTTP 层（agent key 全可用、零依赖）覆盖记忆/检索/技能读/wiki；MCP 层补提炼/统计/配置/信号/角色/三池/自进化/技能写侧。
- **零配置继承**：直接从 WorkBuddy 已生效的 `mcp.json` 取地址与密钥，不新增 key、不重复配置。
- **密钥不落盘**：身份文件只写地址与 `agent_id`；密钥一律来自环境变量或既有 `mcp.json`。
- **防代理劫持**：`urllib` 显式无代理 opener（等价 httpx `trust_env=False`），防代理劫持内网。
- **append 格式**：content 首行自动生成 `# {ISO时间戳} {role}`，符合 422 校验。
- **提炼纪律**：永远 async；≥20 文件分批 + 批间 30–60s；429 不立即重试（batch_scan 兜底）。
- **故障隔离**：客户端错误只抛 `SGMEError` 并在 CLI 打印，绝不阻塞 WorkBuddy 主循环。
- **真源管理**：`adapters/workbuddy/` 为源码唯一副本，`~/.workbuddy/skills/sgme/` 是部署副本，改代码后重跑 `install.py`。

## 复现坑位

- **MCP 命令报「需要 mcp 库」**：MCP 层命令必须用装了 `mcp` 的 SGME 项目 venv 执行；HTTP 层任意 python3 可跑。
- **写侧能力 403**：`skill-put` / `skill-delete` / `skill-rename` 服务端强制管理员 Key → 设 `SGME_ADMIN_KEY`。
- **HTTP refine 403**：`/v1/admin/refine/*` 是 admin 端点 → 用 MCP 层 `refine-trigger`。
- **记忆 agent_tag 打错**：`env-info` 看「agent key 来源」，若显示走了 `SGME_AGENT_KEY` 兜底，说明既没配 `SGME_WORKBUDDY_KEY` 也没读到 `mcp.json`。
- **代理劫持内网**：客户端已显式忽略代理环境变量；若仍超时，检查系统代理（Clash 等）。
- **技能不生效**：新增/更新技能后需**新开 WorkBuddy 对话**才被加载。
- **selfcheck 写心跳**：会 append 一条 `workbuddy-selfcheck` 会话（幂等），`--no-append` 可跳过；`--static-only` 可离线只跑矩阵。
