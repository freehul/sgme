# 接入后自检清单（Self-check）

> 部署 + 接入完成后，运行以下 **8 项**；**全绿 = 接入完成**，向用户汇报结果。
> 端点占位：HTTP `http://<NAS_IP>:9910`；MCP `http://<NAS_IP>:9913/mcp`；密钥一律从环境变量读取。
> 与 MCP `agent_onboarding.self_config.requirement` 同口径（2026-09-28 对齐）。
> 能力平面：SGME 的核心模块是 `memory / skills / wiki`；接入后只需宿主适配器，专业 skill 按需从 SGME 检索，不批量安装。

## 八项检查

### ① 发现
- **目的**：确认实例存在且版本可见
- **动作**：`GET /v1/health`（或 MCP `health`）
- **判据**：HTTP 200，含 `version` 与状态字段（`llm` / `refinement` / `vector` / `onboarding`；能力清单在 `agent_onboarding`，指引全文在 `GET /v1/onboarding/docs`）
- **失败**：回任务卡 ① 的发现三步

### ② 连通与身份
- **目的**：确认鉴权通、Agent 身份独立
- **动作**：调用 `agent_onboarding`（MCP）并确认返回能力清单
- **判据**：调用成功；密钥为本 Agent 专属 `agt_*`（由 register 签发；非共享主 key、非默认 key）
- **admin 通道**：配置了 admin key 时，调一个 admin 端点（如 `GET /v1/admin/agents`）确认 200；未配置则标注「待用户补充」
- **失败**：403 → 检查 Key 与来源（默认开发 Key 仅限本机回环；日常调用用 Agent Key）

### ③ 写入
- **目的**：确认本轮落盘链路通
- **动作**：`POST /v1/append`（`session_key` + `started_at` + 首行 `# {ISO} {role}`）
- **判据**：返回 JSON 中 `status: new`；同一轮重复提交被幂等处理
- **失败**：400 → 核对参数与首行格式

### ④ 检索
- **目的**：确认召回链路通
- **动作**：`POST /v1/search`，用刚写入的关键词
- **判据**：能命中该条并带溯源
- **失败**：向量未配时纯 BM25 属正常；仍不中 → 检查写入是否成功、检索词是否匹配

### ⑤ 提炼
- **目的**：确认夜间/收尾提炼任务可达
- **动作**：MCP `refine_trigger`（`async_mode=true`，默认即异步），或 HTTP admin `POST /v1/admin/refine/trigger_async`
- **判据**：拿到任务号；稍后可查状态
- **失败**：429 → 稍后重试或交服务端兜底；模型 Key 缺失 → 回任务卡 ①

### ⑥ 技能
- **目的**：确认按需技能检索协议可用
- **动作**：`skill_search("检索协议")` 或任一实际需要的主题
- **判据**：返回结果（如《技能检索协议》）；命中后 `skill_get` 拉全文
- **失败**：空结果 → 确认查询词；仍空 → 记录并继续（不阻断）

### ⑦ 自我配置
- **目的**：确认接入纪律已写入你的身份文件（换会话不失忆的根基）
- **动作**：读身份文件（SOUL.md / AGENTS.md / CLAUDE.md 等），确认含 `SGME-ONBOARDING-v2` 模板
- **判据**：标记存在且版本 ≥ v2；模板内容与 `agent_onboarding().self_config.template` 一致（以模板为准）
- **失败**：缺失 → 按 `agent_onboarding` 返回的 `self_config` 写入；无法写入 → 如实报告主人，禁止谎称完成

### ⑧ 适配器
- **目的**：确认纪律层载体已就位（官方适配器或显式通用接入）
- **动作**：`skill_search("adapter")` 或 `skill_search("<你的宿主名> 适配器")`；命中 `adapter-<host>` 则 `skill_materialize` 落盘并按包内说明安装
- **判据**：已安装对应官方适配器（`~/.sgme/` 或宿主技能目录可见部署副本）；**或**显式声明「走 MCP 通用接入」且 ⑦ 已完成
- **失败**：搜不到适配器 → 记录并走通用 MCP（⑦ 仍必做）；不要因缺适配器阻塞接入

## 汇报模板（给用户）

```
✅ SGME 接入自检
- 端点：http://<NAS_IP>:9910（版本 x.y.z）
- ①发现 ✓ ②连通 ✓ ③写入 ✓ ④检索 ✓ ⑤提炼 ✓ ⑥技能 ✓ ⑦自我配置 ✓ ⑧适配器 ✓
- 适配器：<adapter-xxx 已安装 | 通用 MCP>
- 缺失/待办：<无 或 列出>
```

## 常见失败速查

| 症状 | 首查 |
|---|---|
| 403 | 鉴权/来源：远程必须 Agent Key；请求头 `X-API-Key` |
| 400 | 参数：`session_key` / `started_at` / 首行格式 |
| 连接失败 | 地址端口、防火墙、系统代理（Python 设 `trust_env=False`） |
| 检索不中 | 写入是否成功、检索词、向量是否配置 |
| 提炼不动 | 模型 Key（`missing_keys`）、限速（429 交兜底） |
| ⑦ 不过 | 身份文件未写入 / 版本仍是 v1 → 以 `self_config.template` 为准重写 |
| ⑧ 搜不到 | `skill_search("adapter")`；仍空 → 通用 MCP + ⑦，勿阻塞 |

---

## English

**Post-setup self-check — run all eight; all green means you're done.** ① Discovery: `GET /v1/health` → 200 with `version` + status fields (`llm`/`refinement`/`vector`/`onboarding`; full capabilities come from `agent_onboarding`; online guide: `GET /v1/onboarding/docs`). ② Auth/identity: call `agent_onboarding`; your key must be the dedicated `agt_*` minted via register (not a shared master or default key). If an admin key is configured, call one admin endpoint (e.g. `GET /v1/admin/agents`) and expect 200; otherwise note it as pending user input. Default dev keys are loopback-only (remote → 403). ③ Write: `POST /v1/append` with `session_key` + `started_at` + `# {ISO} {role}` first line → `status: new`. ④ Read: `POST /v1/search` retrieves the new entry with provenance. ⑤ Refine: MCP `refine_trigger` (`async_mode=true`) or admin `POST /v1/admin/refine/trigger_async` returns a job id. ⑥ Skills: `skill_search("…")` returns hits; load with `skill_get`. ⑦ Self-config: your identity file contains the `SGME-ONBOARDING-v2` template (aligns with `agent_onboarding.self_config`). ⑧ Adapter: `skill_search("adapter")` → install `adapter-<host>` via `skill_materialize`, **or** explicitly use generic MCP with ⑦ done. Report a short summary (endpoint, version, eight checkmarks, adapter choice, gaps). Common issues: 403 auth/origin, 400 params, connection (proxy — set `trust_env=False`), retrieval (write success? vector configured?), refine (missing keys / rate limit), ⑦ missing template, ⑧ no adapter hit (fall back to generic MCP).
