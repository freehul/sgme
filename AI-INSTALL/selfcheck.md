# 接入后自检清单（Self-check）

> 部署 + 接入完成后，运行以下 6 项；**全绿 = 接入完成**，向用户汇报结果。
> 端点占位：HTTP `http://<NAS_IP>:9910`；MCP `http://<NAS_IP>:9913/mcp`；密钥一律从环境变量读取。

## 六项检查

### ① 发现
- **目的**：确认实例存在且版本可见
- **动作**：`GET /v1/health`（或 MCP `health`）
- **判据**：HTTP 200，含 `version` 与 `capabilities`
- **失败**：回任务卡 ① 的发现三步

### ② 连通与身份
- **目的**：确认鉴权通、Agent 身份独立
- **动作**：调用 `agent_onboarding`（MCP）并确认返回能力清单
- **判据**：调用成功；密钥为 `SGME_AGENT_KEY` / `agt_*`（远程不要用 admin key）
- **失败**：403 → 检查 Key 与来源（远程仅 Agent Key 可用）

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
- **动作**：`POST /v1/refine_trigger`，`async_mode=true`
- **判据**：拿到任务号；稍后可查状态
- **失败**：429 → 稍后重试或交服务端兜底；模型 Key 缺失 → 回任务卡 ①

### ⑥ 技能
- **目的**：确认按需技能检索协议可用
- **动作**：`skill_search("检索协议")` 或任一实际需要的主题
- **判据**：返回结果（如《技能检索协议》）；命中后 `skill_get` 拉全文
- **失败**：空结果 → 确认查询词；仍空 → 记录并继续（不阻断）

## 汇报模板（给用户）

```
✅ SGME 接入自检
- 端点：http://<NAS_IP>:9910（版本 x.y.z）
- ①发现 ✓ ②连通 ✓ ③写入 ✓ ④检索 ✓ ⑤提炼 ✓ ⑥技能 ✓
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

---

## English

**Post-setup self-check — run all six; all green means you're done.** ① Discovery: `GET /v1/health` → 200 with `version` + `capabilities`. ② Auth/identity: call `agent_onboarding`; remote calls use an Agent key only (403 otherwise). ③ Write: `POST /v1/append` with `session_key` + `started_at` + `# {ISO} {role}` first line → `status: new`. ④ Read: `POST /v1/search` retrieves the new entry with provenance. ⑤ Refine: `POST /v1/refine_trigger` (`async_mode=true`) returns a job id. ⑥ Skills: `skill_search("…")` returns hits; load with `skill_get`. Report a short summary (endpoint, version, six checkmarks, gaps). Common issues: 403 auth/origin, 400 params, connection (proxy — set `trust_env=False`), retrieval (write success? vector configured?), refine (missing keys / rate limit).
