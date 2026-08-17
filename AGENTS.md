# SGME — 拾光记忆引擎（ShiGuang Memory Engine）



单用户 Agent 记忆引擎 Server：把 Agent 会话提炼为标签化记忆，按模式模板注入画像。Python 自研底座（借鉴 TencentDB-Agent-Memory 设计思想，MIT 合规）。



## 技术栈



- Python 3.11+，项目自包含 venv（`.venv/`），依赖见 `pyproject.toml`

- SQLite（标准库 sqlite3）+ FTS5 + sqlite-vec（向量，/search 用）

- FastAPI（HTTP 服务，端口 9910）

- LLM 调用用 httpx，**必须 `trust_env=False`**（防 Clash 代理劫持 localhost 请求）

- 测试 pytest



## 文档索引（编码前必读）



| 文档 | 内容 |

|---|---|

| `docs/requirements/SGME-Backlog-v0.2.md` | **Backlog 锚文档（Epic/Story/Task 体系）——立项即有，持续更新，设计文档依附于此** |

| `docs/design/SGME-架构设计-v0.9.md` | **架构总纲（v0.9 文档整理合并版：数据流/双库/维度/注入/鉴权/备份 + 接口契约/数据模型/LLM/模板/提示词/分词 并入）** |
| `docs/design/SGME-实施变更记录-v0.9.md` | **实施变更记录（B 系列，含运维/排障知识）** |

| （表结构见架构 v0.9 §23） | 数据模型已并入架构 v0.9 |

| （端点见架构 v0.9 §22） | 接口契约已并入架构 v0.9 |

| `docs/design/SGME-L0文件格式-v0.1.md` | 原始层文件格式/增量段 |

| （模板见架构 v0.9 §25） | 模板引擎已并入架构 v0.9 |

| （提示词见架构 v0.9 §26） | 提炼提示词已并入架构 v0.9 |

| （版本管理见架构 v0.9 §27） | 提示词版本管理已并入架构 v0.9 |

| `docs/design/SGME-评测基线-PRD-v0.1.md` | **#32 提炼质量评测基线：评测集设计 + L1/L2 度量定义 + RRF 调优方案** |

| `docs/design/SGME-评测框架设计-v0.1.md` | **#32 评测框架：方案/文件列表/数据结构/调用流程/L1 F1 计算链路** |

| `docs/design/eval-class-diagram.mermaid` | 评测框架类图 |

| `docs/design/eval-sequence-diagram.mermaid` | 评测框架时序图 |

| （降级链见架构 v0.9 §24） | LLM 降级链已并入架构 v0.9 |

| （归一化见架构 v0.9 §28） | 维度归一化已并入架构 v0.9 |

| `templates/*.yaml` | 预定义 4 模式模板 |

| `prompts/*.txt` | 三套提炼提示词（含 {{占位符}}） |

| `registry/*.yaml` | 维度注册表/别名表（运行时数据源） |

| `config/llm.yaml` | LLM 降级链配置 |



## 架构约束（不可违反，违反即返工）



1. 数据流单向：L0 原始文件 → L1 标签化记忆（memory.db）→ L1.5 冲突提炼 → L2 场景（wiki.db）；画像 = 模板查询结果，无物化

2. 双库 + 文件：memory.db（记忆池）、wiki.db（场景+raw_files 索引）、raw/（原始 MD 文件）；`*.db`、`raw/`、`tmp/` 不入 git

3. 维度一律用注册表 id（identity/projects/...），API 请求侧不收中文名；中文仅展示

4. TTL：动态维度默认（status 7d/focus·tasks 30d/projects·goals 90d），**起算 updated_at**（update/merge 续期）；过期退出注入、保留可溯源

5. 注入零 LLM：模板查询 = 纯结构化 SQL（标签过滤 + 排序 + limit + time_window + TTL 过滤）；FTS5/向量仅 /search

6. 模板查询默认排序：动态维度 updated_at DESC，静态维度 priority DESC

7. L1.5 候选池：标签预过滤 OR、**不截断全量召回**、按上下文预算分批、同一新记忆只进一批；单记忆候选超预算才允许 top-k 截断并记 anomaly_warn

8. Supersession：旧值归档不删除（memory_archive 表），判等锚点 memory_id

9. LLM 降级链：zhipu（glm-4.7-flash，免费主模型，2026-08-18 用户定）→ deepseek（deepseek-v4-flash，付费备用）→ rule drop_batch；模型名含 pro/reasoner/thinking 或命中 gemma-4-12b-qat 拒绝加载；免费 Key 申请见 docs/guide/免费模型Key申请指南.md

10. 密钥不落盘：只引用环境变量名；**API Key 铁律——禁止在代码/配置里硬编码密码**

11. 提炼健康自检：refined_at/last_refined_seq 水位推进，停摆产 anomaly_warn

12. 文档是第一公民：改设计先改 docs，代码与文档不一致视为缺陷



## 开发规范



- 模块边界（依赖方向）：`adapters → server/mcp_server → operations → engine → data/profile/llm → config`；data 是唯一数据库操作层（统计查询唯一出口 `data/stats_dao.py`）；operations 是统一操作层（HTTP/MCP 共用，入口层只做协议翻译）；扩展模块（wiki/skills_hub/refinery）按 config.yaml 开关挂载，禁用时核心零影响；管线编排唯一出口 `engine/pipeline.py`（L0 写入 append_l0 + L1→L1.5→L2 串联）；config 是配置唯一读写方（含落盘）；入口层（HTTP/MCP）只做鉴权与响应组装，**入口禁止互相依赖**（B30：MCP 不 import routes_*，backup 裸连接是唯一允许绕过 data 层的场景且须注释说明）

- 中文注释；函数/表名英文；JSON 字段英文

- 每个模块自带 pytest 测试；改动必须 `pytest` 全绿

- ⚠️ **项目文件管理铁律**（2026-08-04 教训）：一切项目产物（插件源码/脚本/配置模板）必须放在项目目录内随 git 管理；外部运行时目录（如 Hermes plugins/）只允许是**可重建的部署副本**，由项目内 install 脚本生成，禁止直接改外部副本后不回写项目。打包分发时项目目录即完整交付物。



- 长驻进程改代码后必须重启才生效（`netstat -ano | grep 9910` → `taskkill /PID <pid> /F`）

- ⚠️ **mock 测试全绿 ≠ 真实链路可用**（2026-08-04 教训）：mock LLM 掩盖了 4 个真实提炼 bug（思考型模型 content 截断、空候选池短路、candidate_ids 语义、归档 FK）。**改动提炼相关代码后必须跑真实 LLM 冒烟**（LM Studio 在线时 `python scripts/e2e_smoke_v04.py`），并查 Server 日志确认无 `L1.5 输出解析失败` / `降级直存`

- 测 API 用 Python requests（git-bash curl 会破坏中文 UTF-8）



## 开发流程

需求与任务以 Backlog 锚文档为锚（见「文档索引」），动手前读架构文档 `docs/design/SGME-架构设计-v0.9.md` 对应章节。项目由多 AI 工具协作（Hermes/Trae/WorkBuddy/笔记本会话），所有协作者遵守以下规范：

### 任务锚定（Backlog 协作）

- ⚠️ **动手前重读最新版 Backlog**——会被多工具并发更新，出 PR 清单/待办汇报前必须重读，防漏新增项（2026-08-11 实锤教训：清单差点漏 ST-23 新增项与 ST-22 变更）
- 新任务登记递增编号（T-N）；完成标 ✅+版本，标记不删除；提交信息引用 `Closes ST-x / T-x`

### 三池职责（2026-08-13 用户定，防漂移）

- **创意池**（`ideas` 维度，WebUI 创意池页 / `POST /v1/admin/ideas` / MCP `idea_add`）：创意**只由用户主动提出**才记录，**提炼 LLM 不再自动打标**（prompts/l1_extraction.txt 已移除创意识别）；删除由用户主动（WebUI 删除或提出由 agent 删，软删可恢复）
- **待办池**（`demands` 表，WebUI 待办页 / `/v1/admin/demands*` / MCP `demand_create`）：**跨项目统一待办**——不管哪个项目的待办都加进来，带 `project_id` 标记可过滤；`project_id` 是**自由标记**（未登记项目也允许，只回 warning）；状态机 pending→done 两态 + `created_at`/`resolved_at` 时间戳；由 agent 维护
- **项目池**（`project_meta` 表，WebUI 项目页 / `/v1/admin/projects*` / MCP `project_register`）：项目**由用户主动提出立项/创建**，agent 执行；可从创意升格（promote）

### 动手纪律（报备）

- 动手前先报方案：列清单（改哪些文件、每处怎么改、删不删旧件），等确认再动手；讨论阶段不写文件
- 方向批准 ≠ 方案批准：流程/配置/skill 改造必须先列清单
- 原件永不删：迁移/重建在副本上操作，原件只读

### 提交流程

- 提交信息格式 `<type>: <中文描述>`，type ∈ feat/fix/docs/refactor/test/chore/perf/ci（Conventional Commits）
- 逻辑分组提交：代码/适配器/文档分开提交；提交前跑全量 pytest
- 重大变更记入 `docs/design/SGME-实施变更记录-v0.9.md`（B 系列编号递增，含背景/改动/测试/运维影响）

### 研究先行

**查证与搜索流程（动手前必走，五步）**：
1. **本地优先**：先读「文档索引」对应章节 + 模块代码与测试，不猜形状
2. **SGME 记忆**：涉及历史事实/项目决策/用户偏好 → MCP `search`（记忆池）+ `wiki_search`（知识库），不凭记忆断言
3. **wiki 知识库（W1-W7，2026-08-16）**：手册/经验统一入 wiki_pages（category 分类 + FTS5 检索 + description 摘要 + status supersession）；检索走 `wiki_search`/`wiki_pages`/`wiki_page`，写回走 `wiki_page_add`/`wiki_page_update`（PATCH append ADD-only + hash 去重），自进化走 `wiki_evolve_trigger`（会话经验自动回写，费用门禁 + 规则闸门）
3. **官方权威**：涉及工具/SDK/API/框架 → 官方文档为准（不猜格式）
4. **联网兜底**：实现前先找轮子——GitHub 搜索 → 网页搜索 → 库文档，优先成熟方案（不重复造轮子）；JS 渲染/复杂页面用专业抓取工具（如 Firecrawl），不用裸 curl
5. **三连败升级**：同一问题重试 3 次无效 → 停止重试，按 官方文档 → GitHub Issues → 社区 → 重评估方案 升级（不要在同一方向蛮力试）

### 验收纪律

- 每完成一个任务：跑该模块测试，再进下一个
- pytest 分档（2026-08-13 用户定）：**常规改动**提交前跑相关模块测试（`python scripts/test_fast.py <关键词>` 或按 git diff 自动推导，秒级）；**全量 pytest** 只在里程碑/发布/跨模块重构后跑（约 10 分钟，零 LLM/网络消耗）；两种都**汇报 passed/failed 数字**，仅 exit code 不算证据（2026-08-11 用户纠正）
- 提炼相关改动必须真实 LLM 冒烟（`python scripts/e2e_smoke_v04.py`）+ 查 Server 日志无 `L1.5 输出解析失败` / `降级直存`（mock 全绿≠真实可用，见开发规范）
- 测 API 用 Python requests（git-bash curl 会破坏中文 UTF-8）

# SGME 接入纪律（SGME-ONBOARDING-v1）

你有一个长期记忆引擎 SGME（ShiGuang Memory Engine），运行在 NAS（192.168.10.10）。它的职责：把我们的会话提炼成标签化记忆，按场景注入回来，让你不再失忆。

**服务发现**（找不到时按序）：
1) 探测 http://192.168.10.10:9910/v1/health；
2) 失败读 ~/.sgme/install.json（地址/端口/Key 引用）；
3) 仍失败 → 向主人报告「SGME 未发现」。

**使用纪律**（五条铁律）：
1. 每轮对话结束 append 当前轮次——纯落盘零 LLM 成本，崩溃不丢；
2. 会话结束 refine_trigger(async_mode=true) 触发提炼；
3. 对话开始时 inject 按场景取画像 / search 检索相关记忆；
4. 主动关怀靠消费信号——**信号消费=主动关怀，谁消费谁标记**：拿到 care_* 信号后 signal_claim 原子认领 → 关怀用户 → signal_ack 回执（认领失败=已被其他 agent 消费，跳过即可）。获取信号两条路：
   - 短连接（无常驻进程）：每次对话开始 signal_pull 拉未消费信号；
   - 长连接（有常驻能力，**主动关怀首选**）：挂 SSE 事件流 `GET http://192.168.10.10:9910/v1/events/stream?subscriber_id=<你的agent_id>`（带 X-API-Key），care_*/memory_updated/anomaly_warn 一产生即实时推送 → 立即 claim→关怀→ack；断线重连带 Last-Event-ID 头补偿，不丢事件；
5. 对话开始时（或用户指定角色时）role_list 看可用角色 → role_assemble(role_id) 拿人设并按其说话——**换皮不换芯**，角色只是沟通外皮，记忆池不动。

**事件对接**（主动关怀的触发源，常驻 agent 必读）：SGME 事件三类——care_*（关怀：情绪/待办到期/过劳/每日）、memory_updated（记忆更新）、anomaly_warn（异常）。三种接法任选：
- SSE 长连 `GET /v1/events/stream?subscriber_id=<agent_id>`（实时推送 + Last-Event-ID 断线补偿）——**常驻 agent 首选**；
- 游标拉取 `GET /v1/events/pull?subscriber_id=<agent_id>`（持久游标，适合定时任务轮询）；
- MCP `signal_pull`（会话内短连接）。
SSE/pull 走 HTTP :9910 带 X-API-Key；signal_pull 走 MCP。

**通信渠道**（兜底铁律）：当前会话是兜底通信渠道——任何主动消息（关怀/提醒/告警等），无论是否已发送到其它通道（微信/飞书/Telegram 等），都必须在当前会话也发一条；此兜底持续生效，直到用户明确取消。

**强制查询**：涉及用户/项目历史事实的问题（之前/以前/上次/还记得…），必须先 search 再回答，不得直接说「不知道」；查询不到时如实说明"记忆库中未找到"。

**写入格式**：append 的 content 首行必须是 `# {ISO时间戳} {role}`（user/assistant），否则返回 422。

**批量提炼纪律**：≥20 文件必须分批（每批≤20）+ 批间 30–60 秒；429 不立即重试（交服务端 batch_scan 兜底）；永远 async 模式。

**接口**：HTTP API http://192.168.10.10:9910 ｜ MCP http://192.168.10.10:9913/mcp，请求头 X-API-Key（key 由主人配置：config/.env 的 SGME_ADMIN_KEY/SGME_AGENT_KEY，或管理员签发的 agt_* key；默认 dev key 仅限本机回环，远程调用一律 403）。

**历史会话补导入**：本适配器提供历史会话全量导入方法（把接入前的存量会话补进 SGME）：
   `D:/Projects/SGME/.venv/Scripts/python.exe D:/Projects/SGME/adapters/dsh/import_history.py`
   幂等可重跑（已导入的自动跳过）。需要补录历史时执行它即可，然后汇报导入数量。

> 注：以上路径为本机安装时生成；仓库迁移/克隆到其他机器后，重跑 `adapters/dsh/install.py` 即可刷新为本机路径。

---

以下为 Trae 提交信息规则（scene 块，仅 Trae 识别，其他工具忽略）：

--- scene: git_message ---
提交信息格式：`<type>: <中文描述>`（type ∈ feat/fix/docs/refactor/test/chore/perf/ci）；描述概括改动不罗列文件；有补充说明时与描述空行分隔；中文描述，术语保留品牌原名。
---
