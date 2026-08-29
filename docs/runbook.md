# SGME 运维手册 (Runbook)

> 最小闭环 v0.4 · 单用户 Agent 记忆引擎 Server

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11+ |
| 操作系统 | Windows / macOS / Linux |
| 依赖 | FastAPI、uvicorn、pyyaml、httpx、pytest |
| SQLite | 标准库 sqlite3 + FTS5（Python 自带） |
| LLM | OpenAI 兼容 LLM 提供商（本地/云端任选，配置见 `config/providers.yaml`，品牌由用户选） |

## 2. 安装

```bash
# 1. 克隆仓库
git clone <repo-url> SGME
cd SGME

# 2. 创建虚拟环境
python -m venv .venv

# 3. 激活虚拟环境
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 4. 安装依赖（含 dev）
pip install -e ".[dev]"
```

## 3. 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `SGME_HOST` | 监听地址 | `127.0.0.1` |
| `SGME_PORT` | 监听端口 | `9910` |
| `SGME_ADMIN_KEY` | 管理员 API Key | `dev-admin-key-change-me` |
| `SGME_AGENT_KEY` | Agent API Key | `dev-agent-key-change-me` |
| `SGME_BEARER_TOKEN` | Bearer 令牌（传输层鉴权，不设则旁路） | （空，旁路） |
| `<PROVIDER>_API_KEY` | OpenAI 兼容提供商 API Key（示例：`AGNESAI_API_KEY`（agnes 主链免费）/ `SILICONFLOW_API_KEY`（硅基流动免费）/ `ZHIPU_API_KEY`（智谱免费兜底）；env 引用不落盘，见 `config/providers.yaml`） | （空） |
| `SILICONFLOW_API_KEY` | 硅基流动 API Key（向量 embedding，`search.vector.api_key_env` 引用；BAAI/bge-m3 免费，实名认证后零费用） | （空） |
| `SGME_MCP_PORT` | MCP 端口 | `9913` |
| `SGME_MCP_DISABLED` | 设为 `1` 关闭 MCP | （空） |

**连接地址约定（客户端访问 SGME 统一用变量，勿硬编码主机）**：SGME 可能跑在本机或 NAS，主机地址唯一真相是 `~/.sgme/install.json`（`http.host` / `http.port` / `mcp.host` / `mcp.port`）或环境变量 `SGME_HTTP_URL` / `SGME_MCP_URL`。**本文所有示例统一引用以下两个变量**，换机器/迁移只需改一处：

```bash
# bash / curl 示例统一引用（SGME_HTTP_URL 优先，缺省回退本机开发默认）
export SGME_HTTP_BASE="${SGME_HTTP_URL:-http://localhost:9910}"
export SGME_MCP_URL="${SGME_MCP_URL:-http://localhost:9913/mcp}"
```

```python
# python 示例统一引用
import os
SGME_HTTP_BASE = os.environ.get("SGME_HTTP_URL", "http://localhost:9910")  # NAS：export SGME_HTTP_URL=http://192.168.10.10:9910
SGME_MCP_URL = os.environ.get("SGME_MCP_URL", "http://localhost:9913/mcp")
```

**默认开发 Key 仅限本机回环来源**：`dev-agent-key-change-me` / `dev-admin-key-change-me` 未设置环境变量时的内置兜底值，只允许 127.0.0.1 / ::1 / localhost 调用，**非本机来源一律 403**（防仓库公开后默认 Key 被远程滥用）。自定义 Key（环境变量设置，或经 `/v1/admin/agents/register` 签发的 `agt_*`）不受限。

**生产部署务必修改默认 Key：**

```bash
# Windows (PowerShell)
$env:SGME_ADMIN_KEY = "your-strong-admin-key"
$env:SGME_AGENT_KEY = "your-strong-agent-key"
$env:SGME_BEARER_TOKEN = "your-bearer-token"

# macOS / Linux
export SGME_ADMIN_KEY="your-strong-admin-key"
export SGME_AGENT_KEY="your-strong-agent-key"
export SGME_BEARER_TOKEN="your-bearer-token"
```

## 4. 启动

### 4.1 开发模式（mock LLM）

无需外部 LLM，直接启动：

```bash
python -m sgme
```

启动后输出：
```
[SGME config] 加载完成: 维度数=15 别名维度=15 链=['refinement'] 链长={'refinement': 3
INFO:     Uvicorn running on http://127.0.0.1:9910
```

### 4.2 生产模式（接 OpenAI 兼容 LLM）

1. **准备 LLM 提供商**（任选 OpenAI 兼容提供商，连接字段见 `config/providers.yaml`）：
   - 免费链（2026-08-22 用户定；2026-08-29 zhipu 移出链，B121）：Agnes agnes-2.5-flash（主，当前 $0/1M token，`AGNESAI_API_KEY`）→ 硅基流动 DeepSeek-V4-Flash（免费，`SILICONFLOW_API_KEY`）→ rule drop_batch 兜底；申请见 docs/guide/免费模型Key申请指南.md
   - 模型名禁止含 `pro`/`reasoner`/`thinking`，禁止 `gemma-4-12b-qat`

2. **配置降级链**：编辑 `config/llm.yaml`（只写链结构，连接字段由 providers.yaml 注入——provider 名即 providers.yaml 键名）：
   ```yaml
   chains:
     refinement:
       - provider: agnes          # 主链（2026-08-22 用户定）：agnes-2.5-flash 免费，1-4s 快
         model: agnes-2.5-flash
       - provider: siliconflow    # 第二优先：DeepSeek-V4-Flash 免费，1-3s
         model: deepseek-ai/DeepSeek-V4-Flash
       - provider: rule           # 兜底（2026-08-29 zhipu 移出链，B121）
         action: drop_batch
   ```

3. **设置提供商 Key**（按 providers.yaml 的 `api_key_env` 字段，示例）：
   ```bash
   export AGNESAI_API_KEY="..."          # Agnes 主链 key（agnes-2.5-flash 免费，申请见 docs/guide/免费模型Key申请指南.md）
   export SILICONFLOW_API_KEY="..."      # 硅基流动 key（DeepSeek-V4-Flash LLM 第二优先 + 向量 embedding BAAI/bge-m3 免费，见 §12）
   # ZHIPU_API_KEY 已废弃：2026-08-29 zhipu 移出降级链，providers.yaml 已删该段（B121）
   ```

4. **启动 SGME**：
   ```bash
   python -m sgme
   ```

### 4.3 服务化部署（Windows Service 守护）

**背景**：手动拉起的后台进程在电脑重启后失效，需要常驻守护。

**方案**：NSSM 注册 Windows 服务 `SGME`——开机自启（AUTO_START）+ 崩溃自动重启（AppExit Restart + AppRestartDelay 5000 + `sc failure` 三级重启），日志轮转 10MB 到 `tmp/sgme-service.log`。

**安装**（管理员 PowerShell/CMD）：
```bat
scripts\install_sgme_service.bat
```

**验证**：
```bat
sc query SGME        :: STATE: 4 RUNNING + START_TYPE: 2 AUTO_START 为正常
netstat -ano | findstr :9910
```

**卸载**：
```bat
sc stop SGME && sc delete SGME
```

## 5. 验证命令

### 5.1 健康检查

```bash
python -c "import requests; print(requests.get('$SGME_HTTP_BASE/v1/health').json())"
```

预期输出（新建库首次启动）：
```json
{
  "status": "ok",
  "version": "1.0.0b1",
  "llm": {
    "available": true,
    "provider": "<providers.yaml 中的 provider 名>",
    "model": "<该 provider 配置的模型>"
  },
  "refinement": {
    "watermark_age_sec": null,
    "queue_depth": 0,
    "last_refined_at": null,
    "stalled": false,
    "heartbeat_ok": true
  },
  "vector": {
    "available": true,
    "engine": "sqlite-vec",
    "memory_vectors": 0,
    "scene_vectors": 0,
    "connectivity": {
      "available": true,
      "provider": "siliconflow",
      "model": "BAAI/bge-m3",
      "latency_ms": 172,
      "error": null
    }
  },
  "model_config": {
    "missing_keys": [],
    "notice": ""
  }
}
```

### 5.2 写入会话（L0 捕获）

```python
import requests

resp = requests.post(f"{SGME_HTTP_BASE}/v1/append",
    headers={"X-API-Key": "dev-agent-key-change-me"},   # 本机开发默认 key；远程须自定义
    json={
        "session_key": "test-session",
        "started_at": "2026-08-04T10:00:00Z",
        "content": "# 2026-08-04T10:00:00Z user\n测试消息\n# 2026-08-04T10:00:01Z assistant\n你好！\n",
    })
print(resp.json())
```

### 5.3 触发提炼

```python
resp = requests.post(f"{SGME_HTTP_BASE}/v1/admin/refine/trigger",
    headers={"X-API-Key": "dev-admin-key-change-me"},   # 本机开发默认 key
    json={"file_id": "<上一步返回的 file_id>"})
print(resp.json())
```

### 5.4 注入画像

```python
resp = requests.post(f"{SGME_HTTP_BASE}/v1/inject",
    headers={"X-API-Key": "dev-agent-key-change-me"},
    json={"mode": "daily"})
print(resp.json())
```

### 5.5 搜索记忆

```python
resp = requests.post(f"{SGME_HTTP_BASE}/v1/search",
    headers={"X-API-Key": "dev-agent-key-change-me"},
    json={"query": "记忆引擎", "scopes": ["memory"]})
print(resp.json())
```

### 5.6 端到端冒烟（minimal-closure）

```bash
python scripts/e2e_smoke.py
```

预期输出：
```
[1] append ok
[2] health (提炼前): queue_depth=1
[3] refine ok: memories_count=2
[4] inject ok: blocks=3
[5] search ok: results=1, trace 非空
[6] health (提炼后): queue_depth=0, watermark 推进
E2E SMOKE PASSED
```

### 5.7 端到端冒烟（v0.4 完整链路）

```bash
.venv\Scripts\python.exe scripts/e2e_smoke_v04.py \
    --admin-key dev-admin-key-change-me \
    --agent-key dev-agent-key-change-me
```

链路覆盖：
```
append → refine/trigger → L2 场景生成 → tier0/refresh
→ inject（Tier0 摘要）→ search（RRF）→ events/pull（memory_updated）
→ health（可观测性字段）→ backup/create → backup/restore
```

预期输出：
```
[ 1] [OK] Server online status=ok version=1.0.0b1
[ 2] [OK] append file_id=...
[ 3] [OK] refine status=refined memories=2 l15_stored=2
[ 4] [OK] L2 active scenes=1
[ 5] [OK] tier0 refresh ok summary_length=...
[ 6] [OK] inject blocks=3 tier0.present=True
[ 7] [OK] search results=1 routes=['bm25', 'vector', 'rrf']
[ 8] [OK] events=1 memory_updated=1 next_cursor=True
[ 9] [OK] health llm.available=True queue_depth=0 ...
[10] [OK] backup created snapshot_id=full_...
[11] [OK] backup restored snapshot_id=full_...
E2E SMOKE V04 PASSED
```

> **注意**：L2 场景生成、Tier0 摘要、向量检索均依赖 LLM（本地或云端 OpenAI 兼容提供商，见 §4.2）。
> LLM 不可达时自动降级（直存记忆、Tier0 静态直出、纯 BM25 检索），冒烟仍可通过。

## 6. 测试

```bash
# 全量测试（mock LLM，无外部依赖）
pytest tests/ -q

# minimal-closure 单模块测试
pytest tests/test_config.py -q          # T0 配置
pytest tests/test_storage.py -q         # T1 存储
pytest tests/test_llm.py -q             # T2 LLM 降级链
pytest tests/test_raw.py -q             # T3 L0 捕获
pytest tests/test_refine.py -q          # T4 L1 提取
pytest tests/test_l15.py -q             # T5 L1.5 冲突
pytest tests/test_profile.py -q         # T6 模板引擎
pytest tests/test_server.py -q          # T7 HTTP 服务
pytest tests/test_e2e.py -q             # T8 端到端

# v0.4 完整化单模块测试
pytest tests/test_storage_v04.py -q     # T9 storage 扩展
pytest tests/test_l2.py -q              # T10 L2 场景聚合
pytest tests/test_signal.py -q          # T11 信号引擎
pytest tests/test_tier0.py -q           # T12 Tier0 摘要
pytest tests/test_search_v04.py -q      # T13 向量检索 + RRF
pytest tests/test_backup.py -q          # T14 备份恢复
pytest tests/test_health_v04.py -q      # T15 可观测性增强
pytest tests/test_server_v04.py -q      # T16 HTTP 服务集成
pytest tests/test_e2e_v04.py -q         # T17 端到端验收
```

## 7. 数据目录

| 路径 | 说明 |
|---|---|
| `data/memory.db` | 记忆池（memories/archive/tags/sources/registry/memory_vectors/signal_events/signal_subscribers） |
| `data/wiki.db` | 场景库（raw_files 索引 + scenes/scene_memories/scene_versions） |
| `data/tier0_summary.json` | Tier0 LLM 摘要（含 generated_at，48h 过期） |
| `data/agent_keys.json` | 注册 Agent Key 持久化（register 签发记录，重启不丢） |
| `data/backups/` | 备份快照目录（full_/incremental_/monthly_/pre_restore_ 前缀） |
| `raw/sessions/*.md` | L0 原始会话文件 |
| `raw/notes/*.md` | L0 原始笔记文件 |
| `raw/archive/*.zst` | >90 天原始文件冷归档（zstd 只读） |
| `tmp/` | 临时文件 |

**备份**：使用 `POST /v1/admin/backup/create` 端点创建一致快照（SQLite backup API），详见 §14 备份恢复。

## 8. 常见问题

### Q: 启动报 `SQLite objects created in a thread can only be used in that same thread`

A: 已在 `storage/db.py` 设置 `check_same_thread=False`。若仍出现，检查是否使用了外部注入的连接（未走 `_connect`）。

### Q: LLM 提炼失败，记忆未入库

A: 检查降级链配置 `config/llm.yaml`（连接字段见 `config/providers.yaml`）。本地兜底不可达时自动降级云端主链，全挂则 `drop_batch`（该批丢弃，status=error）。refine/trigger 端点在 L1.5 LLM 不可用时降级直存（不丢数据）。

### Q: 中文搜索无结果

A: FTS5 默认 tokenizer 对中文支持有限。`search` 模块已实现 LIKE 兜底：FTS5 无结果时自动降级 LIKE 模糊匹配。

### Q: 端口 9910 被占用

A: 查找并终止旧进程：
```bash
# Windows
netstat -ano | findstr 9910
taskkill /PID <pid> /F

# macOS / Linux
lsof -i :9910
kill -9 <pid>
```

或修改端口：`export SGME_PORT=9912`

### Q: 如何重置数据

A: 删除 `data/` 和 `raw/` 目录后重启，自动重建。

```bash
rm -rf data/ raw/
python -m sgme
```

### Q: L2 场景未生成

A: L2 场景聚合依赖 LLM。检查 `config/providers.yaml` 配置的提供商是否可达（本地示例：LM Studio 是否启动、模型是否加载、端口 1014 是否可达）。LLM 不可达时记忆仍会入库（L2 的 `_ensure_persisted` 兜底直存），但不会生成场景叙事文档。可手动触发：`POST /v1/admin/refine/trigger`。

### Q: Tier0 摘要 present=false

A: 摘要文件 `data/tier0_summary.json` 缺失或超 48h 过期。手动触发：`POST /v1/admin/tier0/refresh`。LLM 不可达时 Tier0 自动降级为静态维度模板查询直出（present=false），不影响注入主链路。

### Q: 向量检索降级为纯 BM25

A: 向量检索依赖 sqlite-vec 扩展 + 向量服务（`config/sgme.yaml` `search.vector`：硅基流动 BAAI/bge-m3，`SILICONFLOW_API_KEY`，1024 维）。任一不可用即降级。查看日志（health 的 `vector.connectivity` 字段显示模型连通性，失效会发 anomaly_warn 信号）：
- `sqlite-vec load_extension 失败` → 走 numpy 余弦降级路径
- `embed: 连接错误` → 向量服务不可达，走纯 BM25

### Q: 恢复备份后数据不一致

A: 恢复前系统会自动再备份当前状态（`pre_restore_` 前缀快照）。如恢复后异常，可用 `pre_restore_` 快照回滚。恢复后自动校验溯源链完整性（memories → sources → raw_files）。

## 8.1 MCP 接口（v1.0.0b1）

SGME Server 同进程提供 MCP 出口（streamable HTTP transport，端口 9913），与 HTTP API（9910）功能等价。

- 端点：`http://<host>:9913/mcp`
- 工具集（29）：append / inject / search / memory_get / memory_reject / refine_trigger / refine_batch / refine_status / stats / health / config_get / config_update / agent_onboarding / wiki_page / wiki_search / wiki_pages / idea_add / demand_create / project_register / role_list / role_assemble / role_active_get / role_active_set / signal_pull / signal_claim / signal_ack / refine_status 等（以 agent_onboarding 返回的 ONBOARDING_TOOLS 为准）
- 连接即发现：接入后先调 `agent_onboarding` 工具获取版本 / 能力清单 / 快速上手指引（self-serve，无需人工配置）
- 用途：SCSM 或其他 Agent 经标准 MCP 协议调用 SGME（跨机部署无需改配置文件，配置经 config_update 远程设置）
- 端口可配：环境变量 `SGME_MCP_PORT`（默认 9913）；`SGME_MCP_DISABLED=1` 可关闭

验证（需在 SGME venv 环境，注意 unset PYTHONPATH）：

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def t():
    async with streamablehttp_client(f"{SGME_MCP_URL}") as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            print([x.name for x in tools.tools])
asyncio.run(t())
```

## 9. API 速查

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/v1/append` | Agent | L0 捕获：写 raw 文件 + 索引 |
| POST | `/v1/inject` | Agent | 画像注入（Tier0 摘要优先 + 模板查询） |
| POST | `/v1/search` | Agent | 记忆检索（BM25 + 向量 + RRF 融合） |
| GET | `/v1/memory/{id}` | Agent | 记忆详情（含 sources + archive_chain） |
| GET | `/v1/health` | Bearer* | 健康检查（含 LLM 可用性 + 提炼心跳 + 停摆标记） |
| GET | `/v1/events/pull` | Agent | 事件拉取（游标补偿，返回 events + next_cursor） |
| GET | `/v1/events/stream` | Agent | 事件 SSE 推送（支持 Last-Event-ID 重连） |
| POST | `/v1/admin/agents/register` | Admin | 签发 Agent Key（`agt_*`，明文仅此一次返回） |
| GET | `/v1/admin/agents` | Admin | 列出已注册 Agent（agent_id/role/scope/status） |
| DELETE | `/v1/admin/agents/{agent_id}` | Admin | 吊销 Agent Key（`default` 不可吊销，改环境变量） |
| GET | `/v1/admin/stats` | Admin | 统计（记忆/原始层/维度分布/水位） |
| POST | `/v1/admin/refine/trigger` | Admin | 手动触发提炼（L1→L1.5→L2 + 信号发布） |
| POST | `/v1/admin/tier0/refresh` | Admin | 手动触发 Tier0 摘要生成 |
| POST | `/v1/admin/backup/create` | Admin | 创建快照（level=full/incremental/monthly） |
| GET | `/v1/admin/backup/list` | Admin | 列出可用快照 |
| POST | `/v1/admin/backup/restore` | Admin | 恢复快照（恢复前自动再备份） |

*`/v1/health` 仅需 Bearer（若开启），不强制 X-API-Key。

## 9.1 鉴权 onboarding：API Key 注册 / 使用 / 吊销

SGME 双角色鉴权：`Agent`（读写记忆）与 `Admin`（管理端点）。Key 三来源：环境变量（`SGME_AGENT_KEY` / `SGME_ADMIN_KEY`）、默认 dev key（仅本机开发）、register 签发。

**签发**（Admin 调用，为每个新 Agent 发独立 Key）：

```python
import requests
admin = {"X-API-Key": "dev-admin-key-change-me"}   # 本机开发默认 key

r = requests.post(f"{SGME_HTTP_BASE}/v1/admin/agents/register",
    headers=admin, json={"agent_id": "my-agent", "scope": ["memory"]})
print(r.json())
# {"agent_id": "my-agent", "api_key": "agt_<uuid>", "role": "agent",
#  "scope": ["memory"], "note": "密钥仅此一次返回，请妥善保存"}
```

**使用**：请求头 `X-API-Key: agt_<uuid>`，与 env 主 key 等价（scope 取并集）；签发记录落盘 `data/agent_keys.json`，重启不丢。

**吊销**（Admin 调用；`default` 即 env 主 key 不可吊销，改环境变量）：

```python
r = requests.delete(f"{SGME_HTTP_BASE}/v1/admin/agents/my-agent", headers=admin)
print(r.json())   # {"status": "ok", "agent_id": "my-agent", "revoked": 1}
```

**默认 dev key 限制**：`dev-agent-key-change-me` / `dev-admin-key-change-me` 仅限本机回环来源（127.0.0.1 / ::1 / localhost），非本机来源 403；远程部署必须换自定义 key（env 或 register 签发，见 §3）。

## 10. L2 场景聚合

L2 场景聚合把 L1.5 裁决后的标签化记忆聚合为叙事文档，存入 `wiki.db` 的 `scenes` 表。

### 10.1 工作原理

- **触发时机**：`refine_file` 完成后自动调用 `l2.aggregate`
- **三动作策略**（优先级从高到低）：
  1. **UPDATE**：新记忆与现有 active 场景主题相关 → 整合进场景正文（heat+1，旧内容归档 `scene_versions`）
  2. **MERGE**：多个 active 场景因新记忆主题重合 → 合并为新场景（heat=sum+1，旧场景 archived）
  3. **CREATE**：新记忆主题无对应场景 → 新建场景（heat=1）
- **热度**：新建=1，更新+1，合并=sum+1
- **软删除**：低价值场景 `status=archived`（不物理删除，保留可溯源）
- **阈值预警**：active 场景数超 `l2.warn_thresholds`（黄 150 / 橙 180 / 红 200）→ 产 `anomaly_warn`（软策略，不阻塞）

### 10.2 配置

`config/sgme.yaml`：
```yaml
l2:
  max_scenes: 200
  warn_thresholds:
    yellow: 150
    orange: 180
    red: 200
```

### 10.3 验证命令

```python
# 触发提炼后查 scenes 表
import sqlite3
conn = sqlite3.connect("data/wiki.db")
conn.row_factory = sqlite3.Row
for r in conn.execute("SELECT scene_id, title, heat, status FROM scenes"):
    print(dict(r))
```

## 11. Tier0 画像摘要

Tier0 是注入画像的第一层（~200 tokens persona 摘要），每日由 LLM 生成。

### 11.1 工作原理

- **生成**：`profile/tier0.py` 的 `generate_summary` 拉取静态维度（identity/family/social/values）高优先级记忆 → LLM 生成摘要
- **存储**：`data/tier0_summary.json`（含 `generated_at` ISO 时间戳 + `summary` 文本）
- **过期检测**：48h 内 `present:true`（摘要有效），超 48h `present:false`（降级静态直出）
- **降级**：LLM 不可达 → 返回 None + 日志告警 + 不阻塞提炼管线；注入时读不到摘要自动降级为静态维度模板查询直出（priority≥70，top 10）
- **定时任务**：`sgme/__main__.py` 启动时注册每日 00:00 cron（asyncio 定时器）
- **手动触发**：`POST /v1/admin/tier0/refresh`

### 11.2 注入行为

`/v1/inject` 的 Tier0 block：
- 摘要有效（48h 内）→ `present:true`，content = LLM 摘要
- 摘要过期/缺失 → `present:false`，content = 静态维度直出

### 11.3 验证命令

```bash
# 手动触发摘要生成
curl -X POST $SGME_HTTP_BASE/v1/admin/tier0/refresh \
    -H "X-API-Key: your-admin-key"

# 查看摘要文件
cat data/tier0_summary.json
```

## 12. 向量检索 + RRF 融合

`/v1/search` 支持 BM25（FTS5）+ 向量检索 + RRF（倒数排名融合）。

### 12.1 工作原理

- **BM25**：SQLite FTS5 全文检索（中文降级 LIKE 模糊匹配）
- **向量检索**：sqlite-vec 扩展余弦相似检索（不可用时走 numpy 内存余弦降级）
- **RRF 融合**：`score = Σ 1/(k + rank + 1)`，k=60 标准常数，BM25 + 向量两路结果归一合并
- **降级链**：sqlite-vec 不可用 → numpy 余弦；embeddings 端点不可达 → 纯 BM25 + LIKE

### 12.2 Embedding 生成

- **模型**：`BAAI/bge-m3`（硅基流动，1024 维；`config/sgme.yaml` `search.vector` 配置；免费模型需实名认证，调用零费用）
- **Key**：`SILICONFLOW_API_KEY` 环境变量（Bearer 头；daemon 服务环境需注入）
- **端点**：硅基流动 `https://api.siliconflow.cn/v1`（OpenAI 兼容 /embeddings）
- **存储**：`memory_vectors(memory_id, embedding BLOB, model, dims, embedded_at)`
- **触发时机**：`refine_file` 完成后自动为新记忆生成 embedding
- **dims 字段**：记录向量维度，模型切换后判断重嵌
- **切换影响**：更换向量模型 → 旧向量（如 nomic 768 维）不兼容，需 `scripts/backfill_vectors.py --force`（记忆）+ `backfill_scene_vectors.py --force`（场景）全量重灌

### 12.3 配置

`config/sgme.yaml`：
```yaml
search:
  vector:
    enabled: true
    model: BAAI/bge-m3
    base_url: https://api.siliconflow.cn/v1
    api_key_env: SILICONFLOW_API_KEY
  rrf:
    k: 60
```

### 12.4 验证命令

```python
import requests
resp = requests.post(f"{SGME_HTTP_BASE}/v1/search",
    headers={"X-API-Key": "dev-agent-key-change-me"},
    json={"query": "记忆引擎", "scopes": ["memory"]})
body = resp.json()
print("routes:", body["meta"]["routes"])  # ['bm25', 'vector', 'rrf'] 或 ['bm25']（降级）
for r in body["results"]:
    print(r["content"][:30], "routes:", r["routes"], "trace:", len(r["trace"]))
```

## 13. 信号引擎

信号引擎在提炼完成与异常发生时发布事件，支持 push（SSE）与 pull（游标）两种交付模式。

### 13.1 事件类型

| 类型 | source | 触发时机 | payload |
|---|---|---|---|
| `memory_updated` | `refine` | 提炼成功 | `{file_id, memories_count}` |
| `memory_updated` | `l2` | L2 场景聚合完成 | `{file_id, created, updated, merged, archived}` |
| `anomaly_warn` | `health` | 提炼停摆/LLM 不可达 | `{stalled, llm_available, ...}` |

### 13.2 事件信封

```json
{
  "event_id": "uuid",
  "type": "memory_updated",
  "source": "refine",
  "payload": {"file_id": "...", "memories_count": 2},
  "ts": "2024-01-01T10:00:00Z"
}
```

### 13.3 拉取模式（pull）

```
GET /v1/events/pull?subscriber_id=<id>&last_signal_id=<cursor>&limit=100
```

返回 `{events: [...], next_cursor: "<event_id>"}`。持久游标 `signal_subscribers` 表记录每订阅者上次消费位置，断线重连可补偿。

### 13.4 推送模式（SSE）

```
GET /v1/events/stream
```

Server-Sent Events 推送，支持 `Last-Event-ID` 头重连补偿。重放窗口 1 小时，超窗口事件合并为 `*_summary` 信号。

### 13.5 抑制语义

SGME 发布侧**不做合并过滤**（§11.1），事件带唯一 id + source 标记。同源同类型 30 分钟内重复抑制由**消费端（SCSM）**执行。事件可附 `suppress_hint` 辅助消费端抑制。

### 13.6 验证命令

```python
import requests
resp = requests.get(f"{SGME_HTTP_BASE}/v1/events/pull",
    headers={"X-API-Key": "dev-agent-key-change-me"},
    params={"subscriber_id": "test-sub", "limit": 50})
body = resp.json()
print(f"events={len(body['events'])} next_cursor={body['next_cursor']}")
for e in body["events"]:
    print(f"  {e['type']} source={e['source']} ts={e['ts']}")
```

## 14. 备份恢复

### 14.1 快照分层

| level | 说明 | 双库 | 原始层 |
|---|---|---|---|
| `full` | 全量快照 | 全量 | 全量 |
| `incremental` | 日增量 | 全量（SQLite 无真增量） | 仅当日新增/变更文件 |
| `monthly` | 月归档 | 全量 | 全量 |

### 14.2 管理端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/admin/backup/create` | 创建快照（参数 `level`） |
| GET | `/v1/admin/backup/list` | 列出可用快照 |
| POST | `/v1/admin/backup/restore` | 恢复快照（参数 `snapshot_id`，恢复前自动再备份） |

### 14.3 备份策略

- **SQLite backup API**：免停机一致快照
- **轮转**：本地保留最近 7 份全量 + 月归档
- **异地副本**：配置 `backup.remote_dir` 时推送副本到远程目录（NAS 挂载 / 异机），失败仅告警不阻塞
- **冷归档**：`archive_raw_cold(days=90)` >90 天原始文件 zstd 压缩为只读冷归档
- **恢复**：恢复前自动再备份当前状态（`pre_restore_` 前缀），恢复后校验溯源链完整性

### 14.4 配置

`config/sgme.yaml`：
```yaml
backup:
  dir: data/backups
  schedule: "04:00"         # 每日自动备份（0.8 方案 B 定时器，本地时区）
  raw_cold_days: 90
  remote_dir: null          # 异地副本目标，null 则跳过
```

### 14.5 验证命令

```python
import requests
admin = {"X-API-Key": "dev-admin-key-change-me"}   # 本机开发默认 key

# 创建全量快照
r = requests.post(f"{SGME_HTTP_BASE}/v1/admin/backup/create",
    headers=admin, json={"level": "full"})
snap_id = r.json()["snapshot_id"]
print(f"created: {snap_id}")

# 列出快照
r = requests.get(f"{SGME_HTTP_BASE}/v1/admin/backup/list", headers=admin)
for s in r.json()["snapshots"]:
    print(f"  {s['snapshot_id']} level={s['level']}")

# 恢复
r = requests.post(f"{SGME_HTTP_BASE}/v1/admin/backup/restore",
    headers=admin, json={"snapshot_id": snap_id})
print(f"restored: {r.json()['restored']['snapshot_id']}")
print(f"pre_restore: {r.json()['pre_restore_snapshot']}")
```

## 15. 可观测性

### 15.1 健康检查端点

`GET /v1/health` 返回：
```json
{
  "status": "ok",
  "version": "1.0.0b1",
  "llm": {
    "available": true,
    "provider": "<providers.yaml 中的 provider 名>",
    "model": "<该 provider 配置的模型>"
  },
  "vector": {
    "available": true,
    "engine": "sqlite-vec",
    "memory_vectors": 12,
    "scene_vectors": 3,
    "reason": null,
    "connectivity": {
      "available": true,
      "provider": "siliconflow",
      "model": "BAAI/bge-m3",
      "latency_ms": 172,
      "error": null
    }
  },
  "refinement": {
    "watermark_age_sec": 120,
    "queue_depth": 0,
    "last_refined_at": "2024-01-01T10:00:00Z",
    "stalled": false,
    "heartbeat_ok": true
  },
  "model_config": {
    "missing_keys": [],
    "notice": ""
  }
}
```

### 15.2 提炼停摆检测

- `check_refinement_stalled`：`refined_at` 游标停滞超 24h → `stalled=true` + 产 `anomaly_warn`
- `check_llm_available`：轻量 ping LLM 端点，不可达 → `llm.available=false`
- `check_vector_availability`：sqlite-vec 扩展 + 向量表行数，不可用 → `vector.available=false`（含 reason）
- `check_heartbeat`：综合心跳（LLM 可用 + 队列深度 + 最近提炼时间 + 停摆标记）
- 心跳定时任务：每 10 分钟一次（`sgme/__main__.py` 注册）

## 16. Docker 部署

单机 / NAS 一键部署（HTTP API + WebUI :9910、MCP :9913）。多阶段镜像自带 WebUI 与 git（skills_hub 依赖），首次启动自动物化默认 `sgme.yaml` 到数据卷。

### 16.1 准备

```bash
# 克隆仓库（任选远程）
git clone https://gitee.com/freehul/sgme.git SGME
cd SGME

# 密钥：复制 .env.example 为 docker.env 并填入真实值
# （ZHIPU_API_KEY / SILICONFLOW_API_KEY / SGME_ADMIN_KEY / SGME_AGENT_KEY，docker.env 已被 gitignore，勿提交）
cp .env.example docker.env
```

### 16.2 启动与验证

```bash
docker compose up -d --build
# 验证
curl $SGME_HTTP_BASE/v1/health
# WebUI（含在镜像内）：打开 $SGME_HTTP_BASE/
# MCP：$SGME_MCP_URL
```

### 16.3 配置（sgme.yaml）

- **首次启动**（空数据卷）自动把默认 `config/sgme.yaml` 物化到数据卷 `config/` 下（entrypoint 复制镜像内模板）
- 默认含生产调优：`l15.prescreen.enabled: true` + `fallback: skip_conflict`（embed 不可达时跳过冲突检测直接 store，防全量召回烧钱）与 `search.vector`（siliconflow BAAI/bge-m3 1024 维免费托底）
- 改配置：编辑数据卷 `config/sgme.yaml` 后重启容器；程序资源 `llm.yaml` / `providers.yaml` 始终读镜像内版本
- 找数据卷：`docker volume ls` / `docker volume inspect <volume>`（compose 卷名形如 `sgme_sgme-data`）

### 16.4 升级

```bash
git pull
docker compose up -d --build
```

**自动更新（ST-34，2026-08-21）**：
- WebUI 检测到新版本（GitHub Releases API，`update_check` 配置段可调 enabled/interval_hours/source）→ 健康卡片显示提示条 → 用户确认「立即更新」→ 写意图文件 `$SGME_HOME/update/request.json`
- 主机侧代理 `scripts/sgme-host-updater.sh`（NAS root cron 每 5 分钟轮询）执行：git pull → docker build 新镜像 → 备份 compose → 换 tag → compose up → 健康验证 → 成功清请求 / 失败自动回滚旧镜像
- 容器无特权（不挂 docker.sock），更新由主机脚本执行；**NAS 自动更新代理已部署（2026-08-22 复验）**：脚本 /vol1/1000/Docker/sgme/scripts/sgme-host-updater.sh + root cron `*/5 * * * *`（→ logs/updater.log），端到端验证通过

### 16.5 NAS（群晖）部署

- 部署真相源：`deploy/nas-docker-compose.yml`（模板，`{{IMAGE_TAG}}` 占位），NAS 生产 compose 非 git 仓库（B64 遗留）
- 流程（B64 纪律）：改项目 git → push（github + gitee 双推）→ NAS 拉取（`/vol1/1000/git/sgme.git` bare 仓接 gitee remote 后 fetch）→ NAS 构建 → 更新 compose → up -d
- 数据卷 bind mount 到共享文件夹（如 `/vol1/1000/Docker/sgme/data` → `/data`），文件站可直接备份
- skills-hub 可选 bind mount：`/vol1/1000/git/skills-hub.git` → `/git/skills-hub.git`（file:// 直访，免 SSH key）
