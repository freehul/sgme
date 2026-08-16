# SGME 接入契约 v0.1

> **读者**：第一次接入 SGME 的 AI Agent（或给它配接入的人类）。回答一个问题：**我这个 Agent 该怎么接进 SGME？**
> 本文件是接入策略的**单一入口**；技术细节（端点/schema/验收）在《SGME-接口契约-v0.1》，本文件只做决策。

## 1. 三线决策树（先判断，再动手）

```text
你的宿主 Agent 有没有官方适配器？
  ├─ 有（Hermes / DSH）──────────→ 走【官方适配器线】§2
  └─ 没有 ──→ 你的宿主有没有 hook（SessionEnd/Stop 事件）？
        ├─ 没有 ────────────────→ 走【MCP 通用线】§3（默认，先用起来）
        └─ 有 ──────────────────→ 走【自研适配器线】§4（读接口契约自己造）
```

**总原则：默认向最省心收敛。** 能用 MCP 就用 MCP（零适配器成本），只有「想要会话结束自动提炼 + 实时信号」才值得自研适配器。

## 2. 官方适配器线（Hermes / DSH）

SGME 官方只维护 2 个适配器，其余一律不官方维护（走 §3 / §4）。

| 适配器 | 目录 | 接入形态 | 装法 |
|---|---|---|---|
| Hermes | `adapters/hermes/` | memory.provider 槽位（system_prompt_block + 每轮 sync_turn + 会话结束提炼） | `python adapters/hermes/install.py` |
| DSH | `adapters/dsh/` | Cordis 原生插件（首步注入 + 逐轮入库 + 工具 + 自进化） | `dsh plugin add dsh-sgme` 或 `adapters/dsh/install.py` |

两个官方适配器**同一条质量标准**（见 §5），装法/验证/常见坑见各自 README。

## 3. MCP 通用线（无 hook 的 Agent，默认）

**适用**：没有官方适配器、且宿主无 hook 的 Agent（如 Trae、ZCode、各类 CLI agent）。

**做法（零适配器成本）**：

1. 连 MCP `http://localhost:9913/mcp`，带 `X-API-Key`
2. 连上后第一件事调 `agent_onboarding()`——返回版本 + 29 工具清单 + 快速上手 + self_config 自助配置段
3. 按 self_config 把模板写进自己的身份文件（SOUL.md / AGENTS.md / CLAUDE.md，按你的工具机制自选）

**代价（必须知道的约束）**：无 hook 意味着「会话结束自动提炼」失效，只能：

- **自律**：会话收尾主动 `refine_trigger(async_mode=true)`
- **兜底**：服务端 `batch_scan` 定时扫 status=new（默认 10 分钟一轮），记忆不会丢，只会晚提炼

> 这就是为什么「默认先用起来」= MCP：零开发，代价只是提炼晚一点。

## 4. 自研适配器线（有 hook 的 Agent）

**适用**：宿主有 SessionEnd/Stop 事件机制，想要「会话结束自动提炼 + 实时信号 + 性能最优」的深度集成（如 Claude Code、Reasonix、自建 agent）。

**做法**：读《SGME-接口契约-v0.1》，照「§5 最小动作集 + §6 验收标准」实现。参考官方两个适配器源码（`adapters/hermes/`、`adapters/dsh/`）的「捕获→提炼→注入→检索」映射。

**为什么值得**：hooks 事件驱动比「LLM 自觉调 MCP」可靠（会话结束必然触发，不靠模型记得）；HTTP 直调零协议开销、可用 SSE 长连做实时关怀。

## 5. 官方适配器质量标准（两条硬标准）

凡是「官方适配器」，必须同时满足：

1. **install.py**：一键部署（注册 agent / 写 key / 生成部署产物），幂等可重跑
2. **README 四件套**：装法 + 能力清单 + 验证命令 + 常见坑

达标者才进 `adapters/` 官方目录；不达标的适配器不进官方（只能作为社区示例自担维护）。

## 6. 准入判断表（一眼查）

| 你的宿主 | 有官方适配器？ | 有 hook？ | 走哪条线 |
|---|---|---|---|
| Hermes | 是 | 是 | 官方适配器 |
| DSH | 是 | 是 | 官方适配器 |
| Claude Code | 否 | 是 | 自研适配器 |
| Reasonix | 否 | 是 | 自研适配器 |
| Trae / ZCode | 否 | 否 | MCP 通用 |
| 其它无 hook agent | 否 | 否 | MCP 通用 |

## 7. 相关文档

- 《SGME-接口契约-v0.1》——自研适配器的端点/schema/最小动作集/验收标准
- `docs/agent-onboarding.md`——MCP 接入的完整操作指引
- `adapters/hermes/README.md`、`adapters/dsh/README.md`——官方适配器装法