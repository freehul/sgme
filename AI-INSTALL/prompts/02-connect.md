# 任务卡 ② — 接入与验证

> **目标**：让 AI 把宿主接入 SGME——能写、能查、能提炼；并在完成时给出可复现的自检证据。
> 协议细节以 `../agent-onboarding.md` 为唯一真相；本卡负责选路与验收。

## 步骤

### 1. 选接入路径（三选一）

| 路径 | 适用 | 方式 |
|---|---|---|
| **通用直连** | 任何能发 HTTP/MCP 的 Agent | HTTP `http://<NAS_IP>:9910`；MCP `http://<NAS_IP>:9913/mcp`；请求头 `X-API-Key`（用 `SGME_AGENT_KEY` 或签发的 `agt_*`） |
| **Hermes** | Hermes Agent | 适配器经技能库获取（`skill_search("adapter")` → 同机 `skill_materialize` 落盘装包 / 跨机 `skill_get` 取正文自行写盘；物化落盘在 SGME 服务端，跨机拿不到产物）；源码副本另有 `adapters/hermes/` |
| **DSH** | DeepSeek Harness | 适配器经技能库获取（`skill_search("adapter")` → 同机 `skill_materialize` 落盘 / 跨机 `skill_get` 取正文自行写盘），按包内 README 执行 `install.py`，再 `dsh plugin add dsh-sgme` |

> 提示：权限分家——日常记忆读写用 Agent Key（`SGME_AGENT_KEY` / 专属 `agt_*`）；管理端点（含 register 签发）用 admin key（`SGME_ADMIN_KEY`），admin key 不当日常身份用（反查为合成身份 `default`）。默认开发 Key 仅限本机回环，远程调用一律 403（必须自定义 Key）。

### 2. 申请本 Agent 专属 Key（必做）

**每个 Agent 必须申请自己的专属 Key（`agt_*`），不得复用共享主 Key 或默认 Key。**

**a. 先确保 admin key 可得**（调用签发接口需要它），按优先级：

- 接入侧环境已有 `SGME_ADMIN_KEY` → 直接用；
- 能访问 SGME 服务端（同机 / 有 SSH）→ 从服务端 `config/.env` 读取 `SGME_ADMIN_KEY`，并同步到接入侧环境；
- 都不可得 → 向用户说明最小手动步骤（用户把服务端 `config/.env` 的 `SGME_ADMIN_KEY` 写入接入侧环境文件；不要在对话里传明文），**不阻塞主流程**，并在汇报中标注「admin key 待用户补充」。

**b. 调用 register 签发**（用上一步的 admin key）——`agent_id` 按宿主名命名（小写字母 / 数字 / 连字符，如 `hermes` / `dsh` / `claude-code`）：

```python
import os, requests

base = os.environ["SGME_BASE_URL"].rstrip("/")
headers = {"X-API-Key": os.environ["SGME_ADMIN_KEY"]}   # 从环境读，不硬编码

r = requests.post(f"{base}/v1/admin/agents/register", headers=headers,
                  json={"agent_id": "hermes", "scope": [], "agent_model": ""})
print(r.json())   # 已存在则返回 409 ERR_CONFLICT（见「失败处置」）
# 返回：{"agent_id": "hermes", "api_key": "agt_<uuid>", "role": "agent",
#       "scope": [], "agent_model": "", "note": "密钥仅此一次返回，请妥善保存"}
```

**c. 保存专属 Key**：`agt_*` 明文**仅此一次返回**——立即写入接入侧环境变量 `SGME_AGENT_KEY`（Hermes：`HERMES_HOME/.env`；DSH / 其他：各自环境变量机制）。密钥只进环境文件。

**d. 立即验证**：用新 Key 调 `POST /v1/search`（body `{"query": "接入自检"}`）——返回 200 且带 `results` 即 Key 生效（`GET /v1/health` 免鉴权，验不出 Key）。

### 3. 配置（只走环境变量）

| 变量 | 用途 |
|---|---|
| `SGME_BASE_URL` | 例如 `http://<NAS_IP>:9910` |
| `SGME_AGENT_KEY` | 本 Agent 专属 `agt_*`（上一步签发；不复用共享 Key） |
| `SGME_ADMIN_KEY` | 管理类操作（记忆纠错 / wiki 编辑 / 三池 / 配置）所需；**自动同步**：能读到 SGME 服务端 `config/.env` 就一并写入接入侧（与 agent key 同处）；读不到则在汇报中标注「由用户补充」 |

**禁止**把 Key 写进代码、文档或对话；密钥只进环境文件。

### 4. 联通自检（4 项，缺一不可）

1. **发现**：调用 `agent_onboarding`（MCP）或 `GET /v1/health` —— 拿到版本与状态字段（完整能力清单以 `agent_onboarding` 为准）。
2. **写入**：`POST /v1/append`，body 必带 `session_key` + `started_at`(ISO) + `content`（首行格式 `# {ISO} {role}`）；返回 `status: new` 即成功。
3. **检索**：`POST /v1/search` 用刚写入的关键词检索——能命中并带溯源。
4. **提炼**：MCP `refine_trigger(async_mode=true)`（或 HTTP admin `POST /v1/admin/refine/trigger_async`）触发一次——拿到任务号即算通过（本项可选，但推荐）。

> 网络提示：Python 用 `requests.Session()` 并 `trust_env=False`（防代理劫持本机/内网请求）；git-bash 的 curl 可能破坏中文 UTF-8，测试优先用 Python。

## 验收（全部满足才算完成）

- [ ] 专属 Key 已签发（`agt_*`）且已写入接入侧（非默认/共享 Key）
- [ ] `agent_onboarding` / `health` 调用成功（版本可见）
- [ ] append 写入返回 `status: new`，且重复提交同轮次被幂等处理
- [ ] search 能召回刚写入的内容
- [ ] 向用户报告：接入路径、端点、4 项自检结果

## 失败处置

| 症状 | 处置 |
|---|---|
| 403 | 鉴权/来源问题：远程必须自定义 Key（默认开发 Key 一律 403）；检查请求头与来源地址 |
| 400 | 参数问题：核对 `session_key` / `started_at` / 首行格式 |
| 连接超时 | 检查地址与端口；本机/内网请求绕开系统代理（`trust_env=False`） |
| search 不中 | 确认写入成功、检索词与内容匹配；向量未配时走纯 BM25 属正常 |
| `agent_id` 已存在（`ERR_CONFLICT`，409） | 复用已有 Key；或先吊销后重签：`DELETE /v1/admin/agents/{agent_id}`（`default` 不可吊销） |
| 读不到服务端 `config/.env` | 汇报中标注「admin key 待用户补充」；**不阻塞主流程**（admin key 到位后补做专属 Key 签发） |

---

## 整段复制版（中文）

> 你是我的 AI 助手。现在把我自己接入 SGME（个人记忆引擎）：
> 1. 端点：HTTP `http://<NAS_IP>:9910`、MCP `http://<NAS_IP>:9913/mcp`；密钥一律从环境变量读取（不要向我索要明文）。
> 2. 先申请你的专属身份：用 admin key（环境变量 `SGME_ADMIN_KEY`；读不到 SGME 服务端 `config/.env` 就向我说明最小手动步骤，不要阻塞）调 `POST /v1/admin/agents/register`，签发自己的 `agent_id`（按宿主名命名，如 `hermes`）与 `agt_*` Key（明文仅此一次），写入接入侧环境变量 `SGME_AGENT_KEY`，并把 admin key 一并同步进接入侧环境文件。
> 3. 如果你是 Hermes 或 DSH，请使用仓库 `adapters/` 下对应适配器的安装方式；否则走通用直连。
> 4. 完成 4 项自检：①拿到版本与状态字段 ②写入一轮测试并返回 `status: new` ③检索回读命中 ④触发一次提炼（异步）。
> 5. 向我汇报：接入路径、端点、专属 Key 的 agent_id、4 项结果。写入与检索的具体契约见 `AI-INSTALL/agent-onboarding.md`。

## English

**Task card ② — Connect & verify.** Pick a path: generic (HTTP `http://<NAS_IP>:9910` / MCP `:9913/mcp` with `X-API-Key`), Hermes or DSH (fetch the adapter from the SGME skill library: `skill_search("adapter")` → `skill_get` and write the body locally; `skill_materialize` only when you run on the same host as SGME — materialization lands on the SGME host, not yours; repo copies live under `adapters/`). Mandatory first: mint your own dedicated key — obtain an admin key (client-side env `SGME_ADMIN_KEY`, else read the server's `config/.env` on the same host / via SSH; if neither is possible, ask the user for the minimal manual step and continue without blocking), then call `POST /v1/admin/agents/register` with an `agent_id` named after your host (lowercase letters/digits/hyphens, e.g. `hermes` / `dsh` / `claude-code`). The returned `agt_*` plaintext is shown once — write it to the client env `SGME_AGENT_KEY`, verify with an authenticated endpoint (`POST /v1/search` → 200; `GET /v1/health` needs no key and proves nothing about it), and sync the admin key into the client env file too (`SGME_ADMIN_KEY`). Configure via env vars only — secrets live in env files, never in code, docs, or chat. Run 4 checks: discovery (version + status fields), write (append → `status: new`), read (search hits the new content), refine (async trigger, optional but recommended). Daily calls use an Agent key; admin endpoints (e.g. register) use the admin key; default dev keys are loopback-only (remote → 403). Use Python `requests` with `trust_env=False`; avoid git-bash curl for UTF-8 payloads.

**Copy block (English):**

> Act as my AI assistant and connect yourself to my SGME memory engine:
> 1. Endpoints: HTTP `http://<NAS_IP>:9910`, MCP `http://<NAS_IP>:9913/mcp`; read keys from env vars only (never ask me for secrets).
> 2. First mint your own dedicated identity: with an admin key (env `SGME_ADMIN_KEY`; if you can't read the server's `config/.env`, tell me the minimal manual step and continue without blocking) call `POST /v1/admin/agents/register` to claim your own `agent_id` (named after your host, e.g. `hermes`) and `agt_*` key (plaintext shown once); write it to the client env `SGME_AGENT_KEY`, and sync the admin key into the client env file as well.
> 3. If you are Hermes or DSH, use the matching adapter under `adapters/` in the repo; otherwise connect directly.
> 4. Run 4 checks: (a) fetch version + status fields, (b) write a test entry and get `status: new`, (c) retrieve it via search, (d) trigger one async refinement.
> 5. Report: path used, endpoint, your dedicated agent_id, and the 4 results. Contracts are in `AI-INSTALL/agent-onboarding.md`.
