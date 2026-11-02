---
name: sgme-operations
description: SGME Gateway 运维与日常操作：健康检查、记忆写入、提炼、待审队列、运维看板、凭证与角色、技能库治理、部署升级和安全排障。当服务异常或需要执行 SGME 运维操作时使用。
category: sgme-operations
tags: [skill, sgme, operations, gateway, memory, wiki, refine, review, credentials, roles, skills, troubleshooting]
version: 2.0.0
pattern: auto
uses:
  - sgme
---

# SGME Gateway 运维

> 下文命令统一用环境变量：`$SGME_URL`（如 `http://<NAS_IP>:9910`）、`$SGME_ADMIN_KEY`、`$SGME_AGENT_KEY`。
> 密钥只从环境变量读，不在命令行历史、日志、截图或回复里出现明文。

## 先判断问题类型

- **记忆**：历史事实、偏好、项目上下文 → `inject` / `search(scopes=["memory"])`；用户要求记住 → `append`，会话结束再异步 `refine_trigger`。
- **Wiki**：手册、设计、经验、踩坑 → 先 `wiki_search` / `wiki_pages`，再 `wiki_page`；稳定经验用 `wiki_page_add` 或 `wiki_page_update` 沉淀。
- **技能**：专业方法 → `skill_search` → `skill_digest` → `skill_get`；普通技能不 materialize。
- **待审/裁决**：低置信度记忆停在队列、注入里看不到 → 见「待审队列」；TTL 与 LLM 用量异常 → 见「运维看板」。
- **凭证/角色**：客户端取不到 key、角色卡不生效 → 见「凭证管理」「角色运维」。

## 健康检查

1. 调 `health` 或 `GET /v1/health`，记录 `status`、`version`、LLM 可用性和提炼水位。
2. 连接失败时先检查服务发现地址、端口和 `X-API-Key`，HTTP 客户端访问本机服务使用 `trust_env=False`。
3. 服务仍不可达才检查进程/容器和日志；重启前保留日志与数据，不清空数据库。
4. health 正常但技能搜不到时，先检查 `skills.source_dirs`，再执行技能索引自愈/重建端点并查看 `fts_drift_detected`。

## 写入和提炼

- `/v1/append` 的正文首行必须是 `# {ISO8601} {role}`；只提交真实 user/assistant 消息，不把工具回执写成对话。
- 单轮写入保持幂等；批量提炼使用异步入口，按服务端返回的任务状态轮询，429 使用退避，不立即重试。
- 记忆纠错用 `memory_reject` / `memory_unreject`，不直接删除原始会话或记忆归档。

## 待审队列（2.0 / D-30）

- **现象**：WebUI 待审页有积压；某条刚提炼的记忆在 `inject`/`search` 里查不到。
- **判断**：L2 低置信度产物被打了 `needs_review=1`。这类记忆不进主注入块、不进人格证据，只在待审队列可见，等人工放行或拒绝。
- **查队列**（Agent Key；`limit` 1–200 默认 50，`offset` ≥0，`dimension` 走 memory_tags 精确过滤）：

```bash
curl -s -H "X-API-Key: $SGME_AGENT_KEY" \
  "$SGME_URL/v1/admin/review/pending?limit=50&offset=0&dimension=<dimension_id>"
# → {"items":[...], "total": N}；items 是完整记忆行
```

- **放行**（幂等；`note` 可选，会带时间戳追加进 notes，不覆盖既有备注）：

```bash
curl -s -X POST -H "X-API-Key: $SGME_AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"note":"用户确认可用"}' \
  "$SGME_URL/v1/admin/review/<memory_id>/approve"
```

- **拒绝**（软删：`status='rejected'` + `reject_reason` + `rejected_at`，可 `memory_unreject` 恢复）：

```bash
curl -s -X POST -H "X-API-Key: $SGME_AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"reason":"用户否认的过期偏好"}' \
  "$SGME_URL/v1/admin/review/<memory_id>/reject"
```

  `reason` 为空/空白直接 400 `ERR_INVALID_ARGS`；拒绝是幂等的，已拒绝的行不会被二次理由覆盖。

- **批量消化超期项**（慎用）：默认 7 天未处理按「保守直存」处理——放行进库但 notes 追加 `未经人工确认（超期 N 天保守直存）`，是**未经人工裁决**的记忆，后续只能按该标记批量复查。

```bash
curl -s -X POST -H "X-API-Key: $SGME_AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"days":7}' "$SGME_URL/v1/admin/review/auto-resolve"
# → {"resolved": N}；N 是本次实际消化条数
```

- **坑**：`approve` 只把 `needs_review` 置 0，**不会**把 `status='rejected'` 拉回 active；对已拒绝的行调 approve 等于没救。要恢复先 `memory_unreject`。
- **坑**：待审列表只列 `status='active'` 的行，拒绝后从列表消失是预期，不是丢数据。

## 运维看板（2.0）

两个端点都是 Admin Key、只读、`days` 取值 1–365（越界或非整数静默回退 30）。

- **TTL 处置**：`GET /v1/admin/ops/ttl-disposition?days=30`
  返回按天分组的五档计数（固定枚举，顺序即看板顺序）：`valid_expired`（有效过期）、`renewed`（续期）、`anchor`（锚定）、`sleeping`（休眠）、`archived`（归档），另给 `total` 汇总。
- **LLM 用量**：`GET /v1/admin/ops/llm-cost?days=30`
  返回按 `(day, provider, model, stage)` 的 `prompt_tokens`/`completion_tokens`/`calls` 明细与 `totals` 汇总。

```bash
curl -s -H "X-API-Key: $SGME_ADMIN_KEY" "$SGME_URL/v1/admin/ops/ttl-disposition?days=30"
curl -s -H "X-API-Key: $SGME_ADMIN_KEY" "$SGME_URL/v1/admin/ops/llm-cost?days=30"
```

- **现象**：看板全 0，响应带 `"note": "审计表尚未创建（无处置记录）"` 或 `"用量账本尚未创建（无记账）"`。
- **判断**：`ttl_disposition` / `llm_usage_daily` 是懒建表——首次有处置/记账才创建。HTTP 200 + 空集 + note 是**正常空态，不是 404/500 故障**；先确认最近是否真的没有提炼/召回老化任务跑过。
- **处置**：note 长期存在且业务明显在跑 → 查提炼/召回任务是否真的执行、`SGME_HOME` 是否指错（表建到了另一个库）；不要手工建表绕过。

## 凭证管理

6 个端点（注意读侧是 `/v1/credentials`，不在 `/v1/admin` 下）：

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/v1/credentials` | agent | 列表：键名 + 元数据，**不含值**，默认过滤已删 |
| GET | `/v1/credentials/{key}` | agent | 取单值（记 read 审计），不存在/已删 → 404 |
| GET | `/v1/credentials/export` | agent | 批量导出明文；`only=` 与 `prefix=` 互斥，`only` 缺键 → 404；记领域审计 + SecurityLog `credential_export` |
| POST | `/v1/credentials` | admin | 写入/更新；已软删键名 → 409，需先 restore，不自动复活 |
| DELETE | `/v1/credentials/{key}` | admin | 软删（幂等） |
| POST | `/v1/credentials/{key}/restore` | admin | 恢复（幂等，清 `deleted_at`） |

- **铁律：值永不明文展示。** 列表、日志、聊天、工单、截图、排障回执都只出现键名/描述/指纹，不回显值。核对是否已配置看「键名是否存在 + 描述 + 审计记录」，不要把值打印出来对照。
- **导出是唯一批量明文出口，必须二次确认**：先向用户确认目标范围（`only` 还是 `prefix`，含哪些键），明确点头后才执行；导出内容落到受控文件或直接管道给消费方，不回贴对话。事后可按 SecurityLog 的 `credential_export` 事件核对 `target`（`all` / `only:...` / `prefix:...`）。

```bash
# 只列键名，确认真实存在哪些
curl -s -H "X-API-Key: $SGME_AGENT_KEY" "$SGME_URL/v1/credentials"
# 二次确认后按前缀导出，输出重定向到受控文件（不要 cat 到终端）
curl -s -H "X-API-Key: $SGME_AGENT_KEY" \
  "$SGME_URL/v1/credentials/export?prefix=DEEPSEEK_" > <受控路径>/creds.json
```

- **现象**：写入/更新/导出报「主密钥不可用」（`state=missing|invalid_format|probe_failed`）。
- **判断**：`SGME_CRED_KEY` 缺失或不是合法的 44 字符 Fernet key；`probe_failed` 表示库内已有密文但当前 key 解不开（换过 key）。缺 key 时**绝不**明文兜底，写路径直接拒绝。
- **处置**：从部署配置注入正确的 `SGME_CRED_KEY` 后重启。不要随手重新生成 key——既有密文会永久不可读；确需轮换，先解出全部值再统一重写。
- **坑**：`export` 必须先于 `/{key}` 注册。若前置代理/重写规则把 `export` 当键名转发，会命中详情端点导致 404 或取错值——先直连服务验证。
- **坑**：写/删/恢复/导出四处都写 admin_op 审计；排障时以审计为准，不以客户端缓存为准。

## 角色运维

**三层存储，别混：**

| 层 | 位置 | 内容 |
|---|---|---|
| 角色卡 | `<home>/roles/*.json`（容器内 `/data/roles`，持久化卷） | Character Card V2 兼容子集：`name`/`description` 必填，其余可选 |
| 基线层 | 卡内 `extensions.sgme_profile` | 五键白名单：`biography`、`values`、`hobbies`、`voice`、`personality_slots`；多余键拒绝 |
| 演化层 | `data/role_evolution/<role_id>.json` | 四类 kind：`timeline`、`feelings`、`personality_changes`、`values_changes` |

- **端点**：`GET/POST/DELETE /v1/admin/roles/{role_id}`（DELETE 是归档到 `.archive/`，原件永不物理删）、`GET/POST /v1/admin/roles/{role_id}/persona`、`GET/POST /v1/admin/roles/{role_id}/evolution`。
- **`role_id` 白名单**：`^[a-z0-9][a-z0-9_-]{0,63}$`。大写、中文、`/`、`..` 一律拒绝（防路径穿越），报错可能是笼统的 `ERR_INTERNAL`，先查 id 形态。
- **persona 是唯一物化例外**：落 `data/personas/<role_id>.md`，属运行数据，不入 git；演化层同理。
- **血泪坑（切库/升级最容易踩）**：镜像内置卡**只在首次启动时物化**——entrypoint 判断 `$SGME_HOME/roles` 目录不存在才 `cp /app/roles/*.json`。目录已存在时**新镜像不会覆盖已有卡**，所以「升级后内置人设没变」不是 bug。必须直接往卷里传卡：

```bash
docker cp roles/butler.json sgme:/data/roles/butler.json   # 覆盖单张
docker exec sgme ls /data/roles                            # 核对落盘
docker restart sgme
```

  裸机部署同理：直接改 `<home>/roles/<role_id>.json`，不要指望重装程序覆盖。

## 技能库运维

- **库位置**：`<home>/data/skills.db`（生产容器内 `/data/data/skills.db`）。**不是 `memory.db`**——查错库会得出「技能还在文件系统」的错误结论。
- **表职责**：`skills` 主表（`name` 主键 + `sha256` 增量锚点 + frontmatter 拆列 + `content` 正文 + `status`）；`skill_files` 附属文件（references/scripts/assets，BLOB + sha256）；`skill_revisions` 修订史（append-only）；`skill_meta` 生命周期（`active`/`deprecated`/`tombstone` + `superseded_by`）；`skill_uses` 依赖边（含 `strength`）。

```bash
sqlite3 <home>/data/skills.db \
  "select count(*) from skills where status='active'; select count(*) from skill_files; select count(*) from skill_revisions;"
```

- **uses 门禁会阻断写库**：悬空引用（目标不存在或已墓碑）、依赖环（报错带 `A→B→A` 完整路径）、自引用一律拒绝。内置 `skill-registry-protocol` 不在表里但恒可用，是唯一例外。删除时先看入向引用：其他技能 `uses` 显式依赖且未解除 → 409 + `referenced_by` 清单，确需强删加 `?force=true`；正文自然语言提及只列 warning 不拦。
- **格式 lint 阻断入库**：name 仅小写字母/数字/`-`/`_` 且 ≤64 字符、不以连字符首尾；description 非空 ≤1024 字符；正文总行数 **<500**（≥500 记 error）。error 阻断，warn 放行。写入口 `PUT /v1/admin/skills/{name}` 与预检 `POST /v1/admin/skills/{name}/lint` 同一套规则。
- **附件回填**：`skill_files` 是附属文件的唯一真源；从文件系统收编用回填脚本，默认 dry-run，确认后加 `--apply`。SKILL.md 本身不写入（正文在 `skills.content`，避免双份）。

```bash
python scripts/backfill_skill_files.py --skills-root <技能目录> --db <home>/data/skills.db
python scripts/backfill_skill_files.py --skills-root <技能目录> --db <home>/data/skills.db --apply
```

- **编写工作流**（不碰 git，直接读写 skills.db）：

```bash
python scripts/skill_workspace.py pull <name> --dir <工作目录>
# 编辑 SKILL.md / references/...
python scripts/skill_workspace.py status --dir <工作目录>   # 有差异退出码 1
python scripts/skill_workspace.py push   --dir <工作目录>   # 先 lint，error 拒绝
```

  `push` 会写 `skills` + `skill_files`（多余文件删除）并记 `skill_revisions`；库内该技能在 pull 后被别处改过时默认拒绝，先重新 pull 合并，`--force` 才以本地为准覆盖。

## 部署与升级踩坑

**① `tar --exclude=data` 会排掉代码包**

- **现象**：升级包解开、容器重启后起不来，日志 `ModuleNotFoundError: No module named 'sgme.data'`（或其它 `sgme.*` 缺失）。
- **判断**：`--exclude=data` 按名字匹配**任意层级**的 `data` 目录，把 `sgme/data/` 这个代码包一起排掉了。包体不完整，不是依赖问题。
- **处置**：改用 `--exclude=./data`，只排归档根下的用户数据目录；打包后必须核对代码包在不在：

```bash
tar --exclude=./data -czf sgme-2.0.tar.gz -C <project-root> .
tar -tzf sgme-2.0.tar.gz | grep -c '^\./sgme/data/'   # 必须 >0
```

  已经装坏就重打重传，不要靠补 `__init__.py` 或手工拷目录糊过去（会漏模块）。

**② 技能 lint 不能拿库里的 `content` 直接调 `lint_skill`**

- **现象**：全库扫描报「缺少 frontmatter」，几百条全红。
- **判断**：入库时 frontmatter 已拆成 `name`/`description` 等列，`skills.content` 只剩正文；对库内数据要求 `---` 围栏必然全量误报，不是技能真的坏了。
- **处置**：走真实入口，别自己拼参数：

```bash
python scripts/lint_all_skills.py --db <home>/data/skills.db --json
python scripts/lint_all_skills.py --source-dir <技能目录>          # 目录模式
```

  只有**写入口**（用户提交 SKILL.md 原文）才该 `require_frontmatter=True`。

## SQLite 锁与并发写

- **现象**：日志出现 `database is locked`，接口偶发 500，提炼跑批期间尤其多。
- **判断**：连接已在 `_connect` 设 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`，瞬时并发写会自动等待 5 秒；若仍频繁报锁，说明有长事务或提炼任务长时间持写锁，不是超时值不够。
- **处置**：

```bash
sqlite3 <home>/data/memory.db "PRAGMA journal_mode;"     # 应为 wal
sqlite3 <home>/data/memory.db "PRAGMA busy_timeout;"     # 应为 5000
```

  再查是否有外部进程用 `sqlite3` 开着写事务、提炼是否单批塞入过多写入。排查只读可用 `sqlite3 -readonly`。
- **禁止**：调低/清零 `busy_timeout` 换「更快的失败」；删 `-wal`/`-shm` 文件；在服务运行时用 `VACUUM`/大事务抢锁。

## 安全边界

- Agent Key 只读/写 Agent 端点；技能写入、删除、改名、配置、重建使用 Admin Key。运维看板与角色管理同属 Admin 档。
- 密钥只从环境变量或部署配置读取，不在聊天、技能、日志或仓库中粘贴明文；凭证值永不明文展示，批量导出必须先二次确认。
- 清库、恢复、迁移属于高风险操作：先备份并核对精确目标，保留可回滚凭证，禁止对原始会话做不可逆删除。
- 待审 `auto-resolve`、批量导出、`--force` 覆盖/强删、`--apply` 回填都是会改变状态的动作：先说明影响面，得到确认再执行。
