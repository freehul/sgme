# 任务卡 ② — 接入与验证

> **目标**：让 AI 把宿主接入 SGME——能写、能查、能提炼；并在完成时给出可复现的自检证据。
> 协议细节以 `../agent-onboarding.md` 为唯一真相；本卡负责选路与验收。

## 步骤

### 1. 选接入路径（三选一）

| 路径 | 适用 | 方式 |
|---|---|---|
| **通用直连** | 任何能发 HTTP/MCP 的 Agent | HTTP `http://<NAS_IP>:9910`；MCP `http://<NAS_IP>:9913/mcp`；请求头 `X-API-Key`（用 `SGME_AGENT_KEY` 或签发的 `agt_*`） |
| **Hermes** | Hermes Agent | 运行 `adapters/hermes/` 的安装脚本，按适配器 README 配置端点与 Key 环境变量 |
| **DSH** | DeepSeek Harness | 安装 npm 包 `dsh-sgme`，按 `adapters/dsh/sgme-bridge/README.md` 配置 |

> 提示：远程接入必须用 Agent Key（`SGME_AGENT_KEY` / `agt_*`）；用 admin key 远程调会被拒（403）。默认开发 Key 仅限本机回环。

### 2. 配置（只走环境变量）

| 变量 | 用途 |
|---|---|
| `SGME_BASE_URL` | 例如 `http://<NAS_IP>:9910` |
| `SGME_AGENT_KEY` | Agent 身份 Key（每 Agent 独立签发为佳） |

**禁止**把 Key 写进代码、文档或对话。

### 3. 联通自检（4 项，缺一不可）

1. **发现**：调用 `agent_onboarding`（MCP）或 `GET /v1/health` —— 拿到版本与能力清单。
2. **写入**：`POST /v1/append`，body 必带 `session_key` + `started_at`(ISO) + `content`（首行格式 `# {ISO} {role}`）；返回 `status: new` 即成功。
3. **检索**：`POST /v1/search` 用刚写入的关键词检索——能命中并带溯源。
4. **提炼**：`POST /v1/refine_trigger`（`async_mode=true`）触发一次——拿到任务号即算通过（本项可选，但推荐）。

> 网络提示：Python 用 `requests.Session()` 并 `trust_env=False`（防代理劫持本机/内网请求）；git-bash 的 curl 可能破坏中文 UTF-8，测试优先用 Python。

## 验收（全部满足才算完成）

- [ ] `agent_onboarding` / `health` 调用成功（版本可见）
- [ ] append 写入返回 `status: new`，且重复提交同轮次被幂等处理
- [ ] search 能召回刚写入的内容
- [ ] 向用户报告：接入路径、端点、4 项自检结果

## 失败处置

| 症状 | 处置 |
|---|---|
| 403 | 鉴权/来源问题：远程必须 Agent Key；检查请求头与来源地址 |
| 400 | 参数问题：核对 `session_key` / `started_at` / 首行格式 |
| 连接超时 | 检查地址与端口；本机/内网请求绕开系统代理（`trust_env=False`） |
| search 不中 | 确认写入成功、检索词与内容匹配；向量未配时走纯 BM25 属正常 |

---

## 整段复制版（中文）

> 你是我的 AI 助手。现在把我自己接入 SGME（个人记忆引擎）：
> 1. 端点：HTTP `http://<NAS_IP>:9910`、MCP `http://<NAS_IP>:9913/mcp`；密钥从环境变量 `SGME_AGENT_KEY` 读取（不要向我索要明文）。
> 2. 如果你是 Hermes 或 DSH，请使用仓库 `adapters/` 下对应适配器的安装方式；否则走通用直连。
> 3. 完成 4 项自检：①拿到版本能力 ②写入一轮测试并返回 `status: new` ③检索回读命中 ④触发一次提炼（异步）。
> 4. 向我汇报：接入路径、端点、4 项结果。写入与检索的具体契约见 `AI-INSTALL/agent-onboarding.md`。

## English

**Task card ② — Connect & verify.** Pick a path: generic (HTTP `http://<NAS_IP>:9910` / MCP `:9913/mcp` with `X-API-Key`), Hermes (`adapters/hermes/`), or DSH (`dsh-sgme` npm package). Configure via env vars only (`SGME_BASE_URL`, `SGME_AGENT_KEY`) — never put keys in code, docs, or chat. Run 4 checks: discovery (version/capabilities), write (append → `status: new`), read (search hits the new content), refine (async trigger, optional but recommended). Remote calls require an Agent key (`SGME_AGENT_KEY` / `agt_*`); admin keys are rejected remotely (403). Use Python `requests` with `trust_env=False`; avoid git-bash curl for UTF-8 payloads.

**Copy block (English):**

> Act as my AI assistant and connect yourself to my SGME memory engine:
> 1. Endpoints: HTTP `http://<NAS_IP>:9910`, MCP `http://<NAS_IP>:9913/mcp`; read the key from env `SGME_AGENT_KEY` (never ask me for the secret).
> 2. If you are Hermes or DSH, use the matching adapter under `adapters/` in the repo; otherwise connect directly.
> 3. Run 4 checks: (a) fetch version/capabilities, (b) write a test entry and get `status: new`, (c) retrieve it via search, (d) trigger one async refinement.
> 4. Report: path used, endpoint, and the 4 results. Contracts are in `AI-INSTALL/agent-onboarding.md`.
