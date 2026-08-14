# DeepSeek Harness (dsh) × SGME 适配器

[DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) 的 SGME 记忆接入适配器，
走 **dsh 原生插件**（Cordis 插件 SDK），不是 Python bridge 桥接（dsh 有完整插件 SDK，运行时能力全在 TS 内完成）。

## 与其他适配器的差异

| 适配器 | 形态 | 运行时 Python 依赖 |
|---|---|---|
| hermes | Python 插件部署副本 | 是（Hermes 加载 Python 插件） |
| reasonix | Python bridge.py（hooks 桥接） | 是（hooks 调外部脚本） |
| trae | Python bridge.py（瘦桥接） | 是（MCP 直连 + 桥接） |
| **dsh（本适配器）** | **TS 原生插件（Cordis SDK）** | **否（运行时零 Python）** |

dsh 有完整插件 SDK（工具注册/事件监听/上下文注入/命令注册），运行时能力全部在 TS 插件内完成，
不需要 Python bridge 做运行时桥接。Python 侧仅 `install.py`（安装引导）+ `import_history.py`（历史会话补导入，可选）。

## 工作原理

| 能力 | dsh 侧实现 | SGME 端点 |
|---|---|---|
| 画像 + 相关记忆注入 | `ctx.on('agent/pre-step')` 首步拦截 + `agent.inject(message)` | `POST /v1/inject` + `POST /v1/search` |
| `memory_search` 工具 | `ctx.tools.register(defineTool(...))` | `POST /v1/search`（scopes: memory） |
| `wiki_search` 工具 | `ctx.tools.register(defineTool(...))` | `POST /v1/search`（scopes: wiki） |
| `/sgme` 命令 | `ctx.command.register(...)` | `POST /v1/search`（memory + wiki） |
| 会话入库 | `ctx.on('session/event')` 拦 `turn/end` → L0 → 触发提炼 | `POST /v1/append` + `POST /v1/admin/refine/trigger_async` |

## 目录

- `sgme-bridge/` — dsh 原生 TS 插件本体（标准 dsh 插件结构，可独立 `pnpm verify` + `dsh plugin add`）
- `install.py` — 一键安装引导（注册 agent + 写 .env + 打印 dsh plugin add 命令）
- `import_history.py` — 历史会话补导入（可选，与 reasonix 同款幂等可重跑）
- `.env` — 适配器侧注册的 agent key（gitignore，不进仓库）；dsh 加载路径另见 <项目根>/.env
- `tests/` — Python 侧单元测试（install + import_history）

## 前置条件

1. SGME Gateway 运行中（`http://127.0.0.1:9910`，默认端口）
2. dsh 已安装（`dsh --version` 可执行）
3. SGME 项目 venv 可用（`.venv/Scripts/python.exe`）

## 安装

```bash
# 1. 注册 agent + 写 .env + 生成 AGENTS.md + 打印 dsh 加载命令
<项目根>/.venv/Scripts/python.exe <项目根>/adapters/dsh/install.py --dir D:/Projects/<目标项目>
# key 会写入两处：
#   - adapters/dsh/.env   （Python 侧：import_history.py 等用）
#   - <项目根>/.env       （dsh 加载路径，插件据此读 key）
# 若 dsh 从其他目录启动，用 --dsh-env <该目录>/.env 指定

# 2. 加载 dsh 插件（本地 link 模式，改代码即生效）
dsh plugin --profile web add "link:D:/Projects/SGME/adapters/dsh/sgme-bridge"

# 3. 启动 dsh（需在含 .env 的目录下，或在启动前 export SGME_AGENT_KEY/SGME_ADMIN_KEY）
dsh --profile web
```

## 验证

```bash
# 确认插件挂载
dsh --profile web --dump-config
# 期望：@sgme/sgme 插件出现在已加载列表

# 会话内验证检索
/sgme 测试

# 查 SGME 确认 L0 入库
curl http://127.0.0.1:9910/v1/admin/sessions -H "X-API-Key: <admin-key>"
```

## 数据流

```
dsh 会话开始 → agent/pre-step 首步拦截 → sgme-bridge
    ├─ POST /v1/inject        （用户画像）
    ├─ POST /v1/search        （项目相关记忆）
    └─ agent.inject(message)  （注入 inbox）

dsh 每轮对话 → memory_search / wiki_search 工具（按需）
    └─ POST /v1/search

dsh 对话回合结束 → session/event(turn/end) → sgme-bridge
    ├─ 收集本 turn 消息 → 转 L0 格式
    ├─ POST /v1/append       （session_key=dsh-<sessionId>，agent_id=dsh）
    └─ POST /v1/admin/refine/trigger_async
```

## 关键设计

- **运行时零 Python 依赖**：TS 插件用 Node 内置 `fetch`，不调 bridge.py
- **故障隔离**：所有 SGME 调用失败只 log + 返回空，绝不阻塞 dsh 主循环
- **key 管理**：注册 key 落 `.env`（install.py 写入），TS 插件 config 用 `!!js process.env.SGME_AGENT_KEY/SGME_ADMIN_KEY` 引用（cordis.patch.yml），dsh 启动时把 `<cwd>/.env` 物化进 process.env——代码与配置零硬编码，符合 SGME 密钥不落盘铁律
- **防代理劫持**：fetch 不读 `HTTP_PROXY` 环境变量（防 Clash 劫持 localhost），用显式 `127.0.0.1`
- **v1 能力边界**：首步注入 + 每 turn append + 2 工具 + 1 命令；每轮注入/攒批留 v2

## 复现坑位

- dsh v0.1 API 不稳定（`SESSION_FORMAT_VERSION=0`，no compatibility promise）——插件代码加版本探测，pin dsh commit
- `agent/pre-step` 注入语义复杂（agent-instructions 是首次注入 + 变更检测）——v1 只做首步注入，避开去重/预算冲突
- TS 构建链对 SGME 项目（纯 Python）是新增依赖——`sgme-bridge/` 自带 `package.json`，`lib/` 产物提交，运行时不要求宿主装 pnpm
