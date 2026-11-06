# SGME 运维手册（Runbook）— 2.0

> **适用版本**：SGME 2.x（生产实测 2.0.4；版本以 `GET /v1/health` 的 `version` 为准，镜像 tag 仅作部署标识）
> **适用形态**：源码运行（Windows/macOS/Linux）与 Docker / NAS 部署；单用户 Agent 记忆引擎 Server。
> **口径声明**：阶段命名与分层**唯一口径 = `sgme/engine/stage_map.py`**（L1 捕获 / L2 提取 / L3 裁决 / L4 梦境）。本文出现的「L1.5 / L2 场景」均为 1.x 历史名，仅在归档节保留。
> **1.x 历史手册**：见文末附录 A（归档，不再维护；其中端点/路径可能已失效）。
> **数据卫生**：本文一律使用占位符（`<SGME_HTTP_URL>` / `<DATA_DIR>` / `<NAS_ROOT>` / `<YOUR_KEY>` / `<IMAGE_TAG>`），不写真实 IP、主机路径与密钥。

## 0. 快速定位

| 我要做什么 | 去哪里 |
|---|---|
| 搞清 2.0 有哪些层、日志里的 stage 叫什么 | §1 架构与阶段名 |
| 装环境 / 首次启动 | §2 环境与安装 |
| 判断服务是否健康 | §3 启动与健康检查 |
| 改配置（模型/阈值/备份/场景开关） | §4 配置 |
| 日常巡检、手动提炼、查数据 | §5 日常运维 |
| 查某个端点怎么调、参数是什么 | §6 常用端点表 |
| 处理故障（停摆 / WAL / 备份 / 熔断 / 场景冻结） | §7 故障排查 |
| 发版、回滚、灾备 | §8 升级与回滚 |
| 外网暴露与安全判读 | §9 安全运维 |

---

## 1. 架构与阶段名（2.0）

### 1.1 分层（唯一口径）

| 2.0 层 | 名称 | 代码 stage 键 | 状态 | 说明 |
|---|---|---|---|---|
| L1 | 捕获 / 剪枝 | （无 refine_run） | 零 LLM | L0 原始文件写入 + `prune.py` 剪枝 |
| L2 | 提取 | `l1_extraction` | 活跃（旧名技术债） | 会话消息块 → 结构化记忆候选（`l2_extract.py / l2_extraction.py / l2_split.py`） |
| L3 | 裁决 | `l1_conflict` | 活跃（旧名技术债） | 候选池裁决 store/skip/update/merge + 近似查重（`l3_adjudicate.py / l3_rules.py`） |
| L4 | 梦境 | `dream` | 活跃（零 LLM 守护） | TTL 五档处置、意义锚回填、日报、归档 |
| — | 遗留场景聚合 | `l2_scene` | **已冻结**（D-53） | 1.x 场景四表只读；9.6 下线范围 |

> 运维提示：日志里仍可能看到 `L1.5 输出解析失败` 等旧名（stage 键未改，历史 7 万+ 行不动）。**汇报与新文档一律用 L2/L3**，对账以 `refine_runs.stage` 原始键 + `stage_map.stage_label()` 为准。

### 1.2 数据流

```text
Agent 会话 --append--> raw/sessions/*.md（L0）
                          │
                          ▼  refine（L2 提取 → L3 裁决）
                     memory.db（memories / memory_archive / tags / sources /
                                memory_vectors / refine_runs / near_dup_quarantine / cards）
                          │
                          ├─ /v1/inject：模板查询 + core 记忆卡 + 锚点块 + 性格参考块（零 LLM）
                          ├─ /v1/search：FTS5 BM25 + 向量 + RRF（四层：memory / wiki / wiki_pages / skills）
                          └─ /v1/events：memory_updated / anomaly_warn / care_*
```

- **两库 + 文件**：`memory.db`（记忆池 + 场景四表 + 审计）、`session.db`（会话索引 raw_files + 提炼游标）、`wiki.db`（知识库，`wiki.enabled`）、`raw/`（L0 原始 MD）。
- **画像无物化**：注入 = 模板查询结果 + core 卡（`memory_cards`）+ 可选锚点/性格块；Tier0 摘要已于 D-79 整体退役。
- **场景层只读**：`scene_aggregate.enabled=false`（生产默认），四表仅图谱/管理页只读展示；场景写端点随 9.6 下线（见 §7.5 / §6.4）。

### 1.3 模块边界（排障时定位代码用）

`adapters → server/mcp_server → operations → engine → data/profile/llm → config`

- `operations/` 是 HTTP/MCP 共用业务层；`engine/` 是提炼管线；`data/` 是唯一数据库访问层；`config.py` 是配置唯一读写方。
- 扩展模块（wiki / skills / skills_hub / refinery / care / credentials）按 `config/sgme.yaml` 开关挂载，禁用时核心零影响。

---

## 2. 环境与安装

### 2.1 源码运行

| 项 | 要求 |
|---|---|
| Python | **3.12 基准解释器**（项目级 venv，禁止依赖 PATH 上的 python；重建走 `scripts/bootstrap_venv.bat`） |
| 依赖 | `requirements.txt` / `pyproject.toml`（`pip install -e ".[dev]"`） |
| SQLite | 标准库 sqlite3 + FTS5 + sqlite-vec（向量，`/search` 用） |
| LLM | OpenAI 兼容提供商（默认免费链 agnes → siliconflow → rule drop_batch，见 `sgme/resources/config/providers.yaml`） |
| 端口 | HTTP 9910、MCP 9913 |

### 2.2 Docker / NAS

- 单机：仓库根 `docker-compose.yml`（数据卷 `sgme-data:/data`）。
- NAS：模板 `deploy/nas-docker-compose.yml`（`<NAS_ROOT>/Docker/sgme/data` → `/data` 的 bind mount）；NAS 生效 compose 非 git 仓库，改部署必须先改仓库模板再同步。
- 首次启动由 entrypoint 把 `sgme.yaml / llm.yaml / providers.yaml` 物化到数据卷 `config/`（已存在则保留用户改动）；数据卷必须**读写挂载**（见 §4.5）。
- 健康探针（双探针，必须保持单行）：9910 `GET /v1/health`=200 **且** 9913 无 key `POST /mcp`=403 → healthy；任一异常/超时 → unhealthy（防 MCP 线程僵死时容器仍 healthy）。

---

## 3. 启动与健康检查

### 3.1 启动

```bash
# 源码
python -m sgme

# Docker
docker compose up -d

# 验证（统一用变量，勿硬编码主机）
curl -s "$SGME_HTTP_URL/v1/health" | head -c 400
```

### 3.2 `GET /v1/health` 字段（2.0）

| 字段 | 含义 | 判读 |
|---|---|---|
| `status` | 服务状态 | `ok` 为准 |
| `version` | 服务版本 | 与部署镜像 tag 双确认（两者可能不同步） |
| `llm.available` / `provider` / `model` / `error` | 提炼 LLM 可用性 | `false` → L2/L3 降级直存（不丢数据但不出标签） |
| `refinement.queue_depth` | 待提炼文件数（`raw_files.status='new'`） | 长时间不降 → 见 §7.1 |
| `refinement.watermark_age_sec` / `last_refined_at` | 提炼水位 | 停摆判定依据 |
| `refinement.stalled` / `stalled_hours` / `heartbeat_ok` / `state` | 停摆标记 | `stalled=true` → 见 §7.1 |
| `vector.available` / `engine` / `memory_vectors` / `scene_vectors` / `reason` | 向量库状态 | `false` → `/search` 降级纯 BM25（见 §7.8） |
| `vector.connectivity` | embedding 端点连通性（provider/model/latency/error） | 失败会发 `anomaly_warn` |
| `model_config.missing_keys` / `notice` | 缺失的模型 Key | 非空即需补 Key（见 §4.4） |
| `credentials_ok` | 凭据模块主密钥自检（启用时） | `false` → 凭据写路径被拒 |
| `update_available` / `latest_version` | 自动更新检测（ST-34） | 仅提示，不自动执行 |
| `onboarding` | 在线接入文档指针 | 免 Key 可读 |

### 3.3 其他健康入口

- `GET /v1/admin/refine/status`：pending/completed/failed/total + 水位 + 最近一次失败（`last_failure`）。
- `GET /v1/admin/ops/health-overview`：存储体检（DB/WAL/raw/备份字节）+ 场景层下线就绪度（`scenes.ready/blockers`）。
- `GET /v1/admin/stats`：记忆/原始层/维度分布/水位/agents。
- MCP 侧等价工具：`health` / `refine_status` / `stats`。

---

## 4. 配置

### 4.1 配置文件位置与加载顺序

| 层 | 位置 | 说明 |
|---|---|---|
| 内置默认 | `sgme/resources/config/{sgme,llm,providers}.yaml` | 随镜像/仓库发布，是兜底模板，勿在运行中改 |
| 用户配置（可写） | **容器/NAS**：`$SGME_HOME/config/`（`/data/config/`）；**源码运行**：`<project-root>/config/`；**只读安装**：`~/.sgme/config/` | 运维手动编辑处。`sgme.yaml` 是主配置；`llm.yaml`/`providers.yaml` 同名文件也会覆盖包内默认（结构缺 `chains/rules`/`providers` 时自动回退并告警） |
| 环境变量 | `docker.env` / 宿主 env | 只放密钥与部署级开关 |

修改用户配置后**需要重启进程**才生效（配置在启动时合并；`config_update` 对 `null/{}` 值不生效——改文件 + 重启是可靠路径）。容器部署改 `/data/config/sgme.yaml` 后 `docker compose restart sgme` 即可（无需重建镜像）。

### 4.2 关键配置段（2.0）

| 段 | 关键键 | 默认/生产口径 | 用途 |
|---|---|---|---|
| `l2_extraction` | `chunk_size` / `overlap` | 6000 / 1000 | L2 提取分块（改小更稳、改大更省调用） |
| `l3_adjudicate.prescreen` | `enabled` / `fallback` | true / `skip_conflict` | 候选池向量预筛；embed 不可达时跳过冲突检测（防烧钱） |
| `l3_adjudicate.near_dup` | `enabled` / `threshold` / `gray_zone` | true / 0.92 / 0.85 | 落库前近似查重（命中进隔离表，不静默丢；见 §7.7） |
| `refine` | `refine_on_append` / `batch_scan.enabled` / `interval_min` | 生产 true / false / 60 | 写入即提炼、批扫兜底（二者至少开一路） |
| `refine` | `llm_override` | `{provider, model, max_tokens}` | 强制提炼主模型（防 agent 声明模型劫持） |
| `refine` | `max_chunk_failure_ratio` / `chunk_retry_times` / `chunk_retry_backoff_s` | 0.0 / 2 / 5.0 | 块级容忍与重试 |
| `refine` | `max_file_attempts` / `max_file_bytes` | 5 / 10485760 | 失败熔断与体积闸（见 §7.4） |
| `scene_aggregate` | `enabled` / `max_scenes` / `warn_thresholds` | **false** / 300 / 250-275-300 | 场景聚合写入开关（D-53 冻结；回滚见 §7.5） |
| `scene_gc` | `enabled` | **false** | 场景夜间合并（随场景冻结一并停用） |
| `search.vector` | `enabled` / `provider` / `model` / `base_url` / `fallbacks` | true / local / bge-m3 / 本地 OpenAI 兼容端点 | 向量检索主备链（1024 维，切换不重灌） |
| `search.rrf` | `k` | 60 | RRF 融合常数 |
| `backup` | `schedule` / `level` / `keep_full` / `keep_incremental_days` / `remote_dir` / `raw_cold_days` | 04:00 / incremental / 7 / 30 / '' / 90 | 备份策略（见 §7.3） |
| `dream` | `enabled` / `schedule` / `max_files` / `archive_days` / `report_dir` | true / 03:00 / 200 / 90 / data/reports | L4 梦境任务 |
| `ttl.disposition` | `enabled` / `renew_max` / `tier_*_ratio` / `sleep_days` | true / 2 / 0.67 / 0.33 / 30 | TTL 五档处置（关 = 回退 1.x 到期写 expired） |
| `ttl.meaning_anchor` | `enabled` / `refresh_days` | true / 1 | 意义锚回填 |
| `persona` | `enabled` / `gate.*` / `inject_block.enabled` | true / 3-2-2 / true | 性格参考块四条件闸门（见 §7.6） |
| `migration` | `read_mode` / `dual_write.enabled` / `shadow.enabled` | new / true / true | 2.0 迁移四阶段（回滚点：`legacy`） |
| `compat` | `enforce` / `enforce_gone` / `stable_since` / `window_days` | false / false / "" / 90 | 兼容期 409/410 强制开关（见 §6.4） |
| `care` | `enabled` / `persona_max_chars` | true / 2000 | 关怀引擎 |
| `skills` / `skills_hub` | `enabled` / `source_dirs` / `budget` / `*_quota` | true / git 源 / 40 | 技能库与配额 |
| `server` | `rate_limit_per_min` / `ip_rate_limit_per_min` / `auth_fail_threshold` / `cors_origins` | 120 / 60 / 0（观察期） / [] | 限流与安全（见 §9） |
| `logging` | `level` / `format` | INFO / console | 日志 |

> 以 `config/sgme.yaml`（仓库）+ 生产 `<DATA_DIR>/config/sgme.yaml` 实际值为准；上表是口径，不是强制值。

### 4.3 环境变量（密钥与部署开关）

| 变量 | 用途 |
|---|---|
| `SGME_HOME` | 数据根（容器 `/data`） |
| `SGME_HOST` / `SGME_PORT` | 监听地址/端口（容器内必须 0.0.0.0） |
| `SGME_ADMIN_KEY` / `SGME_AGENT_KEY` | 管理/Agent 主 Key（**不落盘**） |
| `SGME_BEARER_TOKEN` | 传输层 Bearer（不设则旁路） |
| `SGME_MCP_PORT` / `SGME_MCP_DISABLED` | MCP 端口/关闭 |
| `SGME_CRED_KEY` | 凭据模块主密钥（Fernet，启用时必需） |
| `<PROVIDER>_API_KEY` | LLM/向量 Key（示例 `AGNESAI_API_KEY` / `SILICONFLOW_API_KEY`），按 `providers.yaml` 的 `api_key_env` 引用 |

**默认开发 Key 仅限本机回环**（127.0.0.1 / ::1 / localhost），远程一律 403；生产必须换自定义 Key 或签发 `agt_*`。

### 4.4 缺 Key 的降级语义

| 缺失 | 后果 | 补救 |
|---|---|---|
| 提炼 LLM Key | L2/L3 降级直存（记忆入库、无标签/无裁决） | 补 Key → 重启 → `health.llm.available=true` |
| embedding Key/端点 | `/search` 降级纯 BM25；L3 预筛走 `fallback` | 补 Key/起本地 ollama（bge-m3）→ `vector.connectivity.available=true` |

### 4.5 只读（ro）挂载注意

- `/data` **必须读写**：`memory.db-wal`、`backups/`、`config/`、`logs/` 都在写。
- 若把 `/data/config` 单独以 `:ro` 挂载：配置只读、`config_update`/WebUI 改配置会失败——此时只能「宿主改文件 + 重启容器」。
- `skills.source_dirs`（git 工作区）若 `:ro` 挂载：技能**读**（get/digest/package/files）正常，**写/sync/reindex 会失败**。
- `raw/` 若需异地只读审计，可另挂 `:ro` 只读副本；不要替换主数据目录。

---

## 5. 日常运维

### 5.1 巡检清单（每日/每周）

```bash
BASE="$SGME_HTTP_URL"; K="<YOUR_ADMIN_KEY>"
# 1) 健康
curl -s -H "X-API-Key: $K" "$BASE/v1/health"
# 2) 提炼进度
curl -s -H "X-API-Key: $K" "$BASE/v1/admin/refine/status"
# 3) 存储体检（含 WAL/备份/场景就绪）
curl -s -H "X-API-Key: $K" "$BASE/v1/admin/ops/health-overview"
# 4) 数据规模
curl -s -H "X-API-Key: $K" "$BASE/v1/admin/stats"
# 5) 注入自检（看 tokens_est / core_present）
curl -s -X POST -H "X-API-Key: $K" -H "Content-Type: application/json" \
     -d '{"mode":"daily"}' "$BASE/v1/inject"
```

### 5.2 手动提炼

```bash
# 异步（推荐；立即返回，后台跑）——适配器收尾也用它；规范路径是 kebab-case
curl -s -X POST -H "X-API-Key: $K" -H "Content-Type: application/json" \
     -d '{}' "$BASE/v1/admin/refine/trigger-async"
# 旧下划线别名 /trigger_async 仍可用，但响应带 Deprecation 头，新代码勿用
# 指定文件
curl -s -X POST ... -d '{"file_id":"<file_id>"}' "$BASE/v1/admin/refine/trigger"
```

### 5.3 只读查库（排障用）

容器内/本机均可，**必须只读打开**（`mode=ro`），避免与运行中的写入争锁：

```bash
# 容器内示例（数据卷 /data）
# raw_files（原始层索引）在 session.db；memories/refine_runs 在 memory.db
docker exec -it sgme python3 -c "
import sqlite3
m = sqlite3.connect('file:/data/data/memory.db?mode=ro', uri=True, timeout=10)
s = sqlite3.connect('file:/data/data/session.db?mode=ro', uri=True, timeout=10)
print('memories by status:', m.execute(\"SELECT status, COUNT(*) FROM memories GROUP BY status\").fetchall())
print('raw_files by status:', s.execute(\"SELECT status, COUNT(*) FROM raw_files GROUP BY status\").fetchall())
"
```

> 不要在服务运行时用 `sqlite3 <db>` 默认读写模式做 `UPDATE/VACUUM`；确需修复先按 §7.3 建快照。

---

## 6. 常用端点表（2.0）

> 完整清单以 `GET /openapi.json` 为准（本表只列运维/接入高频项）。鉴权：Agent=Agent Key 可调；Admin=需 Admin Key。

### 6.1 Agent 面

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/append` | L0 捕获（`session_key`+`started_at`+`content`；同键同时刻幂等） |
| POST | `/v1/inject` | 画像注入（见 §6.2） |
| POST | `/v1/search` | 四层检索（memory / wiki / wiki_pages / skills；BM25+向量+RRF） |
| POST | `/v1/answer` | 聚合问答/时序推理（`answer.enabled`） |
| GET | `/v1/memory/{memory_id}` | 记忆详情（sources + archive_chain + pages） |
| POST | `/v1/memory/{memory_id}/reject` / `unreject` | 记忆纠错（软删，可恢复） |
| GET | `/v1/sessions/{file_id}` | 会话原文（raw 文件内容） |
| GET | `/v1/events/pull` / `/v1/events/stream` | 事件游标拉取 / SSE 长连 |
| GET | `/v1/capabilities` | 2.0 能力广告 + 兼容期清单（见 §6.4） |
| GET | `/v1/skills` / `/v1/skills/search` / `/v1/skills/{name}/digest` / `/v1/skills/{name}` | 技能四级披露（搜索须带 `q`，limit≤20） |
| GET | `/v1/skills/{name}/package` / `/files/{relpath}` | 跨机取完整包 / 单文件（带校验头） |
| POST | `/v1/skills/{name}/materialize` | 同机落盘（`dest_dir` 为服务端路径） |
| GET/POST | `/v1/wiki/search` / `/v1/wiki/pages` | 知识库检索/页面 |

### 6.2 `POST /v1/inject` 专项（预算门控）

请求：

| 字段 | 说明 |
|---|---|
| `mode` | `daily` / `coding` / `work` / `full`；不传回落 `daily`（`stats.note` 注明） |
| `max_tokens` | **注入总预算（可选）**。显式传入且 `tokens_est` 超限时按优先级 **core（永不裁）> section > persona_block** 裁剪：先丢 persona_block，再从最后一个 section 的尾部逐条丢 item，直到 ≤ `max_tokens`。**不传 = 行为完全不变**（向后兼容） |
| `custom_filter` | `{dimensions, match, limit}` 自定义查询（与 mode 二选一） |
| `history` / `already_repaired` | 冷场修复（D-72）：传历史才探测；已消费传 true 防重复 |

响应 `stats` 关键字段：`mode` / `queries` / `tokens_est`（裁剪后重算）/ `core_present` / `persona_present` / `anchor_present` / `read_mode` / `dedupe_dropped`（跨 section 去重条数）；传 `max_tokens` 时另有 `budget_max_tokens` / `budget_tokens_before` / `budget_trimmed` / `persona_dropped` / `dropped_items` / `budget_unmet`（core 自身超预算时的诚实标注）。

> 预算口径：模板 `token_budget` 只校验 section 的 Σlimit×30（`profile/template.py`）；core 卡（约 300+ token）与 persona 块不计入，实际注入可能超模板预算——需要硬上限时显式传 `max_tokens`。

### 6.3 Admin 面（高频）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/admin/refine/status` | 提炼进度 + 水位 + 最近失败 |
| POST | `/v1/admin/refine/trigger` / `trigger-async` / `trigger_async` | 手动提炼：`trigger` 同步（单文件 LLM 全挂返回 503）；`trigger-async` 规范异步；`trigger_async` 旧别名（Deprecation 头） |
| GET | `/v1/admin/refine_runs` | 提炼批次审计（stage/status/tokens/action_counts） |
| GET | `/v1/admin/stats` / `/stats/detail` | 规模与分布 |
| GET | `/v1/admin/memories` / `/v1/admin/memories/usage` | 记忆列表/调用统计 |
| GET | `/v1/admin/sessions` / `/{file_id}` | 原始层列表/单文件 |
| GET | `/v1/admin/review/pending` | 待审队列 |
| POST | `/v1/admin/review/{memory_id}/approve` / `reject` | 单条放行/拒绝 |
| POST | `/v1/admin/review/auto-resolve` | 超期自动消化（默认 7 天） |
| POST | `/v1/admin/review/batch-reject` | **批量拒绝**：`{memory_ids:[...], reason?}` → `{results:[{memory_id,ok,error?}], rejected, failed}` |
| GET | `/v1/admin/near-dup/quarantine` | 近似重复隔离区（`status` = pending / discarded / restored） |
| POST | `/v1/admin/near-dup/{qid}/discard` / `restore` | 丢弃（可留 `reason`）/ 放行补录（`supersede` 默认 true） |
| GET | `/v1/admin/cards` / `schema` / `history` / `{card_id}` / `{card_id}/history` | core 记忆卡读取 |
| POST | `/v1/admin/cards/{card_id}/slots/{slot_key}` | 写槽位（覆盖式，旧值进历史；`value` 可 null 清除） |
| GET | `/v1/admin/ops/health-overview` / `ttl-disposition` / `llm-cost` / `refine-efficiency` | 运维总览 / TTL 五档 / LLM 成本 / **块级提炼效率（D-80：重复率·缓存命中率·补跑积压）** |
| POST | `/v1/admin/backup/create` / `restore`；GET `/backup/list` | 备份（见 §7.3） |
| GET/POST | `/v1/admin/agents` / `register` / `{id}`(DELETE) | Agent Key 签发/吊销 |
| GET/POST | `/v1/admin/security/status` / `events` | 安全审计（见 §9） |
| GET | `/v1/admin/usage` / `skills/usage` / `memories/usage` | 调用/成本统计 |
| GET | `/v1/admin/registry` / `consistency` / `{dim_id}` | 维度注册表/一致性体检 |
| GET | `/v1/wiki/pages`（另有 `/v1/wiki/ingest` 等） | 知识库管理 |

### 6.4 兼容期契约（D-58 / D-64）

- `GET /v1/capabilities` 返回：`schema_version` / `api_compat` / `catalog_version` / `compat`（`phase/days_left/stable_since`）/ `enforcement`（`conflict/gone`）/ `endpoints`（409/410 清单 + upgrade_hint）。
- 客户端声明版本：请求头 `X-SGME-API-Version: 2`。**默认不强制**（`compat.enforce=false`、`enforce_gone=false`）。
- 开启 `compat.enforce=true` 后：未声明 2.0 的旧客户端打到「语义已变写端点」（agents/persona/roles/skills 写/backup/config 写/prompts）→ **409 + upgrade_hint**。
- 场景写端点（`POST /v1/admin/scenes/{id}/status`、`POST /v1/admin/scene-gc/trigger`）：随 9.6 下线；路由移除后为 404，开启 `enforce_gone=true` 时为 **410 Gone**。

---

## 7. 故障排查

### 7.1 提炼停摆（queue_depth 不降 / stalled=true）

**判定**：`GET /v1/health` 看 `refinement.queue_depth`、`watermark_age_sec`、`stalled`、`llm.available`；`GET /v1/admin/refine/status` 看 `last_failure`。

**处置顺序**：
1. `llm.available=false` → 按 §4.4 补 Key/查端点（看 `llm.error`）。
2. 队列有积压但 LLM 正常 → 手动触发：`POST /v1/admin/refine/trigger-async`（body 可传 `{file_id?}`；旧别名 `trigger_async` 已标记 Deprecation）；观察 `refine_runs` 是否新增。
3. 单文件反复失败 → 看 `refine_runs.status='error'` 的 `error` 文案；命中熔断（同文件失败达 `max_file_attempts`）见 §7.4。
4. `batch_scan.enabled=false` 的部署（生产）依赖 `refine_on_append=true`；两者都关 → 新文件永远滞留 `status=new`。
5. 水位长期不动且 LLM 正常 → 检查进程是否多实例/只读数据卷（`/data` 只读会导致落库失败，日志有 PermissionError）。

**验证**：`queue_depth` 下降、`refine_runs` 出现新的 `l1_extraction`/`l1_conflict` 记录、`watermark_age_sec` 回落。

### 7.2 WAL 膨胀（memory.db-wal 远大于 memory.db）

**症状**：`GET /v1/admin/ops/health-overview` 的 `storage.memory_wal_bytes` 异常（经验阈值：> 200MB 或 > memory.db 的 50%；本批次新增 `memory_wal_alert`/`memory_wal_threshold_bytes` 字段随该端点返回）。

**原因**：WAL 模式下 checkpoint 被长时间读事务/大批量写入拖住（批量重建、全量回填、长事务脚本）。

**处置**：
1. 先确认可截断（不丢数据，只是把 WAL 合并回主库）：
   ```bash
   docker exec -it sgme python3 -c "
   import sqlite3
   con = sqlite3.connect('/data/data/memory.db')
   print(con.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
   "
   # 低峰期可 TRUNCATE（会等写锁，短暂阻塞写入）
   ```
2. 返回 `(0, N, N)` = 已全部 checkpoint；`(1, ...)` = 有读者挡住 → 找长连接/长事务。
3. 系统侧无周期 checkpoint，建议：批量提炼/重建脚本结束后手动 PASSIVE 一次；每日巡检看 `memory_wal_alert`。
4. 不要在主库运行时 `VACUUM`；确需瘦身先建快照（§7.3）再停服操作。

**验证**：`memory.db-wal` 回落（可到 0）；`health-overview.storage.memory_wal_bytes` 同步。

### 7.3 备份膨胀与保留策略

**现状口径**（典型部署）：`<DATA_DIR>/backups` 存手工 dump（历史），`<DATA_DIR>/data/backups` 存轮转快照（日增量，单份≈1GB，其中 session.db 占大头）。

**保留配置**（`backup` 段）：
- `keep_full=7`：全量保留 7 份；
- `keep_incremental_days=30`：日增量按天保留 30 天（**约 30×1GB ≈ 30GB**，是磁盘最大头）；
- `schedule=04:00`、`level=incremental`；`remote_dir` 配异地副本（可空）。

**建议**：日增量 30 → **7 天**（立即回收约 20 天×1GB）；保留 1 份/月全量；快照压缩或 session.db 单独周备；`backups` 里的 pre-2.0 手工 dump 确认可恢复后转冷存储。

**操作**：
```bash
# 看体积与数量（只读）
du -sh <DATA_DIR>/backups <DATA_DIR>/data/backups
ls -la --time-style=+%Y-%m-%d <DATA_DIR>/data/backups | tail -30
# 建快照 / 列快照 / 恢复（恢复前会自动再备份）
curl -s -X POST -H "X-API-Key: $K" -H "Content-Type: application/json" -d '{"level":"full"}' "$BASE/v1/admin/backup/create"
curl -s -H "X-API-Key: $K" "$BASE/v1/admin/backup/list"
curl -s -X POST -H "X-API-Key: $K" -H "Content-Type: application/json" -d '{"snapshot_id":"<id>"}' "$BASE/v1/admin/backup/restore"
```

> 恢复资产 = 快照 + `<DATA_DIR>/config/.env`（主密钥不随快照）；灾备重建后的 Agent 重签发见附录 A §A.9.2（流程仍适用）。

### 7.4 失败熔断与体积闸（E3/E4）

- **失败熔断**：同一文件连续失败达 `refine.max_file_attempts`（默认 5）→ `raw_files.status='error'`，batch_scan 不再重扫；成功提交时计数清零。目的是终止「空转数万次」。
- **体积闸**：单文件 > `refine.max_file_bytes`（默认 10MB）→ 直接 `status='error'` + `last_error='file_too_large'`，不进 LLM（不耗额度）。
- **复位/重试**：
  - 推荐按接入契约重新 append 同一 `session_key`（**换新的 `started_at`**）→ 追加并重置 `status=new` 重新提炼；
  - 或维护窗口内手工把 `raw_files.status` 改回 `'new'`（仅限已排除原因后，先建快照）；
  - 超大文件：拆分会话后重导，或临时调大 `max_file_bytes`（改配置 + 重启）。
- **验证**：`refine/status.failed` 不再增长；`refine_runs` 有新记录；`health.queue_depth` 下降。

### 7.5 场景层冻结与回滚

- 2.0 已冻结场景层（D-53）：`scene_aggregate.enabled=false`、`scene_gc.enabled=false`；四表（scenes/scene_vectors/scene_memories/scene_versions）**只读保留**，注入与检索不消费，仅图谱/管理页展示。`refine_runs` 若再次出现 `l2_scene` → 说明有人把开关打开了。
- **写端点**：`POST /v1/admin/scenes/{id}/status`、`POST /v1/admin/scene-gc/trigger` 随 9.6 下线（路由移除 → 404；开 `compat.enforce_gone` → 410）。只保留 `GET /v1/admin/scenes` 只读列表。
- **回滚（仅演示/临时）**：`scene_aggregate.enabled=true` + 重启 → 恢复写入；回滚前先确认磁盘与 LLM 预算（每轮场景聚合约 1 次 LLM）。观察指标：`refine_runs.stage='l2_scene'`、`/v1/admin/ops/health-overview.scenes`。

### 7.6 注入预算与去重

- 预算：模板自带 `token_budget` 只约束 section 的 Σlimit×30；core 卡 + persona 块另计。需要硬上限 → 请求传 `max_tokens`（§6.2），响应 `stats.budget_trimmed=true` 表示发生裁剪；`budget_unmet=true` 表示 core 卡本身就超预算（未硬裁）。锚点/回指/冷场块有各自独立预算，不参与该口径。
- 去重：跨 section 同内容去重（memory_id + 前 50 字前缀 + 全内容归一化三层），条数见 `stats.dedupe_dropped`；**同 section 内的重复不做去重**（会在同一块里出现两次，属已知缺口）。
- `persona_present=false` 常见原因：`persona_traits` 证据是 1.x 字符串（无日期/场景字段），四条件闸门（≥3 会话/≥2 天/≥2 场景/来源可信）无法通过——等新结构化证据积累，不是故障。
- 排障：`POST /v1/inject {"mode":"full","max_tokens":1200}` 看 `tokens_est` 与 `blocks`；`tokens_est` 会随线上提炼增长，比较时带时间戳。

### 7.7 近似重复隔离区与阈值

- 链路：L3 落库前向量近似判定；`score ≥ threshold` → 写 `near_dup_quarantine`（不落 memories）；`gray_zone ≤ score < threshold` → 放行落库并计数/抽样日志；低于 `gray_zone` 放行。
- 巡检：`GET /v1/admin/near-dup/quarantine?status=pending`；统计看 `refine_runs.action_counts`（`near_dup_quarantined` / `near_dup_gray`）。
- 处置：逐条 `discard`（**建议填 reason**，是调阈值的唯一标签来源）或 `restore`（`supersede` 默认 true = 替代旧记忆）。
- 调阈值前提：有 ≥ 一周带 reason 的标注；下调 0.92→0.90 隔离量约 ×1.7，→0.87 约 ×3（按生产灰区分桶外推）。无标注不要凭感觉调。

### 7.8 向量降级（纯 BM25）

- 判定：`health.vector.available=false` 或 `connectivity.available=false`；`reason`/`error` 给出原因（端点不可达 / 模型缺失 / HTTP 错误）。
- 处置：起本地 ollama 并 `ollama pull bge-m3`（1024 维）→ 配 `search.vector.base_url` → 重启 → `health` 复查；云端 fallback（siliconflow BAAI/bge-m3）同 1024 维，切换不需重灌索引。
- 降级期：`/search` 仍可用（BM25），L3 预筛走 `fallback: skip_conflict`（跳过冲突检测，直存）。

### 7.9 端口 / 进程 / MCP 僵死

```bash
# Windows
netstat -ano | findstr :9910
taskkill /PID <pid> /F
# Linux/macOS
lsof -i :9910; kill -9 <pid>
```
- 容器内：宿主侧 `docker top sgme | head`（镜像为 slim，未装 procps，容器内没有 `ps`）；MCP 9913 僵死（TCP 通、不回包）时容器探针会判 unhealthy 并自动重启（双探针设计）。
- 抓栈：容器已加 `SYS_PTRACE`，但**镜像未内置 py-spy**——宿主 `pip install py-spy` 后按 `docker top` 查到的宿主 PID 执行 `py-spy dump --pid <pid>`；或临时 `docker exec` 进容器安装后再抓。

### 7.10 备份恢复 / 灾备

- 同机逻辑恢复：`POST /v1/admin/backup/restore`（自动先备份当前状态，`pre_restore_*`；恢复后校验溯源链 `integrity`）。
- 灾备（新机/数据目录丢失）：恢复快照 + `config/.env` → 逐 agent 重签发 `POST /v1/admin/agents/register` → 各机 `/v1/health` + append/search 回环 → 名册核对 `GET /v1/admin/agents`。
- `data/agent_keys.json` 含明文访问凭据（高敏感），不纳入内部快照链；整目录备份会连带复制，存放面自行评估。

### 7.11 首链熔断（降级保护）与心跳降噪

- **症状**：首链（agnes）长时间不可用时，提炼会**静默降级**到付费备用模型（deepseek）。2026-11-05 实测单日 975 次降级，事后才从 `refine_runs.provider` 看出来。
- **机制**（`llm.yaml` → `rules.primary_guard`，出厂默认关闭，生产已开）：
  - 首链连续失败 `failure_threshold`（默认 3）次 → 熔断 `cooldown_minutes`（默认 30）分钟；
  - 熔断期内**不做任何降级**（也不走 rule 丢弃）：跳过提炼，文件保持 `status=new`、不计 `fail_count`，冷却后自动重试（数据不丢）；
  - 每次熔断**打开**发一条 `anomaly_warn`（`source=llm_primary`，payload 含 provider/连续失败次数/冷却/最后错误）；恢复后首链成功自动解除。
- **观测**：`refine_runs.provider` 按小时分布（正常应全为 agnes；持续 deepseek = 首链异常）；`signal_events` 中 `source='llm_primary'` = 熔断事件，`source='refine_cost'` = 降级告警（每小时至多 1 条）。
- **心跳降噪**：`/v1/health` 与心跳的 LLM 探测改为**连续 `health_fail_threshold`（默认 2）次失败**才判不可用，单次网络抖动不再产 `anomaly_warn`。
- **手动恢复**：不想等冷却 → 重启容器（清空进程内熔断状态）；降级链与守卫配置见 `llm.yaml` / `providers.yaml`。
- **2026-11-06 起生产链去 deepseek（用户定）**：`refinement` 链 = **agnes-2.5-flash → rule**，`refine.llm_override` 固定 agnes；`providers.yaml` 的 deepseek 段已注释，**agent_model 声明 deepseek 的文件不再劫持提炼链**；siliconflow 实测 402（余额不足）也从链上移出。`refine_runs.provider` 只应有 agnes。

---


### 7.12 块级提炼成本异常（重复重发 / 缓存未命中，D-80）

- **症状**：日均提炼 token 异常升高（2.0 上线后实测从 1.x 常态 3.4M 涨到 36.1M），但 `llm-cost` 里每记忆 token 基本没变。
- **根因（2026-11-06 定位）**：`refine_on_append=true` 每次追加都触发 → 增量 = 上次成功提交以来的全部消息 → 零容忍提交闸（`max_chunk_failure_ratio=0`）任一块失败就整份不提交、游标不动 → 下一轮把同一批块原样重发。实测同块平均重发 3.7 次，最坏文件 14.4h 内 23 轮 / 835 次调用。
- **一眼定位**：`GET /v1/admin/ops/refine-efficiency?days=7`
  - `repeat_ratio` ≈ 1.0（≤1.3）= 每块只算一次；**> 1.5 就是还在重复重发**。只统计 `chunk_sha IS NOT NULL` 的批次（上线前历史行没有指纹，算进来会虚高——2.0.5 上线首日曾误报 16.3，2.0.6 已修）。
  - `cache_hit_rate` 10%~40% 属正常（新内容总要先跑一遍）；长期 0 = 缓存被关或 key 不匹配。
  - `replay_pending` 长期不降 = 补跑队列堵住，看 `refine_chunk_failures` 的 `error`。
- **D-80 三开关**（`sgme.yaml` → `refine` 段，均可独立关闭回旧行为）：

| 键 | 默认 | 作用 |
|---|---|---|
| `chunk_cache_enabled` | true | 同 (prompt 全文 + 模型链指纹) 的块直接复用，重跑零 token |
| `chunk_replay_enabled` | true | 成功块照常提交+推游标，失败块入 `refine_chunk_failures` 逐块补跑 |
| `append_debounce_s` / `append_min_new_messages` | 300 / 20 | 窗口内同文件只触发一次；小增量交给 batch_scan |

- **界面入口（2.0.7 起）**：健康总览 → 提炼状态下方「块级成本」卡片（重复率 / 命中率 / 缓存省下 token / 补跑积压）；最近批次表新增「缓存 / 块指纹」两列；设置 → 提炼 → 「块级成本治理（D-80）」可直接改三开关与两阈值（保存即热生效）。
- **设置页保存曾经是哑的（2026-11-06 修复）**：`ui/src/api/admin.ts` 的 `updateConfig` 此前 PUT 到 `/v1/admin/config/{section}`，而该路径只有 GET（实测 OpenAPI methods=['get']）→ 405，所有「保存」开关静默失败。2.0.7 起改为 `POST /v1/admin/config`（`{section, values}`）。
- **手动干预**：
  - 队列排障：`SELECT file_id, chunk_sha, attempts, last_error FROM refine_chunk_failures ORDER BY last_at DESC LIMIT 20;`
  - 想立刻补跑：等下一轮 `batch_scan`（每 `interval_min` 分钟）或重启容器触发启动扫描。
  - 想回旧行为：`chunk_replay_enabled=false` + `max_chunk_failure_ratio=0.0`（接口改键已进白名单，不再需要手改文件）。
- **⚠️ 缓存是加速器不是档案**：Dream 按 `chunk_cache_ttl_days`（30 天）+ `chunk_cache_max_rows`（5 万行）淘汰；换模型/换提示词会自然失效（key 含模型指纹与 prompt 全文）。
- **2026-11-06 前的历史坑（仍值得记住）**：`max_chunk_failure_ratio` / `chunk_retry_times` / `chunk_retry_backoff_s` 当时不在 `POST /v1/admin/config` 白名单，改接口会被**静默丢弃**，只能手改 `/data/config/sgme.yaml` 再重启。D-80 已把它们连同 8 个新键一起补进 `SECTION_KEYS`。

---

## 8. 升级与回滚

### 8.1 手动升级（源码/NAS）

```bash
# 1) 备份配置与数据（升级前必做）
cp <DATA_DIR>/config/sgme.yaml <DATA_DIR>/backups/config-$(date +%Y%m%d%H%M).yaml
# 2) 取代码并构建新镜像（NAS 侧）
git pull
docker build -t sgme:<IMAGE_TAG> .
# 3) 备份当前 compose，再换 tag
cp <COMPOSE_DIR>/docker-compose.yml <COMPOSE_DIR>/docker-compose.yml.bak-$(date +%Y%m%d%H%M%S)
sed -i 's|image:.*sgme:.*|image: sgme:<IMAGE_TAG>|' <COMPOSE_DIR>/docker-compose.yml
# 4) 重建并验证
docker compose up -d
curl -s "$SGME_HTTP_URL/v1/health"
```

> **发布前 UI 检查（2026-11-06 起）**：改了 `ui/` 必须过 `cd ui && npm run typecheck`（vue-tsc）+ `npm run build`——Docker 只跑 build（esbuild 剥类型、不检查类型），CI 的 `ui-typecheck` job 才是类型门禁。历史上仓库因此长期挂着 5 个 vue-tsc 红（d3 无类型声明等），2.0.8 已清零。

### 8.2 自动更新（ST-34）

- WebUI 检测到新版本 → 写意图文件 `<DATA_DIR>/update/request.json`（必须含 `"status":"pending"`）。
- 宿主 cron（每 5 分钟）跑 `scripts/sgme-host-updater.sh`：git pull → build → 备份 compose → 换 tag → `compose up -d` → 健康验证；失败自动回滚旧 tag。
- **只保留一处 cron 挂载**（LEO 用户 crontab 与 root 侧触发源重复会导致双实例并发、git ref 锁冲突）——详见附录 A §A.16.4（仍适用）。

### 8.3 回滚

1. 找备份：`<COMPOSE_DIR>/docker-compose.yml.bak-autoupd-*` 或 `*.bak-rollback-*`；
2. 换回旧 tag（`sed` 同上）→ `docker compose up -d`；
3. `/v1/health` 验证版本与 llm/vector；
4. 若新版本已迁移库（2.0 只增字段，旧镜像可读新库），一般无需回滚数据；**跨大版本回滚前**先建 full 快照并 checkpoint WAL。

### 8.4 升级前检查清单

- [ ] `GET /v1/health` 全绿、`queue_depth=0`
- [ ] `WAL` 已 PASSIVE checkpoint（§7.2）
- [ ] 已建 full 快照（§7.3）
- [ ] 配置已备份（§8.1 第 1 步）
- [ ] 磁盘余量 ≥ 新镜像 + 1 份快照
- [ ] `GET /v1/capabilities` 记录 `schema_version`（回滚对账用）

---

## 9. 安全运维（ST-55，摘要）

完整判读清单与通道对照表见附录 A §A.17（仍适用）。2.0 要点：

- 入口：`GET /v1/admin/security/status`（计数概览 + 活封禁）与 `GET /v1/admin/security/events?date=YYYY-MM-DD`（审计）。成功请求不落审计——**要产生判读信号必须发会失败的请求**（无 key/错 key）。
- 默认封禁关闭（`auth_fail_threshold=0`，观察期只计数不封）；转正前必须实测来源区分度（容器桥接可能源坍缩）。
- 重启 = 解封：SecurityState 是进程内存态。
- 探针噪音：9910 health 走绕过；9913 无 key 403 计入失败但回环只计数不封禁。
- 默认 dev key 与 `/v1/admin/keys` 均仅限本机回环；CORS 仅放行回环 + `server.cors_origins` 白名单。

---

## 附录 A：1.x 历史手册（归档，不再维护）

> **归档说明（2026-11-05）**：以下为 v0.4–1.x 时代的运维手册原文，保留用于追溯历史操作与排障知识（如 MCP 僵死探针、自动更新双实例、凭据灾备重签发等仍有效）。
> **其中标注「L1.5 / L2 场景 / Tier0」的管线描述、`config/providers.yaml` 根路径、`tests/test_refine.py` / `tests/test_l15.py`、`POST /v1/admin/tier0/refresh` 等均已失效**——现行口径以本文 §1–§9 与 `GET /openapi.json` 为准。归档文本中的主机路径已用占位符替换。

## A.1. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11+ |
| 操作系统 | Windows / macOS / Linux |
| 依赖 | FastAPI、uvicorn、pyyaml、httpx、pytest |
| SQLite | 标准库 sqlite3 + FTS5（Python 自带） |
| LLM | OpenAI 兼容 LLM 提供商（本地/云端任选，配置见 `config/providers.yaml`，品牌由用户选） |

## A.2. 安装

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

## A.3. 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `SGME_HOST` | 监听地址 | `127.0.0.1` |
| `SGME_PORT` | 监听端口 | `9910` |
| `SGME_ADMIN_KEY` | 管理员 API Key | `dev-admin-key-change-me` |
| `SGME_AGENT_KEY` | Agent API Key | `dev-agent-key-change-me` |
| `SGME_CRED_KEY` | 凭据模块主密钥（Fernet，44 字符 base64；只引用变量名、不落盘）——**可选**，仅启用凭据模块时设置；缺失 / 非法 / 非空库探测失败 → 拒绝写路径（绝不自动生成） | （空） |
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
SGME_HTTP_BASE = os.environ.get("SGME_HTTP_URL", "http://localhost:9910")  # NAS：export SGME_HTTP_URL=http://<NAS_IP>:9910
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

## A.4. 启动

### A.4.1 开发模式（mock LLM）

无需外部 LLM，直接启动：

```bash
python -m sgme
```

启动后输出：
```
[SGME config] 加载完成: 维度数=15 别名维度=15 链=['refinement'] 链长={'refinement': 3
INFO:     Uvicorn running on http://127.0.0.1:9910
```

### A.4.2 生产模式（接 OpenAI 兼容 LLM）

1. **准备 LLM 提供商**（任选 OpenAI 兼容提供商，连接字段见 `config/providers.yaml`）：
   - 免费链（2026-08-22 用户定；2026-08-29 zhipu 移出链 B121；2026-09-01 B144 DeepSeek-V4-Flash 转付费移出）：Agnes agnes-2.5-flash（主，当前 $0/1M token，`AGNESAI_API_KEY`）→ 硅基流动 THUDM/GLM-4-9B-0414（免费档，`SILICONFLOW_API_KEY`）→ rule drop_batch 兜底；申请见 AI-INSTALL/免费模型Key申请指南.md
   - 模型名禁止含 `pro`/`reasoner`/`thinking`，禁止 `gemma-4-12b-qat`

2. **配置降级链**：编辑 `config/llm.yaml`（只写链结构，连接字段由 providers.yaml 注入——provider 名即 providers.yaml 键名）：
   ```yaml
   chains:
     refinement:
       - provider: agnes          # 主链（2026-08-22 用户定）：agnes-2.5-flash 免费，1-4s 快
         model: agnes-2.5-flash
       - provider: siliconflow    # 第二优先：GLM-4-9B-0414 免费档（B144：V4-Flash 转付费移出）
         model: THUDM/GLM-4-9B-0414
       - provider: rule           # 兜底（2026-08-29 zhipu 移出链，B121）
         action: drop_batch
   ```

3. **设置提供商 Key**（按 providers.yaml 的 `api_key_env` 字段，示例）：
   ```bash
   export AGNESAI_API_KEY="..."          # Agnes 主链 key（agnes-2.5-flash 免费，申请见 AI-INSTALL/免费模型Key申请指南.md）
   export SILICONFLOW_API_KEY="..."      # 硅基流动 key（GLM-4-9B-0414 LLM 第二优先 + 向量 embedding BAAI/bge-m3 免费，见 §12）
   # ZHIPU_API_KEY 已废弃：2026-08-29 zhipu 移出降级链，providers.yaml 已删该段（B121）
   ```

4. **启动 SGME**：
   ```bash
   python -m sgme
   ```

### A.4.3 服务化部署（Windows Service 守护）

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

## A.5. 验证命令

### A.5.1 健康检查

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

### A.5.2 写入会话（L0 捕获）

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

### A.5.3 触发提炼

```python
resp = requests.post(f"{SGME_HTTP_BASE}/v1/admin/refine/trigger",
    headers={"X-API-Key": "dev-admin-key-change-me"},   # 本机开发默认 key
    json={"file_id": "<上一步返回的 file_id>"})
print(resp.json())
```

### A.5.4 注入画像

```python
resp = requests.post(f"{SGME_HTTP_BASE}/v1/inject",
    headers={"X-API-Key": "dev-agent-key-change-me"},
    json={"mode": "daily"})
print(resp.json())
```

### A.5.5 搜索记忆

```python
resp = requests.post(f"{SGME_HTTP_BASE}/v1/search",
    headers={"X-API-Key": "dev-agent-key-change-me"},
    json={"query": "记忆引擎", "scopes": ["memory"]})
print(resp.json())
```

### A.5.6 端到端冒烟（minimal-closure）

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

### A.5.7 端到端冒烟（v0.4 完整链路）

```bash
.venv\Scripts\python.exe scripts/e2e_smoke_v04.py \
    --admin-key dev-admin-key-change-me \
    --agent-key dev-agent-key-change-me
```

链路覆盖：
```
append → refine/trigger → L2 场景生成（2.0 起默认冻结，count=0 属预期）
→ inject（core 记忆卡块）→ search（RRF）→ events/pull（memory_updated）
→ health（可观测性字段）→ backup/create → backup/restore
```

预期输出：
```
[ 1] [OK] Server online status=ok version=1.0.0b1
[ 2] [OK] append file_id=...
[ 3] [OK] refine status=refined memories=2 l15_stored=2
[ 4] [OK] L2 active scenes=1
[ 5] [OK] inject blocks=3 core_present=True
[ 6] [OK] search results=1 routes=['bm25', 'vector', 'rrf']
[ 7] [OK] events=1 memory_updated=1 next_cursor=True
[ 8] [OK] health llm.available=True queue_depth=0 ...
[ 9] [OK] backup created snapshot_id=full_...
[10] [OK] backup restored snapshot_id=full_...
E2E SMOKE V04 PASSED
```

> **注意**：L2 场景生成与向量检索依赖 LLM（本地或云端 OpenAI 兼容提供商，见 §4.2）；2.0 A1 起场景层默认冻结（`scene_aggregate.enabled=false`），scenes=0 属预期。Tier0 摘要已于 D-79 整体砍除。
> LLM 不可达时自动降级（直存记忆、纯 BM25 检索），冒烟仍可通过。


### A.5.8 技能包跨机获取（skill_fetch）

```bash
# 下载技能完整包（tar.gz + MANIFEST.sha256 校验，落盘 <dest>/<name>/）
python scripts/skill_fetch.py -n adapter-dsh -d ./skills

# 单文件读（X-File-Sha256 校验）
curl -H "X-API-Key: $SGME_AGENT_KEY" "$SGME_HTTP_URL/v1/skills/adapter-dsh/files/install.py" -o install.py

# 消费遥测（layer=package；管理员 Key）
curl -H "X-API-Key: $SGME_ADMIN_KEY" "$SGME_HTTP_URL/v1/admin/skills/usage?days=7&layer=package"
```

> 配额键 `skills.package_quota.*`（单文件/整包/文件数上限）；同机场景仍用 skill_materialize（落盘在服务端）。
## A.6. 测试

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
pytest tests/test_search_v04.py -q      # T13 向量检索 + RRF
pytest tests/test_backup.py -q          # T14 备份恢复
pytest tests/test_health_v04.py -q      # T15 可观测性增强
pytest tests/test_server_v04.py -q      # T16 HTTP 服务集成
pytest tests/test_e2e_v04.py -q         # T17 端到端验收
```

## A.7. 数据目录

| 路径 | 说明 |
|---|---|
| `data/memory.db` | 记忆池（memories/archive/tags/sources/registry/memory_vectors/signal_events/signal_subscribers） |
| `data/wiki.db` | 场景库（raw_files 索引 + scenes/scene_memories/scene_versions） |
| `data/credentials.db` | 凭据模块独立库（`credentials.enabled=true` 时创建；Fernet 密文 + 审计；主密钥不随快照 / 备份携带） |
| `data/agent_keys.json` | 注册 Agent Key 持久化（register 签发记录，重启不丢）；**敏感等级：高**——明文访问凭据存底，不纳入 SGME 内部快照链，整目录备份会连带复制（存放面自行评估，见 §9.2） |
| `data/backups/` | 备份快照目录（full_/incremental_/monthly_/pre_restore_ 前缀） |
| `raw/sessions/*.md` | L0 原始会话文件 |
| `raw/notes/*.md` | L0 原始笔记文件 |
| `raw/archive/*.zst` | >90 天原始文件冷归档（zstd 只读） |
| `tmp/` | 临时文件 |

**备份**：使用 `POST /v1/admin/backup/create` 端点创建一致快照（SQLite backup API），详见 §14 备份恢复。

## A.8. 常见问题

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

### Q: 注入里看不到「画像摘要」块

A: Tier0 画像摘要已随 D-79（2026-11-05）整体砍除，画像改由 core 记忆卡承担（`profile/core_cards.py` + memory_cards 表）。`/v1/inject` 不再返回 `tier0` 字段；core 卡片是否生效看 `stats.core_present`。端点 `POST /v1/admin/tier0/refresh` 与 `profile/tier0.py` 均已删除。

### Q: 向量检索降级为纯 BM25

A: 向量检索依赖 sqlite-vec 扩展 + 向量服务（`config/sgme.yaml` `search.vector`：硅基流动 BAAI/bge-m3，`SILICONFLOW_API_KEY`，1024 维）。任一不可用即降级。查看日志（health 的 `vector.connectivity` 字段显示模型连通性，失效会发 anomaly_warn 信号）：
- `sqlite-vec load_extension 失败` → 走 numpy 余弦降级路径
- `embed: 连接错误` → 向量服务不可达，走纯 BM25

### Q: 恢复备份后数据不一致

A: 恢复前系统会自动再备份当前状态（`pre_restore_` 前缀快照）。如恢复后异常，可用 `pre_restore_` 快照回滚。恢复后自动校验溯源链完整性（memories → sources → raw_files），校验结果随恢复响应返回（`integrity: {ok, broken_count, broken_samples}`）。

恢复的 `raw/` 语义按快照级别区分（2026-09-24 F-7）：**增量快照 → 合并复制**（只回铺快照内文件，不删快照外文件）；**full / monthly → 覆盖**（可精确回滚）。响应中 `raw_restore.mode` 标出本次模式（merge / overwrite / skipped）。

### Q: 浏览器跨源调用报 CORS 错误（局域网 HTML 工具连不上）

A: CORS 默认只放行本机回环来源（`localhost` / `127.0.0.1` / `[::1]` 任意端口）。其它设备上的页面（如局域网 HTML 工具）需在 `config/sgme.yaml` 显式登记来源：

```yaml
server:
  cors_origins:
    - http://10.0.0.5:8080   # 你的工具页面来源（协议+主机+端口，不带路径）
```

（2026-09-24 F-1 安全收敛：原 `allow_origins=["*"]` 配合无鉴权的 `/v1/admin/keys` 使任意网页可跨源读取密钥，已改为「本机回环默认放行 + 白名单显式登记」。）

## A.8.1 MCP 接口（v1.0.0b1）

SGME Server 同进程提供 MCP 出口（streamable HTTP transport，端口 9913），与 HTTP API（9910）功能等价。

- 端点：`http://<host>:9913/mcp`
- 工具集（43；权威清单以 `agent_onboarding` 返回的 ONBOARDING_TOOLS 为准，本行不再硬编码数量防漂移）：append / inject / search / memory_get / memory_reject / refine_trigger / refine_batch / refine_status / stats / health / config_get / config_update / agent_onboarding / wiki_page / wiki_search / wiki_pages / idea_add / demand_create / project_register / role_list / role_assemble / role_active_get / role_active_set / signal_pull / signal_claim / signal_ack / refine_status 等（以 agent_onboarding 返回的 ONBOARDING_TOOLS 为准）
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

## A.9. API 速查

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/v1/append` | Agent | L0 捕获：写 raw 文件 + 索引 |
| POST | `/v1/inject` | Agent | 画像注入（core 记忆卡 + 模板查询；Tier0 已砍） |
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
| POST | `/v1/admin/backup/create` | Admin | 创建快照（level=full/incremental/monthly） |
| GET | `/v1/admin/backup/list` | Admin | 列出可用快照 |
| POST | `/v1/admin/backup/restore` | Admin | 恢复快照（恢复前自动再备份） |
| GET | `/v1/credentials` | Agent | 凭据列表（键名+元数据，不含值；EP-11，模块启用时） |
| GET | `/v1/credentials/export` | Agent | 导出 dotenv 子集（原始值） |
| GET | `/v1/credentials/{key}` | Agent | 取凭据单值 |
| POST | `/v1/credentials` | Admin | 写入 / 更新凭据（已删键名 → 409） |
| DELETE | `/v1/credentials/{key}` | Admin | 软删（幂等） |
| POST | `/v1/credentials/{key}/restore` | Admin | 恢复（幂等） |

*`/v1/health` 仅需 Bearer（若开启），不强制 X-API-Key。

## A.9.1 鉴权 onboarding：API Key 注册 / 使用 / 吊销

SGME 双角色鉴权：`Agent`（读写记忆）与 `Admin`（管理端点）。Key 三来源：环境变量（`SGME_AGENT_KEY` / `SGME_ADMIN_KEY`）、默认 dev key（仅本机开发）、register 签发。

**签发**（Admin 调用，为每个新 Agent 发独立 Key）——AI-INSTALL 接入流程中此为规定动作（任务卡 ②）：接入的 Agent 须为自身签发专属 Key；admin key 随接入同步至接入侧环境文件（仅环境文件，不进对话/文档）。

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

## A.9.2 agent 重签发流程（灾备重建）

**恢复资产 = 快照 + `config/.env` 两件套**（密钥与密文分家）。「重签发」只针对**灾备重建**（新机 / 数据目录丢失）——**同机逻辑恢复**（`/v1/admin/backup/restore` 只写库 + raw）**不触发**重签发。

1. 恢复数据（快照 + raw）与 `config/.env`；
2. 逐 agent 重新签发：`POST /v1/admin/agents/register`（`agt_*` 明文**仅返回一次**——捕获后立即更新各机 env / 接入文件，不进对话、不进文档）；
3. 冒烟验证：各机 `GET /v1/health` + 一次 `append` / `search` 回环；
4. 名册核对：`GET /v1/admin/agents` 的 `key_ref`（脱敏版）对照「agent_id × 机器 × key 引用位置」清单。

> **agent_keys.json（敏感等级：高）**：明文访问凭据存底——不纳入 SGME 内部快照链（与「主密钥不随快照」同纪律）；NAS 侧整目录 rsync / 文件站备份会**连带复制**该文件，「不纳入」仅覆盖 SGME 内部快照链，存放面需自行评估（见 §7 数据目录表注记）。

## A.10. L2 场景聚合

L2 场景聚合把 L1.5 裁决后的标签化记忆聚合为叙事文档，存入 `wiki.db` 的 `scenes` 表。

### A.10.1 工作原理

- **触发时机**：`refine_file` 完成后自动调用 `l2.aggregate`
- **三动作策略**（优先级从高到低）：
  1. **UPDATE**：新记忆与现有 active 场景主题相关 → 整合进场景正文（heat+1，旧内容归档 `scene_versions`）
  2. **MERGE**：多个 active 场景因新记忆主题重合 → 合并为新场景（heat=sum+1，旧场景 archived）
  3. **CREATE**：新记忆主题无对应场景 → 新建场景（heat=1）
- **热度**：新建=1，更新+1，合并=sum+1
- **软删除**：低价值场景 `status=archived`（不物理删除，保留可溯源）
- **阈值预警**：active 场景数超 `l2.warn_thresholds`（黄 150 / 橙 180 / 红 200）→ 产 `anomaly_warn`（软策略，不阻塞）

### A.10.2 配置

`config/sgme.yaml`：
```yaml
l2:
  max_scenes: 200
  warn_thresholds:
    yellow: 150
    orange: 180
    red: 200
```

### A.10.3 验证命令

```python
# 触发提炼后查 scenes 表
import sqlite3
conn = sqlite3.connect("data/wiki.db")
conn.row_factory = sqlite3.Row
for r in conn.execute("SELECT scene_id, title, heat, status FROM scenes"):
    print(dict(r))
```

## A.11. Tier0 画像摘要（已砍除）

> D-79（2026-11-05）：Tier0 画像摘要整体退役——模块 `sgme/profile/tier0.py`、端点 `POST /v1/admin/tier0/refresh`、每日定时任务、提示词版本与 `data/tier0_summary.json` 均已删除。
> 画像改由 core 记忆卡承担（`sgme/profile/core_cards.py` + memory_cards 表）；`/v1/inject` 不再返回 `tier0` 字段，是否生效看 `stats.core_present`。

## A.12. 向量检索 + RRF 融合

`/v1/search` 支持 BM25（FTS5）+ 向量检索 + RRF（倒数排名融合）。

### A.12.1 工作原理

- **BM25**：SQLite FTS5 全文检索（中文降级 LIKE 模糊匹配）
- **向量检索**：sqlite-vec 扩展余弦相似检索（不可用时走 numpy 内存余弦降级）
- **RRF 融合**：`score = Σ 1/(k + rank + 1)`，k=60 标准常数，BM25 + 向量两路结果归一合并
- **降级链**：sqlite-vec 不可用 → numpy 余弦；embeddings 端点不可达 → 纯 BM25 + LIKE

### A.12.2 Embedding 生成

- **模型**：`BAAI/bge-m3`（硅基流动，1024 维；`config/sgme.yaml` `search.vector` 配置；免费模型需实名认证，调用零费用）
- **Key**：`SILICONFLOW_API_KEY` 环境变量（Bearer 头；daemon 服务环境需注入）
- **端点**：硅基流动 `https://api.siliconflow.cn/v1`（OpenAI 兼容 /embeddings）
- **存储**：`memory_vectors(memory_id, embedding BLOB, model, dims, embedded_at)`
- **触发时机**：`refine_file` 完成后自动为新记忆生成 embedding
- **dims 字段**：记录向量维度，模型切换后判断重嵌
- **切换影响**：更换向量模型 → 旧向量（如 nomic 768 维）不兼容，需 `scripts/backfill_vectors.py --force`（记忆）+ `backfill_scene_vectors.py --force`（场景）全量重灌

### A.12.3 配置

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

### A.12.4 验证命令

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

## A.13. 信号引擎

信号引擎在提炼完成与异常发生时发布事件，支持 push（SSE）与 pull（游标）两种交付模式。

### A.13.1 事件类型

| 类型 | source | 触发时机 | payload |
|---|---|---|---|
| `memory_updated` | `refine` | 提炼成功 | `{file_id, memories_count}` |
| `memory_updated` | `l2` | L2 场景聚合完成 | `{file_id, created, updated, merged, archived}` |
| `anomaly_warn` | `health` | 提炼停摆/LLM 不可达 | `{stalled, llm_available, ...}` |

### A.13.2 事件信封

```json
{
  "event_id": "uuid",
  "type": "memory_updated",
  "source": "refine",
  "payload": {"file_id": "...", "memories_count": 2},
  "ts": "2024-01-01T10:00:00Z"
}
```

### A.13.3 拉取模式（pull）

```
GET /v1/events/pull?subscriber_id=<id>&last_signal_id=<cursor>&limit=100
```

返回 `{events: [...], next_cursor: "<event_id>"}`。持久游标 `signal_subscribers` 表记录每订阅者上次消费位置，断线重连可补偿。

### A.13.4 推送模式（SSE）

```
GET /v1/events/stream
```

Server-Sent Events 推送，支持 `Last-Event-ID` 头重连补偿。重放窗口 1 小时，超窗口事件合并为 `*_summary` 信号。

### A.13.5 抑制语义

SGME 发布侧**不做合并过滤**（§11.1），事件带唯一 id + source 标记。同源同类型 30 分钟内重复抑制由**消费端（SCSM）**执行。事件可附 `suppress_hint` 辅助消费端抑制。

### A.13.6 验证命令

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

## A.14. 备份恢复

### A.14.1 快照分层

| level | 说明 | 数据库 | 原始层 |
|---|---|---|---|
| `full` | 全量快照 | 全量 | 全量 |
| `incremental` | 日增量 | 全量（SQLite 无真增量） | 仅当日新增/变更文件 |
| `monthly` | 月归档 | 全量 | 全量 |

> 数据库范围：memory / session / wiki（启用时）/ credentials（启用时——按数据目录文件存在性纳入，不存在则跳过、绝不建空库）；**恢复资产 = 快照 + `config/.env`**（主密钥不随快照，见 §14.3 / §9.2）。

### A.14.2 管理端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/admin/backup/create` | 创建快照（参数 `level`） |
| GET | `/v1/admin/backup/list` | 列出可用快照 |
| POST | `/v1/admin/backup/restore` | 恢复快照（参数 `snapshot_id`，恢复前自动再备份） |

### A.14.3 备份策略

- **SQLite backup API**：免停机一致快照
- **轮转**：本地保留最近 7 份全量 + 月归档
- **异地副本**：配置 `backup.remote_dir` 时推送副本到远程目录（NAS 挂载 / 异机），失败仅告警不阻塞
- **冷归档**：`archive_raw_cold(days=90)` >90 天原始文件 zstd 压缩为只读冷归档
- **恢复**：恢复前自动再备份当前状态（`pre_restore_` 前缀）；raw/ 按快照级别恢复（增量=合并复制，full/monthly=覆盖，见 §8 FAQ）；恢复后自动校验溯源链完整性（结果见响应 `integrity` 字段）
- **凭据库（EP-11）**：credentials.db 按文件存在性纳入快照；**主密钥（`SGME_CRED_KEY`）不随快照携带**——恢复资产 = 快照 + `config/.env` 两件套；灾备重建后的重签发流程见 §9.2

### A.14.4 配置

`config/sgme.yaml`：
```yaml
backup:
  dir: data/backups
  schedule: "04:00"         # 每日自动备份（0.8 方案 B 定时器，本地时区）
  raw_cold_days: 90
  remote_dir: null          # 异地副本目标，null 则跳过
```

### A.14.5 验证命令

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

## A.15. 可观测性

### A.15.1 健康检查端点

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

> `credentials.enabled=true` 时 `/v1/health` 另附只读字段 `credentials_ok`（凭据模块主密钥自检状态；false → 凭据写路径被拒绝）。

### A.15.2 提炼停摆检测

- `check_refinement_stalled`：`refined_at` 游标停滞超 24h → `stalled=true` + 产 `anomaly_warn`
- `check_llm_available`：轻量 ping LLM 端点，不可达 → `llm.available=false`
- `check_vector_availability`：sqlite-vec 扩展 + 向量表行数，不可用 → `vector.available=false`（含 reason）
- `check_heartbeat`：综合心跳（LLM 可用 + 队列深度 + 最近提炼时间 + 停摆标记）
- 心跳定时任务：每 10 分钟一次（`sgme/__main__.py` 注册）

## A.16. Docker 部署

单机 / NAS 一键部署（HTTP API + WebUI :9910、MCP :9913）。多阶段镜像自带 WebUI 与 git（skills_hub 依赖），首次启动自动物化默认 `sgme.yaml` 到数据卷。

### A.16.1 准备

```bash
# 克隆仓库（任选远程）
git clone https://gitee.com/freehul/sgme.git SGME
cd SGME

# 密钥：复制 .env.example 为 docker.env 并填入真实值
# （ZHIPU_API_KEY / SILICONFLOW_API_KEY / SGME_ADMIN_KEY / SGME_AGENT_KEY，docker.env 已被 gitignore，勿提交）
cp .env.example docker.env
```

### A.16.2 启动与验证

```bash
docker compose up -d --build
# 验证
curl $SGME_HTTP_BASE/v1/health
# WebUI（含在镜像内）：打开 $SGME_HTTP_BASE/
# MCP：$SGME_MCP_URL
```

### A.16.3 配置（sgme.yaml）

- **首次启动**（空数据卷）自动把默认 `config/sgme.yaml` 物化到数据卷 `config/` 下（entrypoint 复制镜像内模板）
- 默认含生产调优：`l15.prescreen.enabled: true` + `fallback: skip_conflict`（embed 不可达时跳过冲突检测直接 store，防全量召回烧钱）与 `search.vector`（siliconflow BAAI/bge-m3 1024 维免费托底）
- 改配置：编辑数据卷 `config/sgme.yaml` 后重启容器；程序资源 `llm.yaml` / `providers.yaml` 始终读镜像内版本
- **凭据模块（可选，EP-11）**：默认 `credentials.enabled: false`；启用 = 数据卷 `config/sgme.yaml` 打开该开关，并给容器注入 `SGME_CRED_KEY`（44 字符 Fernet key；生成：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`）后重启
- 找数据卷：`docker volume ls` / `docker volume inspect <volume>`（compose 卷名形如 `sgme_sgme-data`）

### A.16.4 升级

```bash
git pull
docker compose up -d --build
```

**自动更新（ST-34，2026-08-21）**：
- WebUI 检测到新版本（GitHub Releases API，`update_check` 配置段可调 enabled/interval_hours/source）→ 健康卡片显示提示条 → 用户确认「立即更新」→ 写意图文件 `$SGME_HOME/update/request.json`
- 主机侧代理 `scripts/sgme-host-updater.sh`（NAS 主机 cron 每 5 分钟轮询）执行：git pull → docker build 新镜像 → 备份 compose → 换 tag → compose up → 健康验证 → 成功清请求 / 失败自动回滚旧镜像
- 容器无特权（不挂 docker.sock），更新由主机脚本执行
- **cron 挂载（T-309，2026-10-30 实测修订）**：`/etc/cron.d/` 下**只有 `sgme-watchdog`（root）**，没有 updater 条目（旧文档「root crontab 承担」与实机不符，是 10-29 实测「写意图文件 10 分钟无人处理」的根因）；LEO 无免密 sudo（`sudo -n` 报 password required）。**实测存在 root 侧触发源**（`crontab -r` 清空 LEO crontab 后，10:25:01 仍自动触发更新），故正确做法是**只保留一处挂载**，不要再加第二条：
  ```bash
  # 先确认没有重复挂载（重复 = 双实例，见下条）
  crontab -l                                   # LEO 侧（应为空或仅一条）
  ls /etc/cron.d/                              # root 侧（只有 sgme-watchdog 时说明触发源在 root crontab）
  # 如需在 LEO 侧单独挂（root 触发源已移除时）：
  printf '%s\n' '*/5 * * * * <NAS_ROOT>/scripts/sgme-host-updater.sh >> <NAS_ROOT>/logs/updater.log 2>&1' | crontab -
  ```
- **并发防护**：脚本已内置 `flock -n` 原子自锁（2026-10-30 修订；原实现「`[ -f LOCK ]` 检查 → `touch`」两步有竞态窗口）。**但仍必须只保留一处 cron 挂载**——实测：LEO 用户 crontab 与既有 root 侧触发源重复时，同一分钟起两个实例，`git pull` 并发撞 ref 锁（`cannot lock ref 'refs/remotes/origin/main'` / `Need to specify how to reconcile divergent branches`）→ 一方 `compose up` 失败自动回滚、另一方成功，容器被重建两次
- **NAS 上 `scripts/` 属 root**（`drwx------ root root`），LEO 无法覆盖脚本副本；内置自锁随下一次 root 侧刷新生效，当前以「只留一处挂载」为准
- 前置：`data/update` 属主 LEO；意图文件必须带 `"status": "pending"`（缺该字段或其它值 → 脚本静默 exit 0）
- 验收口径（2026-10-30 实测）：写 pending 意图文件后 ≤10 分钟，`logs/updater.log` 仅出现**一条**「=== 开始自动更新 → vX ===」与「=== 更新成功 → vX ===」，`docker ps` 镜像 tag 同步变化
- **构建耗时与缓存纪律（2026-10-30 实测）**：`Dockerfile` 的 pip 层**之前不得 COPY 版本号会变的文件**（`pyproject.toml` / `README.md` / 源码）——改前每次版本 bump 都会让依赖层失效并全量重装，实测 **31 分 58 秒**（NAS 到 pypi.org 4.5–50 KB/s）。改后（依赖清单固化在 RUN 指令内 + 阿里云 pip 镜像兜底 pypi）实测：首次重装 **13 分 19 秒**、仅 bump 版本 **15 秒**（依赖层 `#8–#15` 全 CACHED）。**只有真正改依赖时才需要重装**

### A.16.5 NAS（群晖）部署

- 部署真相源：`deploy/nas-docker-compose.yml`（模板，`{{IMAGE_TAG}}` 占位），NAS 生产 compose 非 git 仓库（B64 遗留）
- 流程（B64 纪律）：改项目 git → push（github + gitee 双推）→ NAS 拉取（`<NAS_ROOT>/git/sgme.git` bare 仓接 gitee remote 后 fetch）→ NAS 构建 → 更新 compose → up -d
- 数据卷 bind mount 到共享文件夹（如 `<NAS_ROOT>/Docker/sgme/data` → `/data`），文件站可直接备份
- skills-hub 可选 bind mount：`<NAS_ROOT>/git/skills-hub.git` → `/git/skills-hub.git`（file:// 直访，免 SSH key）

---

## A.17. 安全运维（ST-55 外网暴露安全加固，2026-10-11）

### A.17.1 来源实测判读（部署验证清单 1-4 的判读入口）

一切判读走两个入口：`GET /v1/admin/security/status`（计数概览 + 活封禁列表）与 `GET /v1/admin/security/events?date=YYYY-MM-DD`（审计事件）。成功请求不落审计——**要产生判读信号必须发会产生失败的请求**（无 key / 错 key）。

**来源区分度实测（清单 1）**：两台不同外部设备各发一笔错 key 请求 → status 的 `fail_counts` 必须呈现两个不同来源键。若两台设备显示同一来源 = 来源坍缩（容器桥接网络把源地址折叠成网关地址）——**此状态下 `auth_fail_threshold` 必须保持 0（观察期），禁止开启封禁**（否则一台设备的失败会封掉全体用户）。出路：换 host 网络 / 隧道直通模式，或接受 `trusted_proxies` 不可用的事实保持观察期。

**默认 key 守卫（清单 2）**：隧道接通后默认 dev key 从外网调用必须仍 403（net_guard 单一策略源，两入口同规则）。

**/v1/admin/keys 守卫（清单 3）**：从外网调用必须仍 403（含 Host 头兜底路径；执行方式：边缘放行期内发起，或从边缘内侧发起——须确为应用层守卫生效而非网络层不可达）。

**伪造 XFF 回退实测（清单 4）**：注入伪造 XFF 的请求 → 解析结果必须仍为真实来源（status/审计里的来源键不变）= 证明代理确实追加重写 XFF。

### A.17.2 通道对照表

| 部署形态 | 连接对端显示 | trusted_proxies 是否需要 | 回环豁免是否成立 |
|---|---|---|---|
| 本机直连（127.0.0.1 调用） | 127.0.0.1 | 否（名单空=不解析 XFF） | 成立 |
| 局域网直连 | 客户端局域网 IP | 否 | 不成立（非回环） |
| 反向代理（nginx/caddy） | 代理 IP | 是（代理网段/单 IP） | 不成立（按解析后真实来源判） |
| 容器桥接（NAS Docker） | 网关 IP（**源坍缩风险**） | 仅网关为可信代理时解析 | 视来源而定（网关非回环） |
| 隧道（frp/cloudflared） | 隧道进程回环地址 | 隧道地址在名单时解析 XFF | **可能成立**（隧道进程本机回环）——须按 7.1 实测确认 |

⚠️ 回环豁免判定 = 解析后来源（`is_loopback_source`），不是连接对端字面值。隧道落地在本机时对端是 127.0.0.1，若隧道不改写 XFF，所有外网请求都会被判为「本机」从而豁免封禁——这是设计内行为（封禁豁免），但**无 key IP 限流与 per-key 限流不豁免**，仍有兜底。

### A.17.3 重启 = 解封（自锁说明）

SecurityState 是进程内存态：封禁中的 IP 调不了任何端点（含 admin），唯一解 = 重启进程（解封）。重启同时清空失败计数与升级阶梯——阶梯从零开始。

### A.17.4 探针噪音处置（D9 结论）

compose 双探针（9910 health=200；9913 无 key POST /mcp=403）语义不变：health 在 SecurityGate 完全绕过（不计失败不进限流桶）；MCP 探针 403 会被计入失败计数，但探针来源是本机回环 → 回环只计数不封禁（观察期读数可见但不触发封禁）。**探针不会把本机 IP 封掉**。

### A.17.5 试接通演练与回滚

推进序：①部署（通道不通）→ 跑 7.1 清单 1 与连通性确认；②受控试接通（仅用于执行清单 2-4 与 agent 全流程验证；未全过即断开、修复后重测）；③全过后保持接通，观察期 ≥1 周（无合法来源「单窗口失败 ≥20」模式、无 ip_limited 持续误伤、无审计写失败 WARNING，三项无异常 → `auth_fail_threshold` 转 20 转正）。快速通道：清单 1、4 实测通过且 trusted_proxies 对照表已填 → 视为通道可控，可当日转正（首周保留校准窗口）。

**回滚**：旧 tag + compose 重建（新配置键向后兼容，旧版本读新 yaml 自动忽略未知键；`max_body_bytes` 等新键缺省安全）。
