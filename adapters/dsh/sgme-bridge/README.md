# dsh-sgme — SGME 记忆引擎 DSH 插件

把 [SGME 拾光记忆引擎](https://github.com/freehul/sgme) 接入 DeepSeek Harness（DSH），让 DSH 拥有**多智能体共享的长期记忆**与**主动关怀**能力。

你的 AI，从此记得你——聊过的事，它都记得，还会主动关心你。

## 能力

| 能力 | 说明 |
|---|---|
| **画像自动注入** | `agent/pre-step` middleware：会话首步自动拉取 SGME 画像 + 相关记忆注入模型上下文（v2，2026-08-16 从"打日志"升级为真注入） |
| **自动捕获** | session-sync 监听会话事件，每轮对话自动 `append` 落盘（零 LLM 成本） |
| **记忆检索** | `memory_search` 检索 L1.5 标签化记忆池（带溯源） |
| **知识库检索** | `wiki_search` 检索 L2 场景知识库（比记忆更精炼） |
| **主动关怀** | `signal_pull` / `signal_claim` / `signal_ack` 三工具，消费 SGME 关怀信号——信号消费 = 主动关怀，谁消费谁标记 |
| **DSH 规则注入** | 读取 `~/.dsh/dsg-rules/rules.md` 注册为 `dsg:rules` system section（order -70）——身份/铁律/SGME手册/偏好/环境进稳定层，前缀缓存全命中（v0.2，2026-08-16） |

## 前置条件

> ⚠️ **兼容性（DSH RC 支持矩阵，2026-08-20 三端实证）**：
>
> | DSH 版本 | 验证状态 |
> |---|---|
> | `0.1.0-rc.6` | ✅ typecheck + 136 测试通过（开发基线） |
> | `0.1.0-rc.7` | ✅ 同上（rc.7 环境实测） |
> | `0.1.0-rc.8` | ✅ 同上（rc.8 环境实测，当前本机运行版） |
>
> peer 声明 `^0.1.0-rc.6` 覆盖 rc.6 → <0.2.0 全部版本；DSH 处于 RC 快速迭代期，**新 RC 发布后先跑一遍 rc-N 兼容验证再升级本插件**（方法见发布流程文档「RC 兼容验证」一节），验证通过前不承诺新 RC 兼容。

> ⚠️ **本插件依赖 SGME 本体（Python 服务 :9910），没有它插件是空壳**——先装本体，再装插件。两种路径任选：

**路径 A：Docker 一键部署（推荐）**

```bash
git clone https://github.com/freehul/sgme.git
cd sgme
docker compose up -d                      # 首次自动构建镜像并启动（HTTP 9910）
curl http://localhost:9910/v1/health      # 返回 {"status":"ok"} 即就绪
```

> 需要本机已装 Docker；若 SGME 部署在其他机器/NAS，跳过本机安装，直接把下面的 `SGME_BASE_URL` 指过去即可。

**路径 B：Python 本地开发**

```bash
git clone https://github.com/freehul/sgme.git
cd sgme
python -m venv .venv                      # 需 Python 3.11+
.venv\Scripts\activate                # Windows；Linux/macOS: source .venv/bin/activate
pip install -e .
cp config/.env.example .env               # 填入 DEEPSEEK_API_KEY / VOLC_API_KEY 等密钥
python -m sgme                            # 启动服务（HTTP 9910 常驻）
curl http://localhost:9910/v1/health
```

**注册 agent 拿密钥**（两条路径都需要）：本体启动后运行 `adapters/dsh/install.py`，注册 DSH agent，把 `SGME_AGENT_KEY` / `SGME_ADMIN_KEY` 写入 `.env`。

**服务地址**：插件 `baseUrl` 由环境变量 `SGME_BASE_URL` 注入（缺省指向 NAS `192.168.10.10:9910`，面向多 agent 共享场景）。本地部署时设 `SGME_BASE_URL=http://127.0.0.1:9910` 即可，无需改代码。

## 安装

```bash
# 安装到 profile（dsh plugin 转发 pnpm）
dsh plugin --profile <你的profile> add dsh-sgme
```

## 安装后验证（三步自检）

1. **确认插件已挂载**：`dsh --profile <你的profile> --dump-config`，输出应含 `dsh-sgme`；
2. **一条命令自检**：会话内输入 `/sgme`（或 `/sgme status`）——显示 SGME 连接状态、版本、LLM、记忆水位；不可达时直接给出本体安装指引；
3. **首次对话观察**：对话开始会自动注入画像与相关记忆；可先 `/sgme 试试` 确认检索链路。

> 冷启动提示：新装后记忆池是空的，前几轮对话后记忆才开始积累；`wiki_search` 可立即查到 SGME 自带的操作手册。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `/sgme status` 显示不可达 | SGME 本体未启动或地址不对 | 按「前置条件」装好本体，确认 `curl /v1/health` 返回 200；本地部署设 `SGME_BASE_URL=http://127.0.0.1:9910` |
| 401 / key 无效 | agent key 未注册或过期 | 运行 `adapters/dsh/install.py` 重新注册 |
| 插件加载报版本不兼容 | dsh 版本与插件锁定版本不符 | 本插件面向 dsh `0.1.0-rc.6`，升级 dsh 前先确认兼容 |
| 注入/检索超时或异常走外网 | 代理劫持 localhost | 插件 fetch 显式禁用代理；检查系统代理是否拦截 127.0.0.1 |
| 日志去哪看 | — | 插件日志在 dsh stderr；SGME 日志在 `logs/`（本机）或 `docker logs sgme`（容器） |

## 配置

`cordis.patch.yml` 默认值：

```yaml
baseUrl: process.env.SGME_BASE_URL ?? 'http://192.168.10.10:9910'   # 环境变量注入，缺省指向 NAS
agentKey: process.env.SGME_AGENT_KEY   # 环境变量注入，密钥不落盘
adminKey: process.env.SGME_ADMIN_KEY
agentId: dsh
injectMode: daily
syncOnTurnEnd: true
```

- `baseUrl` 与密钥均通过环境变量注入（符合 SGME 密钥不落盘铁律）；`SGME_BASE_URL` 缺省指向 NAS（192.168.10.10:9910），SGME 部署在其他机器时改环境变量即可，无需改代码；
- 生产环境请用 SGME 的 `adapters/dsh/install.py` 注册 agent 后覆盖 dev key（install.py 已把 `SGME_BASE_URL` 一并写入 `.env`）。

## 用法

接入后，DSH 自动拥有 7 个工具：

- `memory_search(query, limit, dimensions, match)` — 查历史事实/偏好/决策，涉及「之前/以前/还记得」时必用；
- `wiki_search(query, limit)` — 查提炼后的场景知识（FTS5 BM25 + 中文分词）；
- `wiki_pages(category, limit)` — 按 category 列知识库手册目录（如 skill/sgme），渐进式披露 L2 索引层（W5）；
- `wiki_page(page_id)` — 按 page_id 拉取知识库手册全文（技能手册/踩坑记录），索引 skill 引导的加载通道（W5）；
- `signal_pull(signal_type, limit)` — 会话开始拉未消费关怀信号；
- `signal_claim(event_id)` — 原子认领信号（谁消费谁标记）；
- `signal_ack(event_id, status, result)` — 写消费回执。

自进化（W4）：每个 turn 结束自动触发 SGME 经验回写（`/v1/wiki/evolve/trigger`，evolveEnabled 默认 true）——会话中的踩坑/新流程由 LLM 提炼后追加到知识库手册「踩坑记录」，多 agent 共享。

## 接入纪律

DSH 接入 SGME 后应遵守五条铁律（详见 [SGME 接入文档](https://github.com/freehul/sgme/blob/main/docs/agent-onboarding.md)）：

1. 每轮对话结束 `append` 当前轮次；
2. 会话结束 `refine_trigger(async_mode=true)`；
3. 对话开始 `inject` / `search` 取画像与记忆；
4. 主动关怀靠消费信号：`signal_pull` → `signal_claim` → 关怀 → `signal_ack`；
5. 角色模板：`role_list` / `role_assemble` 换皮不换芯。

**通信渠道兜底**：当前会话是兜底通信渠道，任何主动消息都必须在当前会话也发一条，直到用户取消。

## 协议

- HTTP API（:9910）+ MCP（:9913），鉴权 `X-API-Key`；
- 事件对接：SSE 长连 `GET /v1/events/stream` / 游标拉取 `GET /v1/events/pull`（常驻 agent 首选 SSE）。

## 开发

```bash
pnpm install
pnpm run verify   # typecheck + test + build
```

## 许可证

[MIT](LICENSE) © 2026 freehul
