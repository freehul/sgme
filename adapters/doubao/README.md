# Doubao Work（豆包工作）× SGME 适配器

豆包工作（Doubao Work，桌面工作台 AI）的 SGME 记忆接入适配器，形态为**豆包工作原生 Skill
+ Python 客户端**（豆包工作无插件市场，其原生扩展机制即 Skills：SKILL.md 自动发现加载 +
脚本/工具执行；与 Hermes 的 provider 槽位、dsh 的 Cordis SDK 对位）。

## 与其他适配器的差异

| 适配器 | 形态 | 运行时 Python 依赖 | 常驻能力 |
|---|---|---|---|
| hermes | Python 插件（memory.provider 槽位） | 是（Hermes 加载 Python 插件） | 有（Hermes 常驻，SSE 长连） |
| dsh | TS 原生插件（Cordis SDK） | 否（运行时零 Python） | 有（dsh 常驻，session/event hook） |
| **doubao（本适配器）** | **Skill（SKILL.md）+ Python 客户端（HTTP+MCP 双层）** | 是（标准库零依赖；MCP 层需 mcp 库，用 SGME venv） | **无（会话型，短连接 signal-pull；可选 care_watch 定时轮询）** |

豆包工作是会话型 agent（无常驻进程、无 SessionEnd hook），按官方三档适配属于**自律型**：
对话开始注入/检索 → 每轮 append → 会话结束 refine（async），主动关怀走短连接 `signal-pull`。

> ✅ **官方适配器登记（2026-09-19）**：本适配器已登记进 SGME 官方适配器清单——`AI-INSTALL/agent-onboarding.md` §6 与 README（中英两版）「接入你的 AI」放置位置表。其他 agent 接入时可通过官方文档发现豆包适配器。

## 能力面（与 MCP 基准 41 个工具一一对应）

**豆包工作没有「工具注册表」——它的工具面就是 CLI 命令面**（SKILL.md 指示 agent 调 `scripts/sgme_client.py`）。
因此客户端把 `sgme/mcp_server.py` 的 **41 个基准工具**逐个映射成子命令，缺一不可：

| 层 | 基准能力（41） | 说明 |
|---|---|---|
| HTTP（零依赖，19 个） | `health` `append` `inject` `search` `answer` `memory-get/reject/unreject` `skill-list/coldstart/search/digest/get/materialize` `wiki-search/pages/page/page-add/page-update` | 记忆/检索/技能读/wiki 读写 |
| MCP（需 mcp 库，22 个） | `agent-onboarding` `refine-trigger/batch/status` `stats` `config-get/update` `idea-add` `demand-create` `project-register` `signal-pull/claim/ack/clear` `role-list/assemble/active-get/active-set` `wiki-evolve-trigger` `skill-put/delete/rename` | 提炼/统计/配置/三池/信号闭环/角色/自进化/技能写侧 |
| 适配器扩展（6 个） | `events-pull` `events-after` `wiki-raw` `mcp`（逃生口）`capabilities` `env-info` | 超出基准的自有通道 |

合计 47 个 CLI 子命令（41 基准 + 6 扩展）。

- 离线核对全量矩阵：`python scripts/sgme_client.py capabilities`（输出 41/41 覆盖表）。
- 写侧/管理类（`skill-put` / `skill-delete` / `skill-rename` 服务端强制；`config-update` / `signal-clear` 有管理员 Key 时自动使用）走环境变量 `SGME_ADMIN_KEY`。
- `skill_get` 支持 `--section` 章节取用；`skill_materialize` 返回 `path` + `sha256`。

## 工作原理

| 能力 | 豆包工作侧实现 | SGME 端点 |
|---|---|---|
| 画像 + 相关记忆注入 | SKILL.md 纪律：对话开始 `inject` + `search`（selfcheck 可验证） | `POST /v1/inject` + `POST /v1/search` |
| 记忆/检索/问答工具 | `sgme_client.py`（HTTP 层 19 个基准命令，零依赖） | `/v1/append` `/v1/search` `/v1/answer` `/v1/memory/*` `/v1/skills/*` `/v1/wiki/*` |
| 会话入库 | SKILL.md 纪律：每轮结束 `append`（session_key 延续，agent_id=doubao-work） | `POST /v1/append` |
| 会话结束提炼 | MCP 层 `refine-trigger`（永远 async）；服务端 batch_scan 兜底 | MCP `refine_trigger`（HTTP 端需 admin key） |
| 主动关怀 | 短连接 `signal-pull`（对话开始）/ `events-pull`（HTTP 游标）；`care_watch.py` 准常驻轮询（可选定时任务） | `GET /v1/events/pull` + MCP `signal_claim/ack/clear` |
| 角色扮演 | MCP `role-list` → `role-assemble`（换皮不换芯）；`role-active-get/set` 记录当前角色 | MCP `role_*` |
| 待办/项目/创意登记 | MCP `demand-create` / `project-register` / `idea-add` | MCP（HTTP 端为 admin 端点） |
| 接入自检 | `selfcheck.py`：静态能力矩阵（41/41 双覆盖）+ 14 项连通性实测 | 全端点 |

## 目录

```
adapters/doubao/
├── SKILL.md            # 豆包工作身份/纪律文件（真源，含 SGME-ONBOARDING-v2 模板 + 41 能力矩阵）
├── install.py          # 部署器：同步到豆包工作技能目录 + 写部署配置 client.env + 自检 + 写接入日志
├── .gitignore          # 部署配置 client.env 不入库（含环境实况地址）
├── scripts/
│   ├── sgme_client.py  # 客户端（HTTP 层零依赖 + MCP 层；41 基准能力 = 41 个子命令）
│   ├── care_watch.py   # 主动关怀守护（可选：pull/watch/claim/ack/clear）
│   ├── selfcheck.py    # 接入自检（能力矩阵 + 连通性，可重跑）
│   └── access-log.md   # 接入日志（部署时追加）
└── tests/
    └── test_client.py  # 客户端单元测试（mock 网络，离线可跑）
```

## 地址解析与密钥（仓库干净，部署侧可连）

客户端构造时按**三级顺序**解析（无需改代码）：

1. 环境变量 `SGME_HTTP_URL` / `SGME_BASE_URL`（HTTP）、`SGME_MCP_URL`（MCP）；
2. 同目录部署配置 `scripts/client.env`（`install.py` 部署时把解析出的地址写进**部署副本**；仓库内只有回环默认）；
3. 回环默认 `http://127.0.0.1:9910`（MCP 端点按同主机「端口 +3 / 路径 /mcp」推导）。

`install.py` 的解析顺序：`--base-url` → 环境变量 → 既有部署配置 → **旧部署副本里写死的地址（迁移沿用）** → 回环默认。
所以把生产地址从源码里清掉后重跑 `install.py`，豆包侧仍连得上原来的 SGME（第一次重部署会沿用旧副本地址并落到 `client.env`）。

密钥：只引用环境变量名，**永不落盘**——`SGME_AGENT_KEY`（agent 能力面）、`SGME_ADMIN_KEY`（写侧/管理能力）。
诊断：`python scripts/sgme_client.py env-info` 打印生效端点与密钥「是否设置」（不打印密钥值）。

## 前置条件

1. SGME Gateway 运行中（`http://<NAS_IP>:9910`，NAS 部署；MCP `:9913`）
2. 环境变量 `SGME_AGENT_KEY`（管理员签发的 `agt_*` key）对豆包工作会话可见；写侧能力另需 `SGME_ADMIN_KEY`
3. Python 3.10+（客户端仅标准库；MCP 层需 mcp 库——用 SGME 项目 `.venv`）

## 安装

```bash
# 0. 要指向 NAS 时先设地址（写进部署副本 client.env，不入库）
export SGME_HTTP_URL=http://<NAS_IP>:9910        # 或 SGME_BASE_URL

# 1. 部署到豆包工作技能目录 + 写部署配置 + 跑自检（默认 ~/DoubaoWork/skills）
python adapters/doubao/install.py

# 指定技能根目录 / 显式地址 / 跳过自检
python adapters/doubao/install.py --dest <DOUBAO_SKILLS_DIR>
python adapters/doubao/install.py --base-url http://<NAS_IP>:9910
python adapters/doubao/install.py --no-selfcheck

# 2. 重启豆包工作 / 开新会话，sgme-bridge 技能自动加载
```

## 验证（装完怎么算成功）

1. `selfcheck.py` 全绿：静态能力矩阵 41/41 + 实测 13 项（HTTP 只读 6 + append 心跳 1 + MCP 只读 6）= 14 项全通过
2. 会话内问一个历史问题：豆包工作应通过 `search` 召回 SGME 记忆（强制查询铁律）
3. 对话结束后 SGME 出现新 L0：`events-pull --subscriber doubao-work` 或管理端 sessions 列表可见
4. 主动关怀：`python scripts/care_watch.py pull` 正常返回（无信号时输出「暂无未消费信号 ✅」）
5. 离线核对命令面：`python scripts/sgme_client.py capabilities` → `41/41`

## 关键设计

- **能力面 = CLI 命令面**：41 个 MCP 基准工具全部有对应子命令（豆包 agent 靠 SKILL.md 指示调脚本，缺命令 = 缺能力）；`capabilities` 可离线自证覆盖度。
- **双通道**：HTTP 层（agent key 全可用、零依赖）覆盖记忆/检索/技能读/wiki；MCP 层补 refine/统计/配置/信号/角色/三池/自进化/技能写侧（HTTP 端这些是 admin 端点，agent key 会 403）。
- **密钥铁律**：只读环境变量 `SGME_AGENT_KEY` / `SGME_ADMIN_KEY`，不落盘、不硬编码、CLI 不回显；部署配置只写地址与密钥环境变量名。
- **地址三级解析**：环境变量 → 部署配置 `client.env` → 回环默认；仓库内不出现真实设备地址。
- **防代理劫持**：urllib 显式无代理 opener（等价 httpx `trust_env=False`），MCP 调用期间临时摘除代理环境变量，防 Clash 劫持内网。
- **append 格式**：content 首行自动生成 `# {ISO时间戳} {role}`，符合 422 校验。
- **提炼纪律**：永远 async；≥20 文件分批 + 批间 30–60s；429 不立即重试（batch_scan 兜底）。
- **故障隔离**：客户端错误只抛 SGMEError 并在 CLI 打印，绝不阻塞豆包工作主循环（同 dsh 设计）。
- **真源管理**：`adapters/doubao/` 为源码唯一副本，豆包工作技能目录是部署副本，改代码后重跑 `install.py`。

## 复现坑位

- **MCP 层 403/不可用**：确认 SGME 的 MCP 端点可达（客户端按 HTTP 地址推导同主机 `:9913/mcp`，也可用 `SGME_MCP_URL` 直接指定）；旧安装清单 `~/.sgme/install.json` 里的 127.0.0.1:9933 只对本机部署有效。
- **MCP 命令报「需要 mcp 库」**：MCP 层命令必须用装了 mcp 的 SGME 项目 venv 执行（`<project-root>/.venv/Scripts/python.exe`）；HTTP 层命令任意 python3 可跑。
- **写侧能力 403**：`skill-put` / `skill-delete` / `skill-rename` 服务端强制管理员 Key——设 `SGME_ADMIN_KEY`。
- **HTTP refine 403**：`/v1/admin/refine/*` 是 admin 端点——用 MCP 层 `refine-trigger`，不要绕行。
- **代理劫持内网**：如果 SGME 偶发超时，检查系统代理（Clash 等）是否劫持内网地址；客户端已显式忽略代理环境变量。
- **技能不生效**：新技能/更新后需重启豆包工作或开新会话才被 skill 发现机制加载。
- **selfcheck 写心跳**：`selfcheck.py` 会 append 一条 `doubao-work-selfcheck` 会话（幂等），`--no-append` 可跳过；`--static-only` 可离线只跑能力矩阵。
- **地址写错导致整轮不可用**：`env-info` 是离线命令，先跑它确认端点与来源，再排查环境变量/`client.env` 优先级。
