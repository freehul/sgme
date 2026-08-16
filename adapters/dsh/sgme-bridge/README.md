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

> ⚠️ **本插件依赖 SGME 本体，没有它插件是空壳**——装插件前先装好 SGME：

1. **安装 SGME 拾光记忆引擎**（GitHub 仓库 [freehul/sgme](https://github.com/freehul/sgme)）：
   ```bash
   git clone https://github.com/freehul/sgme.git
   cd sgme
   # 按 SGME 仓库 README 完成安装并启动（HTTP 9910 常驻运行）
   ```
2. **注册 agent 拿密钥**：运行 SGME 的 `adapters/dsh/install.py`，注册 DSH agent，生成 `SGME_AGENT_KEY` / `SGME_ADMIN_KEY` 并写入 `.env`；
3. **确认 SGME 在线**：`curl http://192.168.10.10:9910/v1/health` 返回 200 后再继续。

## 安装

```bash
# 安装到 profile（dsh plugin 转发 pnpm）
dsh plugin --profile <你的profile> add dsh-sgme
```

## 配置

`cordis.patch.yml` 默认值：

```yaml
baseUrl: http://192.168.10.10:9910
agentKey: process.env.SGME_AGENT_KEY   # 环境变量注入，密钥不落盘
adminKey: process.env.SGME_ADMIN_KEY
agentId: dsh
injectMode: daily
syncOnTurnEnd: true
```

- 密钥通过环境变量注入（符合 SGME 密钥不落盘铁律）；
- 生产环境请用 SGME 的 `adapters/dsh/install.py` 注册 agent 后覆盖 dev key。

## 用法

接入后，DSH 自动拥有 5 个工具：

- `memory_search(query, limit, dimensions, match)` — 查历史事实/偏好/决策，涉及「之前/以前/还记得」时必用；
- `wiki_search(query, limit)` — 查提炼后的场景知识；
- `signal_pull(signal_type, limit)` — 会话开始拉未消费关怀信号；
- `signal_claim(event_id)` — 原子认领信号（谁消费谁标记）；
- `signal_ack(event_id, status, result)` — 写消费回执。

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
