---
name: sgme-operations
description: SGME Gateway 运维与日常操作：健康检查、记忆写入、提炼、待审队列、运维看板、凭证与角色、技能库治理、部署升级和安全排障。当服务异常或需要执行 SGME 运维操作时使用。
category: sgme-operations
tags: [skill, sgme, operations, gateway, memory, wiki, refine, review, credentials, roles, skills, troubleshooting]
version: 2.0.0
pattern: auto
uses:
  - sgme
references:
  - 02-review-queue.md
  - 03-ops-boards.md
  - 04-credentials.md
  - 05-roles.md
  - 06-deploy-pitfalls.md
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

> 待审候选的查询、裁决、批量放行（含 approve/reject/auto-resolve 的坑）。详见 [`references/02-review-queue.md`](references/02-review-queue.md)。

## 运维看板（2.0）

> TTL 处置与 LLM 成本两个看板的端点、空态语义与判读。详见 [`references/03-ops-boards.md`](references/03-ops-boards.md)。

## 凭证管理

> 凭据六端点、鉴权档位、导出风险与主密钥排障。详见 [`references/04-credentials.md`](references/04-credentials.md)。

## 角色运维

> 角色卡三层存储、演化层端点、镜像内置卡不覆盖持久卷的坑。详见 [`references/05-roles.md`](references/05-roles.md)。

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

> 打包排除规则、技能包缓存口径、版本同步等部署期坑位。详见 [`references/06-deploy-pitfalls.md`](references/06-deploy-pitfalls.md)。

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
