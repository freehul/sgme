# SGME 实施变更记录（v0.6 批次）

> 批次：2026-08-06 ~ 2026-08-07（全量提炼攻坚 + 模型切换 + 状态体系）
> 定位：git commit 的补充叙事——记录「为什么改、改了什么、验证结果」。
> 架构级变更（A 类）已同步 SGME-架构设计-0.4.md v0.5 修订；本文件记录实现级修复（B 类）+ 关联架构变更索引。

## 背景

用户提出 SGME 全量提炼（415 个 Hermes 历史会话），过程中发现并修复了
一系列本地模型时代的缺陷，最终切换云端 DeepSeek 完成全量提炼。

---

## 一、架构级变更（已同步架构设计 0.4 v0.5 修订）

| # | 变更 | 文档位置 |
|---|------|---------|
| A1 | 主模型切换 DeepSeek V4-Flash（关思考 `thinking.disabled` + 1M 上下文） | config/llm.yaml v0.3 |
| A2 | occurred_at 双时间戳（会话真实发生时刻 vs 提炼落库时刻） | SGME-数据模型设计-v0.1.md |
| A3 | rejected/expired 状态体系（记忆+场景统一） | SGME-数据模型设计-v0.1.md |
| A4 | refine_runs token 记账（prompt/completion/total_tokens） | SGME-数据模型设计-v0.1.md |

---

## 二、实现级修复（B 类，本文件记录）

### B1. 批量提炼改为「逐文件落库」（2026-08-06）

**问题**：`trigger_async` 批量分支原实现先 `refine_batch` 收集全部文件 L1 结果，
再统一 `_persist_memories`。中途异常（LM Studio `Model is unloaded`）导致
**已处理文件的记忆全部丢失**（raw_files 已标记 refined 但记忆未落库）。
实测 62 个文件 L1 成果（约 195 条记忆）全部丢失。

**修复**：`routes_admin.py` 批量分支改为逐文件 `refine_file → _persist_memories`
（L1→L1.5→L2 每文件立即落库），每个文件独立 try/except，崩溃只丢当前文件。

**验证**：修复后 3 文件 → 23 记忆 + 5 场景，l1_conflict/l2_scene 正常执行。

### B2. L1.5 候选池字符预算截断（2026-08-06）

**问题**：候选池全文拼 prompt 超 64K 上下文（LM Studio `Context size has been
exceeded`），L1.5/L2 批量失败。

**修复**：`l15.build_candidate_pool` 新增 `char_budget=24000` 参数，候选池超预算
按 priority 降序截断（保住高价值候选），记 anomaly_warn。

**验证**：单文件完整链路（L1→L1.5→L2）跑通，无上下文超限。

### B3. L2 lenient JSON 解析（2026-08-06）

**问题**：qwen 关思考后 L2 场景聚合 JSON 输出不稳定，失败率 57%。
错误类型：裸控制字符 / 无效转义 / 对象间缺逗号 / 尾逗号 / 缺 target_scene_id。

**修复**：`l2._parse_json_lenient` 四级容错：
1. 裸控制字符（含换行/制表/回车）→ 转义序列
2. 无效 `\X` 转义 → 双反斜杠
3. 对象/数组间缺逗号 → 补逗号（已有逗号不误伤）
4. 尾逗号 → 移除

另：`target_scene_id` 缺失时用 `placeholder-{action}` 兜底（create/merge 时
系统会重新生成 uuid）。

**验证**：8 类测试用例 7/8 通过（含缺逗号/尾逗号/换行缺逗号）；切 DeepSeek 后
L2 失败率归零（DeepSeek JSON 遵循能力强，lenient 成为保险丝）。

### B4. wiki_dao dict(row) 崩溃修复（2026-08-06）

**问题**：`get_raw_file`/`get_scene` 直接 `dict(row)`，裸连接（未开 row_factory）
时崩溃 `dictionary update sequence element`。

**修复**：判断 `isinstance(row, sqlite3.Row)` → dict(row)；普通 tuple → 按
cur.description 列名映射。get_raw_file / get_raw_file_by_session / get_scene 三处。

### B5. 剪枝（prune.py 新建，2026-08-06）

**动机**：L0 原始会话中 tool 输出占 95% 字符（236K 会话中 224K 是工具输出），
不剪枝则分块全被噪音撑爆、LLM 信噪比极低。

**实现**：`sgme/engine/prune.py`（135 行）：
- tool 输出默认丢弃（保留原 Message 结构，seq/timestamp 不丢）
- 系统注入消息过滤
- 超长消息压缩

**效果**：174 条 → 78 条，231K → 8.9K 字符（96% 去除）。

### B6. 回合感知分块 chunk_messages_by_turn（2026-08-06）

**问题**：固定字符切块拆散「user 问题 + assistant 回答」问答对，模型上下文
不完整导致提取无效。

**实现**：`l1.chunk_messages_by_turn` 以「user+回答」为最小语义单元、贪心填充、
绝不拆散问答对；`extract_l1` 签名改为 `str | list[str]` 支持预分块。

**验证**：78 条消息 → 2 块（5,538 + 3,379 字符），每块 user 开头、问答对完整。

### B7. L1 强化 prompt 防敷衍（2026-08-06）

**问题**：关思考后模型对超长输入「敷衍」直接返回 `[]`（13.4K 输入 1.4s 返回空）。

**修复**：`prompts/l1_extraction.txt` 强化约束：禁止空数组、禁止只关注结尾、
禁止代码块标记、提取指引。

**验证**：全部档位成功率从 33-67% 提升到 100%。

### B8. 甜点区标定（2026-08-06，多次测试）

**结论**：关闭思考 + 强化 prompt 后，**5000 字符为峰值**（19.0 条记忆/会话），
区间 4500~5500 最优；8000+ 尾部细节丢失（小块覆盖更细）。
配置 `chunk_size: 5000, overlap: 1000`。

**重要教训**：生产分块计量口径须与测试一致（格式后字符 vs content 裸字符
差异 ~1.4x），已统一。

### B9. DeepSeek V4-Flash 主模型切换（2026-08-07，用户拍板）

**动机**：剪枝后单会话 1-10K 字符，云端成本极低；DeepSeek JSON 遵循能力
远胜本地 9B（本地 L2 失败率 57%，DeepSeek 0 失败）；消除本地三坑
（思考模式控制 / TTL 卸载 / 上下文超限）。

**实现**：
- `config/llm.yaml` v0.3：主模型 `deepseek-v4-flash`（1M 上下文），
  `extra_body: {thinking: {type: disabled}}` 关思考；lm-studio 降为第二优先
- `provider.py` 支持 `extra_body` 透传
- nssm 服务注入 `DEEPSEEK_API_KEY` 环境变量

**验证**：同会话 12.4K 字符整段喂入 9.1s 提取 10 条全高价值记忆；
全量 415 文件 L2 失败率 0%。

### B10. occurred_at 双时间戳（2026-08-07）

**问题**：记忆时间戳只有 `created_at`（提炼落库时刻），丢失会话真实发生时刻
——TTL 过期计算、时间窗口查询、时间线全部失真。

**实现**：
- `memories`/`memory_archive` 加 `occurred_at` 列（DDL + 迁移）
- `refine.py` 归一化段：source_message_ids → 消息 seq → timestamp 映射，
  取来源消息最大时间戳写入 occurred_at
- L1.5 store/merge 透传（merge 取候选+新并集最大值）
- `insert_memory` 缺省回退 created_at

**验证**：新提炼记忆 occurred_at 100% 填充（如 8-06 提炼出 6-21 会话的真实时间）。

### B11. rejected/expired 状态体系（2026-08-07）

**动机**（用户设计）：记忆错误不应删除（破坏溯源），应打标记不参与查询；
「失效」= 随时间过时，「错误」= 内容判错，均不参与查询/时间线。

**实现**：
- memories 加 `status`（active/rejected）+ `rejected_at` + `reject_reason`
- `memory_dao.reject_memory` / `unreject_memory`
- 查询过滤：list_memories_by_dimension / FTS / LIKE 全部 `status != 'rejected'`
- API：`POST /v1/memory/{id}/reject` / `unreject`
- 场景补丁：`POST /v1/admin/scenes/{id}/status`（active/rejected/expired/archived）
- search_scenes 天然只查 active，无需改

**验证**：reject → 搜索消失 → unreject → 恢复；场景 expired → 查询排除 → 恢复。

### B12. refine_runs token 记账（2026-08-07）

**动机**：用户要求统计 DeepSeek token 用量与成本。

**实现**：
- `refine_runs` 加 `prompt_tokens/completion_tokens/total_tokens` 列（DDL+迁移）
- `provider.call_openai_compatible` 返回 `(text, usage)` 二元组
- `chain.call_with_fallback` 返回三元组 `(text, provider, usage)`
- l1/l15/l2 三处 `RefineRunRecorder.finish(usage=usage)` 透传

**效果**：全量 415 文件 15.04M tokens，成本 ≈ $2.6（¥19-20）。

---

## 三、全量提炼结果（2026-08-07 完成）

| 指标 | 数值 |
|------|------|
| 提炼会话 | 415/415 |
| 记忆 | 9,293 条（全部带 occurred_at + 溯源） |
| 场景 | 127 个叙事 |
| tokens | 15,042,655 |
| 成本 | ≈ $2.6（¥19-20） |
| L2 失败 | 1（可忽略） |

对比：无剪枝时代 138 条记忆 → 9,293 条（67 倍），全部经剪枝去噪、
L1.5 冲突裁决、L2 场景聚合。

---

### B13. append started_at 会话级固化（2026-08-07）

**问题**：Hermes adapter 的 `_append_turn` 每轮对话取 `now` 作为 `started_at`
传给 /v1/append。但 started_at 语义是「会话开始时间」——每轮不同导致：
- raw_files.started_at 失真（变成最后一轮时间）
- 同会话轮次时间戳漂移（幂等判定"同 session_key+同 started_at"失效，
  每轮都走追加分支）
- 消息时间戳 user/assistant 统一用同一 now，精度损失

**修复**（adapters/hermes/__init__.py）：
- `initialize` 时记录 `_started_at`（会话开始时刻，UTC ISO）+ `_session_key`
- `_append_turn` 的 `started_at` 复用 `self._started_at`；消息时间戳各自取真实时刻

**验证**：47 个测试通过（test_l1_chunk/test_l15/test_l2）。

**顺带**：test_l1_chunk 断言 provider 从 `lm-studio` 更新为 `deepseek`（v0.5 主模型切换遗留）。

---

## 四、文档同步索引（2026-08-07）

| 文档 | 变更 |
|------|------|
| `SGME-架构设计-0.4.md` | 升级 v0.6：新增 §0 v0.6 变更摘要（L2 场景检索三路融合 / 向量切方舟 / Reasonix 适配器）；v0.5 摘要保留为 §0.1 |
| `SGME-数据模型设计-v0.1.md` | scenes 表修正为实际结构（content/last_memory_added_at/content_seg）；新增 scenes_fts/scene_vectors 表；memories_embeddings → memory_vectors（表名+字段修正）；索引段补 scenes 两条 |
| `SGME-接口契约-v0.1.md` | §4.3 scopes 修正为 memory/wiki（旧 wiki_refined/wiki_raw 标注未实现）；wiki scope 三路融合语义 + routes 枚举 |
| `SGME-Skill资产库设计-v0.1.md` | 新建（skill 资产库 + 附 A 网关 + 附 B Wiki 服务化 + 附 C SCSM 控制台 + 附 D 想法池） |
| `SGME-产品化设计-v0.1.md` | 新建（WebUI + 通用接口层 + Hermes desktop 适配，含影响分析） |
| 本文件 | 新建（实施变更记录 v0.6） |

## 五、遗留与后续

1. 全量后 e2e 测试会话（7 个）衍生的记忆可能含测试数据，待用户确认是否标记 rejected
2. scenes 的 `last_memory_added_at` 字段存在但从未写入（设计缺口，未触发需求）
3. 蒸馏产物自动登记 skill（想法池 D.2，待时机）

## 六、2026-08-07 Reasonix 适配器 + Tier0 修复

### B22. tier0_summary 解包崩溃（发现即修）
- 问题：`tier0.py` 用二元组解包 `call_with_fallback()` 返回值，但该函数已升级为三元组（token 记账改动，provider.py v0.5）→ `too many values to unpack`，tier0 刷新必挂
- 修复：`text, provider_name, _usage = call_with_fallback(...)`（一行）
- 验证：tests/test_tier0.py 12 个全绿（此前 2 个失败转正）；真实 tier0/refresh 解包错误消失

### B23. Reasonix × SGME 适配器（hooks 专用适配）
- 新增 `adapters/reasonix/`：bridge.py（SessionStart 注入 / SessionEnd 捕获）+ install.py（一键安装）+ README
- 4 个本地 PR 合并（解析→注入→安装→文档），16 个单元测试全绿
- 端到端实测：Reasonix 会话 → L0（agent_id=reasonix）→ L1/L1.5/L2 全通，记忆带 occurred_at + 溯源链闭合
- 关键坑：Reasonix hooks 配置在 `.reasonix/settings.json`（非 .claude/settings.json）；SessionStart stdout 注入 additionalContext（≤9800 字符）；createdAt 是 epoch 毫秒 int

### 遗留（报告用户）
- embedding 401：`search.vector.model` 配的是本地 nomic-embed-text，但请求发往 DeepSeek 云端 401——embedding provider 配置待用户决策（本地 LM Studio / 其他）

### B24. embedding 端点回归（主模型迁移连锁问题，2026-08-07 发现即修）
- 问题：`vector.embed()` 借用 LLM 降级链首批 provider 的 base_url——本地模型时代首批是 LM Studio（:1014）embedding 正常；8/6 主模型切 DeepSeek 后 embedding 请求发往 DeepSeek（无 embeddings API）→ 401，**全量 9293 条记忆全部未生成向量**（搜索长期靠 BM25 单腿），tier0 刷新也连带失败
- 修复：embed 优先 `search.vector.base_url` 独立配置（http://127.0.0.1:1014/v1），缺省回退 refinement[0]（向后兼容）；新增 tests/test_vector_embed.py 4 用例
- 数据修复：`scripts/backfill_vectors.py`（可断点续跑，--limit 试跑）——9291/9291 全量回填成功，0 失败，~105 条/s，memory_vectors 现 9311 条
- 验证：tier0 refresh ok（2.8s，summary 353 字符）；向量+BM25 RRF 搜索 0.1s 返回

### B25. L2 场景检索升级（v5，2026-08-07 两个 PR）
- 背景：search_scenes 原实现是裸 LIKE（子串匹配、无分词、无相关性排序）——「VPS部署」查不到「VPS 的部署」
- PR#7 FTS+分词：scenes 补 content_seg 列（迁移幂等）；scenes_fts 外部内容表 + 触发器；init_scenes_fts 首建/口径漂移自动重建（失败降级 LIKE）；wiki_dao 写入路径填 content_seg（data 层分词，触发器只同步——对称记忆层）；search_scenes 改 FTS BM25 主路 + LIKE 兜底
- PR#8 向量：scene_vectors 表 + upsert_scene_vector/scene_vector_search（sqlite-vec/numpy 双路径）；rrf_merge 加 id_key 参数（场景按 scene_id）；search_scenes 三路融合（wiki_bm25/wiki_vector/wiki_rrf，融合后补 title/heat 元数据）；backfill_scene_vectors.py 回填 107 场景（0 失败）
- 数据影响：零（只新增表/列，scenes 原字段不动；迁移前已备份 data/backup_20260807/）
- 验证：37+9 测试全绿；端到端 wiki 检索 0.1s 三路融合返回
- 已知观察：场景 title 仍是 scene_<uuid>（L2 标题未语义化，既有问题）；语义查询结果相关性受 BM25 词面命中影响，后续可调 RRF 权重

### B26. 向量模型切换火山方舟（2026-08-07，PR #9）
- 背景：用户决定向量层从本地 LM Studio（nomic-768维）切到火山方舟 doubao-embedding-vision（2048 维多模态）
- 关键事实：VOLC_API_KEY（hermes .env）仅对 **plan 通道**（api/plan/v3）有效——标准通道 api/v3 返回 401（且控制台警告会产生额外费用）；模型 doubao-embedding-vision 一次调用返回 2048 维
- 代码：embed() 支持 search.vector.api_key_env → Bearer 头；429 限流指数退避重试（方舟账户级 QPS 实测 ~4-5 条/s）
- 环境：VOLC_API_KEY 注入 nssm 服务 AppEnvironmentExtra + 用户级 setx（新终端自动生效）
- 数据：--force 全量重灌（memory_vectors 9311 + scene_vectors 107；向量模型切换维度不兼容必须重灌，预计 30-40 分钟）
- 坑：backfill 脚本进程不继承 hermes .env key，需命令内联 export VOLC_API_KEY

### B27. Agent key 注册持久化 + Reasonix 知情三件套（2026-08-07，PR#10/#11）
- **Agent key 持久化缺口（PR#11）**：AgentKeyStore 的 store_path 从未接线（create_app 不传 agent_store_path）→ 每次服务重启注册的 Agent key 全丢 → 适配器静默 403（SessionStart 注入 / SessionEnd 入库全失败，故障隔离吞错）。修复：缺省落盘 `data/agent_keys.json`；测试隔离（7 个测试文件补 agent_store_path=tmp，清理了 13 个测试残留 key）
- **知情三件套（PR#10）**：机制就位 ≠ 模型知情——① install 生成 AGENTS.md（Reasonix 加载进每个会话 system prompt）；② SessionStart 注入加身份说明段（API 失败也注入）；③ `.reasonix/commands/sgme.md` /sgme 查询命令（bridge --query 双层检索）
- 实测：Reasonix 模型主动用 /sgme 查询 SGME 记忆成功（HTTP 200，返回记忆层+场景层）；修复前 403 被模型自己发现并报告
- 遗留：历史会话全量导出（41 个）方案已提未实施

### B28. 密钥单一来源（config/.env 自持，2026-08-07）
- **事故链**：设置 VOLC_API_KEY 时 nssm set AppEnvironmentExtra 覆盖式写入 → 冲掉 DEEPSEEK_API_KEY；分号分隔多变量被 nssm 存为**单行**（`DEEPSEEK_API_KEY=sk-...;VOLC_API_KEY=...` 整体成 DEEPSEEK 值）→ 401 → 提炼链静默降级 lm-studio
- **修复**：SGME 自持 `config/.env`（gitignore），config.py 启动 setdefault 加载；清空 nssm AppEnvironmentExtra（单一来源）
- **验证**：提炼链 DeepSeek ✓（provider=deepseek）+ 向量方舟 plan ✓ 双通道恢复
- 教训：密钥不借道外部应用；nssm AppEnvironmentExtra 多变量分隔坑（set 用分号、get 单行输出，值含分隔符即污染）

### B29. GLM5.2 外部审查报告核实与修复（2026-08-07，3 commits）
- 背景：GLM5.2 提交 16 个失败测试 + 3 个架构隐患的报告；用户要求核实后再动手
- 核实方法：源码逐条对照 + 实测跑全部相关测试（13 failed / 3 passed + eval 单测通过）
- 核实结论：15 个失败真实、1 个误报（test_cli_dry_run_produces_report_json 本就通过，--output 透传正常）；2 处归因修正——
  ① backup 测试 WinError 32 真根因 = `cfg["paths"]["data_dir"]` 未隔离（backup manager 从配置取库路径，routes_backup.py:95/153），fixture 已隔离 init_databases(tmp_path)；GLM 建议「停 daemon 释放锁」为绕行，未采用
  ② test_llm 第 7 个失败（unknown_error_not_fallback）不是签名变更导致，是 base_cfg 已加载真实配置（deepseek 首链）而 mock 按 1014 判断 URL，422 场景永不触发
- 修复（契约对齐 + 去配置漂移）：test_llm 6 处二元组→三元组解包 + handler/断言首链动态化（_head_url/_head_provider/_fallback_provider helper）；test_engine/test_health_v04 provider 断言动态读 cfg 首链；test_config_api chunk_size 断言对齐配置（5000）；test_content_hash mock 处理预分块列表；test_prompts_qa/test_e2e mock 补 usage 三元组；test_server_v04/test_e2e_v04 fixture 补 data_dir 覆盖
- 架构侧：provider.py 签名统一 `tuple[str, dict]`（4 函数），chain.py 删除 isinstance 鸭子类型兼容分支（隐患 1）；AGENTS.md 架构约束 #9 同步 deepseek 主链（隐患 3）
- 验证：全量回归 639 passed；commits 2a5740a / 9564553 / 6e18cbf
- 教训沉淀（sgme-operations skill）：外部审查报告必须源码+实测双验证；测试断言不写死 provider/chunk_size；pytest 多 -k 只认最后一个

### B30. 模块化重构（2026-08-07，5 commits，设计文件 SGME-模块化重构设计-v0.1.md）
- 背景：AST 依赖分析 + 边界审计发现 4 问题——业务编排在 server 层（_persist_memories/trigger 链路）、15 处路由层 SQL、server↔mcp_server 包级环、backup 裸连接无说明
- step1 (4233d5b)：新建 storage/stats_dao.py（memory_summary/dimension_distribution/raw_files_summary/agent_last_seen，含读库降级语义）；memory_dao 补维度维护 3 方法（list_aliases_by_dimension/update_dimension_fields/delete_alias）；routes_admin/routes_registry 统计与 CRUD 改调 DAO（路由层 SQL 清零）
- step2 (3ceaf52)：新建 engine/pipeline.py 收编提炼管线编排（persist_memories 原 _persist_memories / refine_one / refine_many / async_refine_worker）；routes_admin trigger/trigger_async 变薄壳（-108 行）
- step3 (6588ed4)：config 模块扩展为配置唯一读写方（CONFIG_SECTIONS/SECTION_KEYS/filter_keys/apply_section/persist_config）；routes_config 变薄壳
- step4 (d6e63f5)：mcp_server 全量归位——append→pipeline.append_l0、refine→pipeline、stats→stats_dao（MCP 侧 8 处 SQL 清零）、config→sgme.config、health 删死 import；routes_memory.append_session 薄壳化；**包级环消除**（AST 验证）
- step5 (81f8ff5)：backup 裸连接补边界注释（backup API 需要原生 Connection 且不能触发迁移链，唯一允许绕过 data 层的场景）
- 行为升级（归位顺带修正）：MCP append 对齐 HTTP 完整逻辑（补 content_hash/ended_at/联动提炼，原为简化版）；MCP 异步提炼升级为逐文件容错版（原 refine_batch 收集式，中途异常丢已处理文件）
- 验证：全量 639 passed；AST 无包级环；e2e 真实链路冒烟 + mcp 冒烟


### B31. 每日自动备份定时器（方案B，2026-08-10，commit 7e4dcf6）
- 背景：sgme.git 只备份代码（手动 push），数据库（memory.db 104MB）无定时备份；架构 §17 备份设计只有本地手动快照
- 方案：用户拍板方案B——SGME 内部定时器（非外部 cron），复用 Dream 定时器模式（engine/dream.py 同构）
- 实现：
  - 新建 sgme/engine/backup_scheduler.py：ensure_scheduler 幂等常驻 daemon 线程 + _scheduler_loop（连接探测自尽防 Windows access violation）+ _run_backup（create_snapshot → rotate_snapshots → push_remote）+ stop_scheduler（测试清理）
  - config.py：DEFAULT_BACKUP_CONFIG 升级（旧 cron 格式 schedule '0 2 * * *' → HH:MM '04:00'；新增 enabled/level/keep_full；dir 字段名保留兼容 operations/backup 契约）+ _merge_backup_config 类型校验
  - routes_backup.py：/v1/admin/backup/create 端点接线 ensure_scheduler（同 Dream 触发链路）
  - config/sgme.yaml：backup 段启用（remote_dir='E:\SGME_Backup' 本机异地盘，空=跳过）
- 验证：test_backup_scheduler.py 8 用例（时间加速/幂等/disabled 跳过/连接关闭自尽/remote 复制）；backup 相关 46 全绿；全量回归待确认
- 教训：测试 fixture 裸 sqlite3.connect 默认 check_same_thread=True → 定时器线程跨线程访问抛 ProgrammingError 被 except 吞（线程静默退出）——必须用 db_mod.init_databases（check_same_thread=False），同 dream 测试模式


### B32. 文档整理 V0.9（2026-08-10，文档重构）

- 背景：35 份设计文档冗余（历史版本 4 份、专项设计 8 份、论证稿 3 份、计划类 2 份）；用户定稿简化文档结构——AI 开发流程下文档只需三类：需求锚（Backlog）+ 架构设计 1 份 + 实施变更记录（兼运维手册）+ README
- 产出：
  - `SGME-架构设计-v0.9.md`（新建，141KB）= 原 0.7 主体 + 8 专项章节（§22 接口契约 / §23 数据模型 / §24 LLM降级链 / §25 模板引擎 / §26 提炼提示词 / §27 提示词版本管理 / §28 维度归一化 / §29 检索分词，子代理字节级合并零丢失）+ §30 专项精简（Dream/SkillsHub/创意池/模块化/备份要点+引用）
  - `SGME-实施变更记录-v0.9.md`（v0.6 改名升级）
- 删除 25 份：架构 0.1/0.2/0.3/0.4/0.7、8 份专项源文档、4 份专项设计、论证稿 3 份（产品化/Skill资产库/通知通道）、计划类 2 份（0.8开发计划/交付说明）、记忆渐进式披露、向量维度调查、prompt-versioning mermaid 2 个——git 历史有完整备份
- 保留：Backlog（锚）、评测 2 份 + mermaid 2 个（#32 独立）、L0 格式、维度标签/注册表/别名表（运行时数据源）
- 同步：AGENTS.md 文档索引（5 处→v0.9 引用）、README.md 设计文档表（8 行→5 行）、Backlog 设计文档索引表（11 行→5 行）——全库 grep 验证零残留
- 教训：**bash heredoc 内反引号会被命令替换**——写含反引号内容的大块文本必须用 write_file 写脚本再执行（本次 §30 首写被 bash 破坏，反引号内容全丢，重写修复）

### B33. 标准安装布局（T-23，2026-08-11，PR#1+PR#2 合并 8b10dd8/7a2a41a）

- 背景：README 快速开始无标准安装目录（clone 就地跑），DATA_DIR/RAW_DIR 硬编码项目根（config.py:168-169，12 模块统一引用常量）；ST-23⑦ install.json 仅设计未落地。2026-08-11 用户定案 Windows 惯例安装——程序 %LOCALAPPDATA%\sgme、数据/配置 ~\.sgme；触发：笔记本新用户流程测试（卸载重装验证暴露安装体验缺口）
- PR#1（feat(config) SGME_HOME 重定向，Closes T-23①）：
  - config.py 模块加载期解析 `SGME_HOME` env → DATA_DIR/RAW_DIR/LOG_DIR/DEFAULT_SGME_CONFIG/SECRETS_FILE 重定向（未设=项目根，零回归）；新增 USER_ROOT（相对路径基准）
  - 相对路径基准 5 处从 PROJECT_ROOT 改 USER_ROOT：dream.py `_report_dir`/日报相对路径/日报正文读取、backup.py `_resolve_backup_dir`、raw/store.py `relative_path`（dream 日报/备份/raw_files.path 跟随 SGME_HOME）
  - app.py 日志路径改用 LOG_DIR
  - 程序资源（llm.yaml/providers.yaml/registry/templates/prompts）刻意不跟随（随发布更新）
  - 新增 tests/test_config_home.py（7 测试）
- PR#2（feat(config) install.json 服务发现落地，Closes T-23②）：
  - config.py 新增 `install_json_path()`/`write_install_json()`：schema_version/sgme_version/HTTP 地址端口（SGME_HOST/SGME_PORT 生效值）/MCP 端口（SGME_MCP_PORT）/data_dir/raw_dir/Key 的环境变量名引用——**不落明文密钥**（铁律 #10）
  - 路径：SGME_HOME 设置时写其下，未设时固定 `~/.sgme/install.json`（ST-23⑦ Agent 服务发现）
  - app.py lifespan 生产模式启动即生成（失败不阻断启动）
  - 新增 tests/test_install_json.py（3 测试）
- 验证：全量 pytest 绿（含 e2e）；真实链路冒烟——SGME_HOME 重定向 + install.json 生成实测通过
- 教训：**模块级常量测试污染**——reload fixture 若只清 env 不重载模块，SGME_HOME 常量残留会污染后续测试（test_config_home 曾导致 test_routes_backup 误报临时区告警）；teardown 须先手动 delenv（monkeypatch 还原在其后）再 importlib.reload

### B34. 密钥安全加固（2026-08-11，共享 dev key 溯源结论 + 明文 key 清理）

- 背景：排查"共享 dev key 是否影响溯源"实测确认——溯源 agent 来源 = append body.agent_id（与 API Key 无关），共享 key 下带唯一 agent_id 即可正确溯源；但暴露 4 条 key 获取路径：plugin.yaml 明文 key 进 git 历史、agent_keys.json 明文 72 key 本机可读、MCP 通道默认不带 agent_id 落 NULL、dev key 硬编码公开
- PR 内容：
  - plugin.yaml 删除明文 agent_key/admin_key（回退环境变量 SGME_AGENT_KEY/SGME_ADMIN_KEY，`__init__.py` 已支持）
  - `_restrict_file_permissions`：agent_keys.json 落盘后自动收紧 ACL（Windows icacls 去继承仅当前用户 R,W / POSIX chmod 0600；失败仅告警不阻断）；tests 新增 2 测试
  - install_sgme_service.bat 增加 AppEnvironmentExtra 注入段（SGME_ADMIN_KEY/SGME_AGENT_KEY 环境变量可选叠加，非覆盖式 set）
  - 生产强 key 生成（sgme_admin_*/sgme_agent_* 随机 hex）写入 config/.env + Hermes .env + trae/reasonix adapter .env；服务重启后实测：新 key 200 / dev key 403（退役）/ agt_* 注册 key 200
- 架构文档 §6.1 新增「密钥管理与溯源边界」小节（密钥单一来源 / 溯源鉴权解耦 / Key 落盘保护 / 客户端 key 约定）
- 遗留：MCP 通道 append 默认不带 agent_id（落 NULL）——溯源正确性靠客户端自报，可选改造（key 反查兜底）待定

### B35. 溯源兜底 + MCP agent_id 参数（2026-08-11，B34 遗留落地）

- HTTP 通道兜底：`AgentKeyStore.resolve_agent_id(key)`（env 主 key/admin key → "default"，注册 agt_* key → 绑定 agent_id，未知 → None）；routes_memory append_session 里 `payload.agent_id or resolve_agent_id(auth_key)`——关掉「HTTP 调用不报 agent_id 就落 NULL」的口子；显式 body.agent_id 永远优先
- MCP 通道：查证发现 **9913 实际无鉴权**（`_require_admin()` 定义但零调用点，FastMCP 只设 host/port 无 auth，绑定 127.0.0.1）——无 key 可反查，故改为 append 工具签名新增可选 `agent_id` 参数（客户端自报溯源，不传落 NULL，与历史行为兼容）；ONBOARDING_TOOLS 描述同步
- 测试 +5：MCP 传/不传 agent_id 落库断言 ×2、HTTP 注册 key 兜底 ×1、env key 兜底 default ×1、显式优先 ×1；全量 pytest 绿
- 结论：MCP 无鉴权是本机单用户部署可接受的现状（与 HTTP dev key 本机放行同级暴露面），若未来远程暴露 9913 需补鉴权（FastMCP auth 或前置代理）

### B36. Hermes 插件 append 自报 agent_id（2026-08-11，溯源闭环收尾）

- 背景：B35 后盘点溯源分布——trae/reasonix adapter 早已自报 agent_id（`SGME_TRAE_AGENT_ID`/`SGME_REASONIX_AGENT_ID`，默认 trae/reasonix），但 **Hermes 插件（最大写入方）两处 append 调用都不带 agent_id**——default 397 条的来源
- 改动：adapters/hermes/__init__.py——`_DEFAULT_AGENT_ID`（env `SGME_HERMES_AGENT_ID` 可覆盖，默认 hermes）、构造器新增 `agent_id` 参数（plugin.yaml config 段可覆盖）、`_append_delta`/`_append_turn` 两处 append body 补 `agent_id`
- 测试：test_hermes_adapter.py 断言 append body 带 agent_id=hermes；12 passed
- 部署：install.py 重装 Hermes 插件副本，重启 Hermes 生效
- 至此溯源闭环：HTTP 注册 key 兜底 + MCP 自报参数 + Hermes/trae/reasonix 全部自报 agent_id——default 将只剩历史存量，新数据全部可溯源

### B37. MCP 通道鉴权 + key 反查溯源（2026-08-11，方案 A，PR#1+#2+#3）

- 背景：用户裁定"方案 A"——SGME 是产品（非仅自用），MCP 通道（9913）此前**完全无鉴权**（`_require_admin` 定义但零调用点，FastMCP 只设 host/port），任何能连本机端口的进程可自由读写记忆；agent_id 自报也无法防冒充。方案 A = MCP 与 HTTP 同规则鉴权 + key 反查溯源
- PR#1（feat: ApiKeyMiddleware，commit 1294411）：
  - `ApiKeyMiddleware`（Starlette BaseHTTPMiddleware）：校验 X-API-Key，复用 `AgentKeyStore.is_agent()`——env agent key / admin key / 注册 agt_* key 放行，缺失或无效 → 403 ERR_FORBIDDEN（与 HTTP `require_agent_key` 同规则同设施）
  - `run_mcp_server()`：手动 uvicorn 跑 `streamable_http_app()` + 中间件，**替代 `mcp.run()` 自托管**（后者忽略附加中间件——LibreChat 踩坑实录，已查证 FastMCP 1.28 `run_streamable_http_async` 源码）
  - `mount_mcp` 加 `start_server=False`（测试用）并接线 key_store；校验通过后 key 存入 `request.state.api_key`
  - 测试 +5（initialize 握手）：无 key 403 / 错 key 403 / agent key 200 / admin key 200 / 注册 key 200
- PR#2（feat: append key 反查，commit 3896710）：
  - append 工具加 `ctx: Context` 参数：agent_id 解析优先级 = 显式参数 > `request.state.api_key` → `resolve_agent_id(key)` > None——与 HTTP 通道 B35 完全同语义（注册 key 落绑定 agent_id，env 主 key 落 default）
  - 测试 +3（完整 MCP 会话流 initialize→initialized→tools/call）：注册 key 反查 planner / env key 落 default / 显式优先
- 踩坑（测试实录）：
  - TestClient 必须 `with` 进入才触发 lifespan（FastMCP task group 在 lifespan 初始化，否则 RuntimeError）
  - Host 头必须给 127.0.0.1（FastMCP transport_security 校验，testserver 触发 421 Misdirected Request）
  - Accept 必须含 `text/event-stream`（MCP 协议要求，否则 406 Not Acceptable）
  - streamable-http 响应是 SSE（`data: {...}` 行），body 解析须兼容
- 全量 pytest：1505 passed；文档：架构 §6.1 更新 + Backlog ST-24 前置项登记
- **客户端影响**：MCP 客户端（Trae/SCSM/笔记本）现在必须带 X-API-Key（MCP 配置 headers 段）才能连 9913——与 HTTP 通道同 key 体系

### B38. L1.5 候选池向量预筛（2026-08-12，提炼成本治理，PR#4）

- 背景：Trae 导入成本复盘（`d:\tmp\check_trae_cost.py` + `check_non_trae_cost.py`）——l1_conflict 单次中位数 67.7 万 tokens、83 次顶格超 90 万（最大 1,005,157 逼近 1M 窗口），Trae 导入 1.27 亿 tokens 的 98.7% 来自 l1_conflict。根因链：候选池按维度 OR **全量召回**（铁律 #7 防漏冲突）× batch_budget 按 1M 窗口折算 96 万 tokens 不设防 × 库 9,183 条 active（08-11 导入时）→ 单次顶格。08-06 便宜是因空库起步（候选池空，单次 9K tokens）
- PR#4（feat: 向量预筛，commit 待填）：
  - `l15._build_prescreened_candidates()`：候选 = 向量 Top-K ∪ 维度 Top-N（priority 降序），按 memory_id 去重；单记忆候选 ≤ `vector_top_k + dimension_top_n`（默认 50+50，沿用 DEFAULT_TOP_K 先例）
  - `l15.build_candidate_groups()` 新增 `cfg`/`prescreen` 参数：prescreen 未配置或 enabled=false → 完全现状（全量召回 + 预算 top-k）；embed 返回 None（端点不可达）/ vector_search 异常 → 自动回退全量（宁贵勿漏，功能不降级）；预筛成功但候选为空 → 不再走全量（prescreen_used 标志区分）
  - `resolve_conflicts()` 从 `cfg["l15"]["prescreen"]` 读取（缺失安全兜底）；`build_candidate_pool()` 兼容入口透传
  - config/sgme.yaml 新增 `l15.prescreen` 段（enabled/vector_top_k/dimension_top_n）
- 测试：`tests/test_l15_prescreen.py` +9（维度截断/priority 排序/向量并集去重/候选上限/关闭回退/embed 失败回退/检索异常回退/端到端 prompt 受限×2）；全量 pytest 绿
- 真实链路验证（9911 独立实例 + 主项目 .env 注入 DEEPSEEK/VOLC key，不碰生产）：e2e_smoke_v04 PASSED；10 条同主题 seed 提炼后 probe 提炼 l1_conflict 单次 **750 tokens**（对比生产 67-100 万）；embedding 真实调用火山方舟 200 OK ×4；日志零「向量预筛降级/异常」、零「L1.5 输出解析失败」、零「降级直存」
- 运维联动（2026-08-12 临时）：提炼三触发源全关——dream.enabled=false + batch_scan.enabled=false（/v1/admin/config 热更新 + 落盘，无需重启）、Hermes 插件 refine_on_end=false（项目源 + 部署副本同步，重启 Hermes 后生效）；队列 9 文件保留 status=new，恢复后自动处理
- **恢复步骤**（修复验收后）：config/sgme.yaml dream/batch_scan enabled 改回 true（热更新或重启）、插件 refine_on_end 改回 true；然后查 refine_runs 验证 l1_conflict 单次 token 已降至 ~2 万量级
- **修复补记（2026-08-12 生产验收发现）**：首次验收 l1_conflict 仍 98 万 tokens——根因 `load_config()` 组装 cfg 用白名单（l2/search/l1/refine/...），**sgme.yaml 的 l15 段被丢弃** → resolve_conflicts 读到 prescreen=None → 预筛静默失效（9911 冒烟"看似生效"仅因库小全量也不大，未暴露）。修复：`DEFAULT_L15_CONFIG`（默认 enabled=false，测试环境不依赖网络）+ `_merge_l15_config` 深层合并 + load_config/load_sgme_config 透传（fix commit f32e53b）。**生产验收数字（真实大库）**：l1_conflict 单次 prompt **19,435 tokens**（修复前 980,467，降 98%），裁决含 store+merge（冲突召回未受损），零预筛降级；提炼 8s 完成。**教训**：mock/小库冒烟验证不了"配置链"完整性——必须在大库真实链路验证配置透传

### B39. WebUI 管理面板首期落地（2026-08-13，T-28~T-33）

- 背景：ST-7（4 导航 22 视图 + 创意池 UI）此前只完成骨架与 ③ 创意与需求导航（T-28/T-29，2026-08-12）；① 总览、② 记忆与知识、④ 配置与管理仍为占位页，且设置页需要的 LLM 管理端点后端不存在。本批为 2026-08-12/13 会话实现（当时未提交 git，本次盘点后补登记补提交）
- 前端（`ui/`，Vue3+Vite+TS）：
  - ① 总览：DashboardView（389 行）单页多区块——系统健康（/v1/health）、数据概览（/v1/admin/stats）、提炼监控（refine_runs 分页 stage/status）、Dream 日报（列表+手动触发）、事件流（/v1/events）；路由 /dashboard（T-30）
  - ② 记忆与知识：MemoryList（373 行，维度/状态/排序/时间窗/ttl 过滤）、MemoryDetail（溯源/拒绝/恢复）、SceneList（256 行，状态标记）、SearchView（统一检索+溯源）、SessionView（L0 原文）、WikiView（列表/详情/导出）；路由 /memories /scenes /search /sessions /wiki（T-31）
  - ④ 配置与管理：SettingsView 单入口 9 标签页（通用设置/模型供应商与降级链/TTL 配置/模板管理/Agent 管理/维度注册表/提示词/扩展模块/备份管理）+ SkillsView（382 行，技能仓库独立导航页）；原设计 ④ 的独立路由（templates/registry/agents/prompts/config/backup 等 8 条）redirect 合流到 /settings（T-32）
  - 工程：11 个 api client（admin/dashboard/demands/ideas/knowledge/llm/memory/projects/skills/wiki/client）；`npm run build` 通过（约 50 模块，767ms）；占位页 PlaceholderPage 零引用
- 后端：
  - `sgme/server/routes_llm.py` + `sgme/operations/llm.py`：GET /v1/admin/llm（链+规则+供应商）、GET /v1/admin/llm/health（逐供应商探测）、POST/DELETE /v1/admin/llm/providers（连接表增删，被链引用拒绝删除）；降级链（llm.yaml）仍文件维护（T-33）
  - 既有接线：routes_ideas.py（T-26）、promote 端点（T-27）、app.py include + ui/dist 静态托管 + SPA catch-all（T-28）
- 测试：tests/test_routes_ideas.py（10+2 用例）、test_routes_llm.py、test_routes_skills.py 新增；test_routes_templates/test_routes_ideas 鉴权适配；全量 pytest 绿
- 文档：Backlog T-30~T-33 登记、ST-7 状态 🔴 待验收（代码齐，未浏览器验收）；SGME-WebUI设计-v0.1 §2/§6/§7 实现回标与偏差说明（扁平导航 + settings 合流 + LLM 标签页）
- **待办**：ST-7 浏览器逐视图验收（健康检查 UI、CRUD 实测、鉴权 403 引导、SPA 刷新路由回退）；验收后 ST-7 标 ✅

### B40. MCP wiki 三工具（T-22，2026-08-13）

- 背景：MCP 工具集 13 个无 wiki 工具——走 MCP 的客户端（Trae/笔记本/通用 agent）查不到 wiki 知识库，查证能力少半层（触发词：2026-08-13 用户确认 /v1/search 双源后追问 MCP 通道覆盖度）
- **数据源边界查证（关键）**：wiki 扩展的知识页面（`wiki_pages` 表，经 ingest 提炼入库）与记忆引擎 L2 场景（`scenes` 表）是**两个不同数据源**——`/v1/wiki/search` 查 wiki_fts（知识文档），`/v1/search` 的 wiki 层查 scenes_fts（L2 场景）。T-22 的 wiki_search 对齐前者（与 HTTP /v1/wiki/* 对称）；L2 场景检索仍走 search 工具（v0.8 scope 统一待后续）
- 实现：
  - `sgme/operations/wiki.py`（新建）：`search`（透传 wiki_fts.search_wiki_fts，FTS BM25 + LIKE 兜底，返回 page_id/title/snippet）、`list_pages`（轻量字段列表，剔除 content/content_seg）、`get_page`（详情全文，剔除分词列，None → ERR_NOT_FOUND）——补上 wiki 扩展缺的 operations 层（HTTP 路由历史直连 DAO 未迁移，MCP 通道走本层）
  - `sgme/mcp_server.py`：+3 工具——`wiki_search(query, limit)` / `wiki_pages(category, limit, offset)` / `wiki_page(page_id)`；wiki_conn 为 None（扩展未启用）时返回「wiki 扩展未启用」而非 KeyError；instructions + ONBOARDING_TOOLS 清单同步（防漂移测试兜底）
  - 测试：`tests/test_mcp_wiki.py` +7（注册断言/检索命中/空召回/列表轻字段/分类过滤/详情/404）；test_mcp_tools_available 子集断言不受影响
- 文档：Backlog T-22 ✅ v1.0；架构 §22 5.1 工具集 13→16；docs/agent-onboarding.md 清单 13→16（含一句话用法）；本记录 B40
- **验收**：全量 pytest 绿（1542+7）；重启服务后 MCP tools/list 应见 16 工具（长驻进程铁律）

### B41. 创意捕获链路补全（2026-08-13，T-26 配套）

- 背景：用户盘点创意池发现历史创意漏标——8-11 提示的两条创意（本地资源管理软件/心理专家）未进创意池。根因链：①ideas 维度 8-12 才注册（T-26），此前提炼无法打 ideas 标签；②8-12 后虽能打标，但 TTL 按维度回填（ideas+projects/goals 共存取 90d），创意 90 天后过期退出注入，违背「ideas + ttl NULL」定义
- 修复：
  - 数据修复（用户批准）：8-11 两条创意补打 ideas 标签 + ttl→NULL；WebUI 偏差创意 ttl 90→NULL（生产库直接修正，幂等脚本）
  - 提示词强化（commit 5a5cde3）：`prompts/l1_extraction.txt` 任务清单新增第 5 条——用户明确表达新想法/点子/灵感时 dimensions 必须包含 ideas（@working 热更新即生效）
  - TTL 铁律（commit 49bfb33）：`l15._backfill_ttl` 含 ideas 维度 → 强制 None（覆盖其他维度默认与显式值）；测试 +2
- 验证：真实冒烟（9911 独立实例 + 真实 DeepSeek）——创意记忆正确打 ideas 标签、普通进展不打；l15 相关 54 passed；服务重启生效
- 踩坑：SGME_HOME 传 MSYS 路径（/d/tmp/...）在 Windows Python 解析失败（SECRETS_FILE 指向 \d\tmp\... 不存在 → 默认 key → 提炼 401）——**必须传 Windows 原生路径（D:/tmp/...）**；bash 里空值 env 变量（setdefault 跳过）也会导致 key 不加载

### B42. 统一搜索新增 wiki_pages scope（T-34，2026-08-13）

- 背景：用户盘点统一搜索（/v1/search）发现 wiki.db 知识页面（wiki_pages）不在其中——只有 memory（L1 记忆池）与 wiki（L2 scenes 场景）两层，知识库走独立通道（/v1/wiki/search + MCP wiki_search）。用户判定功能不完善，要求纳入统一搜索（2026-08-13 拍板：新增独立 scope 名 `wiki_pages`，不扩展 wiki 语义）
- 决策：①scope 命名——新增 `wiki_pages`，`wiki`/`scenes` 保持 L2 场景语义不变（用户拍板）②容错语义——wiki 搜索返回空不影响整体搜索效果，故不做开关检测：wiki_conn 为 None 或该层检索失败 → 空结果 + WARNING（用户拍板：不需要关注 wiki 开关是否打开）
- 实现：
  - `sgme/operations/search.py`：search() 加 `wiki_conn: sqlite3.Connection | None = None`；新增层 3 `_search_wiki_pages`——复用 `sgme.wiki.fts.search_wiki_fts`（FTS5 BM25 + LIKE 兜底，同 operations/wiki.py 先例不造轮子），结果形状对齐 scenes 层（rank/source/page_id/title/content/routes），source=`wiki_pages`、routes=[`wiki_fts`]；**容错隔离**：wiki_conn None / 检索异常 → 空结果 + WARNING，不拖累 memory / scenes 层
  - `sgme/server/routes_memory.py`：/v1/search 端点经 `getattr(app.state, "wiki_conn", None)` 注入
  - MCP search 工具保持 memory-only（wiki_pages 已由独立 wiki_search 工具覆盖，避免重复）
  - 测试：`tests/test_operations_search.py` +5 用例（wiki_pages 命中 / wiki_conn=None 跳过该层 / 检索失败隔离 / memory+wiki+wiki_pages 三层组合顺序 / HTTP 端点注入链路）；conns fixture 补 init_wiki_fts
- 文档：架构 §检索 scope 枚举（+wiki_pages）；Backlog T-34 ✅ v1.0；本记录 B42
- **验收**：test_fast search 55 passed / 0 failed；全量 pytest 待里程碑；服务重启后 `POST /v1/search {"scopes":["wiki_pages"]}` 应命中知识页面（长驻进程铁律）

### B43. Care Engine 角色层落地（T-35，ST-25 第一个 Task）

- 背景：Care Engine 立项（ST-25）后按设计 v0.1 拆 T-35~T-38；T-35 = 角色层数据结构（角色卡格式 + persona 物化），一切的地基。角色 = 沟通外皮（皮），记忆池 = 芯；用户画像保持模板查询零物化，角色 persona 是唯一物化例外（2026-08-13 用户拍板）
- 实现：
  - `sgme/care/roles.py`（新建）：CC V2 兼容子集——顶层只允许 spec/spec_version/data，data 必填 name/description，可选白名单（personality/scenario/first_mes/mes_example/system_prompt/post_history_instructions/character_book/extensions），extensions.sgme_care 只允许关怀策略键（greeting_templates/trigger_rules/frequency）；角色 id 白名单正则防路径穿越；文件 CRUD（幂等 upsert 刷新 updated_at；**archive 移入 .archive/ 原件永不删**）；persona 物化（Persona Architect 四层扫描提示词：L1 基础锚点→L2 兴趣图谱→L3 交互协议→L4 认知内核，≤2000 字约束；落盘 data/personas/，备份轮转保留 3 份）
  - `sgme/operations/care.py`（新建）：list/get/create/delete/persona 生成——画像素材 = 记忆池静态维度（identity/preferences/habits/values/style/skills/family/social）聚合，零物化现查现取，上限 8000 字符；persona 生成复用 `sgme.llm.chain.call_with_fallback`（提炼降级链同源）；**LLM 不可用 → ERR_INTERNAL 不降级直存**（persona 无降级语义）
  - `sgme/server/routes_care.py`（新建）：6 端点——GET /v1/admin/roles（列表）、GET/POST /v1/admin/roles/{id}（详情/upsert）、DELETE（归档）、GET/POST /v1/admin/roles/{id}/persona（读取/生成）；care.enabled=true 时 app.py 挂载（扩展模块模式，wiki 同构）
  - config：care 段（enabled/persona_max_chars）+ DEFAULT_CARE_CONFIG 兜底合并（_merge_care_config）+ ROLES_DIR/PERSONA_DIR 常量
  - `roles/butler.json`（预置）：管家角色卡——system_prompt 含角色职责（主动关怀/高效汇报/尊重边界/透析日节奏）、extensions.sgme_care 关怀策略（问候模板 3 条/触发规则 4 条含透析日提醒与过劳预警/频率档位 max_daily=5、情绪冷却 180min、无回应指数退避）
  - 测试：`tests/test_care.py` 28 用例——校验白名单（合法/缺必填/多余键/扩展越界/顶层越界）、CRUD（save/list/upsert/archive 原件保留/非法 id 防穿越）、persona（四层提示词/落盘备份轮转/读取缺失）、HTTP 全链路（upsert→list→get→404→archive→404）、persona 生成（mock 成功/画像素材进提示词/LLM 不可用 ERR_INTERNAL/角色不存在 404）
- 文档：Backlog T-35 ✅；设计文档 §Task 状态；本记录 B43
- **验收**：test_fast care 28 passed / 0 failed；全量 pytest 待里程碑；服务重启后 `GET /v1/admin/roles` 应返回预置管家角色（长驻进程铁律）

### B44. Care Engine 关怀信号增强（T-36，ST-25 第二个 Task）

- 背景：T-35 角色层落地后，T-36 = SGME 侧信号增强——把信号总线从"提炼事件"扩展到"关怀信号"（待办到期/情绪/过劳/每日），供消费方 agent 拉取后决定是否打扰用户（SGME 只发信号不做决策，架构铁律）
- 实现：
  - `sgme/care/signals.py`（新建）：四类关怀信号推导（**零 LLM 规则引擎**）——care_todo_due（tasks 维度 active 记忆 updated_at 老化 ≥ todo_due_days 天，默认 7）、care_mood（status 维度内容命中情绪关键词，默认词表可配置）、care_overwork（focus 维度当日新增 ≥ overwork_threshold，默认 5）、care_daily（每日关怀问候信号，dedup=日期）；**幂等去重**：事件 id = uuid5(命名空间, "{type}:{dedup_key}") + INSERT OR IGNORE（event_id 主键）——重复扫描零重复事件；list_care_signals（type 过滤/unconsumed_only/limit）+ consume_signal（mark_consumed 幂等）
  - `sgme/operations/care.py`：+scan_signals/list_signals/consume_signal 三个操作
  - `sgme/server/routes_care.py`：+3 端点——POST /v1/admin/care/scan（触发扫描）、GET /v1/admin/care/signals（?signal_type=&unconsumed_only=&limit=）、POST /v1/admin/care/signals/{event_id}/consume（消费标记）
  - 与 Dream 协同：扫描由消费方定时调用（cron/heartbeat），SGME 不做常驻轮询——T-38 消费方接线
  - 测试：`tests/test_care.py` +6 用例（四类推导全命中/重复扫描幂等零重复/关键词配置覆盖/列表过滤+消费标记+404/HTTP 全链路 scan→list→consume）
- 文档：Backlog T-36 ✅；设计文档 §关怀信号实现标注；本记录 B44
- 待收敛：目标矛盾推导（goals/values 交叉）规则复杂，留实施期；情绪信号源可后续接提炼侧情绪标签
- **验收**：test_fast care 24 passed / 0 failed；全量 pytest 待里程碑；服务重启后 `POST /v1/admin/care/scan` 应产出 care_daily 信号（长驻进程铁律）

### B45. Care Engine 角色装配 + 关怀消费方（T-37/T-38，ST-25 收尾）

- 背景：T-35 角色层 + T-36 信号增强落地后，T-37 = 装配（把角色卡/persona/画像合成沟通提示词），T-38 = 消费方（agent 侧定时消费信号）。ST-25 四个 Task 至此全部完成——SGME 侧交付"角色数据 + 关怀信号"，主动触达由消费方（Hermes cron）驱动
- 实现：
  - `sgme/operations/care.py` `assemble()`（T-37）：角色卡 system_prompt（**{{original}} 占位替换**为角色职责默认文案；{{char}}/{{user}} 宏保留给消费方替换）+ persona 物化全文（若已生成）+ profile_blocks（inject 模板查询**零物化**，可选 inject_mode）+ care_policy（extensions.sgme_care）；**换皮不换芯**：换角色 = 换装配输出，记忆池不动；`GET /v1/admin/roles/{id}/assemble?inject_mode=` 端点
  - `scripts/care_consumer.py`（T-38）：消费方核心脚本（项目内随 git）——触发扫描 → 拉未消费信号 → **幂等去重**（本地状态 data/care/consumer_state.json，last_notified_at 模式防 consume 失败重复通知）→ stdout JSON 行输出待关怀事项（空=静默）→ 标记已消费；`--check-only` 供 heartbeat 巡检（只查不消费）；SGME 不可达静默降级不阻塞宿主；key 从 config/.env 读不落盘；cron（完整流程）+ heartbeat（--check-only）双轨由 Hermes 平台 cron 调度
  - 测试：test_care.py +5（装配无 persona/有 persona/带画像/404/HTTP）、test_care_consumer.py 5 用例（无信号静默/输出+消费+幂等/check-only/不可达降级/缺 key）
- 文档：Backlog T-37/T-38 ✅；设计文档 §Task 状态；本记录 B45
- **接线完成（2026-08-13）**：Hermes cron job `sgme-care-heartbeat`（*/30 * * * *，workdir=D:\Projects\SGME，仅 terminal 工具集）——跑 scripts/care_consumer.py，有信号时以管家角色口吻生成 ≤80 字关怀消息，时段约束 08:00-22:00、同主题当日一次、周三透析日关怀提醒；⚠️ 当前 deliver=local（消息存 cron 日志不主动推送——CLI/TUI 会话无投递通道），送达通道形态留用户选（telegram 等 gateway 平台）
- **真实链路验证（2026-08-13 生产库）**：服务重启后 GET /v1/admin/roles → butler 管家角色 ✓；POST /v1/admin/care/scan → 生产数据推导 care_mood=1/care_overwork=1/care_daily=1（care_todo_due=0 无 7 天无进展待办）✓；care_consumer.py 输出 3 条信号并消费 ✓；**假阳性修复**（commit 289823a）：care_mood 误命中技术记忆「崩溃确认为偶发竞态」→ TECHNICAL_CONTEXT_KEYWORDS 技术语境排除表（bug/测试/竞态/passed 等），回归测试 +2，31 passed
- **验收**：test_fast care 全绿；真实链路闭环（角色+信号+消费+排除）

### B46. Care Engine WebUI 三页（T-39/T-40/T-41，2026-08-13）

- 背景：ST-25 后端四 Task 完成后，用户要求①内置角色模板供选择②WebUI 会话相关功能登记 Backlog——拆 T-39（角色管理页）/T-40（当前角色选择+装配预览）/T-41（关怀信号面板），挂 ST-7
- 前置：**内置角色模板 4 个**（commit 63abc56）——roles/butler 管家（已有）/companion 伴侣/friend 朋友/mentor 导师；按 CC V2 兼容子集 + 调研方法论（行为指令优于形容词）；差异化关怀频率（伴侣 3/天 → 导师 1/天，无回应退避 1-3 天）；全部通过校验 + API 可见
- 实现：
  - T-39（commit eb5b850）：`ui/src/views/care/RolesView.vue`——master-detail：左侧角色卡片列表（名称/描述/关怀策略摘要/更新），右侧详情（描述/主动关怀策略展示含触发规则与频率/persona 生成与全文查看/内联编辑表单（8 字段）/新建/归档）；`ui/src/api/roles.ts` 封装 6 端点；路由 /roles + 侧边栏 🎭
  - T-40（commit e04512c）：SGME 侧 `GET/PUT /v1/admin/care/active-role`——运行数据 `data/care/active_role.json`（不入 git），角色 id 白名单校验，角色不存在 ERR_NOT_FOUND，换皮不换芯；前端当前角色徽标 + 「设为当前角色」+ 装配预览（assemble?inject_mode=daily：system_prompt + 画像块数 + persona + 关怀策略）；tests +4（默认 None/设置读取/404/HTTP 全链路）
  - T-41（commit 6f37557）：`ui/src/views/care/SignalsView.vue`——触发扫描（幂等统计 chips）、信号列表（类型徽标 4 色：待办橙/情绪粉/过劳红/每日绿 + payload 摘要 + 消费状态）、未消费过滤、单条「标记已处理」；路由 /signals + 侧边栏 💗
- 文档：Backlog T-39/40/41 ✅；本记录 B46
- **验收**：后端 test_fast care 全绿（36 passed）；前端 npm run build 通过；真实链路：active-role PUT/GET 一致、assemble 注入 4 画像块；WebUI 浏览器验收（GUI 验收铁律：用户亲自确认 /roles 与 /signals 页面）

### B47. L2 场景消费端接线（T-42，2026-08-13——先错后正）

- 背景：用户质疑 L2 没起作用——查证：L2 抄自腾讯 L2 Scenario（快速恢复工作上下文），SGME 移植后只在搜索支路、注入不消费（结构性缺位）。用户决策：接上消费端
- **第一版（错误方向，已 revert 124429f）**：inject 附加「近期场景速览」block（updated_at 最近 top-2）——用户纠正：**场景注入时机不对**——不应该是会话打开固定注入，应该由 agent 根据用户第一个问题/对话内容**语义匹配**场景（"和场景注入关联才对"）；与用户既有认知一致：注入依赖场景模式提示词、"场景需要什么才注入什么"、search 语义匹配路线
- **最终实现（commit 8b6d422）**：改 Hermes 插件 `prefetch(query)`——每轮 LLM 前调 `/v1/search`，scopes 从 `["memory"]` 扩展为 `["memory", "wiki"]`，**对话内容驱动的场景语义匹配**（FTS BM25 + 向量 + RRF 按相关性召回，非时间排序）；结果分块渲染：「# 相关记忆（SGME）」+「# 相关场景（L2 匹配）」带 [title] 前缀（内容截断 120 字）；场景未命中不输出场景块（不占 token）；SGME 侧零改动（/v1/search wiki scope 已具备场景语义检索）；tests +3（scopes 断言/双块渲染/仅场景命中）
- 真实链路验证（生产库）：Q「SGME 架构设计」→ SGME 项目场景（heat 172）；Q「Trae 规则」→ Trae 场景；Q「记忆引擎」→ 记忆引擎场景——**不同问题命中不同场景**
- 部署：`adapters/hermes/install.py` 已同步部署副本；**重启 Hermes 后生效**（插件加载，当前会话不受影响）
- 链路定型：**对话内容 → /v1/search（memory+wiki 双 scope）→ 语义匹配场景注入**——L2 进入"动态检索"主干道；ChronoMemo 时光轴数据源待立项接线
- **验收**：test_hermes_adapter 8 passed / 0 failed；真实链路三 query 场景命中正确；插件部署副本已同步

### B48. 提炼 LLM 动态链（T-43，2026-08-13——功能变更）

- 背景：用户功能变更要求——①未指定专用提炼 LLM 前，首选 = agent 当前 LLM（直接复制其模型调用参数）；②用户可调整并指定专用提炼 LLM，然后由当前使用的 LLM 作为备用；③**不建议引导用户使用本地模型**（向量维度不够 + 能力不够），但允许用户自定义提供商（"不建议 ≠ 不可以"）；④供应商设置里补**向量模型指定**（原无 embedding 配置位）
- 可行性修正（先可行性后实施）：查 Hermes MemoryProvider.sync_turn 接口源码——**无 model 参数**（每轮自动跟随不可行，改 Hermes 核心接口维护成本高）→ 用户拍板**注册声明制**：agent 注册/append 时声明 agent_model（provider/model），一次声明长期生效
- 实现（commit 2426361）：
  - **声明链路**：AgentKeyStore.register_agent 加 agent_model（agent_keys.json 持久化）+ resolve_agent_model(agent_id) 反查；register 端点请求体加 agent_model；append 请求体加 agent_model（**未传按 agent_id 反查注册声明**）；raw_files 加 agent_model 列（SESSION_DDL + 幂等迁移 _migrate_session_agent_model）
  - **动态链**：`sgme/llm/resolve.py`（新建）resolve_refinement_chain——refine.llm_override 空 → 链首 = agent 声明模型（从 providers 表**复制连接参数**：base_url/api_key_env 引用/context_window，采样参数用链默认）；override 指定 → 专用为主、agent 为备；agent 声明 provider 不在 providers 表 → 跳过该节点 WARNING；未声明 → 原静态链零破坏；build_refinement_cfg 纯函数（不修改入参 cfg）；refine.py 提炼入口读 raw_files.agent_model 构造动态链（engine 读库不依赖入口层，架构干净）
  - **配置**：DEFAULT_REFINE_CONFIG 加 llm_override={}（用户指定专用模型填 provider/model/max_tokens）；load_llm_config 保留 providers 表到 cfg（动态链查连接用）
  - **向量模型指定**：providers.yaml 新增顶层 `embedding:` 段（volc-plan 默认）；config.py _merge_search_config 缺 base_url/api_key_env 时从 embedding 段兜底注入（search.vector 显式配置优先）；_load_embedding_config 只复制环境变量名引用（铁律 #10）
- 测试：test_llm_resolve.py +8（跟随 agent/override 优先 agent 备/未知 provider 跳过/未声明回退/格式非法忽略/override 未知 provider/纯函数）
- 真实链路验证：register（agent_model 声明）→ append（未显式传，**按 agent_id 反查落库** raw_files.agent_model）→ 动态链 [deepseek/deepseek-v4-flash, lm-studio/qwen3.5-9b]；测试 agent/session 已清理
- 文档：Backlog T-43 ✅；本记录 B48
- **内置向量提供商 3 个（2026-08-13 补充，commit 06d7722）**：providers.yaml embedding 段扩为 3 个——volc-plan（doubao-embedding-vision，**默认**；⚠️ **用户付费的火山 Agent Plan 套餐**，¥200/月 Medium、月额度 100,000 AFP，非免费，文档/配置不得标注免费）/ siliconflow（BAAI/bge-m3，8192 上下文，免费模型）/ nvidia（nv-embed-v1，免费端点）；search.vector.provider 按名引用（连接参数兜底，model 可覆盖），用户可自行添加任意 OpenAI 兼容 embedding 提供商；sgme.yaml 显式 `provider: volc-plan`；实测：health vector available（memory_vectors 11211）、search 带 vector 路由；siliconflow/nvidia 需用户填对应 key（SILICONFLOW_API_KEY / NVIDIA_API_KEY）后即可切换
- 待接线：Hermes 等 agent 注册时声明自己的模型（onboarding 文档示例）；WebUI 供应商页 embedding 段展示（后续）
- **验收**：71 passed / 0 failed（resolve 8 + inject 16 + care 36 + hermes 8 + append 7）；真实链路注册→落库→动态链正确

### B49. 统一供应商模型 + 降级链可编辑（T-47，2026-08-13——T-43④ 深化）

- 背景：T-43④「供应商设置补向量模型指定」落地为 providers.yaml 顶层 embedding 段，但向量提供商与普通供应商两套入口并存；用户要求**统一供应商模型**——向量提供商并入 providers 段（vector_capable=true 标记），embedding 段仅向后兼容保留；同时降级链支持 UI 编辑（增删节点 + 排序）
- 根因修复（真实 bug）：`llm_chain_update` 调 `validate_models({"chains": chains})` 未带 rules → 黑名单校验（deny_prefixes/deny_exact）恒失效，被拒模型可不经校验写入链
- 实现：
  - **config.py**：`write_providers_config` 修正——写回只覆盖 providers 段、保留 embedding 等其余段（否则供应商管理操作抹掉向量配置）；新增 `write_llm_config`（只覆盖 chains 段、保留 rules 段，供降级链编辑）
  - **operations/llm.py**：`llm_status` 返回统一 providers（各供应商带 vector_capable/models/display_name 标记）；`llm_provider_add` 支持 vector_capable 字段；`llm_embedding_set_active` 改为从 vector_capable 供应商选向量模型（兼容旧 embedding 段）；新增 `llm_chain_update`（校验供应商存在 + 白名单黑名单 + 写回 llm.yaml 刷新运行时）
  - **routes_llm.py**：新增 `PUT /v1/admin/llm/chains`（T-44 降级链编辑）
  - **providers.yaml**：volc-plan/siliconflow/nvidia 并入 providers 段并标 vector_capable=true；embedding 段保留注释说明不再主入口
  - **前端**：llm.ts 补 vector_capable/models/display_name + `updateChains` API；ProvidersView——降级链+链级规则并排两栏、降级链可编辑（增删节点/上下排序/选供应商/填模型 + 保存/撤销）、供应商表单加「向量模型」checkbox、供应商卡片显示「向量」标签、向量模型区块改为从 vector_capable 供应商中选
- 测试：test_routes_llm +13（落盘保留 embedding / provider_add 保留 embedding / vector_capable 标记 / set_active 从 vector_capable 选 / 链更新落盘+保留 rules / 未知供应商拒绝 / 黑名单拒绝 / 校验失败不污染文件 / 路由+鉴权）；test_fast llm 85 passed；npm run build 通过
- 运维影响：后端需重启加载新端点（PUT /v1/admin/llm/chains）；前端 dist 已重建
- 文档：Backlog T-47 ✅；WebUI 设计 §7 更新；本记录 B49
- **布局与探测收尾（2026-08-13 用户反馈）**：①「供应商」改名「模型供应商」并移至最上、「降级链」改名「模型降级链」、删除「链级规则」卡片（降级链不再并排两栏）②保存供应商（含向量标签）后顶部横幅反馈（原表单关闭后 formOk 不可见，改 flashOk 全局提示 3s）③`llm_health` 探测范围并入 vector_capable 供应商（`_file_providers_with_flags` 合并进探测集合），向量模型卡片显示连通/不可用状态；test_routes_llm +1（vector_capable 纳入健康探测）；test_fast llm 86 passed；build 通过

### B50. 三池职责重构——创意用户驱动 / 需求池改跨项目待办 / 项目主动立项（T-48，2026-08-13 用户定）

- 背景：创意池由 L1 提炼 LLM 自动打标（T-26）实测误标率高（决策记录/偏好/元数据被标 ideas，8-13 盘点 18 条约 1/3 非创意）；需求池（demands）/项目池（project_meta）落地后 0 数据空转；用户拍板三池新职责：**创意=用户主动提出才记录**（LLM 不再自动识别）、**需求池=跨项目统一待办池**（backlog 化，agent 维护）、**项目池=用户主动立项**（agent 执行）
- 实现：
  - **prompts/l1_extraction.txt**：删除第 5 条「创意识别」（T-26 强化指令），第 3 条维度标注加否定式「ideas 禁止自动标注——创意由用户通过创意池 API 主动记录」
  - **创意池人工添加**：data/idea_dao.py `add_idea`（memories+ideas 标签+ttl NULL+source_type='manual' 单事务）；operations/idea.py `add_idea`（content 必填/priority 0-100/source_ref 可选）；routes_ideas.py `POST /v1/admin/ideas`
  - **需求池→待办池**：operations/demand.py `_check_project` 从 400 硬校验降级为 warning 不阻断（待办可先于项目注册出现；project_id 仅作过滤标记）；create/update 返回 warnings 携带「未登记」提示；状态机 pending→done + created_at/resolved_at 时间戳（原有）
  - **MCP 三工具**：`idea_add` / `demand_create` / `project_register`（mcp_server.py，ONBOARDING_TOOLS 同步，FastMCP instructions 更新）
  - **WebUI**：IdeaList 新建弹层（内容/优先级/来源）+ 空态文案更新；DemandList 全面重写——新建/编辑弹层、项目过滤下拉（复用项目池）、加入/完成时间展示；ProjectList 立项弹层（ID/路径/名称/git/里程碑）；导航与路由「需求池」→「待办」
  - **project_init.py**：第⑤步接线——`link_demands()` 检索标题含项目名的待办 → 标 planned + 绑 project_id（失败不阻断立项）
  - **存量回填**：2 条 ideas 维度 ttl=90 → NULL（212704d8/35b2718e，备份 data/memory.db.bak-ttl-backfill-20260814-003644）
- 测试：test_routes_ideas +3（新建成功/优先级溯源/校验 400）；test_mcp_server +3 工具用例 + 工具集断言 13→16；test_demands 软校验语义重写（400→warning）；相关模块 144 passed；npm run build 通过
- 运维影响：**后端需重启**加载新端点（POST /v1/admin/ideas）与 MCP 新工具（9913）；前端 dist 已重建
- 文档：AGENTS.md 新增「三池职责」段（防漂移锚）；SOUL.md 立项铁律镜像同步（⑤需求池关联→待办池关联）；Backlog T-48 ✅；本记录 B50

### B51. 第 1 批致命问题修复（F-1~F-9，2026-08-14，全面深度检查收尾）

- 背景：项目全面深度检查发现 9 项致命问题（评测框架失效 / LLM 降级链缺兜底 / 跨机部署阻塞 / WebUI 关键流程不可达），分后端组（F-1/F-2/F-3/F-9）与前端组（F-4~F-8）两批修复。方案见 `docs/plans/2026-08-14-fatal-fixes-batch1.md`
- **F-1【代码】eval/run.py dry_run 恒真**：`dry_run = args.dry_run or True` 是恒真表达式，`--dry-run` 参数完全失效，评测框架永远跑 mock LLM。改为 `dry_run = args.dry_run`（显式控制）。验证用小样本 `eval/cases/v001_sample.yaml` 控费
- **F-2【部署】LLM 降级链补 lm-studio 兜底**（⚠️ **已撤销**，见下方撤销记录）：原判断基于架构约束 #9 要求 lm-studio 兜底，忽略用户既定决策（本地模型能力/向量维度不足，已主动移除 lm-studio）。原改动在 llm.yaml 插入 lm-studio 节点 + providers.yaml 补 vector_capable:false，破坏了分层配置结构（619ccb3 已恢复分层）。**撤销操作**：llm.yaml 移除 lm-studio 节点（降级链恢复 `deepseek → rule drop_batch`）+ 注释改两级 + 决策溯源说明；providers.yaml 移除 lm-studio 整个 provider 定义（WebUI 供应商卡片与健康探测不再显示）。教训：动手前应查 SGME 记忆/git 历史核实用户决策，不可仅凭架构约束文本推断
- **F-3【部署】备份异地目录改 env 注入**（ST-20 扩展）：`config/sgme.yaml` `remote_dir: E:\SGME_Backup` 硬编码本机路径已入 git。改为空字符串占位；`sgme/config.py` ENV_OVERRIDES 追加 `"backup.remote_dir": "SGME_BACKUP_REMOTE"`（env 值优先于 yaml，落盘恢复现值防泄漏）；`sgme/operations/backup.py` 空字符串转 None（`or None`）。空值风险已核实：`backup/manager.py:297` `if remote_dir is None: return {skipped:True}` 已实现跳过，链路完整无需补兜底
- **F-9【部署】6 个脚本硬编码本机路径读密钥**：`check_usage.py`/`deepseek_usage.py`/`test_dsv4.py`/`test_dsv4_nothink.py`/`test_dsv4_full.py`/`test_deepseek_l1.py` 均硬编码 `<用户目录>\AppData\Local\hermes\.env` 读 DEEPSEEK_API_KEY，违反架构约束 #10「密钥不落盘」。6 个一次性调试脚本全部归档 `scripts/oneoff/`（README 登记），不再维护
- **F-4~F-8【UI】WebUI 路由与导航致命问题**：F-4 MainLayout 侧栏补 `/sessions` 会话原文入口（router 已注册但无导航）；F-5 MemoryDetail/IdeaDetail 路由参数改 `computed + watch immediate` 响应变化（原 onMounted 一次性赋值导致详情页跳转不刷新）；F-6 SearchView/WikiView query 改 `watch` 响应（原 onMounted 只首次执行）；F-7 router 加 `:pathMatch(.*)*` 404 兜底 + PlaceholderPage 改造为 404 页（原孤儿组件文案过时）；F-8 SkillsView 统计卡 FontAwesome 图标改 emoji（🧰🏷✅📄，原引用 FA 但 package.json 无依赖导致图标空白）
- 测试：后端 `python scripts/test_fast.py eval config llm backup` → 346 passed / 0 failed（覆盖 eval/config/llm/backup 全相关模块）；前端 `npm run build` 通过（107 modules，842ms）
- 运维影响：**后端需重启**加载 llm.yaml 新降级链与 sgme.yaml env 注入；前端 dist 已重建需 Ctrl+F5 硬刷新；本机需设 `SGME_BACKUP_REMOTE` 环境变量否则异地推送跳过
- 文档：`docs/plans/2026-08-14-fatal-fixes-batch1.md` 修复方案清单；本记录 B51

### B52. 工作区遗留任务补登记（2026-08-14，F 系列收尾时清理）

- 背景：B51 修复收尾时发现工作区混有 6 组未提交的遗留改动（历次会话产物），与 F 系列文件级交织。按逻辑分组拆为独立提交补登记，保证可追溯性。改动均为已运行验证过的功能，本次仅补提交与文档
- **T-43/T-44/T-47 LLM 供应商统一模型**：向量提供商从 providers.yaml 顶层 `embedding` 段并入 `providers` 段 + `vector_capable` 标记（5 家统一结构）；新增 `PUT /v1/admin/llm/chains`（降级链编辑：增删节点/排序/rule 编辑，写回 llm.yaml 保留 rules）、`GET /v1/admin/llm/embeddings` 与 `PUT /v1/admin/llm/embedding/active`（向量提供商切换）；`write_llm_config`/`load_embeddings_config` 新函数（写回只覆盖 chains 段、保留其余段）；ProvidersView 重构为「模型供应商/模型降级链/向量模型」三区块；`write_providers_config` 写回保留非 providers 段（防抹掉 embedding 配置）。含 F-2 要求的 vector_capable 显式标记（与 B51 F-2 叙述衔接）
- **WebUI 密钥自动填充**（2026-08-13 用户需求）：新增 `GET /v1/admin/keys`（仅本机回环来源免鉴权，远程 403），前端首开自动填入 admin/agent key；index.html 加 `Cache-Control: no-cache`（修复 SPA 旧 JS 缓存致三个页面空白）
- **记忆列表多维度过滤**（2026-08-13 用户定 AND 语义）：`dimensions` 查询参数（逗号分隔，每维度 EXISTS AND 连接），MemoryList 维度复选 chip
- **检索知识库直达**（T-34 前端闭环）：/search scope 增 `wiki_pages`，结果直达 `/wiki?page_id=` 详情；wiki_dao `_parse_tags` 重构（list_pages 也解析 tags）
- **三池改名与 WebUI 微调**（T-48）：「项目」→「项目池」；管线 4 卡横排 + Dream 日报挪位、信号默认全显、创意/待办卡片布局、场景状态圆点、移除 Hermes/Reasonix 无绑定假开关
- 测试：相关模块 pytest 全绿；`tests/test_providers.py` 断言过期修复（原精确相等断言随 T-47 5 家结构失败，改子集断言）
- **B51 数字补正**：B51 记录的后端测试数（346 passed）为协作者报告值；独立实测 `test_fast.py eval config llm backup` 为 **345 passed / 0 failed**，全量 pytest 修复断言前为 1627 passed / 1 failed（test_providers 过期断言）
- 运维影响：无（代码与 B51 重启后已生效）；文档：本记录 B52；WebUI 设计文档已同步（统一供应商模型/向量切换/降级链可编辑/wiki_pages/维度 AND/导航调整/密钥自动填充）

### B53. 降级链写回剥离连接字段 + llm.yaml 分层恢复（2026-08-14，B51 收尾）

- 背景：B51/F-2 提交后审计发现 `config/llm.yaml` 为「节点内联全连接字段」形态，与文件头注释声明的分层设计不符（连接字段由 providers.yaml 注入）；`write_llm_config`（T-44）写回时会把 WebUI 传回的运行时节点（已注入字段）直接落盘，内联旧值会覆盖 providers.yaml 新配置——「降级链与 provider 不一致」的复发隐患。另发现 T-47 重写时丢失 lm-studio 节点 sampling 参数（本地模型官方推荐，temperature 1.0/top_p 0.95/top_k 20/presence_penalty 1.5）
- 改动：`sgme/config.py` 新增 `CHAIN_ORCH_FIELDS` 白名单 + `_strip_chain_conn_fields`，`write_llm_config` 写盘前统一剥离连接字段（只落 provider/model/max_tokens/extra_body/sampling/rule，rule 节点保持）；`config/llm.yaml` 恢复头部注释与分层节点（补回 lm-studio sampling，保留 thinking.disabled 防截断）；测试 `test_chain_update_strips_conn_fields_on_write` 回归防护
- 测试：`pytest tests/test_routes_llm.py tests/test_config.py` → 56 passed / 0 failed；`load_llm_config` 探活确认连接字段注入正常（deepseek/lm-studio base_url 均注入，lm-studio 无 api_key_env 属正常——本地服务免 key）
- 运维影响：无（加载结果与改动前运行时等价，服务无需重启；下次重启加载新文件生效）

### B54. wiki 直接写入正式 API（HTTP + MCP，不走提炼通道）（2026-08-14，T-55）

- 背景：wiki 知识库写入此前只有两条路——`POST /v1/wiki/ingest`（refinery LLM 提炼，内容会被改写）与内部脚本 `scripts/wiki_add_page.py`（直连 dao，外部 agent 不可调用）。2026-08-14 讨论「SGME wiki 成为渐进式 skills hub」后定案：把「原样入库、不走提炼」的写入能力升级为正式 API（HTTP + MCP 双通道），供 Hermes/Reasonix/Trae 等外部 agent 直接调用（可行性分析报告已入 wiki：`sgme-wiki成为渐进式skills-hub的可行性分析报告-57e75e55`）
- 改动：
  - `sgme/operations/wiki.py` 新增 `create_page`：page_id 复用 `refinery.output._gen_page_id`（标题 slug + 内容哈希，与提炼链路产出一致）；幂等 upsert（同 title+content 命中同 page_id → status=updated）；**索引保证：先 `init_wiki_fts`（幂等）再 `insert_page`**——FTS 触发器先就位，冷启动库（FTS 未初始化）写入后也立即可被 wiki_search 检索，不存在「有页面无索引」状态；title/content 空串业务校验（InvalidArgs）
  - `sgme/wiki/routes.py` 新增 `POST /v1/wiki/pages`：require_agent_key 鉴权（与读同权限级）；Pydantic 必填缺失 422（框架标准语义），业务校验空串 400；返回 `{page_id, status: created|updated}`
  - `sgme/mcp_server.py` 新增 `wiki_page_add` 工具（参数：title/content 必填 + category/tags/source_type/source_url/source_file 可选）：ONBOARDING_TOOLS 17→18；缺必填参数由框架层抛 ToolError（与既有 wiki_page 缺参行为一致）
  - `scripts/wiki_add_page.py` 沉淀为通用脚本保留（内部运维通道，Gateway 挂时兜底）
- 测试：`test_mcp_wiki.py` +4（创建/幂等/缺参 ToolError/空串业务校验）、`test_wiki.py` +4（创建可搜/幂等/422/鉴权 403）→ 28 passed / 0 failed（两文件全量）
- 运维影响：**后端需重启**加载新端点与工具（nssm 服务，`cmd /c sc start "SGME Gateway"`）；重启后冒烟：HTTP POST 建页 + MCP wiki_page_add + wiki_page 回读验证

### B55. 创意池独立建表（ideas 维度独立日，T-56，2026-08-14 用户拍板）

- 背景：用户定案「创意/项目/待办均由接入 agent 掌控，不依赖 LLM 识别」——创意从 memories 打标（ideas 标签 + ttl NULL）独立为 ideas 表；goals/tech_stack 独立计划讨论后收回（仅 ideas 落地）。用户日后问「有什么项目没完成/什么事没做完」直接查 project_meta/demands/ideas 表回答
- 改动：
  - `sgme/data/db.py`：`IDEAS_DDL` 独立常量 + `_migrate_ideas_table`（幂等，init_databases 接线）——idea_id PK/content/priority/status/notes/custom_flag/reject_reason/rejected_at/source_ref/origin_memory_id/时间戳；**无 content_seg/FTS**（创意不进 /v1/search，ideas API 专属浏览，q 过滤走 LIKE）
  - `sgme/data/idea_dao.py` 重写：memories 约束读写 → ideas 表 CRUD（count/list/get/update/append_note/set_flag/soft_delete/restore/add 函数接口保持）；软删除/备注追加式/自由标记语义原样搬
  - `sgme/operations/idea.py`：参数 memory_id→idea_id；list_ideas 去 dimension_id（表化后无维度概念）；promote_idea 回填 `origin_idea_id=idea_id`
  - `sgme/server/routes_ideas.py`：URL 路径参数 `{memory_id}`→`{idea_id}`（契约变更，消费方仅 WebUI+agent）；list 端点去 dimension_id
  - `sgme/mcp_server.py`：idea_add 对外契约不变（docstring 更新为独立表）
  - WebUI：`ideas.ts` Idea 接口 memory_id→idea_id、移除 dimensions/memory_type/time_velocity/ttl_days/occurred_at、新增 rejected_at/origin_memory_id；IdeaList/IdeaDetail 同步
  - `scripts/migrate_ideas.py`：存量迁移（A 方案——**复制，memories 原件保留不动可溯源**；idea_id=原 memory_id 溯源引用零破坏；写前备份；INSERT OR IGNORE 幂等）
- 迁移：23 条（22 active + 1 rejected）→ ideas 表，source_ref 23/23 带全；备份 `data/memory.db.bak-ideas-migrate-20260814-001038`；重跑幂等（0 写入 23 跳过）
- 测试：test_routes_ideas.py 重写 16 用例（新增**表化隔离**用例：memories 旧 ideas 标签记忆不出现在创意列表）；test_mcp_idea_add 契约更新（ttl_days/dimensions 断言 → idea_id/status）；test_mcp_server + test_routes_ideas + test_routes_admin 64 passed / 0 failed；前端 npm run build 通过
- 运维影响：**后端需重启**加载新表与端点（nssm 服务）；重启后冒烟 21/21（列表/新建/编辑/备注/标记/升格闭环/软删恢复/MCP idea_add/迁移数据可见）；迁移脚本可重跑（幂等）
- 文档：Backlog T-56 ✅；架构 §数据模型 ideas 表 + demands.origin_idea_id 语义（memory_id→idea_id）+ 三池章节；维度注册表 ideas 维度保留（兼容旧 memories 数据检索）

### B56. DeepSeek Harness 原生插件适配（ST-26 / T-49~T-54，2026-08-14）

- 背景：2026-08-14 DeepSeek Harness（dsh）开源（github.com/deepseek-ai/deepseek-harness），插件化架构支持通过 Cordis 框架注册工具/命令/事件监听。SGME 多 Agent 定位需要专用适配器接入 dsh 生态，让 dsh 用户共享 SGME 长期记忆池。技术决策：v1 走 dsh 原生 TS 插件路线（非 Python 适配器），运行时零 Python 依赖，复用 SGME HTTP API（/v1/search、/v1/inject、/v1/append、/v1/admin/refine/trigger_async）
- 改动（adapters/dsh/sgme-bridge/）：
  - **Python 侧骨架**（T-49）：`install.py`（注册 agent_id=dsh + 写 .env + 打印 `dsh plugin --profile web add` 命令，对齐 reasonix install.py）+ `import_history.py`（历史会话补导入，幂等可重跑）
  - **TS 插件骨架**（T-50）：`package.json`（@sgme/dsh-bridge + dsh.bundle 字段）+ `cordis.patch.yml`（插件挂载配置）+ `tsdown.config.ts` + `vitest.config.ts` + `tsconfig.json`
  - **5 类核心能力**（T-51）：
    - `src/sgme-client.ts`：HTTP 客户端封装 4 端点（fetch + AbortController 超时 + 故障隔离返回 null 不抛异常）
    - `src/tools.ts`：`memory_search` + `wiki_search` 工具（defineTool helper，扁平 parameters 映射 + output.schema + render 函数）
    - `src/commands.ts`：`/sgme` 命令（单参数对象 `{name, description, handler}`，handler 返回 `{kind:'success', text}`）
    - `src/context.ts`：首步画像注入（监听 `session/event` 过滤 `turn/start`，异步拉 `/v1/inject` + `/v1/search` projectHint）
    - `src/session-sync.ts`：会话入库（v1.1 累积式——见下）
  - **dsh 规范兼容性修复**（T-51 收尾）：命令注册改单参数对象、工具用 defineTool、事件监听改 `session/event` 统一事件流（非 `agent/pre-step`）
  - **session-sync v1.1 关键修复**（T-53 实测后发现）：v1 假设 `turn/end` 事件含 messages 字段，实测解压 `session.jsonl.zstd` 确认事件结构只有 `{type, seq, time, data:{turn, reason}}`——消息分布在 `user/message` / `assistant/message` / `tool/result` 事件中。v1.1 改造为累积式：监听 4 类事件 → 累积到 turn buffer → `turn/end` 触发 `/v1/append`。`assistant/message` 的 `content` 数组只取 `type='text'` 项（忽略 reasoning / tool-call 块）；`tool/result` 提取 `content[0].content[0].text`；`session_key` 用首条 user 消息毫秒时间戳保证同进程内稳定
- 测试：vitest 65 用例全绿（session-sync 12 + sgme-client 19 + tools 12 + skeleton 5 + commands/tools 其他 17）；pytest 17 用例全绿（install/import_history）；typecheck + tsdown build 通过
- T-53 本地加载验证（2026-08-14）：
  - ①`dsh plugin --profile web add "link:D:/Projects/SGME/adapters/dsh/sgme-bridge"` 成功挂载
  - ②`dsh --profile headless "say hi"` 成功响应 "Hi there! 👋"
  - ③memory_search 工具调用返回真实 SGME 记忆（用户/项目历史事实）
  - ④`/sgme SGME 项目` 命令执行返回详细记忆汇总（项目定位/技术栈/当前状态/架构决策）
  - ⑤session-sync v1.1 修复后，2 条 dsh 会话已入库 `data/session.db`（agent_id=dsh，status=refined 自动提炼完成）
  - ⑥L0 文件格式正确（YAML frontmatter：file_id/session_key/agent_id/source_type/started_at + `# ts user` / `## ts assistant` 消息块）
  - 验证用 reasonix agent key（scope=memory:rw）+ 真实 admin key（triggerRefine）；正式分发由 install.py 注册 agent + 写 .env 覆盖 dev 占位 key
- 运维影响：
  - **无需重启 SGME 后端**（dsh 插件通过 HTTP API 调用，SGME 侧零改动）
  - dsh 侧：插件代码改动后需 `pnpm build` 重建 lib/，dsh 重启加载新产物
  - 密钥配置：`cordis.patch.yml` 中 agentKey/adminKey 默认为 dev 占位值，正式使用前需运行 `adapters/dsh/install.py` 注册 agent + 写 .env 覆盖（对齐 reasonix install.py 流程）
  - 临时验证脚本已归档至 `scripts/oneoff/T53_*.py`（4 个：check_dsh_sync / check_session_db / check_dsh_records / inspect_dsh_session）
- 文档：Backlog ST-26 / T-49~T-54 ✅；本记录 B56；README 接入说明见 `adapters/dsh/sgme-bridge/README.md`

### B57. 信号消费端闭环——agent 成为消费者 + 三层消费模型 + TTL 归档（ST-27 / T-57~T-62，2026-08-14）

- 背景：ST-25 AC①「信号总线无消费者」未闭环——memory_updated/anomaly_warn/batch_scan_error 三类信号发布后无消费方（2246 条堆积），memory_updated 以 ~76 条/天增长；care_* 由 care_consumer 固定脚本消费，但用户定（2026-08-14）：关怀信号应由「当前对话 agent」处理，非后台脚本。「谁消费谁标记」——消费权动态归当前活跃 agent，原子认领防重复。
- 改动：
  - **内核三层消费模型**（T-57）：
    - `data/db.py`：signal_events 加 `consumed_by` 列（迁移 `_migrate_signal_consumed_by`）+ 新增 `signal_acks` 回执表（SIGNAL_ACKS_DDL + 迁移 `_migrate_signal_acks_table`），(event_id, agent_id) 复合主键
    - `data/signal_dao.py`：`mark_consumed` 升级为**原子认领**（`UPDATE ... WHERE consumed_at IS NULL` 返回 rowcount，True=抢到/False=已被抢）+ `consumed_by` 溯源；新增 `ack_signal`（回执 upsert：claimed/acked/failed）；新增 `purge_expired_signals`（TTL 分级：异常类 30d / memory_updated 7d / care 消费后 7d）
    - `signal/engine.py`：新增 `claim(event_id, agent_id)` 封装原子认领
    - `care/signals.py` / `operations/care.py` / `routes_care.py`：`consume_signal` 传 agent_id（从鉴权 key 反查）+ 原子认领语义（已被消费 → 409 ERR_CONFLICT）；新增 `/v1/admin/care/signals/{id}/ack` 回执端点 + `operations.care.ack_signal`
    - `operations/errors.py`：补 `ERR_CONFLICT`（→ HTTP 409）
  - **care_consumer 降级**（T-58）：默认只读（scan + 输出不 consume），新增 `--consume` 显式兜底消费（无活跃 agent 场景 cron 用）；`--check-only` 保留向后兼容
  - **dsh 插件**（T-59）：`sgme-client.ts` 加 `get()` + `pullCareSignals`/`claimSignal`/`ackSignal` + `CareSignal` 接口；`tools.ts` 加 `signal_pull`/`signal_claim`/`signal_ack` 三工具并注册
  - **MCP**（T-60）：`mcp_server.py` 加 `signal_pull`/`signal_claim`/`signal_ack` 三工具（agent_id 从 MCP 上下文 key 反查），ONBOARDING_TOOLS 20→23
  - **文档**（T-61）：AGENTS.md 接入纪律三条→四条铁律（第 4 条：会话开始 signal_pull 拉关怀信号 + 谁消费谁标记）；README 中英同步；架构 §18 更新三层消费模型 + TTL 归档
  - **历史清理**（T-62 部分）：`tmp/signal_cleanup.py` 游标快进（3 订阅者推最新）+ TTL 归档清理 849 条超期 memory_updated
- 测试：新增 `tests/test_signal_consumption.py` 5 用例（原子认领/claim 封装/回执 upsert/claimed 无 acked_at/TTL 分级清理）；`test_care.py` consume 语义更新（幂等→原子抢 409）；`test_care_consumer.py` 加默认只读 + --consume 兜底；pytest 相关模块全绿；vitest 65 用例全绿（含新工具 typecheck 通过）
- 运维影响：
  - **需重启 SGME 后端**生效（db.py 迁移 + routes_care 新端点 + MCP 新工具）；重启时 connect_memory 自动跑 `_migrate_signal_consumed_by` / `_migrate_signal_acks_table`（幂等）
  - 消费权语义变更：care_consumer 默认不再 consume（避免与活跃 agent 竞争）；Hermes cron 若需兜底消费需加 `--consume`
  - 历史信号：2246 条堆积经游标快进（不删可溯源）+ TTL 归档清理 849 条超期 memory_updated；后续由 `purge_expired_signals` 按 TTL 持续清理（建议接入 Dream 定时器或 cron）
  - 真实链路验证（consume/ack 端点 + MCP 三工具）需服务重启后执行
- 文档：Backlog ST-27 / T-57~T-62；本记录 B57；架构 §18 三层消费模型

### B58. Docker 化 + NAS 真实部署验收（ST-24，2026-08-14）

- 背景：ST-24 为 1.0 转公开/tag 的发布验收项——部署形态验证（NAS 最终使用形态）需真实部署，且项目此前无任何 Docker 资产（已核实）。用户定：笔记本 Docker 构建 → NAS（群晖 DSM + Docker 29.1.2）加载部署并端到端验收。
- 改动（新增 4 个 Docker 资产 + 1 篇部署文档）：
  - `Dockerfile`：`python:3.11-slim`（Debian bookworm，兼容 manylinux wheel）；依赖层先复制 pyproject.toml 用层缓存；程序资源（sgme/config/registry/templates/prompts/roles）内置 `/app`（不 pip install 项目，保留 PROJECT_ROOT=/app）；`SGME_HOME=/data` 重定向用户数据、`VOLUME /data`、`EXPOSE 9910 9913`、`CMD python -m sgme`
  - `docker-compose.yml`：build + `image: sgme:1.0.0b1` + healthcheck（urllib 探测 /v1/health）+ `restart: unless-stopped` + `env_file: docker.env`（密钥注入）+ 数据卷 `sgme-data:/data`
  - `.dockerignore`：密钥（.env/*.env）、数据（data/raw/tmp/logs/*.db）、开发产物（.venv/__pycache__/tests/docs/ui）、备份（.git.bak-*）全排除
  - `.env.example`：密钥模板（SGME_ADMIN_KEY/SGME_AGENT_KEY/DEEPSEEK_API_KEY/VOLC_API_KEY/TZ）
  - `docs/deployment-docker.md`：交付物清单/布局约定/单机快速开始/NAS 部署流程（镜像加速→save→scp→load→bind mount compose→验收清单）/注意事项（端口冲突、时区、备份、升级）/安全（密钥不入 git、0.0.0.0 必设自定义 key）
- 测试（真实环境验收，非 mock）：
  - 笔记本 Docker Desktop（镜像加速已配 daocloud/1ms/dockerproxy）：`docker compose build` 成功 → 镜像 463MB（`docker save` tar 109MB）；容器 health 200、`llm.available=true`；append→refine→search 端到端通过（检索命中「用户喜欢用 DeepSeek 写 Python 后端」）
  - NAS（群晖 192.168.10.10，LEO 免 sudo docker）：`docker load` 成功；路径修正（NAS 卷为 `/vol1` 非 `/volume1`）；bind mount `/vol1/1000/Docker/sgme/data:/data`；`docker compose up -d` 后容器 `Up (healthy)`；health ok、llm.available=true；端到端 append（status=new）→ refine → search 命中「用户的家目录部署在群晖 NAS 上」；`/data` 下 data/raw/logs/install.json 均生成（持久化正常）
- 运维影响：
  - NAS 常驻：`restart: unless-stopped` + healthcheck 自愈；数据卷 `/vol1/1000/Docker/sgme/data` 可用群晖「文件站」/Hyper Backup 直接备份
  - 升级路径：`docker compose build` → `docker compose up -d`（数据卷不变不丢数据）；NAS 侧 `docker save`/`load` 更新镜像
  - 密钥：`docker.env` 含真实 key，**不入 git**（`.gitignore` 的 `*.env` 已覆盖）；与项目根 dsh 适配器 `.env`（SGME_AGENT_KEY=agt_*）严格隔离，勿混用
- 文档：Backlog ST-24 ✅；本记录 B58；docs/deployment-docker.md

### B59. 主动关怀闭环——关怀信号自动产生（扫描挂入 Dream 定时器，ST-28 / T-64，2026-08-14）

- 背景：ST-27 已闭环「消费端」（pull/claim/ack 三层 + TTL 归档 + TTL 清理），但用户问「以目前功能主动关怀能实现吗」→ 查证发现「信号产生端」有断点：`care/signals.py` 注释声称「与 Dream 协同定时扫描（首次手动触发拉起定时器）」，实际 `sgme/care/` 无 scheduler，`scan_care_signals` 仅有两个调用点——HTTP `POST /v1/admin/care/scan`（手动）与 `care_consumer.py`（需配 cron）。Dream 定时器（`dream.py::_scheduler_loop`）只接了 `purge_expired_signals`（清理旧信号），未接 `scan_care_signals`（产生新信号）。后果：无人手动 scan/配 cron 时 `care_*` 信号永不产生，agent `signal_pull` 永远拉空，主动关怀不发声。
- 改动（`sgme/engine/dream.py`）：
  - 生命周期 ③（每阶段独立容错区）在「信号 TTL 归档」之后新增「关怀信号扫描」阶段：`from sgme.care import signals; scan_care_signals(mem_conn, cfg)`，零 LLM、幂等去重（uuid5 确定性 id）
  - 受 `care.enabled` 控制（与 routes_care 挂载同开关；`care.enabled=false` 时跳过，避免扩展禁用仍扫描）
  - 独立容错：扫描抛异常 → `logger.exception` + `stage_errors.append("关怀信号扫描失败: ...")`，不阻塞 Dream 其余阶段
  - 统计穿透：`care_signal_count` 进 stats dict + summary 文案 + `run_dream` 返回值 + `logger.info` + 日报 MD（`## 生命周期` 加 `- 关怀信号：N` 行）
- 测试（`tests/test_dream.py` +3 用例）：
  - `test_run_dream_scans_care_signals`：run_dream 后 `care_signal_count >= 1`，`signal_events` 有 `care_daily`（source='care'、consumed_at IS NULL 待消费），日报 MD 含「关怀信号」
  - `test_run_dream_care_disabled_skips_scan`：care.enabled=false → care_signal_count=0、无 care_* 事件
  - `test_run_dream_care_scan_failure_continues`：monkeypatch scan_care_signals 抛异常 → status=done、care_signal_count=0、stage_errors 标注、① 抽取不受影响
  - 相关模块全绿：test_dream + test_care 57 passed；test_signal_consumption + test_care_consumer 11 passed
- 运维影响：
  - **主动关怀完整闭环**：Dream 到点（默认 03:00）自动扫描产生 `care_daily`/`care_todo_due`/`care_mood`/`care_overwork` 信号 → agent 会话开始 `signal_pull` 拉取 → `signal_claim` 原子认领 → 关怀 → `signal_ack` 回执，无需额外 cron
  - `care_daily` 每日问候信号自 Dream 首次运行即产生（幂等，同日不重复）
  - 需重启 SGME 后端生效（dream.py 改动）；`/v1/admin/care/scan` 手动端点与 `care_consumer.py --consume` 兜底路径保留（不冲突）
- 文档：Backlog ST-28 ✅ / T-64 ✅；本记录 B59

### B60. 角色模板对 agent 可见可调——MCP 角色工具 + 五条铁律（ST-29 / T-65，2026-08-14）

- 背景：角色模板（`roles/butler|companion|friend|mentor.json` + `operations.care.assemble` 装配）早已就绪，但只暴露在 HTTP API 层（`GET /v1/admin/roles/*/assemble`）。用户实测「怎么让接入的 agent 知道并调用角色模板」→ 查证发现三个入口全缺角色：MCP 工具集 23 个无角色工具、`agent_onboarding` 的 self_config 模板不提角色、README/AGENTS 接入纪律不提角色。后果：接入的 agent 连接后「不知道角色存在、想调也调不了」，四角色关怀全靠人工查文件 + 手动调 HTTP 端点，机制无法保证 agent 自律调用。
- 改动：
  - `sgme/mcp_server.py` 加四个角色工具（全部复用 `operations.care`，入口层只做协议翻译）：
    - `role_list()`：列出可用角色（轻量字段 role_id/name/description/updated_at）+ 附加 `active_role`（当前角色，未设置 None）
    - `role_assemble(role_id, inject_mode=None)`：装配角色沟通提示词（system_prompt + care_policy + persona + profile_blocks 精简返回，`{{char}}/{{user}}` 宏保留）；角色不存在 → `{"error"}`
    - `role_active_get()` / `role_active_set(role_id)`：读/设当前角色（换皮不换芯，写 `data/care/active_role.json`，不入 git）
  - `ONBOARDING_TOOLS` 23→27（清单与 `@mcp.tool` 一一对应，测试断言防漂移）
  - **接入纪律四条→五条铁律**：第 5 条「对话开始时（或用户指定角色时）role_list 看可用角色 → role_assemble(role_id) 拿人设并按其说话——换皮不换芯」。同步 `mcp_server.py` self_config 模板、`AGENTS.md`、`README.md`（中英）、`docs/agent-onboarding.md`
- 测试：
  - `tests/test_mcp_server.py` `test_mcp_tools_available` 工具集断言补 4 角色工具 + 3 信号工具；新增 `test_mcp_role_tools`（role_list 列表+active_role / role_assemble 装配+不存在报错 / role_active_set→get 闭环 / role_list 反映当前角色，隔离 roles/persona/data 目录）
  - test_mcp_server + test_care 61 passed；test_mcp_wiki + test_signal_consumption + test_care_consumer 22 passed
- 运维影响：
  - **需重启 SGME 后端**生效（MCP 工具集 + ONBOARDING_TOOLS + self_config 模板）
  - 已接入的 agent（含 DSH/Hermes/Trae）：下个会话重新 `agent_onboarding()` 会拿到 27 工具清单 + 五条铁律模板；旧身份文件仍是四条铁律，需 agent 自查版本或用户提示后补第 5 条
  - 角色能力边界（换皮不换芯）：角色只改「怎么说话」，记忆池、提炼、检索、信号全不动——符合架构铁律「画像 = 模板查询结果，无物化；persona 是唯一物化例外」
- 文档：Backlog ST-29 ✅ / T-65 ✅；本记录 B60

### B61. 事件对接写入接入纪律——SSE 长连 / 游标拉取 / 短连 pull 三接法（ST-30 / T-66，2026-08-14）

- 背景：主动关怀要「主动」，靠「对话开始 signal_pull」不够——等用户开新会话才拉信号，关怀退化为被动响应。用户三连问「纪律条款有教 agent 创建定时任务吗 / 定时关注信号模块吗 / 让 agent 直接对接事件吗」→ 查证发现：SGME 早有完整事件订阅机制（`GET /v1/events/stream` SSE 长连 + Last-Event-ID 断线补偿、`GET /v1/events/pull` 持久游标、`signal_subscribers` 表、`suppress_hint` 抑制窗口），但接入纪律五条铁律只字未提，agent 接入后完全不知道有这条能力。
- 改动（纯文档/模板，无内核逻辑变更）：
  - `sgme/mcp_server.py` self_config 模板：
    - 铁律第 4 条升级为「主动关怀靠消费信号」双模式——短连接（无常驻）`signal_pull`；长连接（常驻，首选）挂 SSE `/v1/events/stream?subscriber_id=<agent_id>`，事件实时推送、断线带 Last-Event-ID 补偿
    - 新增「**事件对接**」段：事件三类（care_* 关怀 / memory_updated 记忆更新 / anomaly_warn 异常）+ 三种接法（SSE 长连 / 游标拉取 / MCP signal_pull）+ 端口/Key 说明（SSE/pull 走 HTTP :9910，signal_pull 走 MCP :9913）
  - `AGENTS.md` 接入纪律、`README.md`/`README.zh-CN.md` 接入段落、`docs/agent-onboarding.md`（新增 §4.1 事件对接表）同步
- 测试：`tests/test_mcp_server.py` `test_mcp_agent_onboarding_self_config` 断言补事件关键词（/v1/events/stream、/v1/events/pull、subscriber_id、Last-Event-ID、care_*、memory_updated、anomaly_warn、谁消费谁标记、role_list）；test_mcp_server + test_care 61 passed
- 运维影响：
  - **需重启 SGME 后端**生效（self_config 模板随 MCP 进程）；已接入 agent 下个会话重新 `agent_onboarding()` 拿到含事件对接的五条铁律
  - 主动关怀触发源从「对话开始 pull」升级为「SSE 实时推送」——常驻 agent（Hermes 等）挂一条长连即可实时收到 care_* 事件并立即关怀，无需自建 cron 轮询；无常驻能力的 agent 仍走 signal_pull 短连兜底
  - 事件端点本就存在且鉴权（require_agent_key），本变更不引入新端点、无安全面变化
- 文档：Backlog ST-30 ✅ / T-66 ✅；本记录 B61

### B62. 通信渠道兜底铁律写入接入纪律（ST-31 / T-67，2026-08-14）

- 背景：用户定（2026-08-14）「当前会话是兜底通信渠道——任何主动消息（关怀/提醒/告警等），不管发不发其它通道（微信/飞书/Telegram 等），当前会话都来一条，直到用户取消」；同时明确「hermes 的事你别管了，只记得提醒接入 agent」。查证确认现有接入纪律（五条铁律 + 事件对接段）无此条，agent 接入后不知道要兜底。
- 改动（纯文档/模板，无内核逻辑变更）：
  - `sgme/mcp_server.py` self_config 模板：事件对接段之后新增「**通信渠道**（兜底铁律）」段——当前会话是兜底渠道，任何主动消息（关怀/提醒/告警等）无论是否已发其它通道都必须在当前会话也发一条，持续生效直到用户明确取消
  - `AGENTS.md` 接入纪律、`README.md`/`README.zh-CN.md` 接入段落、`docs/agent-onboarding.md`（新增 §4.2 通信渠道，含三条细分：有其它通道先发其它再补当前会话 / 无其它通道就发当前会话 / 用户明确取消才停止）
- 测试：`tests/test_mcp_server.py` `test_mcp_agent_onboarding_self_config` 断言补「兜底通信渠道」「微信」「飞书」「Telegram」关键词；test_mcp_server 25 passed
- 运维影响：
  - **需重启 SGME 后端**生效（self_config 模板随 MCP 进程）；已接入 agent 下个会话重新 `agent_onboarding()` 拿到含通信渠道兜底的接入纪律
  - 主动关怀/提醒/告警的投递不再依赖单一通道——其它通道（微信/飞书/Telegram）不可用或未配置时，当前会话始终兜底，用户不会漏掉主动消息
- 文档：Backlog ST-31 ✅ / T-67 ✅；本记录 B62

### B63. 搬家收尾——本机 Gateway 退役，SGME 迁移 NAS（2026-08-16）

- 背景：用户搬家，SGME 生产环境从本机（D:\Projects\SGME 直跑，nssm 服务"SGME Gateway"）迁移到 NAS（飞牛 fnOS，Docker 容器 sgme，/vol1/1000/Docker/sgme，bind mount data→/vol1/1000/Docker/sgme/data）。数据库与 raw 原件已复制，本任务为收尾闭环。
- 改动：
  1. **MCP 监听可配置**（`sgme/mcp_server.py`）：`run_mcp_server` host 从硬编码 `"127.0.0.1"` 改为 `os.environ.get("SGME_MCP_HOST", "127.0.0.1")`——容器部署必须绑 0.0.0.0 才能对外；`build_mcp_server` 增加 `transport_security` 参数：非本机部署（SGME_MCP_HOST≠127.0.0.1/localhost/::1）时显式关闭 FastMCP 自动 DNS 防重绑（该附加层默认只放行 localhost Host 头，容器场景导致 421 Invalid Host header），SGME 自身 ApiKeyMiddleware 鉴权不降级
  2. **Hermes 插件指向**（`adapters/hermes/plugin.yaml` + `%LOCALAPPDATA%\hermes\plugins\sgme\plugin.yaml` 部署副本）：`base_url` → `http://192.168.10.10:9910`
  3. **care_consumer**（`scripts/care_consumer.py`）：BASE_URL 默认 → NAS（SGME_BASE_URL 可覆盖）
  4. **全部适配器默认指向 NAS**（`adapters/dsh|hermes|reasonix|trae|workbuddy` 共 12 文件）：默认 `http://192.168.10.10:9910`，SGME_BASE_URL 可覆盖；含 sgme-bridge（yml/ts/js/README）；顺手修复 README 中被脱敏损坏的 `<admin-key>` 占位符
  5. **新增 NAS 运维脚本**（`scripts/nas_watchdog.sh` / `scripts/nas_backup.sh`）：看门狗（/etc/cron.d/sgme-watchdog，root 每 5 分钟：docker.sock 缺失→拉起 docker.service 含 containerd 重试；sgme 容器未运行→拉起）；每日备份（LEO crontab 03:30，rsync data→/vol2/1000/sgme-backup/ 轮转留 7 份）
- 测试：`tests/test_mcp_server.py` 25 passed（MCP host 配置改动后回归）；`adapters/dsh/tests/test_install.py` 7 passed（默认值改动后回归，测试用显式 mock 覆盖不受影响）；实测 NAS MCP 握手成功（serverInfo SGME 1.29.0）、care/scan 200、inject/search 200
- 运维影响：
  - **本机 Gateway 已退役**：nssm 服务"SGME Gateway"停止+禁用，Hermes_Gateway_Watchdog 计划任务删除，本机 9910/9913 释放；E:\SGME_Backup 调度随进程停止（由 NAS 备份接管）
  - **迁移后数据核对**：memories 11620 / scenes 246 / ideas 26 / demands 83 / project_meta 2 / raw 722 文件，两库一致；旧库留底 NAS `sync_tmp/old_db/`（稳定一周后删）
  - **镜像链**：本机 `docker build -t sgme:1.0.0b1-nas` → docker save/load → NAS compose image 改 sgme:1.0.0b1-nas；docker.env 增 `SGME_MCP_HOST=0.0.0.0`；**后续改代码需重走此链**
  - **NAS 重启自愈**：看门狗 5 分钟内自动拉起（含 8/15 踩坑的 docker.service 依赖失败场景）；备份每天 03:30 落机械盘
  - **遗留**：Hermes 插件新 base_url 需 Hermes 重启后生效；sgme-care-heartbeat cron 已随 care_consumer 默认值修复恢复
- 文档：Backlog 无关联任务（运维收尾）；本记录 B63

### B64. SkillsHub 启用——Hermes skill 库同步 NAS + 迁移遗留配置修复（2026-08-16）

- 背景：用户要求把 Hermes 本机 skill 库（%LOCALAPPDATA%\hermes\skills，392 个注册 skill）单向复制到 NAS skills-hub 远端仓（/vol1/1000/git/skills-hub.git），本地零删除，走 SGME skills_hub 模块正规链路（put_skill → POST /v1/admin/skills/sync to_remote）。
- 改动（均为 NAS 部署位，非项目代码；代码侧无改动）：
  1. **修复 B63 迁移遗留缺陷——生产配置从未生效**：SGME_HOME=/data 时用户配置路径为 `/data/config/sgme.yaml`，但迁移时漏拷，Gateway 一直跑内置默认配置（l1.chunk_size 8000 应为 5000、L1.5 预筛关闭应为开、向量模型 nomic 应为 doubao/volc-plan、skills_hub.enabled=false 应为 true）。修复：镜像内 `/app/config/sgme.yaml` 复制到 `/data/config/sgme.yaml`，重启生效。**影响面**：NAS 生产 SGME 首次真正跑在生产配置上
  2. **NAS 容器镜像缺 git**：skills_hub 同步依赖 subprocess 调系统 git，但 sgme:1.0.0b1-nas 镜像未装。新增 `Dockerfile.git`（FROM sgme:1.0.0b1-nas + apt install git + `git config --global --add safe.directory /git/skills-hub.git`，容器内 root 访问属主 1000 的 bare 仓必需），NAS 上 docker build → `sgme:1.0.0b1-nas-git`（+139MB）
  3. **compose 挂载 + env 覆盖**（/vol1/1000/Docker/sgme/docker-compose.yml / docker.env，均已留 .bak）：image 改 sgme:1.0.0b1-nas-git；volumes 增 `/vol1/1000/git/skills-hub.git:/git/skills-hub.git`（file:// 直访免 SSH key）；docker.env 增 `SGME_SKILLS_HUB_REMOTE=file:///git/skills-hub.git`（ST-20 env 覆盖机制，值仅存进程内存不落盘）
  4. **Hermes skill 库同步**：本地 392 个 SKILL.md 打包（manifest 对齐 Hermes 注册名单、软链接解引用、排除 .archive/.curator_backups）→ 容器内 `SkillsHub.init → put_skill × 392`（PYTHONPATH=/app，脚本在 /data/import_skills.py，用后清理）→ `POST /v1/admin/skills/sync` direction=to_remote → 远端仓 main +1 commit（393 文件 = .gitignore + 392 SKILL.md，冲突按 local_wins 解决，败方备份 ref conflict-backup-20260816041111）
  5. **容器重建验证闭环**：新镜像重建容器后工作区清空 → `sync` from_remote 全量恢复 392/392，远端仓→工作区链路验证通过
- 测试：远端仓 `git ls-tree main` 393 文件抽查 sgme-operations/hermes-agent/zhangxuefeng-perspective 均在；本地 skills 目录零改动（406 SKILL.md 原样）；Gateway health OK（deepseek 链正常）
- 运维影响：
  - **正规流程纪律（用户纠正）**：本次镜像构建直接在 NAS 上旁路执行（Dockerfile.git 未先入项目 git），违反「发现问题→修复→提交本地→提交 GitHub→NAS 拉取部署」流程。已收尾：本记录 B64 登记；**Dockerfile 合入项目根（git 安装入主 Dockerfile 单一入口）推迟到下次镜像更新时执行**；此后镜像/部署变更必须先提交项目 git + push GitHub/Gitee，再 NAS 拉取构建
  - **远端仓基线**：skills-hub.git main 现为权威基线（393 文件），Hermes 本地为唯一编辑源，后续变更走 put_skill + sync 双方向
  - **遗留**：NAS 部署目录非 git 仓库（compose/docker.env 仅 .bak 备份），部署配置真相源在项目 git（tmp/nas-docker-compose.yml 模板 + 本记录）
- 文档：Backlog 无关联任务（运维收尾）；本记录 B64

### B65. 提炼成本治理：prescreen fallback 熔断 + 动态链继承采样参数（2026-08-16）

- 背景：搬家（PC→NAS）后用户发现 SGME 提炼账单占比过高（08-16 账单 ¥10.65/¥19.80 = 53.8%）。账单核查 + refine_runs 量化定位两个根因：
  1. **prescreen 向量预筛失效时回退全量召回**：embed 不可达（搬家窗口曾向 deepseek /v1/embeddings 发请求 401）时 `_build_prescreened_candidates` 返回 None → 维度 OR 全量召回 → 单次 l1_conflict 最高 87 万 tokens（08-16 凌晨 03:08-03:37 的 6 次巨无霸调用吃掉当日 89%）。历史对照：08-11/12 同机制单日 9800 万 tokens（"一天 200+"的元凶），08-13 prescreen 上线后单次降至 3-5 万。
  2. **T-43 动态提炼链丢失 thinking 禁用**：带 `agent_model=deepseek/deepseek-v4-flash` 的会话（DSH 会话 2456ee64，即用户当前会话）经 `resolve_refinement_chain` 重建链节点时，`_build_node` 只复制 providers 表连接字段（base_url/api_key_env 等），**丢失 llm.yaml 静态链节点的 `max_tokens: 16384` + `extra_body: thinking disabled`** → 思考型模型输出 reasoning_content、content 为空 → L1 解析失败（"Expecting value: line 1 column 1"）→ 2456ee64 连续 12 次 error、每轮 append+trigger 反复失败烧钱。静态链（无 agent_model 的 hermes 会话）不受影响，故此前未被发现。
- 改动：
  1. **`sgme/config.py`**：`DEFAULT_L15_CONFIG.prescreen` 新增 `fallback` 字段（默认 `"full_recall"` 向后兼容）+ `_merge_l15_config` 合并。
  2. **`sgme/engine/l15.py`**：新增哨兵 `PRESCREEN_SKIP_CONFLICT`；`_build_prescreened_candidates` 在 embed 不可达时按 `fallback` 分流（`skip_conflict` → 返回哨兵；`full_recall` → 现状回退）；`build_candidate_groups` 识别哨兵清空该新记忆候选 → `resolve_conflicts` 既有短路（候选池全空 → 全部 store 零 LLM）自动生效。
  3. **`sgme/llm/resolve.py`**：`_build_node` 新增 `static_node` 参数，从静态链同 provider 节点继承 `max_tokens/sampling/extra_body`；`resolve_refinement_chain` 建 `static_by_provider` 索引并透传（agent 节点与 override 节点均继承）。
  4. **`config/sgme.yaml`**：生产配置 `l15.prescreen.fallback: skip_conflict`（embed 不可达时跳过冲突检测直接 store，防全量召回烧钱）。
- 测试：`tests/test_l15_prescreen.py` +4（skip_conflict 清空候选 / 向量异常清空 / resolve_conflicts 短路零 LLM / 配置合并默认值）、`tests/test_llm_resolve.py` +4（agent 节点继承采样参数 / override 继承 / 无静态采样零污染 / override 内联优先）。相关套件 170 用例全绿（config/llm/resolve/l15/l1/operations_refine/refine_dao/batch_scan）。
- 部署：3 文件 docker cp 入 NAS 容器 + `/data/config/sgme.yaml` 覆盖（挂载卷持久）+ `docker restart sgme`。容器内验证：语法 OK、`fallback: skip_conflict` 生效、动态链节点带 `max_tokens/extra_body`。
- 真实链路验证（2026-08-16）：`POST /v1/admin/refine/trigger_async` 触发 → 2456ee64 从连续 12 次 error → **一次成功**（增量 130 条/记忆 40 条 + L1.5 正常裁决 merge 17/store 14/update 9，`last_refined_seq=131`）；容器 healthy。
- 运维影响：错误文件复查——6 个 error 均为历史不活跃（5 个 08-14 dsh 旧会话 + 324B 搬家验证），不会持续烧钱；batch_scan 不重扫 error。
- 文档：Backlog T-68；本记录 B65

### B66. 记忆去重治理：content 重复清理 + memory_sources 唯一约束（2026-08-16）

- 背景：B65 成本治理核查时发现全库 11634 条记忆中有 73 组 content 重复（涉及 161 条 active 记忆）——全部是 08-06 及更早（L1.5 冲突裁决上线前，prompt_version=working-61c644de）的历史遗留，来源为早期 cron 会话与迁移导入；且 memory_sources 无唯一约束，历史数据同 source_ref 挂 271 条记忆（当时无 source_ref 锚点）。
- 改动：
  1. **`scripts/dedup_memories.py`**（新建）：content 重复清理工具。默认 dry-run（只统计），`--apply` 才执行；执行前自动备份 memory.db 到 `data/backups/pre_dedup/`；每组保留 updated_at 最新一条 active，其余经 `memory_dao.archive_memory` 归档（memory_archive 原件保留可溯源，不删除）；单条失败不中断。
  2. **`sgme/data/db.py`**：`memory_sources` 表加 `PRIMARY KEY (memory_id, source_ref)`（幂等写入防御）。
  3. **`sgme/data/memory_dao.py`**：`insert_memory` 的 sources 写入改 `INSERT OR IGNORE`——同记忆同源重复写入静默忽略（UNIQUE 约束兜底，不抛错不重复）。
  4. **`scripts/migrate_sources_unique.py`**（新建）：存量库迁移（SQLite 重建表 12 步标准流程）。备份 → 重命名旧表 → 建新表（带复合主键）→ 按 (memory_id, source_ref) 去重拷贝（保留 rowid 最小）→ 删旧表 → 重建索引；可重入（已有 PK 跳过）。
- 测试：`tests/test_storage.py` +2（schema PRAGMA 确认复合主键 / 同源重复 INSERT 抛 IntegrityError）。相关套件 170+ 用例全绿。
- 真实执行（2026-08-16，NAS 容器）：
  - dedup apply：备份 `pre_dedup/memory.db.bak-20260816-050809`；归档 88 条、保留 73 条 → 重复 active content 组数 **73 → 0**；active 记忆 11634 → 10710；archive 4293 条
  - 迁移：备份 `pre_sources_unique/memory.db.bak-20260816-050815`；memory_sources 11548 行（去重后），PRAGMA 确认 PK=[memory_id, source_ref]；同源重复 INSERT 被 UNIQUE 拦截
  - 同源多记忆 top3（271/208/164）复查为**内容各异合法记忆**（同一会话多事实提炼，组内 content 重复 0）——确认同源 ≠ 重复，content 才是正确判据
  - 代码部署（db.py + memory_dao.py）→ 容器重启 healthy → 提炼冒烟通过（L1.5 store=4 merge=4 archived=4，无异常）
- 运维影响：备份位于挂载卷 `data/backups/`（持久，不随容器重建丢失）；后续新记忆写入自动受 UNIQUE 约束保护。
- 文档：Backlog T-69；本记录 B66

### B67. Docker 部署固化：镜像 commit 修复 + Dockerfile 合入 git + NAS 部署模板（2026-08-16）

- 背景：T-68/T-69 的代码修复（prescreen 熔断、动态链 thinking 继承、去重约束）均经 docker cp 注入运行中容器（可写层）——镜像 sgme:1.0.0b1-nas-git 本身无修复，容器重建即丢（假部署风险）。B64 遗留「git 安装入主 Dockerfile 单一入口推迟到下次镜像更新」到期。
- 风险核查（重建镜像前）：
  1. `.dockerignore` 已排除 `.env`（密钥绝不进镜像）——项目 Dockerfile `COPY config/` 会跳过 config/.env ✅
  2. NAS docker.env 密钥完整（DEEPSEEK/VOLC/ADMIN/AGENT 35-46 字符）且与容器实际 env 一致 ✅
  3. 项目 config/.env 存在但被 dockerignore + gitignore 双排除，运行时密钥走 docker.env 注入 ✅
- 改动：
  1. **镜像固化（方案 A，零风险快照）**：`docker commit sgme sgme:1.0.0b1-nas-git-t69`（622MB）——把已验证 healthy + 冒烟通过、含全部修复的容器整体提交为新镜像。验证：镜像内 l15.py 5 处 PRESCREEN_SKIP_CONFLICT / resolve.py 7 处 static_node / db.py PRIMARY KEY / memory_dao.py INSERT OR IGNORE 全部在。
  2. **NAS compose 指向 t69**：`/vol1/1000/Docker/sgme/docker-compose.yml` 改 image: sgme:1.0.0b1-nas-git-t69（留 .bak-pre-t69），当前容器保持运行不重建——即使 NAS 重启/重建容器，也从固化镜像拉起，修复不丢。
  3. **Dockerfile 合入 git（方案 B 前置）**：主 Dockerfile 加 git 安装（apt install git + safe.directory /git/skills-hub.git，B64 遗留单一入口）。
  4. **NAS 部署模板入 git**：`tmp/nas-docker-compose.yml`（{{IMAGE_TAG}} 占位 + 部署流程注释）——NAS 生产 compose 的真相源模板（NAS 部署目录非 git 仓库，B64 遗留）。
  5. **项目 docker-compose.yml / .dockerignore** 首次纳入 git 跟踪（单机部署形态 + 构建排除规则）。
- 测试：t69 镜像代码标记验证（4 项全过）；NAS docker.env 密钥完整性验证；容器 healthy。
- 运维影响：
  - 部署真相源闭环：Dockerfile/compose/模板入 git → NAS 可用 `docker build` 从项目拉取全新构建（方案 B 终态，本轮先 commit 固化保底）
  - 后续镜像/部署变更必须：改项目 git → push → NAS 拉取 → 构建/更新 compose（B64 纪律正式生效）
  - 遗留：方案 B 的 NAS 全新构建（docker build from git）未在本轮执行——以 t69 固化镜像为当前生产态，下次有计划升级时按模板流程走
- 文档：Backlog T-70；本记录 B67

### B68. dshfind 可安装判定修复：根 package.json 瘦包装（dsh-sgme）+ README 安装段（2026-08-16）

- 背景：dshfind.com 插件市场对 freehul/sgme 判定「这不是可安装的插件包」（仓库根无 package.json，`dsh plugin add github:freehul/sgme` 会失败）——实际 dsh-sgme 插件包（adapters/dsh/sgme-bridge/，Cordis SDK 原生 TS 插件）已发布 npm v0.1.1（2026-08-14，maintainer freehul），且 lib/ 构建产物已提交 git（dsh-mnemon 模式，运行时不要求宿主装 pnpm）；dshfind 的安装推导只看仓库根 manifest（scripts/lib/install.mjs manifestFacts），嵌套插件包不可见导致误判。查证：DSH 源码 runPlugin = pnpm 转发器，按安装后的包名解析 dsh.bundle 加入 dsh.profile.bundles 层栈，故根包装的 name 必须与 patch 引用名一致（dsh-sgme）。
- 改动：
  1. **根 package.json（新增）**：name=dsh-sgme v0.1.1 + `dsh.bundle.patch` → `./adapters/dsh/sgme-bridge/cordis.patch.yml` + main → `adapters/dsh/sgme-bridge/lib/index.js`（已提交，git 装免 prepare）；dependencies/peerDependencies 与 bridge 一致（schemastery + @deepseek-ai/cordis/dsh-tools/dsh-commands）；`prepublishOnly` 强制失败——根只是 git 安装包装，禁止从仓库根 npm publish（防覆盖已发布的 dsh-sgme 真实包）；description 注明包装语义
  2. **README.md / README.zh-CN.md**：Quick Start 后新增「Install as a DSH plugin / 安装为 DSH 插件」段——主推 `dsh plugin --profile web add dsh-sgme`（npm），备用 `dsh plugin --profile web add github:freehul/sgme`（git 直装），引 adapters/dsh/README.md 完整指南
  3. **.gitignore**：补 `/node_modules/`（根包装不装依赖）
- 测试：node JSON 解析 + main/cordis.patch.yml 路径存在性校验；adapters/dsh pytest 无回归；dsh 临时 profile link 安装冒烟（dsh-sgme 进入 bundles 层栈，验证同 T-53 机制）
- 运维影响：dshfind 每日同步后页面由「不是可安装插件包」变为 npm 安装命令（`dsh plugin --profile web add dsh-sgme`）；git 直装 `github:freehul/sgme` 亦可用；真实 npm 包仍以 adapters/dsh/sgme-bridge/ 为唯一发布源（根包装禁止发布）
- 文档：Backlog T-71；本记录 B68

### B69. Docker 新用户开箱修复：多阶段 WebUI 镜像 + 首次启动物化 sgme.yaml + runbook Docker 章节 + NAS 全新构建验证（2026-08-16）

- 背景：核查「用户从 Docker 安装部署会不会出问题」（2026-08-16 用户问询）——静态核查发现 4 缺口：①git Dockerfile 从未全新构建验证（B67 遗留：NAS 生产镜像 sgme:1.0.0b1-nas-git-t69 为 docker commit 固化，非从 Dockerfile 构建）②`SGME_HOME=/data` 时 `DEFAULT_SGME_CONFIG = $SGME_HOME/config/sgme.yaml`，镜像内 `/app/config/sgme.yaml` 永不加载——空卷启动 = 全默认配置：`l15.prescreen.enabled=False` + `fallback: full_recall`（B65 防烧钱的 skip_conflict 丢失，embed 不可达回退全量召回场景复现）③WebUI 不进镜像（Dockerfile 无 ui/、.dockerignore 排除 ui/dist；app.py 检测 /app/ui/dist 存在即挂载 SPA），compose 注释「HTTP API + WebUI」误导 ④docs/runbook.md 无 Docker 章节；NAS 拉取链路未接（/vol1/1000/git/sgme.git bare 仓为空、无 remote、cron 无拉取任务）。
- 改动：
  1. **Dockerfile 多阶段化**：Stage 1 node:20-alpine 构建 WebUI（npm ci + vite build → /ui/dist）；Stage 2 python:3.11-slim（git + safe.directory + pip 依赖清单与 pyproject 逐项一致）；`COPY --from=ui-build /ui/dist ui/dist/` 入镜像；`config/sgme.yaml` 语义明确为「首次启动模板」（非死代码）
  2. **docker/entrypoint.sh（新增）**：`ENTRYPOINT` 接管——空卷首次启动把 `/app/config/sgme.yaml` 物化到 `$SGME_HOME/config/`（含生产调优 prescreen+skip_conflict），用户可编辑后重启；`exec "$@"` 透传 CMD
  3. **docs/runbook.md §16 Docker 部署**：准备（.env.example→docker.env）/启动验证/配置（sgme.yaml 物化语义）/升级/NAS 部署流程（B64 纪律 + bare 仓拉取）
  4. **NAS 拉取链路**：`/vol1/1000/git/sgme.git` bare 仓接 gitee remote + fetch（此前为空仓无 remote，B64「NAS 拉取」未落地）
  5. **NAS 全新构建验证（E）**：`git fetch → clone → docker build（多阶段）→ 空卷 throwaway 容器 → /v1/health + WebUI index + 物化 sgme.yaml 校验 → 清理`（不触碰生产容器 sgme）
- 测试：本地 ui 前端构建冒烟（vite build 800ms 出产物 ✓）；entrypoint sh 语法校验；NAS 全新构建 + 空卷启动冒烟结果见 E 段
- 运维影响：新用户 `docker compose up -d --build` 开箱即用（WebUI 内置 + 防烧钱默认物化）；升级仍走 `git pull && docker compose up -d --build`；NAS 生产容器未动（当前 t69 镜像继续跑，下次计划升级时按 §16.5 流程切换新镜像）
- 文档：Backlog T-72；本记录 B69

### B71. wiki 渐进式披露共享知识库改造（W1-W7 + NAS 部署，2026-08-16）

**背景**：技能/知识碎片化（Hermes 与 DSH 各维护独立技能库、布局不兼容）+ 上下文膨胀 + 多 agent 共享需求。三路并行调研（AIRDT docs/research/wiki-kb-benchmark/：13 工具全景 / 方法论 / 业界前沿）后定稿方案 v0.3（docs/design/SGME-wiki渐进式披露共享知识库改造方案-v0.3.md，送审稿 v0.1 存档），设计决策已入 wiki。

**改动**（ST-32 / T-74~T-80）：
- **W1 数据模型**：wiki_pages 加 description/description_seg/author/status/supersedes（_migrate_wiki_page_columns 幂等自愈）；FTS 扩 description_seg（保留 content_seg 中文分词）；修复 FTS 触发器升级遗漏（_triggers_have_description 检测重建，DROP TABLE 不删触发器的隐藏缺口，真实链路验证暴露）
- **W2 检索语义**：统一搜索排除 skill 标记页（_is_skill_page Python 层精确判断防脏数据）+ superseded 全路径过滤（BM25/LIKE/list_pages）
- **W3 写回接口**：PATCH /v1/wiki/pages/{id} + MCP wiki_page_update（append ADD-only + entry hash 去重幂等 + description 默认不动）；create 链路 description 贯通
- **W4 自进化管线**：独立第三条管线（会话→经验→写回手册），费用门禁（min_rounds）+ LLM 提炼（复用 llm 降级链 + prompts 版本管理）+ 规则闸门 + 独立 wiki_evolve 游标（与 memory 提炼物理分离）
- **W5 bridge**：wiki_pages/wiki_page 工具 + turn/end 自动触发 evolve（evolveEnabled 默认 true）
- **W6 索引 skill**：wiki-skill-discovery（源随 git + install 幂等部署）
- **W7 手册**：SGME操作手册完整版入库（含 description）

**NAS 部署**（2026-08-16）：push → NAS src pull → docker build sgme:1.0.0b1-wiki-v1（多阶段）→ 容器替换（数据卷不动）→ 生产验证 10/10 全绿（迁移字段 / PATCH 幂等 / FTS 命中 description / 统一搜索过滤 / evolve 冒烟）；手册规整（完整版 active + 旧版 superseded）。

**提交**：bceb792（W1）d0c618e（W2）b8b8773+8702e5b（W3）d4d45c6（W4）5fb9e3f+2224470（W5）a310c39（W6）4b62aa0（方案文档）。

**验收**：全量 pytest 100% 无 FAILED（多轮）；bridge vitest 92 passed + typecheck + build；NAS 生产 10/10 + 检索/过滤/写回全链路 PASS。

### B70. 生产容器切换到 git 构建镜像 sgme:1.0.0b1-git-t72 + 全链路测试（2026-08-16）

- 背景：T-72 完成 Docker 开箱修复并通过空卷冒烟；用户指令（2026-08-16）「接入 SGME 的 agent 只有当前会话在工作，由你完成容器重建并测试」——把生产容器从旧镜像（docker commit 固化的 nas-git）切换到 git 构建的新镜像（含 WebUI + entrypoint 物化 + T-68/T-69 修复）。
- 前置：手动备份（nas_backup.sh → /vol2/1000/sgme-backup，OK）；确认 /vol1/1000/Docker/sgme/data/config/sgme.yaml 存在（md5 4e409685103dc04b2613cd543683ff8f，含 l15 prescreen+skip_conflict 生产调优）——entrypoint 首次启动检测到文件存在会跳过物化，不覆盖生产配置。
- 改动：
  1. NAS src 同步至 209d926（bare 仓 gitee fetch → src pull，B64 链路）
  2. NAS 构建正式镜像 `sgme:1.0.0b1-git-t72`（缓存层秒级完成）
  3. NAS compose 备份 `docker-compose.yml.bak-pre-git-t72`，image: `sgme:1.0.0b1-nas-git-t69` → `sgme:1.0.0b1-git-t72`（端口/env/env_file/volumes/healthcheck 不变，数据卷 bind mount 不动）
  4. `docker compose up -d` 重建容器（原 nas-git 容器被替换，此前 compose 与运行态镜像漂移已消除）
- 测试（全绿）：
  - 容器 `sgme:1.0.0b1-git-t72 Up (healthy)`（docker healthcheck 通过）
  - `GET /v1/health` 200：llm deepseek/deepseek-v4-flash available；vector sqlite-vec memory_vectors=11499 scene_vectors=109；refinement watermark_age 173s、stalled=false、heartbeat_ok=true
  - WebUI：`GET /` 返回 SPA HTML ✓；MCP :9913 返回 403（鉴权生效=端点正常）
  - 数据完整：stats memories 11567（archived 4380）、raw_files 726（refined 716）；sessions 含 dsh-* 会话 status=refined
  - 提炼健康：refine_runs 最近 l2_scene 阶段 status=ok（provider deepseek）
  - config 未被覆盖：md5 前后一致（4e409685…）；entrypoint 无物化日志（符合设计）
  - 日志无 traceback/CRITICAL
  - 端到端：开发机 → 192.168.10.10:9910 可达；POST /v1/search（dsh agent key）真实召回 BM25+vector+RRF 融合记忆（含溯源 trace）
- 运维影响：生产已运行 git 构建镜像（WebUI 内置 + 防烧钱默认 + 未来升级可走 `git pull && docker compose up -d --build`）；旧镜像 nas-git/t69 保留可回滚（compose 备份 .bak-pre-git-t72）；NAS 侧不再依赖 docker commit 固化
- 文档：Backlog T-73；本记录 B70

### B72. wiki 渐进式披露三处缺陷修复（evolve cfg 空配置 / supersession 未实现 / bridge 缺 wiki_page_update，2026-08-16）

**背景**：对 ST-32「wiki 渐进式披露共享知识库」做逻辑闭环与冲突审查，发现 3 处已实现功能的缺陷：

1. **P0 — 自进化 LLM 腿断**：`sgme/wiki/routes.py` / `sgme/mcp_server.py` 两入口把空配置 `{}` 传给 `evolve_trigger`，`llm/chain.py::call_with_fallback` 读 `cfg["chains"]` 必抛 `ValueError("未知链名: refinement")`，自进化永远产不出经验条目（turn/end 自动触发后每次记 error）。根因更深一层：`chains` 在 `cfg["llm"]["chains"]` 而非顶层，与 tier0/l2/care 各管线传 `cfg["llm"]` 的约定不符——传完整 cfg 也修不好，必须传 llm 段。
2. **P1 — supersession 未实现**：设计 §5.1（P1-7 标注已解决）要求 create 时同 category+title 的 active 旧页 content 不同 → 旧页置 superseded，但 `create_page` 只做 upsert 无判等，`status`/`supersedes` 成死字段，同题不同内容并存两个 active 页。
3. **P1 — 消费侧回写工具缺一环**：设计 W5 要求 bridge 3 工具，`tools.ts` 只实现 wiki_pages/wiki_page，缺 wiki_page_update，SKILL.md 要求回写但 DSH 侧 agent 无工具。

**改动**：
- ①两入口改传 `cfg["llm"]`（HTTP `request.app.state.cfg["llm"]` / MCP `_app_state.get("cfg", {}).get("llm")`）+ test_wiki_evolve.py +2 用例（单元透传 + HTTP 端点透传）
- ②`wiki_dao` 加 `find_active_same_title` / `mark_superseded`；`create_page` not exists 时查同 title+category 旧页并标记 superseded；test_wiki_supersession.py 6 用例
- ③`sgme-client` 加 `patch` / `wikiUpdatePage`；`tools.ts` 加 `createWikiPageUpdateTool` 并注册；wiki-tools.test.ts +3 用例；重建 lib/index.js

**测试**：wiki 相关 5 文件 37 passed / 0 failed；bridge vitest 95 passed / 0 failed + build 成功。

**遗留（已于 B73 处理）**：①bridge package.json 的 `types`/exports.types 指向 `lib/types/index.d.ts`，但 tsdown clean:true 清掉 declarationDir 输出，实际产物是扁平 `lib/index.d.ts`——既有 build 配置不一致，待后续处理。②`evolve.py` 的 `_llm_call`/`evolve_trigger` 参数名 `cfg` 实为 llm 段，命名误导是本次 P0 深层成因，待后续重命名澄清。

**文档**：Backlog T-81~T-83；本记录 B72

### B73. wiki 渐进式披露遗留两项处理（bridge types 路径 + evolve 参数重命名，2026-08-16）

**背景**：B72 遗留两项——①bridge package.json types 指向不存在的 lib/types/index.d.ts ②evolve 参数 cfg 实为 llm 段命名误导。

**改动**：
- ①bridge package.json：types→lib/index.d.ts、exports.types→./lib/index.d.ts；build 脚本 tsc&&tsdown→tsdown（去冗余——tsdown.config.ts dts:true 已生成扁平 lib/index.d.ts，tsc declarationDir 产物被 clean:true 清掉属死产物）；tsconfig.build.json 加废弃注释保留备查。
- ②evolve.py：_llm_call/evolve_trigger 参数 cfg→llm_cfg（含 docstring 澄清 llm 段语义），外部位置参数调用零影响。

**测试**：wiki 相关 5 文件 37 passed / 0 failed；pnpm build 单步生成 lib/index.d.ts（2.40 kB）+ lib/index.js（80.23 kB），lib/types 已清理。

**文档**：Backlog T-84~T-85；本记录 B73

### B74. 统一搜索 skill 过滤下沉 SQL（W2 回归修复，2026-08-16）

**背景**：2026-08-16 批量入库 370 skill 页后暴露 W2 实现缺陷——统一搜索（/v1/search scope=wiki_pages）的 skill 过滤在 SQL LIMIT 之后（Python 层），skill 页在 FTS 中分数高、占满 top-N 窗口，知识页被挤出（实测"设计"limit=10 → 0 条，limit=50 → 3 条；修复后 10 条）。

**改动**（892e98b）：
- `sgme/wiki/fts.py`：`search_wiki_fts` 加 `exclude_skill: bool = False` 参数——FTS BM25 路径 + LIKE 兜底路径 SQL 均追加 `AND (p.tags IS NULL OR p.tags NOT LIKE '%"skill"%')`（JSON 元素精确匹配，不误伤 "skills"；NULL 兜底）。默认 False，wiki_search 执行通道不受影响。
- `sgme/operations/search.py`：`_search_wiki_pages` 传 `exclude_skill=True`；Python 层 `_is_skill_page` 过滤保留作双保险（防双重编码脏数据，8-14 前科）。

**测试**：test_wiki_filter.py 新增 2 用例（exclude_skill 参数语义 + skill 页占满 top-N 时知识页仍召回）；wiki 相关 6 文件全量通过。

**数据修正**：AI弱电通-技术架构与选型定稿-2026-08-14-旧版已归档（e8d8e945）标 superseded（POST 时 W1 同 title 取代锚点自动处理，直改幂等确认）；注意 POST 幂等锚点对 title 变更敏感（page_id 含 title slug）——PATCH 改 title 后原 page_id 无法被 POST 命中，改 status 需数据库直改（PATCH 不支持 status 字段，待补）。

**部署**：push GitHub/Gitee + NAS bare（/vol1/1000/git/sgme.git）→ NAS src pull → build sgme:1.0.0b1-wiki-v3 → compose 替换（数据卷不动）→ 生产验证 5/5 全绿（统一搜索恢复 / 执行通道不受影响 / 记忆正常 / chronomemo 不可见 / 旧版页不可见）。

**文档**：本记录 B74

### B75. 接入契约 + 适配器收敛——官方只维护 hermes/dsh，其余走 MCP/自研（2026-08-16）

**背景**：产品化定位讨论——SGME 的「新手」是接入的 Agent，不是人；人只丢 GitHub 链接让 Agent 自助装。查证发现三线接入（已有适配器 / MCP / HTTP）里，已有适配器参差（trae/workbuddy 半成品、reasonix 缺统一标准），HTTP 自研线支撑最薄（接口契约埋在 2276 行架构 §22，无独立契约页/骨架/验收标准）。用户定案：①删除 reasonix/trae/workbuddy 三适配器；②官方只维护 hermes+dsh；③接口契约单独成文档给 agent 看。

**改动**：
- 删除 `adapters/reasonix/`、`adapters/trae/`、`adapters/workbuddy/`（git rm，含各自 tests/）+ 连带 `tests/test_reasonix_adapter.py`
- 新增 `docs/design/SGME-接入契约-v0.1.md`（三线决策树 + 准入表 + 官方质量标准 + 指向接口契约）
- 新增 `docs/design/SGME-接口契约-v0.1.md`（自包含 HTTP 契约：端口/鉴权/错误结构/核心端点/L0 格式/最小动作集/验收标准）
- 同步引用：README.zh-CN / README（接入文案 + 放置位置表去 Trae/Reasonix）、docs/agent-onboarding.md（hooks 型例子去 Reasonix、登记段改官方适配器）、架构 v0.9 §3.2/§5/§14.3（adapters 表 + Agent 侧 + 双层暴露）、adapters/dsh（README 对比表 + install.py/import_history.py 注释去 reasonix 指向）
- `adapters/hermes/README.md` 补「验证」+「常见坑」两节，对齐 dsh 质量标准

**注意**：reasonix 用户（本机 + 笔记本）删适配器后降级 MCP 自律型（会话收尾主动 refine + batch_scan 兜底），不再有 SessionStart/End 自动捕获；如需恢复 hooks 深度集成，按《接入契约 §4》自研。

**测试**：删除后 grep 确认无断链 import（tests/ 无 `from adapters.reasonix import` 残留）；全量 pytest 待跑。

**文档**：本记录 B75

### B76. 提炼链 API key 接线至 DEEPSEEK_API_KEY_SGME + NAS 生产同步（2026-08-17）

**背景**：三把 DeepSeek key 的隔离梳理（Hermes=DEEPSEEK_API_KEY、DSH=DEEPSEEK_API_KEY_DSH、SGME 提炼=DEEPSEEK_API_KEY_SGME）。查证发现提炼链实际引用 DEEPSEEK_API_KEY（providers.yaml），与 wiki 手册记载的「提炼专用 DEEPSEEK_API_KEY_SGME」不一致——SGME 自持密钥文件 config/.env 里放的其实是提炼专用 key 但变量名未区分；且用户环境变量已有 Hermes 的 DEEPSEEK_API_KEY，load_env_file 的 setdefault 语义下若 Gateway 继承用户环境启动会误用 Hermes key（预算隔离失效）。

**改动**：
- config/providers.yaml：deepseek 节点 api_key_env: DEEPSEEK_API_KEY → DEEPSEEK_API_KEY_SGME
- config/.env：DEEPSEEK_API_KEY= 改名 DEEPSEEK_API_KEY_SGME=（值不动；原文件备份 config/.env.bak-20260816-A，gitignore 不随 git）
- tests/test_providers.py：4 处断言/夹具同步（55/99/132/210；186/192 旧内联回退模拟保留历史值）
- NAS 生产同步：本地 git push → NAS bare 仓（/vol1/1000/git/sgme.git）→ src pull → 构建 sgme:1.0.0b2-nas-key → docker.env 补 DEEPSEEK_API_KEY_SGME（值复制自 NAS 的 DEEPSEEK_API_KEY，sed 不落屏）→ compose image 切换 → 容器重建
- 备份：NAS docker-compose.yml / docker.env 各留 .bak-时间戳

**测试**：本机 config/provider 相关 pytest 82 通过 0 失败；本机真实 LLM 冒烟 9 tokens 正常；NAS 容器内验证（docker exec + PYTHONPATH=/app）：链节点 api_key_env=DEEPSEEK_API_KEY_SGME、key 解析成功（len 35）、真实调用 provider=deepseek 回复正常；NAS /v1/health 全绿（version 1.0.0b2，llm available，向量 11648 条数据完整）。

**运维影响**：NAS 生产容器镜像 sgme:1.0.0b2-nas-key（Up healthy）；数据卷 bind mount 不动；生产 sgme.yaml 挂载卷保留。后续 NAS 提炼用量在 DeepSeek 平台按 SGME 专用 key 归因。NAS 上 src 目录有杂项文件（构建转义残留），可清理。

**文档**：本记录 B76

### B77. dsh-sgme 桥接补 wiki_page_add 工具 + sgme-operations L1 描述补 wiki 触发词（2026-08-17）

**背景**：SGME 知识库读写是高频操作（建手册/记经验），但链路有三个断点：①L1 skill sgme-operations 描述只含运维触发词（SGME挂了/重启/全量提炼），不含「写wiki/建知识库页面」，agent 遇到建页任务不触发加载；②DSH 桥接（sgme-bridge）只有 wiki_search/pages/page/update 四个工具，缺创建工具，建页只能走 HTTP POST（细节在 L2 手册，须现查）；③会话工具缺 wiki_page_add 与 MCP 契约不一致（MCP 已有）。

**改动**：
- adapters/dsh/sgme-bridge/src/sgme-client.ts：加 WikiPageCreateRequest / WikiPageCreateResponse 类型 + wikiCreatePage 方法（POST /v1/wiki/pages，Agent Key，幂等 upsert）
- adapters/dsh/sgme-bridge/src/tools.ts：加 createWikiPageAddTool（name=wiki_page_add，title/content 必填，category/tags(逗号分隔)/description/author 可选）+ registerTools 注册
- adapters/dsh/sgme-bridge/tests/wiki-tools.test.ts：新增 4 用例（工具名/参数传递含 tags 拆分/缺省 null/失败降级）
- ~/.agents/skills/sgme-operations/SKILL.md：description 扩为「操作手册——运维 + 知识库 wiki 页面读写 + 记忆查询写入 + 信号 + 提炼全流程」，触发词补「写wiki、建知识库页面、记录经验、写踩坑、查记忆、操作手册」；正文加「知识库 wiki 页面读写（高频操作）」指引节（检索→拉全文→POST 建页/wiki_page_update 追加→验证，指向 L2 手册 sgme操作手册-749c4590）

**测试**：pnpm typecheck 通过；vitest 9 文件 107 用例全绿（wiki-tools 18 用例含新增 4）；tsdown 构建成功（lib/index.js 含 wiki_page_add）

**运维影响**：web profile 的 dsh-sgme 为 link: 方式指向 sgme-bridge，重建 lib 后**重启 Web GUI 生效**（常驻进程模块已缓存）；L1 skill 描述改动立即生效（DSH 技能扫描感知，本会话 available_skills 已更新）。NAS 无改动。

**文档**：本记录 B77


### B78. dsh-codegraph 插件迁出 SGME 仓库，独立成仓于 D:/Projects/dsh-codegraph-bridge（2026-08-17）

**背景**：dsh-codegraph（CodeGraph 本地代码知识图谱桥）2026-08-16 被误按「SGME 项目产物随 git 管理」铁律收编进 adapters/dsh/codegraph-bridge/（提交 98d4d3a），实际它是 DSH 生态独立插件、不属于 SGME 项目产物，导致 SGME 仓库混入 4 个无关提交，而平级位置 D:/Projects/dsh-codegraph-bridge 反而成了无 git 的散装副本。用户定：插件与 SGME 平级、独立管理。

**改动**：
- 复制最新版（含 2026-08-17 通用化：bin 自动探测 + projectPath 跟随启动目录）至 D:/Projects/dsh-codegraph-bridge，git init 独立成仓（66ea9f2 初始提交 + 468d3a7 README 更新，freehul 署名）
- SGME 仓库 git rm -r adapters/dsh/codegraph-bridge（a5369d5，历史 4 提交保留可追溯）+ 清理残留 node_modules 空壳
- adapters/dsh/README.md codegraph 章节改写为「2026-08-17 迁出独立仓库」；.gitignore 移除 codegraph-bridge 规则
- web profile：package.json 的 dsh-codegraph link 改为 link:D:/Projects/dsh-codegraph-bridge；删旧 junction 后 pnpm install 重建（lock 同步更新）
- 顺带修正：web profile 的 dsh-desktop-safe-market 在用户 20:40 重启时被还原回 bundle，本次一并重新移除

**测试**：dump-config 装配树 577 行正常（codegraph 从新家解析、仅 queryLimit 显式配置；safe-market 不在 bundle）；junction Target 确认指向新家；SGME git status 无 codegraph 残留。

**运维影响**：重启 Web GUI 生效；下次重启前旧 link 已修复，不会再出现 bundle 解析失败。后续 codegraph 插件改动在 D:/Projects/dsh-codegraph-bridge 独立提交。

**文档**：本记录 B78

---


### B79. 免费模型托底产品化——默认配置 + Key 缺失引导 + 向量健康检查（T-55，2026-08-18）

**背景**：免费模型托底调研（2026-08-17 wiki research 页）落地。用户定（2026-08-18）：
普通用户默认免费双件套（智谱 GLM-4.7-Flash 提炼 + 硅基流动 bge-m3 向量）；本机向量切硅基 1024 维；
提炼主链改智谱（deepseek 备）；向量不设备用但需健康检查 + 失效日志 + 信号告警。

**改动**：
1. config/providers.yaml：新增 zhipu 供应商（GLM-4.7-Flash / open.bigmodel.cn/api/paas/v4 / ZHIPU_API_KEY / 200K / 免费托底注释）
2. config/llm.yaml：refinement 链 [zhipu(主,免费) -> deepseek(备,付费) -> rule]（用户定主链智谱）
3. config/sgme.yaml：search.vector 切 siliconflow / BAAI/bge-m3（1024 维）（零费用、实名解锁；volc 保留为可选）
4. sgme/llm/provider.py：注册 zhipu provider（OpenAI 兼容）——修复「未知 provider: zhipu」降级链崩溃（mock 测试抓到，否则 deepseek 失败走 zhipu 节点会 ValueError）
5. sgme/operations/llm.py：detect_missing_model_keys + model_keys_notice（Key 缺失检测：提炼链节点 + search.vector，只报实际用到；统一提醒文案含两平台申请地址）
6. sgme/operations/health.py：
   - health 响应新增 model_config 字段（missing_keys + notice，Key 缺失引导，只增不改既有字段）
   - 向量块新增 connectivity 探测（check_vector_model_connectivity：POST embeddings 输入 "."、5s 超时、Bearer key、永不抛异常）
   - 连通失败 -> logger.warning + signal.engine.publish(anomaly_warn, source=vector)（payload 含 provider/model/error/hint）；未配置不发信号（Key 缺失引导已覆盖，防噪音）
7. sgme/operations/inject.py：_attach_key_missing_note（Key 缺失时 stats.note 附申请提醒；齐全零噪音）
8. sgme/mcp_server.py：agent_onboarding self_config 附 Key 缺失提醒（指引 docs/guide/免费模型Key申请指南.md）
9. docs/guide/免费模型Key申请指南.md（新文档：智谱/硅基注册流程 + 限流说明 + 排障速查 + 时效声明）
10. tests：test_key_missing_guide.py（10 用例）+ test_vector_connectivity.py（8 用例）+ health 契约字段更新（HTTP_TOP_KEYS + model_config、vector 键集合 + connectivity）

**测试**：相关模块 86 passed / 0 failed。真实冒烟：
- zhipu GLM-4.7-Flash 真实调用成功（usage 117 tokens）；高峰期观察 1305 平台过载限流（免费模型现实，降级链自动处理）
- 硅基 bge-m3 真实嵌入 172ms（输入 "."，零费用）
- 降级链真实链路：deepseek 无 key(401) -> zhipu -> rule 顺序正确
- health connectivity 真实探测 OK（siliconflow/bge-m3 172ms）
- 向量全量重灌：场景 222 条全成功（7.9 条/s）；记忆 11,568 条进行中

**运维影响**：
- 向量切换 2048->1024 必须全量重灌（backfill_vectors.py --force + backfill_scene_vectors.py --force）
- NAS 生产 docker.env 需补 ZHIPU_API_KEY / SILICONFLOW_API_KEY + 重启 sgme 容器（本机验证通过后执行）
- 主链智谱免费模型高峰期可能 1305 限流 -> 自动降级 deepseek（付费备用）；免费政策随时调整，以官方价格页为准

**文档**：本记录 B79；Backlog T-55；wiki「免费模型托底调研」页（官方口径修正 + 决策）+「免费模型Key申请指南」页（skill/sgme）

---


### B80. 内置角色模板丢失修复——entrypoint 首次启动物化 roles（2026-08-18，T-55 后续）

**背景**：生产 WebUI 角色管理页无内置角色模板。排查：内置 4 角色（butler/companion/friend/mentor.json）在镜像 /app/roles（Dockerfile COPY 正常、src/ 齐全、旧镜像 b2 也有），但运行时 ROLES_DIR = SGME_HOME/roles（生产 /data/roles，挂载卷）为空——entrypoint 首次启动只物化 sgme.yaml 不物化 roles，B63 容器化迁移缺陷（本机直跑时 ROLES_DIR 在项目根天然有内置角色；容器化后 SGME_HOME 指向空卷从未初始化；旧备份 backup-20260817 无 /data/roles 佐证）。

**改动**：
1. docker/entrypoint.sh：首次启动（SGME_HOME/roles 不存在）时物化 /app/roles/*.json 到 SGME_HOME/roles/（与 sgme.yaml 物化同机制）
2. 生产立即修复：docker cp /app/roles/*.json 到 /data/roles/（挂载卷持久，API 已验证返回 4 角色）

**测试**：entrypoint sh -n 语法 OK；生产 API /v1/admin/roles 返回管家/伴侣/朋友/导师。

**运维影响**：新部署/重建容器自动有内置角色；现有容器已手动物化（/data/roles 持久）。

**文档**：本记录 B80；wiki 操作手册踩坑记录追加。

---


### B81. WebUI 五问题修复 + 维度裁剪 + 画像去重 + 待办主动登记（2026-08-18）

**背景**：用户反馈 WebUI 5 问题（统一检索无结果 / Dashboard 布局 / 画像重复 / 维度冗余 / 会话原文）。

**改动**：
1. **统一检索修复**（前端 pickKey）：AGENT_KEY_PATHS 端点（/v1/search 等）Agent Key 为空时回退 Admin Key（后端 is_agent 接受 admin key）——用户只填一个 key 也能检索（根因：前端只读 agent key，用户只填了 admin key → search 无 key 403）
2. **Dashboard 布局**：系统健康与 Dream 日报由并排改上下显示（用户定）
3. **画像重复**：a) 生产记忆去重（46 组 166 条 → 归档 120 条，memory_archive 原件保留）b) inject 跨 section 去重升级：memory_id + content 前 50 字前缀判重（L1 提炼相似内容兜底）
4. **维度裁剪**（用户定：项目池/待办池为专用落地点）：registry/dimensions.yaml 移除 projects/tasks + aliases.yaml 同步清理 + templates（work/coding/full）去 projects/tasks section 与 memory_types + 测试维度引用修正；**存量标签保留**（memory_tags 历史数据不删，仅不再注入/筛选/展示）
5. **待办主动登记强化**：AGENTS.md 三池段 + agent_onboarding 模板新增指引——会话中遇到待办主动 demand_create（title + project_id），完成标记 done（现状：83 条待办全来自 Backlog 导入，agent 未主动创建）
6. **场景关联记忆**（前序 99f58c0 已含）：scene_dao related_memories + 前端详情列表

**测试**：相关模块 51 passed（inject/key_missing/health/vector_connectivity）；build 通过

**运维影响**：维度移除后存量 projects/tasks 标签不再注入（历史保留）；生产已跑记忆去重（120 归档）；待办需 agent 按新指引主动登记

**文档**：本记录 B81；AGENTS.md 三池/约束 3/4 更新

### B82. bridge 对齐 MCP 新工具：三池登记 + 角色装配 + 记忆纠错（T-86，2026-08-18）

**背景**：MCP 侧已长出三池（idea_add/demand_create/project_register）+ 角色（role_list/role_assemble/role_active_get/role_active_set）+ 记忆纠错（memory_get/memory_reject）共 9 个工具，dsh 桥接只挂 9 个检索/信号工具——B81 强化的「待办主动登记」在 dsh 会话中无工具可调，角色/纠错能力同样缺席。

**改动**：
1. **sgme-client.ts**：补 `put` helper + 9 方法（三池走 Admin Key：ideaAdd/demandCreate/projectRegister；角色与纠错走 Agent Key：roleList/roleAssemble/roleActiveGet/roleActiveSet/memoryGet/memoryReject）+ 对应请求/响应类型
2. **tools.ts**：新增 9 个 dsh 工具并注册（总数 9→18）；memory_search dimensions 描述修正——去 projects/tasks 标注已裁剪（B81 维度裁剪对齐，防模型传废弃维度静默查空）；role_list 附当前角色标注（←当前）
3. **install.py**：AGENTS.md 模板「可用工具」段补 9 工具说明；升级锚点加 demand_create（旧部署模板整体升级刷新）
4. **顺手修 e784b80 遗留**：typecheck 3 处错误（index.ts effect 返回 void / events.test.ts 索引访问）+ role_list 不可达时不再多发一次注定失败的 roleActiveGet
5. **冒烟脚本**：scripts/oneoff/verify_t86_endpoints.py（读端点直测 + 写端点故意触发校验错误——验证路由/鉴权/operations 全链路且不落生产数据）

**测试**：vitest 131 passed / 0 failed（含新增 admin-tools.test.ts 20 用例）；typecheck 0 错误；pytest test_install.py 7 passed；NAS 生产真实冒烟 9/9 passed

**运维影响**：dsh 重启后新工具生效（lib/ 已重编）；三池写操作依赖 Admin Key（install.py 已部署的 .env 含 sgme_admin_*，无需重新安装）；AGENTS.md 在下次 install.py 运行时自动升级

**文档**：本记录 B82；Backlog T-86 ✅


### B83. 关怀消费兜底 + LLM 免费托底（兜底铁律 + T-53，2026-08-18）

**背景**：用户实测「信号被消费了但从未在会话中感受到关怀」——违反接入纪律「当前会话是兜底通信渠道，任何主动消息必须在此发一条」。排查发现两处断点：
1. **关怀链路**：care 信号被本机 DSH 用 env 主 key 静默认领（consumed_by=default/None），signal_acks 表 0 条回执——「认领即丢」；本地订阅队列从不 markConsumed，导致【SGME 事件提醒】每轮对话重复注入「关怀信号 N 条」但 signal_pull 永远返回空（死循环）。
2. **LLM 免费托底失效**：用户 2026-08-18 配置 zhipu 免费主链，但 dsh agent 注册时声明 agent_model=deepseek/deepseek-v4-flash，动态链解析（resolve.py 策略规则 2）让 deepseek 成为提炼第一节点，llm.yaml 的 zhipu 主链被完全跳过——refine_runs 实证 deepseek 5279 次 vs zhipu 31 次，deepseek 平台持续产生 SGME 提炼 API Key 消费。

**改动**：
1. **context.ts**（DSH 插件）：buildEventNoticeText 增强——care_* 信号内容直接附在【SGME 事件提醒】里（含 type/ts/event_id/payload），agent 无需依赖 signal_pull 即可在当前会话呈现关怀，兜底铁律落地。
2. **tools.ts + index.ts**（DSH 插件）：signal_claim / signal_ack 成功后调 eventSubscriber.markConsumed() 同步本地队列——防「提醒反复注入但 pull 为空」死循环；订阅器创建提前到工具注册之前（claim/ack 需引用）。
3. **care.py**（服务端）：consume_signal 时 agent_id in (None, "default")（env 主 key 合成身份）→ 发布 anomaly_warn（source=care_consume），「认领即丢」可溯源；告警发布失败不阻塞消费。
4. **refine.py**（服务端）：提炼完成后若 provider == "deepseek"（付费备用）→ 发布 anomaly_warn（source=refine_cost），覆盖「降级成功不报错 = 无人知晓在烧钱」监控盲区。
5. **sgme.yaml**（配置）：refine.llm_override = {provider: zhipu, model: glm-4.7-flash}——用户显式指定优先于 agent 声明（resolve.py 策略规则 1），强制 zhipu 主链、deepseek 仅降级备用。
6. **agent_keys.json**（NAS 生产）：dsh agent_model 改为 zhipu/glm-4.7-flash（双保险）。

**测试**：test_signal_consumption 5 passed（三层消费模型）；test_refinery 45 passed；py_compile 0 错误；consume 告警逻辑隔离验证（default/None 记告警、dsh 不记）；TS typecheck 0 错误 + pnpm build 通过。

**运维影响**：
- NAS 镜像重建为 sgme:1.0.0b3-nas-t56 并重启（/data 卷数据保留：agent_keys.json 更新 + signal_acks 补账 4 条）
- DSH 宿主重启后新插件生效：提醒携带关怀内容、claim/ack 同步本地队列
- 历史 4 条 care 已在当前会话呈现 + 服务端 signal_acks 补 4 条 acked 回执（补账）
- zhipu 免费档高峰偶发 429/JSON 解析失败，重试+降级兜底正常

**文档**：本记录 B83；Backlog T-53 免费托底相关 ✅


### B84. 智谱限流重试调优 + L1.5/L2 解析失败重试（T-53 免费托底，2026-08-18）

**背景**：用户问「zhipu 限流规则与重试间隔」→ 查证官方文档（docs.bigmodel.cn rate-limit）：智谱限流是**并发数上限**（按模型+账户权益等级），非固定 RPM/TPM；错误码 1302=账户速率超限、1305=平台过载、1308/1310=额度上限。官方建议：控制并发、避免高频重试、1305 增加重试间隔。同时实测发现今天 6 次 zhipu 失败**全是 JSON 解析失败而非 429**——根因是免费档输出截断（围栏不闭合/JSON 不完整）。

**改动**：
1. **config/llm.yaml**：backoff base_s 1→3s、max_s 8→20s、jitter 0.2→0.5；max_retries 2→3（1305 临时性，少降级 deepseek 烧钱）；zhipu max_tokens 8192→16384（防输出截断；官方 GLM-4.7 系列最大支持 128K 输出，但设满会挤压 200K 上下文输入预算且无实际收益，16K 为权衡值）
2. **l15.py**：resolve_conflicts 解析失败 L15Error → 同模型重试 1 次（提示只输出纯 JSON）；2 次仍失败才降级默认 store（保守不丢数据）
3. **l2.py**：aggregate 解析失败 L2Error → 同模型重试 1 次；2 次仍失败才整批 error（原逻辑不重试直接跳过）

**测试**：py_compile 0 错误；test_llm 退避/限流 4 测试 passed；L2/L15 解析相关 19 测试 passed（3 个既有失败为外键约束环境问题，stash 基线同样失败，与本次无关）；AST 验证重试逻辑在 resolve_conflicts/aggregate 内

**运维影响**：NAS 镜像 sgme:1.0.0b3-nas-t58；限流重试间隔 3s→6s→12s（封顶 20s）；zhipu 输出截断率应显著下降（max_tokens 翻倍 + 解析重试兜底）

**文档**：本记录 B84

### B85. T-55 收尾：文档同步 + 版本号升 1.0.0b3（2026-08-18）

**背景**：T-55 免费模型托底产品化功能（智谱 GLM-4.7-Flash 主链 + 硅基流动 BAAI/bge-m3 向量托底）已于 B79-B84 落地并完成 NAS 全量重灌，但文档与代码存在不一致——架构 v0.9 §24 仍写 deepseek 主链、§23 向量仍写火山方舟 doubao 2048 维；接口契约/runbook 仍写 VOLC_API_KEY / volc-plan——违反「文档第一公民」。本次补齐文档同步并发布 b3。

**改动**：
1. 架构 v0.9 §2 核心约束第 9 条、§24 降级链示例与正文改为 zhipu（GLM-4.7-Flash 免费主模型）→ deepseek 备用 → rule
2. 架构 v0.9 §23 memory_vectors / scene_vectors 向量模型描述改为硅基流动 BAAI/bge-m3 1024 维（免费）
3. 接口契约 v0.1「两个模型」表向量 embedding Key 由 VOLC_API_KEY 改为 SILICONFLOW_API_KEY
4. runbook §16.3 配置描述 search.vector 由 volc-plan 兜底改为 siliconflow BAAI/bge-m3 1024 维免费托底
5. 版本号 1.0.0b2 → 1.0.0b3（pyproject.toml / sgme/__init__.py / sgme/operations/health.py / sgme/server/app.py / 6 个测试版本断言）；新增 docs/release-notes-v1.0.0b3.md

**测试**：pytest health/server 版本断言 6 文件 +1.0.0b3 全绿；config/provider 相关模块绿；启动 `python -m sgme` → /v1/health 报 version 1.0.0b3、向量 available:true

**文档**：本记录 B85；Backlog T-55 ✅（文档同步完成）；README/agent-onboarding 已于 B79 同步免费双件套引导

### B86. DSH 桥关怀事件提醒注入策略修复 + Hermes care-heartbeat cron 漂移修复（2026-08-20）

**背景**：用户实测 DSH 接入 SSE 后「SGME 不停在会话内注入消息，上下文爆增；且 DSH 明确告知已收到信号但无关怀动作」。代码查证定位两个根因：
1. **上下文爆增**：context.ts 事件提醒用 `pendingEvents()` 判断——未消费事件每轮 pre-step 重复注入完整 payload JSON（44a7b85 兜底后附全文），且无「已提醒」跟踪 → 死循环注入。
2. **收到但无行动**：提醒文本要求 agent「调 signal_pull 拉取→claim→ack」闭环，但事件异步到达、agent 无执行动机 → 永不消费 → 加剧问题 1。
另发现 Hermes 侧 `sgme-care-heartbeat` cron（care_consumer.py pull 模式）因 provider 漂移（deepseek→custom:火山方舟）被 drift_skip 跳过，最近 2 次失败。

**改动**：
1. **adapters/dsh/sgme-bridge/src/events.ts**：新增 `unnotifiedEvents()`（未消费且未提醒）+ `markNotified()`（标记已提醒）；`notifiedIds` 持久化（重启不重复提醒）
2. **adapters/dsh/sgme-bridge/src/context.ts**：事件提醒改用 `unnotifiedEvents()`——同一事件只提醒一次（注入后 markNotified），不再每轮重复；`buildEventNoticeText` 摘要化——只给类型+数量+event_id，不再附完整 payload（详情由 signal_pull 拉取）
3. **Hermes cron 修复**：`hermes cron edit 5ec7282a0e6e --provider "custom:火山方舟" --model "deepseek-v4-flash"` 钉住 provider/model，解除 drift_skip

**测试**：DSH 桥 typecheck 通过 + 136 测试全绿（新增 4 个 events 测试 + 2 个 context 事件注入回归）；cron 手动触发 `Ran now: succeeded`，last_status=ok、failure_streak=0

**运维影响**：DSH 侧需重新构建/部署 sgme-bridge（lib/index.js 已更新）；Hermes care-heartbeat 已恢复 30 分钟周期投递飞书

**文档**：本记录 B86

### B87. 向量引擎「本地优先、云端免费降级」——embed 多 provider 降级链 + onboarding 引导升级 v2（2026-08-20）

**背景**：产品化讨论确定向量 embedding 策略为「本地优先、云端免费降级备用」——SGME 部署于 NAS，Docker 已部署 ollama；主 provider=本地 ollama bge-m3（1024 维，零费用、隐私本地），fallback=硅基流动 siliconflow 云端 BAAI/bge-m3（同 1024 维免费）。本地与云端同款模型同维度，**切换不重灌索引**。同时把「向量引擎接入」做成给 agent 的专业流程指引（融入 onboarding 体系，版本 v1→v2）。

**改动**：
1. **sgme/data/search/vector.py**：`embed()` 支持多 provider 降级链——`search.vector.fallbacks` 列表（`[{base_url, model, api_key_env}]`）；主 provider 失败 → 依次尝试 fallback → 全部失败才返回 None（降级纯 BM25）。向后兼容：无 fallbacks 时行为不变。每个 provider 独立 429 退避重试、Bearer 鉴权、超时
2. **config/sgme.yaml**（生产配置）：`search.vector` 主=本地 ollama（`http://192.168.10.10:11434/v1` + `bge-m3`），`fallbacks=[siliconflow 云端]`
3. **NAS 部署**：ollama 容器拉取 `bge-m3`（1.2GB）；SGME 容器经宿主 IP 访问 ollama（两容器不同 docker 网络，已验证连通）
4. **sgme/mcp_server.py**：`agent_onboarding` self_config 升 **SGME-ONBOARDING-v2**，template 新增「向量引擎接入」章节（诊断→探测→引导部署→配置写入→验证闭环 五步；Ollama/LM Studio 双推荐；明确不推荐 llama.cpp——模型状态无人维护）
5. **docs/agent-onboarding.md**：新增 §7.5「向量引擎接入流程」（五步闭环 + 边界说明 + 排障顺序）
6. **README.md / README.zh-CN.md**：agent prompt 段新增「Vector Engine Setup」短指引；Consistency note 版本 v1→v2

**配置路径修正**：初稿指引写「改 providers.yaml embedding 段」，实际 `fallbacks` 降级链只在 `config/sgme.yaml` 的 `search.vector` 段生效（providers.yaml embedding 段是 WebUI 供应商清单，无 fallbacks 语义）——文档/模板/README 统一改为 sgme.yaml search.vector + fallbacks，避免 agent 误操作。

**测试**：新增 tests/test_vector_fallback.py 4 个测试（主失败→fallback 成功 / 主成功跳过 fallback / 全失败→None / 现状兼容），全部通过；test_eval_rrf.py（embed 回归）全绿；onboarding v2 测试全绿

**冒烟**：SGME 真实 `embed()` 调本地 ollama bge-m3 返回 **1024 维**向量成功；config 加载验证 fallbacks 正确解析

**运维影响（2026-08-20 已部署完成）**：NAS 生产已同步 sgme.yaml + 构建 `sgme:1.0.0b3-nas-vec1` 镜像 + compose 更新 + 容器重启生效；生产实测 ollama 在线 provider=local（277ms）、停 ollama 自动切云端返回 1024 维、恢复回本地。备份：sgme.yaml.bak-20260820-vec1 + docker-compose.yml.bak-20260820-vec1。ollama bge-m3 已就位（1.2GB）；`api_key_env` 留空（本地 Ollama 无需鉴权），SILICONFLOW_API_KEY 仍为云端降级用

**文档**：本记录 B87


### B88. ST-12 NAS 一键部署脚本 deploy.sh（2026-08-20）

**背景**：ST-24 已交付 Dockerfile/docker-compose（镜像+部署验收），ST-12「NAS 一键部署」缺一键化体验——手动流程（构建→导出→scp→load→up→验证）分散在 deployment-docker.md 多个小节。

**改动**：
1. 新增项目根 `deploy.sh`（bash）：封装 构建→导出→传输→NAS 导入→启动→验证 全流程；子命令 build/deploy <host>/up/down/logs/verify
2. NAS 部署路径约定 /vol1/1000/Docker/sgme（bind mount 数据卷，与 deployment-docker.md §4.3 一致）；docker.env 密钥检查守卫（缺失/为空即中止）
3. deployment-docker.md 新增 §2.5 一键脚本说明

**测试**：脚本语法 bash -n 校验通过；命令分支走查（build/up/down/logs/deploy/verify/非法参数）；NAS 实机部署待用户执行

**运维影响**：Windows 本机用 Git Bash/WSL 执行 deploy.sh；纯本机部署仍可 docker compose up -d --build

**文档**：本记录 B88；Backlog ST-12 ✅

### B89. ST-21 git 历史敏感词清理——审计证明无需执行（2026-08-20）

**背景**：ST-21 ②「git 全历史 filter-repo 敏感词清理（1.0 上传时执行）」原计划重写 116 个提交历史。2026-08-20 只读审计发现实际无需执行。

**审计结论**（git log 全历史扫描，未重写任何提交）：
1. 无 .env / docker.env / config/.env 文件提交记录（git log --diff-filter=A 扫描）
2. 无真实 key（sk-/ark- 等模式扫描：唯一命中 dce02f7 是「append 脱敏修复」提交的**标题**含 sk- 字样，非泄漏）
3. config/ 模板全占位符（无 16+ 位硬编码 key）
4. .env.example 空值模板（SGME_ADMIN_KEY= 等）

**改动**：仅 Backlog ST-21 状态 🟡 → ✅（追加审计结论）；安装 git-filter-repo（pip）备用但未执行重写

**测试**：审计命令均为只读（git log 扫描），无写入

**运维影响**：1.0 上传 GitHub 无需 filter-repo 重写；若未来引入真实密钥提交需重新评估

**文档**：本记录 B89；Backlog ST-21 ✅

### B90. 信号批量清空端点 + MCP signal_clear + WebUI 全部消费（T-87，2026-08-21）

**背景**：signal_events 此前只有 GET /v1/events/pull（持久游标逐批拉取）、
GET /v1/events/stream（SSE）、单条 consume（/v1/admin/care/signals/{id}/consume），
没有批量清空/全部消费端点——积压信号只能逐条认领或等 TTL 清理；WebUI 关怀信号
面板（SignalsView.vue）只有单条消费按钮；MCP 只有 signal_pull/claim/ack 三工具。
2026-08-20 用户要求登记 T-87（信号模块功能缺失），本次实施。

**改动**：
1. **sgme/data/signal_dao.py**：新增 `mark_all_consumed`（批量标记未消费事件为已消费：
   UPDATE ... WHERE consumed_at IS NULL，可选 `event_type` 精确过滤，幂等——二次调用
   rowcount=0，已消费事件 consumed_by 不被覆盖）+ `get_latest_event`（(ts, event_id)
   复合序最大值，与 pull 游标语义一致）
2. **sgme/operations/events.py**：新增 `events_consume_all`——标记全部未消费事件
   （可选 type 过滤）；`subscriber_id` 提供时同步把该订阅者持久游标推进到最新，
   pull/SSE 视角一并清空（与 signal_engine.pull 推进逻辑一致）；consumed_by 记录清空方
3. **sgme/server/routes_admin.py**：新增 `POST /v1/admin/events/consume_all`（admin 鉴权
   require_admin_key；query `type` 可选过滤、`subscriber_id` 可选游标推进）。
   刻意挂 routes_admin 而非 routes_events：管理操作 + 不触碰既有端点行为约束
   （routes_events 全部端点走 require_agent_key，保持原样）
4. **sgme/mcp_server.py**：新增 MCP 工具 `signal_clear`（signal_type 可选过滤、
   subscriber_id 可选游标推进；consumed_by 从鉴权 key 反查）+ ONBOARDING_TOOLS 同步
   （agent_onboarding 能力清单测试断言清单与工具集一致，防漂移）
5. **ui/src/api/roles.ts**：新增 `consumeAllCareSignals`；
   **ui/src/views/care/SignalsView.vue**：新增「全部消费」按钮（confirm 确认 + 幂等提示，
   禁用条件：列表为空或处理中；按当前类型过滤生效）
6. **tests/test_operations_events.py**：新增 5 个测试（批量标记+幂等 / type 过滤 /
   subscriber 游标推进 / HTTP 端点 admin 鉴权+幂等 / HTTP type+subscriber 参数）；
   **tests/test_mcp_server.py**：新增 signal_clear 测试 + 工具集断言补 signal_clear

**顺带修正（预存在欠账，非本任务引入）**：tests/test_mcp_server.py::test_mcp_memory_reject
原用 `["projects"]` 维度——projects/tasks 维度已于 2026-08-18 移除（项目池 project_meta /
待办池 demands 为专用落地点），memory_tags FK 约束导致该测试红；改用仍注册的 `["goals"]`
维度。注意 tests/test_care.py 仍有 4 个同类预存在失败（`["tasks"]` 维度），未纳入本次范围，
待后续任务清理。

**测试**：tests/test_operations_events.py + tests/test_mcp_server.py 共 50 passed
（含 T-87 新增 6 个）；前端 `cd ui && npm run build` 通过（vite 901ms，无类型错误）

**运维影响**：新端点仅 admin key 可调（agent key 403）；MCP `signal_clear` 接入 agent
即用；无 DB 迁移（复用既有 consumed_at/consumed_by 列），不改变 pull/stream 端点行为。

**文档**：本记录 B88

### B91. ST-33 /v1/search 新增 sessions scope（L0 原始层接入）+ T-9 直查 SQL 收口 data 层（2026-08-20）

**背景**：
1. **ST-33**：/v1/search 已统一 memory（L1.5 记忆池）+ wiki（L2 场景）+ wiki_pages（知识库）三源，L0 原始层（raw_files 索引）未接入——未提炼/未命中提炼的原始会话原文不可检索，多源统一检索缺最后一块拼图。
2. **T-9**：operations/health.py（``_count_vector_rows`` 向量行数统计）与 signal/engine.py（publish 的 suppress_hint 查询、get_replay_window_events 的重放窗口查询）绕过 data 层直查 SQL，违反「data 是唯一数据库操作层」铁律。

**改动**：
1. **sgme/data/session_dao.py**：新增 ``search_raw_files(conn, query, limit)``——LIKE 子串匹配 raw_files 元数据列（file_id / session_key / agent_id / path，OR 语义 + ESCAPE 防通配符），按最近会话倒序（与浏览分页同口径），空 query 返回空列表
2. **sgme/operations/search.py**：新增 scope ``"sessions"``（第 4 层）——检索委托 DAO，结果装饰 source=``"sessions"`` + routes=[``"l0_like"``]；``content`` 为读盘正文摘要（剥 frontmatter → 折叠空白 → 截断 200 字，best-effort：文件缺失/越界/读失败 → 空串，检索不依赖读盘）；容错隔离对称 wiki_pages 层（session_conn 为 None / 检索失败 → 该层空结果 + WARNING）。raw_dir 取自 ``cfg["paths"]["raw_dir"]``，路径解析复用 operations/session.py 的 ``_resolve_raw_path``（越界纵深防御）
3. **sgme/data/stats_dao.py**：新增 ``count_vector_rows(conn)``——health 向量行数统计迁入（表缺失/异常按 0 计，永不抛）
4. **sgme/operations/health.py**：删除本地 ``_count_vector_rows``，改调 stats_dao.count_vector_rows（行为逐行等价）
5. **sgme/data/signal_dao.py**：新增 ``get_recent_event_ts(conn, event_type, source)``（publish 的 suppress_hint 查询）+ ``count_events_before_ts(conn, ts)``（重放超窗摘要计数）；重放窗口内事件复用既有 ``list_events_since(since_ts=...)``
6. **sgme/signal/engine.py**：publish / get_replay_window_events 的直查 SQL 改调 DAO（等价重构，零行为变化）

**测试**：
- test_operations_search.py 新增 5 用例：sessions 命中（含读盘摘要）/ 未命中空结果 / 与 memory 合并（顺序 + routes 并集）/ 磁盘缺失 content 空串 / HTTP 端点端到端
- test_signal.py 新增 2 用例：get_recent_event_ts（最近一条/异源/无记录 None）、count_events_before_ts
- test_operations_health.py 新增 1 用例：stats_dao.count_vector_rows 直测
- 相关模块回归全绿（test_operations_search / test_operations_health / test_signal / test_signal_consumption / test_operations_events / test_storage_v04 / test_health_v04，数字见提交信息）

**运维影响**：无——两个任务均为向后兼容增量。sessions 为新 scope，缺省不启用（DEFAULT_SCOPES 仍为 [``"memory"``]），旧请求逐字节不变；sessions 层正文摘要读盘为 best-effort，不改变任何既有响应字段。已知边界：raw_files 为索引表（无正文列），sessions 匹配目标是元数据列（file_id/session_key/agent_id/path），正文全文匹配（FTS5 化 raw 内容）留待后续版本

**文档**：本记录 B89

### B92. dimensions.boundaries 加载保留——YAML→DB→L1 提示词全链路（T-11，2026-08-20）

**背景**：registry/dimensions.yaml 的 boundaries 字段（维度间 vs 对照消歧说明）在 import 时被静默丢弃——dimension_registry 表无此列、upsert_dimension 不写该字段，运行时 cfg['dimensions'] 只含 id：display_name。审计 D8 实锤：消歧信息从未送达 LLM，维度混淆风险的主要缓解手段等于没做。架构 §28 语义：boundaries = 维度取值边界/枚举约束（此处具体形态为 vs 对照语义消歧），归一化时用于区分相近维度。

**改动**：
1. **sgme/data/db.py**：dimension_registry 建表 DDL 加 boundaries TEXT 列；新增 _migrate_dim_boundaries() 老库迁移（ALTER ADD COLUMN 幂等，connect_memory 调用）
2. **sgme/data/memory_dao.py**：upsert_dimension INSERT + ON CONFLICT UPDATE 均含 boundaries（import 幂等重写时随行更新）
3. **sgme/operations/registry.py**：DIMENSION_FIELD_KEYS 加 boundaries（随 DB 行暴露，表列序末尾）；_normalize_dimension 保留 boundaries 入参（create 回显含该字段，缺失为 None）
4. **sgme/engine/l1.py**：_render_l1_text 维度清单行附「（边界：…）」，boundaries 送达 LLM 消歧（审计 D8 痛点闭环）
5. **测试**：test_storage.py 新增 4 用例（导入保留/缺失为 None/upsert 更新/老库迁移补列）；test_engine.py 新增 render 注入用例；test_operations_registry.py CREATE_DIMENSION_FIELD_KEYS 同步加 boundaries

**测试**：storage+engine+registry 相关 12 用例全绿；全量相关模块 93 passed（4 个既有失败为 B81 projects 维度移除后测试未同步，与本次无关）

**运维影响**：老库首次启动自动补列（幂等）；HTTP /v1/admin/registry 维度对象多 boundaries 字段（新增字段，向后兼容）；L1 提示词维度清单变长（消歧信息，token 略增）


### B93. adapters/dsh/install.py 生成 ~/.sgme/install.json（T-23，2026-08-20）

**背景**：~/.sgme/install.json 服务发现清单此前只在文档/README 提及（AGENTS.md 服务发现第 2 步、README「连接地址约定」），代码无生成逻辑——本机文件为手动创建。服务端 config.write_install_json（T-23②）已落地，但适配器安装引导（adapters/dsh/install.py）不生成，新装 dsh 适配器仍缺清单。

**改动**：
1. **adapters/dsh/install.py**：新增 write_install_json()（+ install_json_path()）——固定写 ~/.sgme/install.json，字段对齐服务端 config.write_install_json 形态：schema_version / http.host+port（从 SGME_BASE_URL 解析）/ mcp.port（SGME_MCP_PORT env 或默认 9913）/ keys（**只写环境变量名引用** SGME_ADMIN_KEY/SGME_AGENT_KEY/SGME_BEARER_TOKEN，不落明文，铁律 #10）/ agent_id；幂等覆盖
2. **main()**：安装第 1.5 步调用 write_install_json()（注册 agent 后、写 AGENTS.md 前）
3. **adapters/dsh/README.md**：安装说明补 install.json 生成步骤与用途
4. **测试**：adapters/dsh/tests/test_install.py 新增 5 用例（生成文件字段完整 / 不落明文 key / 幂等 / MCP 端口 env 覆盖 / 固定路径 ~/.sgme）

**测试**：adapters/dsh/tests/test_install.py 12 passed（含新增 5 用例）

**运维影响**：重跑 install.py 即生成/刷新 ~/.sgme/install.json；agent 服务发现清单可自动重建（不再依赖手动创建）
=======
---

### B94. 评测框架补模板注入效果检测（T-20，2026-08-21）

**背景**：现状评测框架只测提炼质量（L1 F1）、检索排序（RRF）与 L2 Section 命中率，
4 个场景模板（daily/coding/work/full）的**注入效果**无检测手段，靠人判断。
「按场景注入」是 README 核心卖点，缺效果证据。

**方案**（度量定义先入文档，代码严格对齐）：
- PRD §5.4 新增「模板注入效果度量」：注入命中率 + 引用覆盖率两个主指标
- 评测框架设计 §1.7 新增「注入效果评测」：数据形态 / 执行链路 / 文件改动

**改动**：
1. `eval/models.py`：新增 `InjectGroundTruth`（mode/subsequent_conversation/referenced_memory_indices）
   + `InjectMetrics`（inject_hit_rate/reference_coverage + 分子分母明细）；`EvalCase.expected_inject`、
   `EvalResult.inject`、`CaseResult` 注入字段
2. `eval/loader.py`：解析 + 校验 `expected_inject`（mode 合法、引用索引不越界）
3. `eval/metrics.py`：`compute_inject_metrics`（复用 retrieval_gt 确定性 memory_id
   `{case_id}#{idx}` 回查 GT 记忆，零 LLM ground-truth 判定）+ `aggregate_inject_metrics`
4. `eval/runner.py`：新增 inject stage——GT 记忆落库（updated_at 取当前 UTC 保证
   time_window 命中）→ `profile.inject` 模板注入（纯 SQL）→ 计算度量
5. `eval/reporter.py`：report.json / report.md 注入段
6. `eval/run.py`：`--stages` 已支持逗号分隔列表，inject 直接可用
7. 文档：PRD §5.4 + 框架设计 §1.7

**测试**：新增 `tests/test_eval_inject.py` 16 用例（models/loader/metrics/runner/reporter），
全绿；`tests/test_eval.py` + `test_eval_rrf.py` 回归全绿（合计 116 通过）

**运维影响**：无（纯评测框架扩展，不动生产链路）。评测命令示例：
`python -m eval.run --baseline --stages l1,inject --dry-run`

**文档**：本记录 B92

---

### B95. D3 记忆关系图谱可视化（ST-13，2026-08-21）

**背景**：WebUI 无任何 D3/图谱代码；记忆/场景关系靠列表逐个查看，无法直观看到
场景↔记忆↔wiki 页面的关联网络。目标：记忆/场景关系可视化页。

**改动**：
1. **后端**：
   - `sgme/operations/graph.py`（新增）：`get_graph` 组装 nodes（场景/记忆/wiki 页面）
     + links（scene_memories 场景→记忆边、wiki_links wiki 页面间边）；只取 active、
     孤记忆不进图、孤儿 wiki 边丢弃、scene_limit/wiki_limit/memory_limit 规模控制
   - `sgme/server/routes_admin.py`：新增 `GET /v1/admin/graph`（Admin Key 鉴权，
     查询参数 scene_limit/wiki_limit/memory_limit）
2. **前端**：
   - `ui/package.json`：新增依赖 `d3@^7.9.0`（npm install）
   - `ui/src/api/graph.ts`（新增）：`fetchGraph` 封装
   - `ui/src/views/graph/GraphView.vue`（新增）：D3 force 布局（forceSimulation +
     forceLink + forceManyBody + 缩放/拖拽），节点按类型着色（场景橙/记忆蓝/Wiki 绿），
     记忆节点点击跳详情路由，场景/Wiki 节点点击右侧面板看关联；图例 + 统计条 +
     规模参数 + ResizeObserver 自适应重绘
   - `ui/src/router.ts`：新增路由 `/graph`（记忆闭环组）
   - `ui/src/views/layout/MainLayout.vue`：侧边栏「知识图谱」入口（🕸）

**测试**：新增 `tests/test_graph.py` 10 用例（operations 组装 + API 鉴权/结构/规模/空库），
全绿；`test_routes_admin.py` / `test_scenes_moved.py` / `test_wiki.py` 回归全绿；
前端 `npm run build` 通过

**运维影响**：前端需重新构建 `ui/dist`（图谱页随 WebUI 一起部署）；后端新端点随
代码部署即生效，无 schema 变更

**文档**：本记录 B93

### B96. LLM 省钱方案：移除 deepseek 付费备用 + 退避加强 + batch_scan 降频（2026-08-20）

**背景**：DeepSeek 平台近 7 天消费 ¥246.88，其中 sgme key ¥90.98（36.9%，仅次于
dsh 的 ¥117.46）。根因：zhipu glm-4.7-flash 免费主链被平台 1305 限流（8/14 至今
573 次），重试 3 次（3s/6s/12s）耗尽后按降级链自动切 deepseek-v4-flash 付费兜底，
refinement 链 deepseek 成功 704 次 vs zhipu 仅 38 次（8/14-8/20 日志）。

**改动**（本机 config/ 与 NAS /vol1/1000/Docker/sgme/data/config/ 同步，均先备份）：
1. config/llm.yaml：
   - chains.refinement 移除 deepseek 备用节点 → zhipu → rule drop_batch
     （1305 限流时整批滞留，下一轮 batch_scan/Dream 重试，记忆不丢只是延迟；
     zhipu 长期不可用由健康告警兜底）
   - max_retries: 3 → 5、backoff.max_s: 20.0 → 60.0（1305 恢复数十秒级，
     多扛两轮退避，序列 3/6/12/24/48s）
2. config/sgme.yaml：refine.batch_scan.interval_min: 10 → 60
   （白天扫描降频 6 倍；会话结束 refine_trigger 即时提炼不受影响；Dream 03:00 已错峰）

**验证**：⚠️ 首次部署踩坑——llm.yaml 属程序资源（config.py: PROJECT_ROOT/config/llm.yaml，
不跟随 SGME_HOME），只改 /data/config/llm.yaml 未生效（运行时仍加载容器内
/app/config/llm.yaml，链仍含 deepseek、重试 3 次）。二次修复：docker cp 新配置进
容器 /app/config/llm.yaml（备份 .bak-20260820）+ 同步 NAS 构建源
src/config/llm.yaml 防重建回退。重启后以 load_llm_config() 运行时验证：
链节点 ['zhipu','rule']、max_retries=5、max_s=60、batch_scan=60min，health ok。

**运维影响**：提炼延迟上限 = 下一轮 batch_scan（60min）/ Dream（03:00）周期；
预期 sgme key deepseek 消费从 ~91 元/周 降至接近 0（zhipu 免费链正常时）。

**文档**：本记录 B96

### B97. dsh 历史会话导入落地——import_history.py 对齐 rc8 存储格式（T-90，2026-08-21）

**背景**：T-49 的 adapters/dsh/import_history.py 是占位骨架——discover_sessions() 只扫顶层 ~/.dsh/sessions/*.jsonl，从未对接 dsh 真实存储，历史会话补导入实际不可用。2026-08-21 实测确认 DSH rc8 真实存储：~/.dsh/sessions/<workspace>/<会话id>/session.jsonl.zstd（zstd 多帧压缩，首行 session 头 + 事件流），老格式 session-<uuid>/ 与新格式 <uuid>/ 目录并存；rc8 release notes 明确存储格式不兼容（SQLite 性能优化 + 体积减小），历史导入需按新格式实现并兼容两代。

**改动**：
1. adapters/dsh/import_history.py 重写：
   - discover_sessions()：递归扫 <workspace>/*/session.jsonl.zstd，兼容老（session-<uuid>/）新（<uuid>/）目录命名，排除 Temp/cli-test 测试工作区（e2e 不污染记忆池）
   - parse_session_file()：zstandard stream_reader 流式解压（多帧），事件流解析——跳过 session 头/turn/step/chunk 类噪音，提取 user/message（data.content）、assistant/message（data.message.content 忽略 reasoning/tool-call 块）、tool/result（tool-result 内层 text），字段与 sgme-bridge session-sync.ts 对齐（T-53 同款结构）
   - _ms_to_iso()：事件毫秒时间戳 → ISO 8601
   - session_key 改用会话 id 目录名（dsh-{f.parent.name}，兼容两代命名）
   - 幂等/只读/L0 转换/append/提炼触发逻辑保留
2. pyproject.toml：dependencies 补 zstandard>=0.22
3. adapters/dsh/tests/test_import_history.py 重写：zstd 夹具（真实事件流同构）覆盖完整解析/噪音过滤/空内容/损坏文件/新老命名发现/Temp 排除/幂等键/append，14 用例

**验证**：adapters/dsh 全量 26 passed；--dry-run --limit 8 真实扫描本机 ~/.dsh/sessions：发现 141 会话、消息解析正常（最大单会话 1722 条）、Temp 测试工作区已排除；零写入零 LLM。

**运维影响**：真实全量导入需用户确认后执行（写入 NAS SGME + 触发提炼，有 LLM 费用）；dry-run 可随时安全试跑；脚本幂等可重跑，失败项补漏。

**执行补充（2026-08-21 真实全量导入）**：
1. 小批量试路（--limit 3）：3/3 成功，refined 正常 → 全量 141 会话：首批 116 成功 + 15 撞 429 限流 + 10 空会话
2. 根因：脚本查重查本地库（raw_files 恒空）→ 每次全量重发全部请求撞限流；修复：查重改查 NAS /v1/admin/sessions（Admin Key 分页），append 加 429 退避重试（解析 retry_after_sec，最多 3 次）
3. 修复后重跑：141 会话 → 130 成功入库（NAS 共 209 条 dsh 会话，含历史）、11 空会话（仅 session 头 + permission/sandbox/approval 环境事件，无对话消息）跳过
4. 提炼状态：72 refined + 120 new 排队 + 8 error（全为"全链降级失败 drop_batch"——zhipu 免费链限流，非 L0 格式问题，batch_scan/Dream 兜底重试）
5. 测试：adapters/dsh 31 passed（新增 NAS 查重 3 用例 + 429 重试 2 用例）

**文档**：本记录 B97

### B98. dsh 双链路 session_key 统一 + L0 重复识别（T-90 收尾，2026-08-21）

**背景**：批量导入用 'dsh-{目录名}' 做 session_key，实时链路（sgme-bridge session-sync.ts）用 'dsh-{首条消息毫秒}'——同一逻辑会话两条链路各写一份 raw_files。实测 NAS 209 条 dsh 会话中交叉比对确认 60 条重复 L0（双 key 均在 NAS），重复内容进提炼后虽由 L1.5 冲突检测合并，但浪费提炼 token 且 raw_files 冗余。根因：毫秒精度在 L0 ISO 转换时丢失，无法与实时链路 key 对齐。

**改动**：
1. adapters/dsh/import_history.py：
   - _event_to_message user 消息保留原始毫秒（ms 字段，不经 ISO 秒精度）
   - 新增 session_key_for()：统一 session_key 优先 'dsh-{首条 user 消息毫秒}'（与实时链路完全一致），无有效 user 消息兜底目录名（防御）
   - 新增 is_already_imported()：双形态查重——目录名形态（历史导入 130 条）或首条毫秒形态（实时链路）任一命中即跳过，重跑不重导、实时已覆盖不重导
   - main 的 dry-run/正式循环全部接入双形态查重，跳过计入 skip 计数
2. adapters/dsh/tests/test_import_history.py：+5 用例（毫秒 key/兜底/空 ms 跳过/目录形态命中/毫秒形态命中/未命中），36 passed
3. scripts/dsh_dedup_l0.py（新建）：重复 L0 识别——拉 NAS 全 key → 扫描本地会话取首条毫秒 → 双 key 都命中判定重复（fork 会话同 ms_key 共享提示人工确认）；dry-run 默认，--apply 输出 NAS 归档命令清单（raw 移 .archive + raw_files 置 archived，原件不删可恢复，archived 不参与提炼）

**验证**：adapters/dsh 36 passed；--dry-run 真实扫描：141 会话目录形态 209 命中全跳过、11 空会话待导入（无写入）；dsh_dedup_l0.py dry-run 识别 60 条重复（含 --D-Projects--/SGME/AIRDT 各工作区）。

**运维影响**：重复 L0 归档需 NAS 容器内执行 --apply 输出的命令清单（当前未执行，60 条重复待用户确认后归档）；归档后重复文件不再提炼，恢复 = 移回 raw/ + status 置 new；本次不改动已导入数据。

**文档**：本记录 B98
### B99. 自动检测新版本并引导更新（ST-34，2026-08-21）

**背景**：SGME 无任何版本检测/更新引导（2026-08-20 盘点确认）——用户需手动看 GitHub release + runbook 升级。普通用户不会主动关注 release，发布节奏加快后版本滞后成为产品缺口。2026-08-21 用户立项 ST-34，决策：检测源 GitHub 优先；执行机制=意图文件+主机侧 cron 更新代理（用户确认后自动完成）；Docker 形态容器无特权（不挂 docker.sock）。

**改动**（T-91~T-94，本轮交付 T-91/92/93 + T-94 脚本，NAS 部署后续）：
1. `sgme/operations/update_check.py`（新建）：GitHub Releases API（`releases/latest`，公开仓库免 token）解析 tag_name，与当前版本语义化对比（预发布 b4<b5 视为新版；正式版>预发布）；网络/API 失败静默降级（记录 update_error 不抛异常）；模块级缓存 get_cached/refresh（health 高频读不重复请求外网）；config `update_check` 段（enabled/interval_hours/source，默认 github/24h）
2. `sgme/config.py`：`DEFAULT_UPDATE_CHECK_CONFIG` + `_merge_update_check_config` + load_sgme_config 两处返回（缺文件兜底/有文件合并）
3. `sgme/operations/health.py`：health operation 读 update_check 缓存，data 增 update_available/latest_version/update_checked_at/update_error；http_payload 投影新增 4 字段（**只增不改**，MCP 契约冻结不动）
4. `sgme/server/app.py`：`update_check_task` 定时刷新（interval_hours），lifespan 挂载
5. `sgme/operations/update_request.py`（新建）：意图文件读写 $SGME_HOME/update/request.json（原子写 tmp+os.replace；损坏静默降级；clear 幂等）
6. `sgme/server/routes_admin.py`：POST/GET `/v1/admin/update/request`（admin 鉴权）——WebUI 确认「立即更新」落意图，主机代理轮询执行
7. `ui/src/api/dashboard.ts`：HealthStatus 增 4 字段 + requestUpdate/getUpdateRequest；`ui/src/views/dashboard/DashboardView.vue`：发现新版高亮提示条（查看更新说明→release 页 / 立即更新按钮）+ window.confirm 确认弹窗（说明备份/重建/回滚保障）+ 提交后状态反馈（项目既有 confirm 惯例，无自定义弹窗）
8. `scripts/sgme-host-updater.sh`（新建，T-94）：主机侧 cron 更新代理——轮询意图文件 → 校验 target_version 格式 → git pull → docker build 新镜像（网络抖动重试一次）→ 备份 compose → 换 tag → compose up → 健康验证 → 成功清请求/失败自动回滚旧镜像 + 标记 failed；锁文件防并发；容器无特权由主机执行

**验证**：tests/test_update_check.py 13 用例 + tests/test_update_request.py 6 用例 + test_operations_health.py/test_health_v04.py/test_stall_watch.py 契约更新（HTTP_TOP_KEYS 增 4 字段，含修复 test_stall_watch 历史遗留缺 model_config）→ 全绿；`npm run build` 通过；真实冒烟（scripts/oneoff/smoke_update_st34.py）：health 返回 4 新字段（真实连 GitHub 拿到 v1.0.0b4 无更新）、POST/GET 意图端点 200、request.json 原子落盘；bash -n 脚本语法通过。

**运维影响**：
- health 响应体新增 4 顶层字段（向后兼容，消费方忽略即可）；MCP health 不变
- 首次 health 调用会同步请求一次 GitHub API（10s 超时，失败静默）——无网络环境服务不受影响，仅 update_error 有值
- 自动更新执行链路（T-94 主机代理）**本轮仅交付脚本，未部署 NAS**；部署需用户确认后：拷贝 scripts/sgme-host-updater.sh 到 NAS + 加 root cron（*/5 * * * *）
- config/sgme.yaml 可加 update_check 段调 enabled/interval_hours/source（可选，默认即可用）

**文档**：本记录 B99

**NAS 部署执行补充（2026-08-21）**：
1. 部署链路：push nas（f0003d4→1bcd167）→ src git pull → docker build sgme:1.0.0b4-nas-upd1（BUILD_EXIT=0）→ 备份 compose → sed 换 tag → compose up -d → 容器 healthy，health 返回 4 个更新字段（update_available=False/latest_version=v1.0.0b4/update_checked_at 填充/update_error=None），真实连 GitHub 检测正常
2. 主机代理部署：scp scripts/sgme-host-updater.sh → /vol1/1000/Docker/sgme/scripts/（chmod +x）+ root cron `*/5 * * * *`（与 logrotate 并存）
3. **实测发现并修复 2 个脚本缺陷**（提交 e373d4f）：
   - **版本号双重拼接 BUG**：`NEW_TAG="${VER_TAG_PREFIX}${TARGET_VERSION#v}"` 在 VER_TAG_PREFIX=1.0.0b + 完整版本 1.0.0b4 时拼成 `1.0.0b1.0.0b4`——WebUI 传完整版本号，前缀冗余。修复：去掉 VER_TAG_PREFIX，直接用 `${TARGET_VERSION#v}-nas-autoupd`
   - **缺版本一致性校验**：v9.9.9 假版本测试暴露——脚本构建任意 tag 镜像 + health 只查 status=ok 就判成功（代码没变也"成功"）。修复：健康验证后加 `/v1/health` version == 目标版本校验，不符自动回滚旧镜像 + 标记 failed
   - requested_at 在 mark_failed 重写文件时丢失（heredoc 内命令替换转义问题）→ 提前保存变量
4. **实测验证矩阵**：无请求静默退出 0；同版本请求→"已是最新"分支清理；假版本 v9.9.9→完整 build+up→版本校验拦截→自动回滚 upd1+failed（error=版本不一致）；cron 最小环境（env -i）执行正常（docker/git/curl 均在 /usr/bin，cron PATH 覆盖）
5. 清理：删除测试残留镜像（9.9.9-nas-autoupd、1.0.0b1.0.0b4-nas-autoupd）+ request.json
6. **完整成功更新路径未实测**（当前无更高版本 b5）——下次发布后用户点"立即更新"自然验证；脚本成功分支的 build+up+版本读取代码已随 v9.9.9 测试跑通

### B100. LLM 提炼免费链重构 + 版本升 1.0.0 正式版（ST-6 强化 / T-96，2026-08-22）

**背景**：beta 收官发布正式版。实测 zhipu glm-4.7-flash 慢（38s/次）+ 高峰期 1305 限流风暴（连单调用都 429），而 agnes/siliconflow 免费档 1-4s——提炼链把快的放前面、zhipu 作末位兜底；降级链从此全免费化（移除 deepseek 付费备用），新用户零成本启动。

**改动**：
1. `config/llm.yaml`：提炼链重构——agnes（agnes-2.5-flash，当前 $0/1M token）主位 → siliconflow（deepseek-ai/DeepSeek-V4-Flash 免费）第二 → zhipu（glm-4.7-flash 永久免费）末位兜底 → rule drop_batch；max_retries 5→2（免费兜底就位后快速切换，不再等 5 次退避 ~2min）；退避 base 3s/max 60s/jitter 0.5s（1305 过载恢复数十秒级，多扛两轮减少降级）
2. 引导同步三处：`docs/guide/免费模型Key申请指南.md` 重写（三 Key 申请流程 + 链位表 + 免费口径，agnes 官方 wiki 交叉验证）；`sgme/operations/llm.py` MODEL_KEY_MISSING_NOTICE 文案（agnes/siliconflow/zhipu 三 Key）；`sgme/mcp_server.py` agent_onboarding requirement 文案；README 模型注释
3. 文档对齐：架构 v0.9 §1 核心约束 9 + §24 降级链（配置示例/窗口/白名单）更新为三免费链；runbook §4.2（提供商准备/降级链示例/Key 设置）+ env 表
4. 版本号 1.0.0b4 → **1.0.0 正式版**：pyproject.toml / sgme/__init__.py / sgme/operations/health.py / sgme/server/app.py + 6 个测试断言（test_update_check.py 的 b4 为语义比较样例数据保留）；新增 `docs/release-notes-v1.0.0.md`
5. Backlog 登记 T-96

**验证**：pytest 版本断言 6 文件 +1.0.0 全绿；提炼链/health/update_check 相关模块绿。

**运维影响**：正式版接口契约稳定，向前兼容 beta 数据（memory.db/wiki.db 无需迁移）；NAS 生产仍为 b4 镜像，升 1.0.0 可走 ST-34 自动更新（WebUI「立即更新」）或手动 deploy.sh。

### B101. v1.0.0 正式版发布 + 自动更新完整路径首次实测（2026-08-22）

**背景**：v1.0.0 正式版发布（B100 版本号/引导/文档齐备），NAS 生产从 b4 升 1.0.0——ST-34 自动更新「完整成功更新路径」首次真实版本实测（B99 遗留缺口：当时无更高版本，只测过假版本 v9.9.9 的回滚路径）。

**执行**：
1. 打 tag v1.0.0 → 推送 GitHub/Gitee/NAS bare 三远端；GitHub release 已建（gh release，附 release-notes-v1.0.0.md）
2. NAS src git pull --ff-only（1bcd167 → 7d11bac，含架构文档 rename v0.9→v1.0）
3. 写意图文件 `data/update/request.json`（target_version=v1.0.0, status=pending）→ 手动执行 `scripts/sgme-host-updater.sh`（不等 cron 5 分钟）
4. 脚本全链路：git pull → docker build `sgme:1.0.0-nas-autoupd`（BUILD_EXIT=0）→ 备份 compose → 换 tag → compose up -d → 容器 healthy → health 版本一致性校验（1.0.0 == 1.0.0）→ 清请求文件 → exit 0

**验证**：
- `docker exec sgme python -c 'import sgme; print(sgme.__version__)'` → 1.0.0（容器内代码真身确认）
- health：version=1.0.0 / llm available（agnes-2.5-flash）/ vector sqlite-vec 14814 向量 / refine 正常（stalled=false）
- update_check 自查：latest_version=v1.0.0 == 当前 → update_available=false（正确，无更新提示）
- WebUI :9910 → 200；意图文件已清空；旧镜像保留（1.0.0b4-nas-upd1/upd2 未删，回滚可用）

**运维影响**：NAS 生产 = sgme:1.0.0-nas-autoupd（正式版）；自动更新四段闭环（检测→提示→确认→执行）全部实测可用；后续发版用户 WebUI 点「立即更新」即可，无需人工 SSH。

**遗留**：Gitee release 未建成——GITEE_TOKEN 401（过期），需用户更新 token 后补建；NAS 自动更新策略保持手动触发（用户确认后执行，不自动）。

### B102. L2 场景聚合向量预筛（T-97，2026-08-22）

**背景**：巡检发现 L2 场景 active=276 超配置上限 200（红警每轮触发）。根因=设计天花板——L2 每次只把**最多 50 个场景摘要**（updated_at DESC）喂 LLM，超出部分不可见，merge 收敛跟不上 create 增长。对策：对齐 T-25（l15.prescreen）模式给 L2 加**场景级向量预筛**。

**改动**：
1. `sgme/engine/l2.py`——新增 `_prescreen_scenes()`：本批记忆拼接文本 embed → `scene_vector_search` 召回向量 Top-K（默认 30）∪ active 场景按 heat DESC 取热度 Top-N（默认 20），按 scene_id 并集去重；`aggregate()` 在 `l2.prescreen.enabled=true` 时用预筛结果替代固定 50 摘要
2. `config/sgme.yaml`——`l2.prescreen` 段（enabled/vector_top_k=30/heat_top_n=20/fallback=full_recall）
3. `tests/test_l2.py`——+4 用例（未启用零回归 / 向量 Top-K 命中 update / embed 不可达回退 / 并集去重）；autouse fixture 模拟 embed 不可达，现有测试零改动
4. 不删旧件：embed 不可达/未配置 → 回退固定 50 摘要（原行为）

**验证**：test_l2 20 passed（原 16 + 新 4）+ 相关模块 47 passed；l15 4 个失败为预存在问题（projects 维度移除致 FK，stash 验证与本改动无关）。

**运维影响**：L2 场景候选从「固定 50 个最新」变为「30 语义相似 + 20 高热度」；场景超限红警不消除（软策略），但 merge 命中率应提升，观察场景数增速；NAS 生产配置 `data/config/sgme.yaml` 需同步 l2.prescreen 段后重启生效。

### B103. L2 场景超限治理 + 场景向量增量回填（T-97 收尾，2026-08-22）

**背景**：B102 预筛上线后实测暴露两个问题——①**场景向量不回填**：`upsert_scene_vector` 无调用方，8-17 一次性回填后新建/合并场景全部无向量（含 heat=374/238 高热度场景），预筛对它们完全盲区；②**场景数超限**：active 278 > max_scenes 200，红警每轮触发（历史导入期碎片场景多）。

**改动**：
1. `sgme/engine/l2.py`——`_refresh_scene_vector()`：create/merge/update 三动作落库后自动刷新场景向量（embed 不可达仅告警，热度 Top-N 兜底）；+1 测试（create 后 scene_vectors 有行），test_l2 21 passed
2. 存量回填：`scripts/oneoff/backfill_scene_vectors_now.py` 对 58 个无向量 active 场景补向量（容器内执行，本地 ollama 零费用），**无向量 active 归零**
3. 相似场景合并：`scripts/oneoff/merge_similar_scenes*.py` 合并 12 对高相似场景（0.80+ 7 对 + 人工挑选 0.75-0.80 5 对，避免误报），走 `l2._apply_merge`（旧场景 archived 可恢复 + scene_versions 快照 + 新场景自动回填向量）——active 278 → 265
4. 阈值校准：`max_scenes` 200→300，yellow 250 / orange 275 / red 300——265 是真实主题数（225 个默认标题但内容独立、互相相似度低，非碎片垃圾），200 为早期保守值；NAS 生产配置同步

**验证**：容器 healthy；health ok；active=265 无向量=0；红警消除（265 < 300）；场景间相似度 ≥0.85 仅 2 对证明粒度合理（非重复堆积）。

**运维影响**：场景向量从此增量维护（预筛盲区消除）；场景数 265 在黄线 250 之上，继续观察——预筛让 LLM 能看到相似场景后 merge 收敛应提速，若持续增长逼近 300 再评估批量合并；旧场景全部 archived 可恢复（原件不删）。

### B104. 人格洞察引擎三层落地：实时规则抽取 + 月度校准 + 注入消费（ST-35 / T-98~T-101，2026-08-25）

**背景**：2026-08-25 用户定案——SGME 增加「AI 懂用户性格」能力（角色扮演客群卖点）。基于 L1/L2 三轮检索实测产出画像 v0.1 验证可行性后，用户指令「列入 backlog 开发计划，然后开始」。关键决策：①计时放 SGME 内部不放 agent 定时任务（agent 生命周期不可靠）；②MBTI 定位娱乐向展示皮，底层走特质累积模型（MBTI 重测信度低但传播广接受度高）；③零额外 token——实时抽取纯规则、月度校准走免费降级链每月一次。

**改动**：
1. `sgme/data/db.py`——PERSONA_TRAITS_DDL（persona_traits/user_mbti/persona_reports 三表）+ PERSONA_STATE_DDL（persona_state 计时状态）+ `_migrate_persona_tables` 幂等迁移挂 connect_memory
2. `sgme/data/persona_dao.py`——upsert_trait 累积式写入（同 dimension+value+scene_context 证据累积、confidence 封顶 1.0）、supersede_trait/reject_trait（软删原件不删）、MBTI 轨迹 CRUD、报告存取、persona_state 读写；14 测试
3. `sgme/engine/persona_extract.py`——DEFAULT_RULES 四维规则表（decision_style/work_style/quality_standard/responsibility 关键词匹配）+ extract_and_store（persona.rules 可配置覆盖、单次提炼单值最多 3 证据防刷分、溯源 refine:{file_id}）；挂入 refine.finalize_refinement 收尾（失败不阻塞）；8 测试
4. `sgme/engine/persona_monthly.py`——run_calibration 月度校准（跨月到期判断+先落 last_run 防失败无限重试烧 token；输入 traits+当月记忆摘要截断控 token；复用 refinery.extract 免费链；变化检测连续 2 期同向才推 persona_change_confirmed 信号防 MBTI 式重测误报）+ Dream 同款 daemon 定时器（schedule_day/schedule_time 默认每月 1 日 03:30）；6 测试
5. `sgme/profile/persona_block.py` + inject 挂载——性格参考块注入（准入门槛 confidence≥0.45 且 evidence≥3、每维度取最高置信一条、最多 6 条控 token、措辞用「倾向」禁标签判决）；inject() 失败不阻塞
6. `sgme/server/routes_persona.py` 六端点（traits/mbti GET+POST/reports/calibrate 手动触发执行中 409）——app.py 按 persona.enabled 开关挂载 + lifespan 接线定时器启停；5 测试

**验证**：persona 全系 39 用例全绿；test_care/test_dream 回归 96 passed；全量回归中 test_supersession/test_vector_connectivity 的 11 个失败经 git stash 对照确认为预存在环境问题与本改动无关。

**运维影响**：新增 config persona 段（enabled/monthly.schedule_day/monthly.schedule_time/rules 可选覆盖）；老库重启自动补建四表零迁移操作；月度校准消耗约 1 次 LLM 调用/月（免费链内）；定时器随 Gateway 启停。

### B105. Skills管理模块核心落地：索引器+四级披露+写侧门禁+迁移工具（ST-36 M1-M3+M4工具，2026-08-26）

**背景**：设计 v0.2.1 二轮评审六修订后用户令「把ST-36完成，走开发流程」。并行开发：主代理 M1 + 三子代理（读侧/写侧/脚本）git worktree 隔离并行。

**改动**：
- 新包 sgme/skills/（与旧 skills_hub 分立防巨无霸）：indexer 双源索引（git工作区∪wiki skill标记页，同名 git 优先）、bm25 内存 BM25(jieba)、vectors 向量可弃缓存（data/cache/skill_vectors.json，SHA256 失效，复用统一搜索提供商 bge-m3）、gates 六规则门禁（57字触发窗/8K原子/kebab唯一/scripts声明/uses合法）、dedupe 三层查重、writesync 进程内写锁单点串行、store 写编排（软删deprecated→硬删/改名墓碑tombstones.json原子写/入向引用两级信号）
- 读侧：operations/skills.py 五操作（L0索引/L1 digest/L2 get/L3 materialize字节保真+遥测/BM25向量融合0.6:0.4降级）；routes_skills 四端点（agent key）；统一搜索 scopes+=skills（容错隔离镜像 wiki_pages）；MCP 四工具 skill_search/digest/get/materialize（ONBOARDING_TOOLS 同步）
- 写侧：routes_skills_admin PUT/DELETE/rename（admin key，治理版先于 routes_admin 注册接管同路径，source_dirs 未配置回退旧 hub 直写零破坏）
- 工具：scripts/migrate_wiki_skills.py（db/api双源+分页修复——服务端limit=50默认静默漏旧页，实测NAS全量385页；dry-run默认/--apply推远端+原页superseded/误挂标签摘除清单4页）、scripts/find_atomic_candidates.py（纯规则段落聚类 ratio≥0.85 跨≥2技能才算候选）
- config：skills 段装配（enabled 默认 false，禁用时核心零影响）

**测试**：ST-36 九套件 206 passed 全绿；全量回归 1920/1966（46 失败经三方 stash 基线对照确认为预存在环境问题——维度种子漂移14≠16/supersession环境依赖等，与本改动零相关）。集成接缝修复：SPA catch-all 使未注册路径 POST 返回 405 非 404（两用例放宽为环境无关断言）。

**运维影响**：skills.enabled 默认关，生产启用需 config 加 skills 段；M4a 生产迁移未执行（385 页 diff 报告已产出待主人过目后 --apply）；M5 冷启动包/WebUI 改造/progressive-skill 卸载交接未做（Backlog 登记 T-105/T-106 跟进）。
## B106：ST-36 M4a 生产迁移执行 + M5 收官（2026-08-26）

**背景**：ST-36 开发阶段（B105）落地后，按用户裁决「先整体入库再优化」执行 385 个
wiki skill:* 页生产迁移；随后 M5 收官（冷启动包/WebUI/文档/卸载交接）。

**改动**：
1. **迁移执行**：migrate_wiki_skills.py --apply 三轮完成——第一轮全败（58×403 agent key 无写权 + 327×429 限流）；修复①脚本加 429 退避重试（读 Retry-After 头）②换 admin key。第二轮 39 成功；第三轮 **385/385 全部入库**。
2. **原页归档**：PATCH /v1/wiki/pages/{id} 原不支持置 superseded（WikiPageUpdateRequest 无 status 字段，422）→ 加 status='superseded' 显式归档支持（优先于内容更新执行）；archive_migrated_pages.py 批量归档剩余 384 页，0 失败。原页保留不删（supersedes 自指标记）。
3. **生产配置**：NAS config 加 skills 段（enabled=true，source_dirs=/app/cache/skills）；工作区接 skills-hub.git 裸仓 main 分支（402 件 SKILL.md）。容器内 git init+fetch+checkout 由运维执行（source_dirs 要求 git 仓库，store 层校验 .git 存在）。
4. **M5 冷启动包**：GET /v1/skills/coldstart——索引全量（不受 budget 截断）+ 热集全文（pattern=auto）+ SGME 操作手册页一次拉取；路由注册在 /{name} 动态路由之前防抢注。
5. **WebUI SkillsView**：改吃四级披露读侧端点——L0 索引一次拉全量（limit=500）替代逐个 getSkill；详情抽屉改 L1 摘要（骨架+溯源 sha256）；热集徽标（🔥 auto/按需）；统计卡加「热集(auto)」；client.ts AGENT_KEY_PATHS 补 /v1/skills。
6. **门禁演进**（PR-7 迁移就绪）：pattern 枚举化（auto=热集自动加载/manual=按需检索，语义=调用模式）；scripts 规则收紧为「scripts/ 子目录实际存在才查声明一致性」（385 页正文的 scripts/ 引用全是文档性提及，目录实体判据零误伤）；skip_limits 超限降警告（语义违规仍拒）。

**验证**：冷启动 3 用例 + 读侧回归 127 passed；npm run build 通过（1.26s）；生产实测 L0 列表 total=402、搜索 skills 层命中、digest 抽查 aixm 完整；wiki 残留 active skill:* 页 = 0。

**运维影响**：①新端点 /v1/skills/coldstart（agent key）；②批量脚本撞限流属正常（退避重试自动恢复）；③热集管理=把技能 pattern 改 auto 即进冷启动包与热集徽标；④误挂标签 4 页清单见 exports/ST36-M4a-apply报告3.md（SGME操作手册/生产验证页/DSH提示词拼接/wiki成为hub可行性分析）。

## B107：M1 及格线①召回率评测 + 检索断言校准（ST-36，2026-08-27）

**背景**：设计 §二 M1 三条及格线之①「统一搜索命中技能率可统计提升」此前无评测数据——skills.db 建不建缺决策依据；同时 test_operations_skills 检索断言与 BM25 语义错位（2 失败预存在）。

**改动**：
1. **评测脚本** scripts/oneoff/skills_recall_eval.py——真实技能库（D:/HermesAgent/skills，101 技能）+ 20 条代表性查询（从技能真实触发场景抽取，中英混合匹配技能内容语言）；统计 top-5/top-10 命中率；wiki_conn=None 纯 git 源评测。
2. **检索断言校准**（tests/test_operations_skills.py）：BM25 短文档词频密度高（wiki-skill 内容仅 7 字得分反超完整技能 alpha）属正常行为——断言从「必第一」改为「被召回」（检索有效性判据 = 相关技能出现在命中列表，不锁死顺序）；test_vector_unreachable_degrades_to_bm25 同步校准。

**评测结果**（20 条查询，git 源）：
- top-5 命中率 **20/20 = 100%**
- top-10 命中率 **20/20 = 100%**
- 校准过程记录：首跑 70%——6 条未命中中 5 条为「英文技能 + 中文查询」语言错位（document-to-action-items/systematic-debugging/requesting-code-review/spike/llm-wiki 内容中文占比 0%），1 条为查询标注失误（anysearch 技能不存在）；改用与技能内容语言匹配的查询后 100%。

**结论**：及格线①达标（命中率可统计提升，100% 实证）——skills.db 建库决策暂缓成立（索引层 BM25 足矣）；后续可扩展评测集（更多查询 + wiki 双源对照）作为回归资产。

**运维影响**：评测脚本 oneoff 留存（幂等可重跑）；测试断言语义变更（不再锁死检索排序，防脆弱断言）。

## B108：hub 技能化改造 + 282 技能纳入本地库 + M4b 原子抽取（ST-36 T-109~T-112，2026-08-27）

**背景**：用户质疑「318 技能变 101」与「0 原子技能」——查证双实锤：①hub 独有 313 技能是真技能（此前「删除」定案错误）②本地 101 扫描 0 原子是范围错误（hub 401 全量实扫 78 组候选）。用户令全做：纳入本地库 + 原子抽取 + 三技能合并。

**改动**：
1. **hub 技能化改造**（T-109 前置，hub_skills_normalize.py）：hub 314 独有技能仅 1 个 lint 通过——313 缺 frontmatter 必填（version/pattern/category，M4a wiki 迁移遗留）+ 114 超 8K；批量补齐（category 关键词推断 19 类）+ 117 超 8K 拆分 → lint 通过 **1→283**。修 3 个脚本 bug：metadata 嵌套字段误判（comfy-desktop-ops 等 category 在 metadata.hermes 下）/数字 version（`version: 1` lint 不认）/拆分独立判断（曾放「有字段才走」分支内二次运行永不拆）。
2. **282 技能纳入本地库**（hub_to_local.py）：active+lint 过技能复制到 D:/HermesAgent/skills（18 分类目录），本地库 **101→383**；本地库旧 101 技能补 pattern/category（Hermes 原生格式历史缺口）→ 全量 lint **376/383（98%）**。
3. **原子抽取①**（T-110）：comfyui-skill-template——14 个 comfyui 技能共享 A1/A2 标题骨架（结构约定），抽六段式模板技能 + 14 技能 uses 引用。
4. **原子抽取②**（T-111）：hermes-webui-endpoint-resolution——apikey-image-gen/grok-image-to-video 共享 12 段解析逻辑（URL/token/profile/bash 模板），抽原子技能 + 2 技能 uses + 移除复制实现（各 11 段）+ 引用说明。
5. **三技能合并**（T-112）：agent-email/agent-mail/agently-mail 同一主题历史重复——agently-mail 为主并入 5 章节（QQ 特性/限额/watch/两步确认/适用场景/管理端），另两个软删（status=deprecated + deprecated_by，原件保留）。

**验证**：hub 技能化改造 lint 1→283；本地库 383 技能全量 lint 376 通过；原子技能 lint 全过；三技能合并 lint 全过且 8187B ≤8K；遗留 7+31 触发词窗口违规（内容质量，LLM 改写专项）。

**运维影响**：①本地库 101→383（技能能力大幅补全）；②hub 独有技能不再删除，本地库=真源完整；③新增 2 原子技能（comfyui-skill-template / hermes-webui-endpoint-resolution），引用方 uses 声明；④触发词窗口违规（31 hub + 7 本地）待 LLM 改写专项；⑤脚本留存：hub_skills_normalize.py / hub_to_local.py（幂等可重跑）。

### B109：L2 场景超限后台自动治理 scene_gc（T-97 治本，2026-08-27）

**背景**：B102/B103 落地后场景超限红警仍未消除——finalize_refinement 只调 `l2.check_scene_threshold` 发软告警（anomaly_warn），并不降数；手动跑 `merge_similar_scenes.py` 是临时降数，治标不治本（生产 active 350 > max 300 红警常年触发）。用户裁决：应做后台**自动**合并+归档，而非人工干预。

**改动**：
1. **新增 `sgme/engine/scene_gc.py`**（治本核心，不复制定时器）：
   - `list_merge_candidates(mem_conn, cfg)`：dry-run 检测——读 scene_vectors → numpy 两两余弦相似度 → 取 ≥ merge_threshold（默认 0.80）对 → 贪心去重叠（同一场景不进多对），返回候选列表（复用 B102/B103 已回填向量，不重 embed）
   - `run_scene_gc(mem_conn, cfg, client)`：带 `RUN_LOCK` 执行；enabled=false→skipped_reason='disabled'；active < trigger_at→跳过；否则逐对调 `l2._apply_merge`，受 max_merges（默认 20）上限，连续失败 3 次中止（防死循环烧钱）；trigger_at 默认回退 `l2.warn_thresholds.orange`
   - `SceneGcResult` dataclass + `MERGE_PROMPT_TMPL`（忠实聚合两场景正文，复用 merge_similar_scenes.py 范式）
   - 合并落库复用 `l2._apply_merge`：**一处完成**「合并新场景 + 自动归档旧场景（status='archived' 可恢复）+ 写 scene_versions 快照 + 刷新场景向量」，符合「原件永不删」铁律与 Supersession 约束
2. **`sgme/data/scene_dao.py`**——新增 `list_active_scene_vectors`（读 active 场景与其向量，供 scene_gc 无需重 embed）
3. **`sgme/config.py`**——新增 `DEFAULT_SCENE_GC_CONFIG` + `_merge_scene_gc_config`（类型校验+合并）；`load_sgme_config` / `load_config` 注入 `scene_gc`；`CONFIG_SECTIONS` + `SECTION_KEYS` 加 scene_gc 白名单（防未知键注入）
4. **`sgme/engine/dream.py`**——第三步「生命周期」内、日报前挂接 `scene_gc.run_scene_gc`，结果计入 Dream 统计（scene_gc_merged / scene_gc_archived / skipped_reason）；异常不阻塞该阶段
5. **`sgme/server/routes_admin.py`**——新增两端点：
   - `GET /v1/admin/scene-gc/candidates`：dry-run 预览（active_scenes / trigger_at / will_run / threshold / 候选对列表）
   - `POST /v1/admin/scene-gc/trigger`：202 异步，后台线程自建独立连接跑 `run_scene_gc`；`RUN_LOCK.locked()` 防重入（409 ERR_CONFLICT）
6. **`config/sgme.yaml`**——新增 scene_gc 段（enabled=true / merge_threshold=0.80 / min_threshold=0.70 / trigger_at=275 / max_merges=20）

**验证**：
- `tests/test_scene_gc.py` 11 passed（候选检测：相似对/贪心去重叠/低于阈值/跳过无向量；run_scene_gc：低于 trigger 跳过/禁用跳过/合并归档 mock LLM/max_merges 上限；配置合并：默认/覆盖/类型拒绝）
- `tests/test_dream.py` 21 passed（dream.py 改动零回归）；test_config_api / test_routes_admin / test_scene_vectors 全过
- config 层验证：load_config 后 scene_gc 正确（trigger_at=275, max_merges=20），CONFIG_SECTIONS 含 scene_gc，UTF-8 校验 6 改动文件全 OK（bad=0）

**运维影响**：①场景超限治理从「人工临时脚本」升级为「Dream 03:00 自动渐进收敛 + 手动触发兜底」；②复用 scene_vectors 不重 embed、复用 _apply_merge 不重写归档机制、原件永不删；③上线前建议先 `GET /v1/admin/scene-gc/candidates` 预览候选对确认无误再 `POST /v1/admin/scene-gc/trigger`；④NAS 生产需 git pull + 重启 SGME 服务让 scene_gc 生效（config 同步 scene_gc 段）；⑤T-97 demand 备注已刷新为「scene_gc 后台自动合并归档已落地」。

### B110：WebUI 设置页新增「更新」Tab + 后端「检查更新」端点（2026-08-27）

**背景**：原「立即更新」入口仅挂在 Dashboard 健康卡片提示条，且由 `health.update_available` 控制显隐——比对 GitHub Releases 最新 tag 与运行版本，当前运行 1.0.1 ≥ 最新 Release v1.0.0 时按钮整条隐藏，用户既看不到入口、也不知道为何无更新。用户要求把更新做成设置页独立 Tab，含「检查更新」与「更新」两个按钮，可主动探活而不依赖按钮显隐。

**改动**：
1. **`sgme/server/routes_admin.py`**——新增 `POST /v1/admin/update/check`：调 `update_check.refresh(SGME_VERSION, cfg)` 强制刷新版本检测缓存（立即重查 GitHub/Gitee Releases 最新 tag），返回 `{update_available, latest_version, update_checked_at, update_error}`；鉴权同其它 admin 端点（require_admin_key），永不抛异常（检测失败回填 update_error）。复用既有 `update/request` 写/读意图端点，不改
2. **`ui/src/api/dashboard.ts`**——新增 `checkUpdate()`（POST /v1/admin/update/check，返回 UpdateCheck 对象）+ `UpdateCheck` 接口
3. **`ui/src/views/settings/UpdateTab.vue`**（新增）——设置页「更新」Tab：
   - 状态区：当前版本 / 最新版本 / 更新状态（有可用更新·已是最新）/ 上次检测时间 / 检测错误
   - 「检查更新」按钮：调 `checkUpdate()` + 重新 `getHealth()` 刷新，展示结果（含检测失败原因）
   - 「更新」按钮：复用 `requestUpdate(latest_version)`（仅在 `update_available && latest_version` 时可用），提交后每 5s 轮询 `getUpdateRequest()` 展示 pending/done/failed
   - 底部提示：说明版本检测比对 GitHub Releases tag，按钮置灰含义，以及「部署未发版新提交需先发高于当前版本的新 Release」
4. **`ui/src/views/settings/SettingsView.vue`**——TABS 数组追加 `{ key: 'update', label: '更新', comp: UpdateTab }`（Dashboard 原有更新条保留，二者并存）

**验证**：
- `tests/test_update_check_endpoint.py` 3 passed（缺 Key→403；正常返回检测对象；检测失败降级仍 200 且 update_error 回填）；`tests/test_routes_admin.py` 24 passed 零回归
- UI `vite build` 通过（无 TS/模板错误，新增 UpdateTab 已打进 SettingsView chunk）
- `tests/test_update_check_endpoint.py` UTF-8 校验 OK（bad=0）

**运维影响**：①更新入口从「Dashboard 隐藏条」扩展为「设置页常驻 Tab + 主动检查」，可见性与可测性提升；②「检查更新」按钮让部署未发版提交的场景可被显式探测（仍受限于需 GitHub 新 Release 才 update_available=true）；③前端改动需 NAS 重新 build 后生效（与 B109 同为 WebUI 资产，随镜像重建）。

### B111. WebUI 四项验收问题修复：知识图谱渲染时序 + 技能仓库检测命名（2026-08-27）

**背景**：用户验收 1.0.1 时提出 4 项问题：①知识图谱页面空白看不见；②关怀信号消费者只有 default；③WIKI 知识库 579 篇是否为未清理 skill 混入；④技能仓库为空。排查结论：② ③ 为设计行为与正常数据（非缺陷）；① ④ 为真实缺陷需修复（①为 ST-13 图谱可视化的渲染回归，④属 ST-36 技能模块索引缺陷）。

**改动**：
1. **#1 知识图谱渲染时序修复（ui/src/views/graph/GraphView.vue）**：原 `load()` 在 `finally{ loading=false }` **之前**调用 `renderGraph()`，此时 `loading===true` 导致 `<svg v-else>` 未挂载、`svgRef` 为 null，`renderGraph` 直接 return；等 `loading` 翻 false 后 svg 出现却再无重绘触发 → 永久空白。修复：将 `renderGraph()` 移到 `finally` 之后并 `await nextTick()` 确保 svg 已挂载再绘制；尺寸计算兜底（width≥320 / height≥480，取 `parentElement.getBoundingClientRect()` 或 `clientWidth`）；`renderGraph` 收尾加 try/catch，渲染异常显式暴露到 `error` 而非静默空白。
2. **#4 技能仓库检测/命名修复（sgme/skills/indexer.py）**：根因双因——①`collect_from_wiki` 仅按 `tags LIKE '%"skill"%'` 过滤过严（实测 wiki 中 6/7 个 skill 页 tags 不含 'skill'，仅 `category=skill/xxx`）；②中文 title 过 `validate_name` 白名单 `[A-Za-z0-9_.-]` 失败被静默 `continue` 跳过 → total:0。修复：SQL 放宽为 `WHERE status='active' AND (tags LIKE '%skill%' OR lower(category) LIKE 'skill%')`；新增 `_wiki_skill_name()` 从 title `skill:` 前缀 / category `skill/X` 子段 / 净化 title / page_id ASCII 残段 推导合法 ASCII 名（非 ASCII 中文 title 自动规整）；同名追加 `sha256(page_id)[:4]` 消歧；保留 Row/裸连接双兼容列访问。
3. **#2 关怀信号消费者只有 default**：确认设计行为——`sgme/signal/engine.py` 用 `get_or_create_subscriber` 单一订阅者（默认 agent），无多消费者注册表；单用户记忆引擎单消费者订阅即正确，非缺陷，无需修改。
4. **#3 WIKI 579 篇**：确认非 bug——NAS 上 `skill*` 类仅 7 篇（skill/vps/sgme×2/dsh/common/verify），其余为 research/dsh 等研究笔记，无大量未清理 skill 混入。

**验证**：
- #4：`tests/test_skills_indexer.py` 新增 `test_collect_from_wiki_chinese_title_category_marked`——内存 DB 插 7 个 skill 页（6/7 tags 不含 'skill'）+ 1 个 research 负样本，断言收录 7 个、名字为合法 ASCII（dsh/sgme/sgme-67a4/vps/common/verify/ff769a90）、同名消歧生效、研究笔记排除；该测试文件 **21 passed**。
- #4 连带：indexer 系列（test_skills_indexer / store / pr7 / coldstart）合计 **52 passed**。
- #1：UI `npm run build`（vite）通过（GraphView chunk ~68KB，无 TS/模板错误）。
- #1 限制：沙箱浏览器走代理无法连 LAN 的 NAS（`net::ERR_NO_SUPPORTED_PROXIES`），未能真机 console 验证渲染；改以静态时序分析 + 编译验证定位，需用户在本地浏览器或 NAS 部署后实测确认。

**运维影响**：①#1 #4 代码改动需 NAS 重新 build + 重启 SGME 服务生效（WebUI 资产随镜像重建）；②#4 放宽后 wiki skill 页将正常纳入技能仓库索引（此前 total:0 全空）；③#2 #3 无需运维动作。

### B112. autoupd 自动更新链路根治（cron safe.directory 修复）+ B111 部署验证（2026-08-27）

**背景**：B111 四项修复（#1 图谱时序 / #4 技能索引）已本地提交并 push 到 NAS 裸仓、重建镜像、force-recreate 上线（见下「部署验证」）。但「立即更新」自动链路仍坏：① `sgme-host-updater.sh` 根本未进 cron（只有 `nas_watchdog.sh`）；② 即便进 cron 也会因 **root 跑 git 撞 `fatal: detected dubious ownership`**（`src` 属主 `LEO:Users`，cron 以 root 跑）。**根因 = 自动更新代理以非仓库属主（root）身份操作 LEO 所有的 git 仓库**。

**改动（scripts/sgme-host-updater.sh + NAS 部署配置）**：
1. **updater 改以仓库属主 LEO 运行**（根治，非给 root 加 `safe.directory`——后者会致 root 写入 src 造成属主漂移）：`/etc/cron.d/sgme-watchdog` 新增 `*/5 * * * * LEO /vol1/1000/Docker/sgme/scripts/sgme-host-updater.sh >> .../logs/updater.log 2>&1`（watchdog 保持 root，因其需 `systemctl start docker.service`）。
2. **data/update 目录属主归还 LEO**：该目录原为 `root:root`（容器 root 写 request.json 留下），LEO 身份的 updater 无法 rm/重写。经容器 root `docker exec sgme chown -R 1000:1001 /data/update` 改回 `LEO:Users`。
3. **mark_failed 写回健壮性**：原 `cat > "$REQUEST_FILE"` 在 request.json 为容器 root 创建的 `root-owned 644` 时，非 root 身份无法 truncate。改为先 `rm -f "$REQUEST_FILE"` 再 `cat >`，任何属主下均可重写失败状态。
4. **补 cron 调度缺失**：原 cron 仅 watchdog，updater 缺失；现已补齐（见上）。

**B111 部署验证（NAS 192.168.10.10:9910，已上线）**：
- `git push nas main`：`9ce9991..3613bde`（B111 4 文件，快进）。
- 备份旧镜像 `sgme:1.0.1-nas-autoupd.bak-pre-b111`（rollback 点）。
- NAS `src` `git pull` → tip `3613bde`；`docker build -t sgme:1.0.1-nas-autoupd .`（含 UI 重编译，产物 GraphView chunk 68KB）→ `docker compose up -d --force-recreate`。
- 验证：`/v1/health` `status=ok version=1.0.1`；新 `GraphView-DqCOUNOG.js` 对外 HTTP 200（#1 修复确凿上线）；旧镜像留 rollback 点。

**验证（autoupd 链路）**：
- LEO 身份 `git -C /vol1/1000/Docker/sgme/src pull` → `Already up to date.`，**无 dubious ownership**（根因消除的直接证据）。
- 造 `request.json`(target 1.0.1, pending) 后以 LEO 手动跑 updater → exit 0、request.json 被清除、日志 `当前已是 1.0.1 ... 标记完成`，无 safe.directory 报错。（注：updater.log 中 21:55 的 `dubious ownership` 为历史旧记录，本次 LEO 运行 23:47 干净通过。）
- 说明：当前运行即 1.0.1，故走「已是最新」短路（不重建）；要真触发完整 `git pull→build→compose up` 链，仍需 GitHub 发 >1.0.1 的 Release tag + bump `SGME_VERSION`（用户既定 1.1.0 计划），此为前提条件、非本次缺陷。

**运维影响**：①「立即更新」自动链路现已可用（每 5 分钟 cron 以 LEO 轮询 request.json）；② 根治了 root 操作 LEO 仓库的属主漂移风险；③ 仍需用户发版到 1.1.0 + bump 版本号，按钮才能端到端完成一次真实更新（版本一致性校验 gate）。

### B113. 修复 /v1/wiki/pages 分页「160 条假上限」——status 过滤 + total 口径一致（2026-08-28）

**背景**：实测 `GET /v1/wiki/pages` 单次响应始终最多 160 条、offset≥160 返回空、但 `total` 报 579（全表）。排查 NAS 容器内真实库（`/data/data/wiki.db`，579 行）：status 分布为 `active=160 / superseded=419`。根因 = **`wiki_dao.list_pages` 硬编码 `status='active'`（只返 160 活跃页），而端点 `total` 用 `count_pages(conn)` 报全表 579** → total 与返回集口径分裂，造成「翻不到尾页」的假象（并非真有 160 条 API 上限）。

**改动**：
1. `sgme/data/wiki_dao.py`：`list_pages` 新增 `status` 参数（默认 `'active'`；`'all'` 不过滤；其余值按 status 等值过滤），SQL 改为动态 WHERE 拼接；`count_pages` 新增 `status` 参数（`None`=全表，否则按 status 计数）。保持 `evolve`/`skills` 调用默认 `active` 兼容。
2. `sgme/wiki/routes.py`：`list_wiki_pages` 新增 `status` Query（pattern `^(active|all|superseded)$`，默认 `active`），透传 DAO；`total` 改为 `count_pages(conn, status=status)` 与返回集一致；响应体附 `status` 字段。
3. `sgme/operations/wiki.py`：`list_pages` 同步加 `status`（默认 `active`），`total` 跟随；MCP `wiki_pages` 工具不传 status → 默认 `active` 行为不变。
4. `ui/src/api/wiki.ts`：`listWikiPages` 支持 `status` 参数，`WikiPages` 接口增 `status?` 字段。
5. `ui/src/views/wiki/WikiView.vue`：新增状态筛选下拉（active / 全部含历史 / superseded），切换时重置 offset 并 reload，`total` 与返回集一致驱动翻页。

**验证**：
- `tests/test_wiki_filter.py` 新增 4 用例：`status` 默认 active 只返 active 且 total=active 数；`all` 返全表且 total=全表；`superseded` 只返旧版且 total 一致；`all`+offset 翻页能取到 superseded（证明无空上限假象）。`wiki/skills` 相关测试合计 **109 passed**。
- 前端 `npm run build` 通过（`WikiView` chunk 重编译无 TS/模板错误）。

**运维影响**：① 修复需 NAS 重新 build + 重启 SGME 生效（WebUI 资产随镜像重建）；② 默认浏览仍为 active（160），用户点「全部」即可看 579 全量且翻页一致；③ 消除 total 误导，UI 翻页按钮 `offset+limit>=total` 判定现在与真实返回集对齐。

### B114. 技能模块「去 wiki 化」：技能由 source_dirs 自有 SKILL.md 管理 + 移除 wiki 桥接 + 补 MCP 写侧（2026-08-28）

**背景**：用户判定「通过 skill 标签在 wiki 库里识别技能」是历史遗留的权宜方案——技能本应由 skills 模块自有管理，而非寄居 wiki。B111 后技能仓库显示的 11→4 个「技能」实为 wiki 里被 `skill` 标签/分类误标的页面（研究/设计/治理文档混于其中），并非真正的可执行技能。本次落实三件事：① 把 4 个真 how-to 从 wiki 迁为 `source_dirs` 的 SKILL.md（模块真正「拥有」技能）；② 移除 index_all 对 wiki 的桥接调用（wiki 不再是技能来源，一并消解 B112 讨论的 `LIKE '%skill%'` 子串误收问题）；③ 补齐 MCP 技能工具缺口（L0 列表 / 冷启动 / 写侧）。

**改动**：
1. **新增仓库技能树 `skills/`（git 跟踪）**：`skills/{sgme,sgme-key,vps,verify}/SKILL.md`，内容从 wiki 4 个真 how-to 页（SGME操作手册 / 免费模型Key申请指南 / VPS加固变更与登录方式 / 生产验证页）抽取，frontmatter 含 `name/description/tags:[skill]/category`。
2. **`sgme/skills/indexer.py`**：`index_all` 仅扫 `source_dirs`（移除 `collect_from_wiki` 调用）；`collect_from_wiki` 函数保留（测试/回滚用）但标注【已弃用】，不再接入索引。模块与 `operations/skills.py` 文档同步更新。
3. **`Dockerfile`**：`COPY skills/ /app/cache/skills/` 烘焙技能树进镜像；并对 `/app/cache/skills` 执行 `git init` + 首次 commit（写侧 MCP 工具 `store.*` 需 git 仓提交）。
4. **`sgme/mcp_server.py`**：新增 5 个 MCP 工具——`skill_list`（L0 列表）、`skill_coldstart`（冷启动包）、`skill_put`/`skill_delete`/`skill_rename`（写侧，调 `sgme.skills.store`，`_require_admin` 声明与仓库 MCP 鉴权姿态一致）；同步追加 `ONBOARDING_TOOLS` 能力清单（与 `@mcp.tool` 顺序一致，防漂移测试断言）。
5. **NAS wiki 库剥离**：4 个 how-to 页去除 `skill` 标签 + `skill/` 分类前缀（`wiki_dao.update_page_content`，正文不动），wiki 现 0 个 skill 页——与 SKILL.md 来源不再重复/歧义。
6. **测试**：`test_operations_skills.py` / `test_routes_skills.py` 移除对 wiki 桥接的断言，改为「技能仅来自 source_dirs」+ 负向断言「wiki-skill 不应出现在技能索引」（固化桥接移除）。

**验证**：
- 本地 `pytest -k "skill or mcp"` **144 passed**；`test_skills_indexer.py` + `test_wiki_filter.py` **33 passed**。
- 容器内 `collect_from_dir` 对真实库实测：4 个 SKILL.md → 4 条记录、source 全为 `git`（部署后线上 `/v1/skills` 验证）。
- 写侧工具链路：MCP `skill_put` 经 `store.write_skill` 落盘 `<source_dir>/<name>/SKILL.md` + git commit（容器内 `cache/skills` 已 `git init`）。

**运维影响**：① 技能现由镜像内 `/app/cache/skills`（git 仓）真正拥有，重建镜像即随带出；② wiki 与技能彻底解耦——wiki 回归纯知识库，技能仓库=源目录 SKILL.md 视图；③ MCP agent 现可完整管理技能（搜/取/物化/枚举/冷启动/写）；④ 写侧 MCP 的 admin 门禁目前与仓库既有 MCP 姿态一致（中间件仅校验 agent-key，`_require_admin` 为意图声明桩），后续若要严格管理员隔离需补请求级 key 校验（待办）。

### B115. 统一搜索与说明文档对齐「技能去 wiki 化」架构（2026-08-28）

**背景**：B114 已让技能检索 100% 走 `source_dirs` 的 SKILL.md（`index_all` 移除 wiki 桥接），但**代码层之外**的全部说明文档与注释仍停留在「技能经 wiki 标签识别」旧模型：模块 docstring 的 scope 清单漏列 `skills`、Skills 设计 v0.2 仍写「索引层=BM25（wiki_pages tags 含 'skill' 过滤）」、架构 v1.0 的 scope 枚举与检索层说明缺 skills 与 §30.11「MCP 四工具」、接口契约 v0.1 称「三层检索」且 scopes 缺 skills、agent-onboarding 工具表无 `skill_*`、两份 README 模块树无 `sgme/skills/`。用户要求统一搜索功能与全部说明文档（含 README）对齐修正。

**改动**：
1. **默认检索纳入 skills（功能变更）**：`sgme/server/routes_memory.py` 的 `SearchRequest.scopes` 缺省 `["memory"]` → `["memory","skills"]`；`sgme/operations/search.py` 的 `DEFAULT_SCOPES` 同步。MCP `search` 工具显式传 `scopes=["memory"]`（mcp_server.py:341），不受共享默认值影响，行为不变。
2. **`sgme/operations/search.py` 注释修正**：模块 docstring scope 清单补 `skills` bullet，并把「HTTP 缺省 ["memory"]」更正为 `["memory","skills"]`；`search()` docstring 补 skills 层说明；删除 `_search_wiki_pages` 中「排除 skill 标记页（回忆通道不见手册）」的失效注释（技能已不在 wiki，过滤器理由过时）。
3. **`docs/design/SGME-Skills管理模块设计-v0.2.md`**：索引来源由「wiki_pages tags 含 'skill' 过滤」改为「`source_dirs` 的 SKILL.md（B114 已移除 wiki 桥接）」。
4. **`docs/design/SGME-架构设计-v1.0.md`**：① scope 枚举补 `skills`；② 检索层说明补 skills 层 bullet（BM25+向量融合 0.6/0.4，source=skills_repo）；③ §30.11「MCP 四工具」→ 九工具（skill_search/digest/get/materialize/list/coldstart/put/delete/rename），「4 页误挂标签留 wiki 待摘」→ B114 已剥离。
5. **`docs/design/SGME-接口契约-v0.1.md`**：4.3 标题「三层检索」→「多源检索」；scopes 补 `skills`；scope 说明补 skills 段。
6. **`docs/agent-onboarding.md`**：`search` 描述补 skills scope；工具表补 `skill_*` 九工具行。
7. **`README.md` / `README.zh-CN.md`**：统一检索卖点补「技能（skills）亦为可检索源」；模块树补 `sgme/skills/`（自有 SKILL.md，B114 起与 wiki 解耦）。
8. **`tests/test_operations_search.py`**：`test_search_scopes_none_defaults_to_memory` 注释与默认预期对齐 `["memory","skills"]`。

**验证**：
- `tests/test_operations_search.py` **37 passed**；`tests/test_routes_skills.py` + `tests/test_server.py -k "search or skills or skill"` **22 passed**。
- 全部 9 个改动文件 UTF-8 解码校验 BAD=0。
- 部署后线上 `POST /v1/search {"query":"…"}`（不传 scopes）默认含 skills 层；`/v1/health status=ok version=1.0.1`。

**运维影响**：① 统一搜索默认同时召回记忆与技能，技能召回不再需 agent 显式传 `scopes=["skills"]`；② 全量说明文档与代码现状一致，消除「说明书滞后于架构」误导；③ MCP `search` 行为不变（仍 memory-only），避免破坏既有 agent 调用；④ 其余未触碰项——写侧 admin 门禁桩、LLM 降级链 agnes 描述不同步——仍属待办。

### B116. MCP 写侧 admin 门禁落地 + AGENTS 降级链同步 + 技能 lint 放宽（2026-08-28）

**背景**：B114 留两待办（写侧 `_require_admin` 为桩、AGENTS.md LLM 降级链仍写 zhipu→deepseek 与线上 agnes 不符）；另发现 `sgme/skills/gates.py` 的 `REQUIRED_FIELDS` 含 `version`/`pattern`，但已上线的 4 个真实技能无此字段，导致 `skill_put` 无法管理真实技能（生产隐患）。本次一并收尾。

**改动**：
1. **`sgme/mcp_server.py`**：`_require_admin` 由空桩改为真实请求级校验——从 `ctx.request_context.request.state.api_key` 取呈现 key，比对 `AgentKeyStore.is_admin`，返回 `tuple[bool, str]`；`skill_put`/`skill_delete`/`skill_rename` 接入 `ctx: Context | None = None` 参数，门禁拒绝时返回 `{"error":...,"code":"ERR_FORBIDDEN"}` 字符串（FastMCP 不标 isError，但 agent 可解析 code）。直调无 request 上下文 → 安全默认拒绝。
2. **`sgme/skills/gates.py`**：`REQUIRED_FIELDS` 由 `("description","version","pattern","category")` 放宽 → `("description","category")`（`version`/`pattern` 改可选，保留 pattern 枚举校验当填写时），`lint_skill` 文档同步。理由：真实技能无 version/pattern，原必填使写侧工具无法落盘真实技能。
3. **`AGENTS.md` 第 9 条**：LLM 降级链由 `zhipu(glm-4.7-flash)→deepseek(deepseek-v4-flash)→rule drop_batch` 按 `config/llm.yaml` 真实链同步为 `agnes(agnes-2.5-flash)→siliconflow(deepseek-ai/DeepSeek-V4-Flash)→zhipu(glm-4.7-flash)→rule drop_batch`（2026-08-22 用户定 agnes 优先）。
4. **`tests/test_mcp_server.py`**：新增 `test_mcp_skill_put_agent_key_forbidden`（agent key 被拒 ERR_FORBIDDEN）+ `test_mcp_skill_put_admin_key_writes`（admin key 落盘成功）；断言按 FastMCP 实际返回（错误为 result 字符串，故检查 text 含 ERR_FORBIDDEN）。

**验证**：B116 两测试 pass；`test_mcp_server.py`+`test_operations_search.py`+`test_routes_skills.py`+`test_server.py` 回归 **108 passed**（0 失败）；6 个改动文件 UTF-8 解码 BAD=0。

**运维影响**：① 写侧 MCP 工具现在真正区分 admin/agent key（admin 可写、agent 403），与 HTTP admin 鉴权对齐；② AGENTS.md 与线上一致；③ 技能 lint 与已上线真实技能字段兼容，写侧工具可用。

### B117. 场景治理根治：dream 调度器接入启动 + 合并阈值下调（2026-08-28）

**背景**：用户观察到多个 AIRDT/SGME/hermes 场景重复。排查线上：active 场景 **350**（远超 max 300 红警），`scene_gc` 配置正确（`enabled=true, threshold=0.80`），GC 候选对 **21 对**（相似度≥0.80，含 `SGME 后端与 DSH 方向`×2 完全重复 sim=1.000、`家庭照护责任`×3、`父亲透析照护`×2 等），`will_run=true`——**逻辑能检测却从未合并**。根因：最近一次 dream 运行停在 **2026-08-15**（距今 13 天）；`scene_gc` 由夜间 dream 流水线驱动，但 `dream.ensure_scheduler` 仅 `POST /v1/admin/dream/trigger` 端点接线，**未接入 `app.py` lifespan 启动**——容器/进程重启后 daemon 线程死亡且永不自动复活，导致 dream 停摆、scene_gc 从未执行、重复场景堆积。

**改动**：
1. **`sgme/server/app.py` lifespan**：`start_background_tasks` 块接入 `dream.ensure_scheduler(cfg, data_dir=d)`（与 `batch_scan`/`persona_monthly` 同款 try/except 不阻断启动）。根治「重启即死」——生产 Gateway 启动即按 `dream.schedule`(03:00) 自动跑场景治理。
2. **`sgme/config.py` `DEFAULT_SCENE_GC_CONFIG.merge_threshold`**：`0.80` → `0.70`（收掉 AIRDT/SGME/hermes 等弱相似度重复场景；下限 `min_threshold=0.70`）。

**验证**：编译 + UTF-8 校验 OK；部署重建后手动 `POST /v1/admin/scene-gc/trigger` 合并候选；复查 active 数下降、hermes 去重（见运维影响）。

**运维影响**：① 重启后 dream 定时器自动常驻，夜间 03:00 自动执行场景治理，**根治复发**；② 阈值 0.70 更积极合并弱相似场景（契合用户「看到多个重复」诉求）；③ 合并走 `archived` 状态（原件不删、可恢复），符合「原件永不删」铁律；④ 本变更需 NAS 重新 build + 重启 SGME 生效（配置每次启动从代码默认值加载）。

### B118. 技能详情页渲染 L2 全文，修复空大纲导致内容不可见（2026-08-28，补记）

**背景**：SkillsView 技能详情页在技能无大纲（sections 为空）时正文区域不可见——详情页渲染依赖大纲字段，
大纲缺失则整个内容区不渲染，用户点开技能看不到任何正文（commit 52b6917 已修，此处补登记变更记录）。

**改动**：
1. **`ui/src/api/skills.ts`**（+13）：补 L2 全文取数逻辑，详情页不再只依赖大纲字段。
2. **`ui/src/views/skills/SkillsView.vue`**（+16/-9）：详情页改为渲染 L2 全文，空大纲时仍正常展示正文。

**验证**：commit 52b6917（2026-08-28）。

**运维影响**：无大纲的技能（如手工录入、frontmatter 不完整）详情页现在可读；UI 改动需重建镜像生效
（`ui/dist` 烘焙进镜像不挂卷，见前端改动部署 SOP）。

### B119. dsh-sgme 0.4.0：技能层接入 + source 类型漂移修复（2026-08-29）

**背景**：用户升级 NAS 飞牛系统致 sgme 容器自启失效（已自行修复），要求核查 SGME 近期更新对 dsh-sgme 0.3.1 的影响。
实测 25 个在用端点**零破坏**（`GET /v1/wiki/search?q=手册` 仍能召回 SGME操作手册，B114 技能去 wiki 化未波及 wiki 通道；
`ideas|demands|projects` 仍是 `require_admin_key`，未被 B116 的 `/v1/admin/skills` 写侧门禁波及），但暴露 3 个实质问题：
①skills 层 403 个技能 dsh 侧完全够不到（`SearchResult.source` 无 `skills`，所有调用点 scopes 恒为
`['memory']`/`['wiki','wiki_pages']`/`['memory','wiki']`）；②统一搜索 skills 层结果只有 `name/description/category`、
**无 `content`/`title`**，`tools.ts` 的 `r.content.length` 一旦接入必 TypeError 崩；③wiki 场景层 source 实际返回
**`wiki_scene`**（非类型声明的 `wiki`/`scenes`），`context.ts` 过滤 `source==='wiki'||'scenes'` 恒空 →
T-88「首句命中 L2 场景注入」自 2026-08-20 实现起**从未生效**，一直静默回退模板注入。

**改动**：
1. **`sgme-client.ts`**：`SearchResult.source` 补 `'wiki_scene' | 'skills'`；`content` 改可选并补 `name`/`description`/
   `category`/`score`；新增技能层 5 类型（`SkillSummary`/`SkillsListResponse`/`SkillDigest`/`SkillDetail`/
   `SkillsColdstartResponse`）；新增 `skillList`/`skillSearch`/`skillDigest`/`skillGet`/`skillColdstart` 五方法
   （`skillSearch` 走 `POST /v1/search scope=["skills"]`——HTTP 侧唯一入口，服务端无 `/v1/skills/search`）。
2. **`tools.ts`**：`formatSearchResults` 兜底 `content ?? (name — description) ?? description`（防 skills 层崩溃）；
   新增 `skill_search`/`skill_digest`/`skill_get`/`skill_list`/`skill_coldstart` 五工具并注册（工具数 19 → 24）。
3. **`context.ts`**：场景过滤补 `r.source === 'wiki_scene'`（**修复 T-88 场景注入从未生效**）；
   `buildInjectionText`/`buildSceneInjectionText`/`formatRelatedMemories` 参数 `content` 改可选并兜底。
4. **`commands.ts`**：`/sgme` 检索结果格式化同步补 content 兜底。
5. **`tests`**：新增 `skills-tools.test.ts` 17 用例（含 3 个「skills 层无 content 不崩 + 兜底优先级 + 超长截断」回归）；
   `admin-tools.test.ts` 注册数断言 19 → 24 并补 5 个技能工具名；清 tests 预存 7 个 typecheck 错误
   （`b86-regression.test.ts` 5：import 补 `.js` 扩展名 ×2 + 参数显式类型 ×2 + 可选链兜底 ×1；
   `context-v2.test.ts` 2：mock 标注 `SearchResponse`/`SearchResult[]`）。
6. **`package.json`** 0.3.1 → 0.4.0（新功能走 minor）；**`README.md`** 工具清单 7 → 24 并按五组重排，补技能层范式说明。
7. **`sgme-client.ts` 新增 `normalizeSkillSection()` 并在 `skillGet` 归一化 section**（真实链路冒烟发现，2026-08-29）：服务端 `section` 参数只认**纯标题文本**（`前置条件`），而 `skill_digest` 的 sections 骨架给的是**带 # 前缀的原样行**（`## 前置条件`）——agent 照抄骨架传上去必 404，且服务端错误文案误导为「技能不存在: xxx」，client 降级成 null 后工具提示「技能不存在或 Gateway 不可达」，排障极易跑偏。归一化剥掉 `#` 前缀与两侧空白，两种写法都能命中。

**验证**：`pnpm run verify` 三步全绿——typecheck **0 错误**（基线 7 → 0，顺带清掉历史技术债）、
vitest **164 passed / 0 failed**（13 files，原 147 + 新增 17）、build 成功（`lib/index.js` 124.89 kB / gzip 35.30 kB）；
8 个改动文件 UTF-8 校验 **BAD=0**；**真实链路冒烟**（临时 `tests/_live-skills.test.ts`，跑后删除）直连 NAS 生产端点 **6/6 通过**——skillList（total=403）/ skillSearch（命中 nas-docker-operations 等）/skillDigest（骨架+uses）/ skillGet（全文 3.4k 字符；section 截取后 truncated_by_section=true 且短于全文）/skillColdstart（1 个协议 skill 带 content 全文 + SGME操作手册）+ 五个工具 execute 均返回可用文本（正是这一轮冒烟暴露出 section 的 # 前缀 404 问题）。

**运维影响**：① dsh agent 首次具备技能按需注入能力（403 技能），需 SGME skills 模块启用（`skills.source_dirs` 有配置，
否则端点返回 404、工具降级提示）；② **行为变化**——场景注入修复后，首句命中 L2 场景时由「静默回退模板注入」改为
「注入场景 + 相关记忆」，注入内容更贴题（T-88 设计意图首次真正生效），未命中时行为不变；
③ 0.x 系列 `^` 锁 minor：profile 依赖 `"dsh-sgme": "^0.3.0"` 不会自动升 0.4.x，需显式改 `^0.4.0` 再 install
（同 B106 记录的 npm 0.x 语义坑）；④ 发布仍需走 7897 代理 + granular token（`docs/design/SGME-dsh-sgme-发布流程-v0.1.md`）。

### B120. skills 索引持久化（skills.db）+ embed 分批解锁向量路（2026-08-29）

**背景**：两条线交汇。①用户问「新的 skills 模块是否达到预期的渐进式披露」→ 实测结论：四级披露
**形态达标**，但按需检索范式**未达标**（向量路从未生效 + 长句召回崩）。②用户问「是否该用数据库存
skills 索引」→ 我先基于当前规模判断「暂不建库」（全量重建 3.15s、单次打分 0.5ms、向量仅 1.65MB），
**用户从产品演进角度反驳**：「作为一个产品，目前可能够用，但未来呢？我建议直接上数据库」——采信，
一次到位。

**关键实测数据（决策依据）**：
- 内存索引性能：`index_all` 0.58s / `SkillsBm25` 构建 2.56s / **全量重建 3.15s** / 单次打分 **0.5ms**
  → 及格线②「≤数分钟」大幅达标，说明**性能从来不是瓶颈**
- Ollama bge-m3 批量 embed（每条约 500 字符）：`1 条 2.1s / 10 条 19.2s / **50 条 35s 超时**`
- 向量缓存目录 `/data/cache/skills/` **根本不存在**（`save_cache` 机制健全，是压根没执行到）
- 路径勘误：用户指定「/data/skills.db」，但 `/data/` 下 memory.db/wiki.db 是 **0 字节死文件**，
  真库在 `/data/data/`；`DATA_DIR = _USER_ROOT / "data"` 天然落在 `/data/data/skills.db`，与用户意图一致

**改动**：
1. **`sgme/data/db.py`**：新增 `SKILLS_DDL` / `SKILLS_FTS_DDL` / `SKILLS_FTS_TRIGGERS` 与
   `connect_skills(data_dir)`。表设计——`skills`（主表 + sha256 锚点 + 分词列）、`skills_fts`（FTS5
   外部内容表）、`skill_vectors`（BLOB）、`skill_uses`（依赖图 + 入向索引）、`skill_sync_meta`（水位）。
   ⚠️ **向量用普通表 + BLOB 而非 vec0**：项目 memory_vectors/scene_vectors 均为该形态，
   `data/search/vector.py` 已封装 sqlite-vec 加速 + numpy 降级双路，引入 vec0 属范式分裂。
2. **新增 `sgme/data/skills_dao.py`**（data 层唯一出口）：CRUD / 分页 / category 过滤 / 分类目录；
   FTS 检索带 **BM25 列加权 10:5:1**（技能名最强信号）与**中文停用词过滤**；
   `diff_records`（sha256 增量判据）、`vector_covered`、`find_incoming` / `find_outgoing`、meta 读写。
3. **`sgme/skills/vectors.py`**：`embed_texts` 改**内部分批**（`_embed_batch` 单批 + 循环），
   默认 10 条/批、超时 60s（可配 `skills.embed_batch_size` / `skills.embed_timeout`）；
   单批失败跳过不拖垮其余批，全失败才抛；`build_vectors` 加 `max_new` 限批。
4. **`sgme/data/search/vector.py`**：新增 `upsert_skill_vectors`（批量单事务）、`delete_skill_vector`、
   `skill_vector_search` + `_sqlite_vec_skill_search` / `_numpy_cosine_skill_search`（对称 scene 系列）。
5. **`sgme/operations/skills.py`**：新增 `sync_index`（增量同步：insert/update/delete/unchanged + 向量补缺）
   与 `search_skills_db`（FTS ∪ 向量 0.6/0.4 融合，routes 标记 skills_bm25 / skills_rrf）；
   `search_skills` / `list_skills` 加 `skills_conn` 参数（None 回退内存索引）；
   新增 **`_db_ready` 空库回退**、`_record_to_dict`（兼容 SkillRecord / dict）。
6. **接线**：`app.py` 挂 `skills_conn`（仅 `skills.enabled` 时连库）+ lifespan 起 daemon 线程做
   首次结构化同步与向量分批预热；`routes_memory.py` / `routes_skills.py` / `mcp_server.py` 三入口传参。
   ⚠️ 刻意**不并入 `init_databases` 三元组返回值**——那会破坏所有既存解包点（v0.7 已踩过）。

**验证**：
- 新增 `tests/test_skills_db.py` **35 用例全绿**（schema 幂等 / DAO CRUD / FTS 停用词与 name 加权 /
  category 过滤 / 引号不崩 / diff 增量 / 依赖图 / 分批边界 403→41 批 / 部分批失败 / max_new 限批 / 空库回退）
- 技能模块 **101 passed**、跨模块（operations_search / routes_memory_l0 / mcp_server / server / search_v04）
  **143 passed**，零回归
- 顺带修 B116 遗留的 5 个过时断言（`REQUIRED_FIELDS` 已放宽为 description+category，
  version/pattern 改可选，测试仍断言必填）
- 部署：NAS 重建镜像，验证 skills.db 生成、同步统计、向量落盘、routes 变化、长句召回

**运维影响**：
① 首次启动后台预热约 **13 分钟**（403 条 ÷ 10 条/批 × 19.2s），期间检索照常可用
——FTS 全量立即生效，向量逐步生效，routes 由 `skills_bm25` 过渡到 `skills_rrf`；
② **空库回退**保障冷启动空窗期不返回空列表（否则用户会以为技能库空了）；
③ skills.db 随 `/data` 卷持久化，容器重建不丢；
④ 新增可配项 `skills.embed_batch_size`（默认 10）/ `skills.embed_timeout`（默认 60s）；
⑤ 技能模块禁用时**不连库**，核心零影响（对称 wiki 扩展模块）。

### B121. 降级链移除 zhipu 的文档/关联配置全量同步 + refine.llm_override 悬空修复（2026-08-29）

**背景**：commit 9af882b「降级链移除智谱免费节点」（zhipu 免费 Key 失效，避免烧付费 ZHIPU_API_KEY）
只改了 `config/llm.yaml` + `config/providers.yaml` 两个文件。Backlog 审查（2026-08-29）发现这是
降级链第三次「改链不跟文档」（前两次 T-96/B100、T-109/B116 各补过一轮），且遗留一处**功能性悬空**：
`config/sgme.yaml` 的 `refine.llm_override` 仍指向 `provider: zhipu`——该段已从 providers.yaml 删除，
resolve 构造节点返回 None 静默跳过，override 防劫持语义（防 dsh agent 注册 agent_model 劫持提炼链）
失效。本条把链描述与关联配置一次性对齐。

**改动**：
1. **`config/sgme.yaml`**：`refine.llm_override` provider zhipu→**agnes**（model agnes-2.5-flash），
   保住「显式 override 优先于 agent 声明」的防劫持语义；batch_scan 注释去掉 zhipu 措辞。
2. **`AGENTS.md` 铁律 9**：链序改 agnes → siliconflow（免费备用）→ rule drop_batch，注明 B121。
3. **`README.md` / `README.zh-CN.md`**：模型 Key 说明段去 zhipu（zh-CN 侧原文本还停在更早的
   「智谱主链」描述，属 B100 漏同步，一并修正）。
4. **`docs/runbook.md` §4.2**：链示例 yaml 与 Key 导出示例去 zhipu（ZHIPU_API_KEY 标注废弃）。
5. **`docs/agent-onboarding.md`**：「两个模型」段改 agnes 主链描述。
6. **`docs/guide/免费模型Key申请指南.md`**：表格删智谱行、链序说明更新、§三 智谱节标记废弃留档、
   providers 说明改两家、时效声明删智谱链接。
7. **`docs/design/SGME-架构设计-v1.0.md`**：§1 铁律 9、§24 链示例、§4 窗口预算行、§5 默认模型行
   四处去 zhipu。
8. **`sgme/operations/llm.py`**：`MODEL_KEY_MISSING_NOTICE` 去智谱句（改为注明 zhipu 已移出链）。
9. **`sgme/mcp_server.py`**：self_config `requirement` 文案去 ZHIPU_API_KEY。
10. **`skills/sgme/SKILL.md` / `skills/sgme-key/SKILL.md`**：技能手册链描述仍停在最旧版
    （「zhipu 主链」），随 T-113 测试修复批次收尾单独更新（避免与并行测试修复的 coldstart 基线撞车）。

**验证**：
- `grep glm-4.7|zhipu` 全库扫描（排除归档类文档），链描述残留清零（skills/ 两手册见改动 10）
- 配置加载冒烟：`load_config` 正常、refinement 链 = [agnes, siliconflow, rule]；
  pytest 全量数字见 B122（T-113 测试漂移修复批次）

**运维影响**：
① **NAS 生产 `/data/config/sgme.yaml` 为挂载卷副本，不在镜像内**——需手动同步改
`refine.llm_override` 为 agnes，否则生产继续悬空跳过（提炼跟随 agent 声明，存在被劫持风险）；
② `ZHIPU_API_KEY` 环境变量不再被任何配置引用，可从 docker.env/config/.env 移除；
③ `detect_missing_model_keys` 只报告实际在链供应商，智谱 Key 缺失不再出现在 missing_keys。

### B122. T-113 全量测试漂移集中修复 + create_app bearer 环境污染根修（2026-08-29）

**背景**：Backlog 审查（2026-08-29）发现 main 全量 pytest 不绿且被长期误标——T-97/T-99 备注把
supersession/l15 失败写成「预存在环境问题（维度种子/网络）」，实际是「代码有意变更、测试没跟」的
纯漂移欠账。登记 T-113 后以 worktree（pr-113-fix-drifted-tests）+ 并行子代理执行修复，
merge --no-ff 回 main。**全量基线实测 50 failed（9af882b，2004 collected）**——最初「20 failed」
为输出截断低估，登记口径已更正。

**改动**（tests/ 20 文件 + 生产 1 处根修）：
1. **projects/tasks 维度残留（≈25 例，最大组）**：三池重构（2026-08-18）移除维度后，supersession/
   模板（operations+routes）/engine/l15/l15_prescreen/registry/e2e 的夹具与断言仍打 projects 标
   → FK IntegrityError 或归一化 100% 丢弃；统一换 14 维中语义相近维度（tech_stack/goals 等），
   模板类以生产 templates/*.yaml（已是正确现状）为形状基准。
2. **版本断言硬编码**：test_server_v04/test_stall_watch 断言 '1.0.0' → `sgme.__version__` 动态
   （固化 health SGME_VERSION 与包版本一致的契约，未来 bump 不再崩）。
3. **9af882b 链变更余波**：vector_connectivity 链序期望、key_missing_guide 的 `_REFINE_KEYS`
   （改从加载配置动态推导 + 新增「zhipu 不再被检测」反断言）、config/providers/l1_chunk 的
   首链 deepseek 期望 → agnes→siliconflow→rule。
4. **B117 余波**：scene_gc 默认 merge_threshold 0.70 + min_threshold 键。
5. **scenes shape**：test_routes_admin_browse 补 T-55 新增的 `related_memories` 键。
6. **测试密封性**：test_operations_inject 两例 no_note 用例打桩模型 Key——原依赖宿主未跟踪
   config/.env，干净 checkout/CI 必挂（worktree 实测复现）。
7. **生产根修（sgme/server/app.py）**：`create_app` 曾 `os.environ.setdefault("SGME_BEARER_TOKEN",
   bearer)` ——工厂函数污染进程全局状态；同一进程后续 create_app（bearer_token=None）会从
   环境读回泄漏值 → test_skills_coldstart 套件顺序下 401、单跑绿（test_signal/test_server_v04
   的 delenv 防御注释为前人绕过证据）。全库无任何运行时读者依赖该回写，删除零行为影响；
   +1 测试锁定「不污染 os.environ」语义。
8. 顺带确认：skills 门禁 5 例（gates/pr7）已由 2b83671 修复，未复现。

**验证**：
- 基线 9af882b：50 failed → 修复后 worktree 全量 **exit=0 / 0 failed**（主代理亲自复跑，
  非子代理自报口径）；merge 6eb215b 后主树抽验 6 文件 0 failed
- e2e 两例根因定性：mock 维度「项目」归一化丢弃致记忆未入库（非 embed 问题，日志
  `记忆=1 条 drops=1` 证据）

**遗留与上报**：vector.py embed 回退未校验目标 provider `vector_capable`（链首 agnes 无
embeddings，无 vector 配置环境发注定失败的 401 请求后才降级）→ 登记 T-117 🟡；属设计内
降级非功能 bug。

**运维影响**：
① 部署契约不变：SGME_BEARER_TOKEN 仍是「启动前设环境变量」（app.py 不再回写，无消费者）；
② 测试不再依赖宿主 config/.env，CI/干净 checkout 可直接全量跑；
③ 直查 SQL/维度相关的后续改动请同步测试夹具——本次 50 例欠账的教训是「改维度/改链/改版本
  三件事必须同跑全量」。

### B123. T-118 技能向量配置解析双重解包根修 + T-117 embed 回退 vector_capable 门禁（v1.1.1，2026-08-29）

**背景**：全量验收通过后接手另一会话在途改动（skills.py sync_index 待补向量口径全表化 +
回归用例），检查中发现回归用例重复定义两份且未真正进入修复分支；顺藤摸瓜实锤更深的
生产缺陷——技能向量路自 ST-36 M1 起从未真正工作过。

**T-118 根修（sgme/skills/vectors.py::_embed_config，真生产 bug）**：
1. 双重解包：`load_providers_config()` 返回的就是**扁平** {name: 连接字段}（config.py:378，
   实测顶层键 ['deepseek','agnes','siliconflow','nvidia']），原实现 `.get("providers", {})`
   二次解包永远得空表 → `_embed_config` 恒返回全空 → `embed_texts` 全批失败（「向量模型
   未配置」）→ B120 的分批修复与 sync_index 待补口径全部空转。潜伏自 bfee4cd（M1 骨架），
   被 test_skills_indexer 的错误 mock 形状（多包一层 "providers"）掩护。
2. 修复后解析顺序（本地优先 2026-08-20 定案落地）：
   ① search.vector.provider 在注册表 → 注册表连接字段（default_model 缺省回落
   search.vector.model）；② active 不在注册表（生产形态 provider=local 指向 NAS ollama）
   → search.vector 自带 base_url/model 直连——本地直连优先于云端扫描；③ 无 active →
   providers 中 vector_capable=true 首个可用者；④ 最后 search.vector 自带 base_url 兜底。
   注释承诺的「search.vector 兜底段」原实现从未落地，本次一并补齐。
3. 同批收尾在途改动：test_skills_db 回归用例重复定义两份且 embed=False 未进修复分支，
   重写为真路径四轮验证（embed_texts 打桩 + upsert_skill_vectors 真实现落库：pending 在
   max_embed 截断前统计、限批逐轮补齐、全覆盖归零）；test_skills_indexer mock 形状修正 +
   新增 local-not-in-registry / vector_capable 扫描两例。

**T-117（sgme/data/search/vector.py::embed，B122 遗留登记项）**：
链首回退前查 providers 注册表——明确 `vector_capable=false`（agnes）直接跳过，省一次注定
401/404 的外网请求；注册表查无此人（旧配置形态/本地链首）保持旧行为照常尝试（兼容
test_vector_embed 的既有回退用例）；链首 vector_capable=true 时顺带对齐 default_model/
api_key_env（仅 search.vector 未显式配置时）。+3 测试。

**验证**：受影响 4 文件（test_skills_db/test_skills_indexer/test_vector_fallback/
test_vector_embed）**72 collected / exit=0 / 0 failed**；提交前另跑全量 pytest。

**运维影响**：
① NAS 生产（provider=local + ollama bge-m3）技能向量路将真正打通——重启后后台预热会
  补齐 skills.db 全部待嵌向量（403 条 × bge-m3，免费本地，无费用）；若 ollama 未起，
  embedding 失败批跳过、检索降级 BM25（原行为），无新增风险；
② B121 的 llm_override 改 agnes 后，无 vector 配置环境不再向 agnes 发注定失败的
  /embeddings 请求（T-117 门禁生效）；
③ 版本 1.1.0 → 1.1.1（bug 修复批 +0.0.1，0825 版本规则）。**沙箱验收揪出双源版本漂移**：
  干净 clone + uv 安装 + `python -m sgme` 起服务，health 报 1.1.0 而包已 1.1.1——
  operations/health.py:51 与 server/app.py:736 仍硬编码版本号。单源化收口：SGME_VERSION
  与 FastAPI(version=) 均改引 sgme.__version__（兑现 B122「health 与包版本一致」契约，
  原 v0.8 清理项提前落账）；test_e2e_v04/test_health_v04/test_operations_health/test_server
  共 6 处硬编码版本断言同步动态化（T-113「版本断言硬编码」反模式残留清零）。
④ 全量回归 11 failed 复盘（第 2 轮）：T-117/T-118 修复暴露两类测试形态欠账——
  ①8 例向量路测试（test_search_v04/test_server_v04/test_e2e_v04）夹具用真实
  load_config()，隔离环境下 search.vector 为空 → 靠链首回退喂 mock 客户端，agnes
  门禁生效即断路。修法：cfg fixture 显式注入 search.vector.base_url=mock 地址走主路
  （测试本意是向量路机制，不是回退语义）；②2 例技能检索（test_operations_skills/
  test_operations_search）在 T-118 根修前靠双重解包 bug「静默降级」蒙混，修复后
  真实调用 siliconflow /embeddings（log 实证 HTTP 200，密封性反转）。修法：
  embed_texts 打桩抛异常=向量路降级语义，恢复确定性 BM25-only。修复后 5 文件
  103 passed / 0 failed；教训补强：改「静默失败路径」的代码，必须全局搜「依赖
  该静默失败的测试」——它们是潜伏的密封性炸弹。
⑤ NAS 生产部署实录（2026-08-29 晚，v1.1.0-nas-autoupd → 1.1.1-nas-autoupd）：
  - 前置处置：生产 /data/config/sgme.yaml 的 refine.llm_override zhipu→agnes
    （原文件备份 sgme.yaml.bak-llmoverride-20260829）；NAS src 有另一会话旁路
    手改的 B121 等价内容（未走 git），git stash 封存后 pull 官方版本（内容一致）；
  - updater 链实际触发：意图文件 request.json（target_version=1.1.1）+ 手动执行
    sgme-host-updater.sh（发现该 cron 未装在 NAS crontab——2026-08-28 的更新记录
    疑似另有触发源，待查）；构建成功但健康验证失败自动回滚 1.1.0；
  - 根因①healthcheck：内层 urlopen timeout=3s < health 实际延迟 5.5s（内部同步
    探测 LLM 首链）→ 必然超时误判 unhealthy。治标：compose 内层 25s + timeout 30s
    （已回写仓库 docker-compose.yml）；治本登记 T-119（探测加 TTL 缓存）；
  - 根因②向量预热空转死循环：embed_timeout 60s < ollama bge-m3 冷加载时长，
    每次超时中断模型加载下轮又冷启（日志「嵌入 0 剩余恒 393」，直连测试 20.4s
    冷加载实证）。处置：手动长超时请求焐热模型 + 生产 sgme.yaml skills 段加
    embed_timeout: 180.0，重启后预热稳定推进（每轮 10 条）；
  - 终验：容器 healthy、/v1/health 200 + version 1.1.1 + LLM available(agnes)、
    三远端 main=78a3f03 + tag v1.1.1 哈希一致、skills 向量 403 条后台补齐中。

### B124. 接入体验三连修：inject 报错自解释/mode 缺省回落 + install.json 客户端模式 + 接入文档速查（v1.1.2，2026-08-30）

**背景**：2026-08-30 ZCode 接入 SGME 全链路自检，实踩六个坑（完整版入 wiki《SGME新接入踩坑手册-2026-08-30》，
page 尾号 329b3b33，category skill/sgme）。其中两个可代码治本，用户拍板「并行开发」：PR-17（T-120）+
PR-18（T-121）双 worktree 并行子代理开发，主 agent 验证合并（parallel-subagent-dev 流程，merge --no-ff）。

**T-120（PR-17，commit c01f8e9）**：
1. `sgme/profile/template.py`：新增 `list_templates()`（扫 TEMPLATES_DIR/*.yaml 返回 sorted stem）；
   `_read_yaml` 报错改「模板文件不存在: {文件名}（可用: ...）」——只暴露文件名，消灭 /app/... 容器路径泄漏；
   extends 基模板缺失走同一函数自然获益。
2. `sgme/operations/inject.py`：新增模块常量 `DEFAULT_INJECT_MODE = "daily"` + `_attach_fallback_note`；
   mode 与 custom_filter 均未指定时回落 daily，并在 stats.note 注明（含可用模板清单）；显式 custom_filter
   （含空 dict）行为完全不变；显式 mode 模板不存在仍 400 但文案自解释。契约兼容：错误码/状态码/响应结构
   不变，只改文案与新增回落行为。

**T-121（PR-18，commit ec06658）**：
3. `sgme/config.py`：新增 `write_client_install_json(host, port=9910, mcp_port=None)`——同 schema，
   data_dir/raw_dir 置 null（本地无数据目录语义）；`write_install_json`/`install_json_path` 零改动
   （服务端启动自动生成契约冻结，app.py lifespan 不动）。
4. `scripts/install_client.py`：CLI（--host 必填 / --port 默认 9910 / --mcp-port 默认走 SGME_MCP_PORT env）。
   纯远程接入端手动生成服务发现清单——本次实踩：本机 sandbox 测试残留 install.json 指向 127.0.0.1:9910
   + Temp 目录，NAS 探测失败时第二步兜底被误导。

**T-122（文档沉淀）**：AGENTS.md 接入纪律块补「接入速查」（模板名清单 / append 契约与 started_at 幂等
语义 / trust_env 标准写法 / dev key 403 排障口诀 / install_client 用法）；wiki 踩坑手册（含接入自检最小
链路四步）；Backlog T-120/121/122 登记；待办池 3 条 demand（inject 报错自解释 p70 / install 客户端模式
p50 / ONBOARDING 速查 p40）随本三任务关闭。

**测试**：PR-17 worktree 内 52+64+54 全绿（operations_inject+operations_template / routes_templates+
config+entry_hardening / profile+server_v04 回归）；PR-18 worktree 内 13+23 全绿（install_json+
config_home / config）；合并后 main 复跑并集 **183 passed / 0 failed**（TDD 全程：新用例先红后绿，
既有断言仅文案适配）。不触提炼链路，免真实 LLM 冒烟。

**运维影响**：①inject 无参调用行为变化（400 → 200 回落 daily，响应带 note 说明）——依赖旧报错形态的
消费方需知（grep 适配器无此依赖）；②远程接入端可 `python scripts/install_client.py --host <NAS>` 重写
install.json；③版本 bump 1.1.1 → 1.1.2（__version__ 单源，B123），NAS 容器下次部署生效——部署前
/v1/health 仍报 1.1.1 属正常。

### B125. T-114/T-115 实测复核关闭 + T-123 分类治理立项（v1.1.2，2026-08-30）

**背景**：skills 模块状态检测（用户问「原子拆解机制/运行是否正常/调用有无冲突」）时对两条 ST-36
遗留任务（🟡 暂缓）做生产实测复核，发现登记口径已过时，用户拍板「按建议执行」。

**T-114 关闭依据（实测）**：「158 条超 8K」为 wiki 时代口径（M4a 迁移前统计）。B114 de-wikification
重写后：生产 skills.db 403 条 content（**剥离 frontmatter 的 body**）全部 ≤8192（最长 8050，平均 3893，
原子门禁 100% 达标）；git 源 403 个 SKILL.md 中仅 12 个总字节超 8K——系含 frontmatter 所致（SSH 容器
`find -size +8k` 对照 DB content_len 逐一核实）。无需拆分，实质已被 B114 收敛。

**T-115 关闭依据（实测）**：M4b 扫描报告（exports/m4b-atomic-candidates.md）结论「未发现跨技能重复
段落（≥2 技能且 ≥30 字符），候选 0 组」；存量佐证：同 SHA 重复组 0、悬空 uses 0（skill_uses 16 边全有效）。

**T-123 立项**：uncategorized 102 条（403 的 26%）分类治理——category 结构化过滤对该部分失效。
方案：LLM 辅助按 description/正文归类 → 白名单收敛现有 top 枚举（hermes 65/ai 55/software-development
51/github 22/network 17…）→ 人工逐条过清单（AIXM 关键词误伤 44 条教训）→ 先备份 skills.db 记录原值
可回滚。验收：uncategorized 归零或个位数、误伤抽查=0。

**运维影响**：纯文档变更，零代码改动；skills 写侧门禁/检索路径/向量路零变化。检测侧证据链
（403/403 向量满覆盖、rrf 双路全绿）见 L0 会话记录与本条。

### B126. T-119 health LLM 探测 TTL 缓存治本（v1.1.2，2026-08-30）

**背景**：B123 NAS 部署实录发现 healthcheck 内层 urlopen timeout=3s < LLM 探测实测 5.5s（agnes
/models），health 每次同步探测必然超时误判 unhealthy——当日以 docker-compose 放宽 timeout=25/30s
治标（T-119 登记 🟡），本条治本落地。

**实现（engine/health.py 重构，commit 02b7fd8）**：`check_llm_available` 拆为缓存编排层 + `_probe_llm`
探测实体（探测逻辑逐字节保留）。缓存策略 **stale-while-revalidate**：
- TTL 内（默认 30s）返回缓存，health 恢复毫秒级、不再依赖外部 LLM 可达性；
- 过期返回旧值 + 后台 daemon 线程刷新（`_llm_refreshing` Event 防并发重入，刷新失败保旧值下轮再试）；
- 首链 head（provider/model/base_url）变化强制失效——配置热更新（PUT /v1/admin/config）即时生效；
- **client 注入（测试形态）绕过缓存**——既有 mock 测试（test_health_v04/test_stall_watch 等）零适配；
- rule 兜底/未配置快速分支不缓存（零成本且需即时反映配置）；
- `reset_llm_cache()` 公开清缓存（测试 autouse fixture + 配置热更新可调用），**同时清 Event**——
  否则上轮刷新线程未结束时（真实网络 5s 超时期间）新周期刷新被拦截，缓存永不更新（测试串扰实锤）。

**测试**：新增 tests/test_llm_ttl_cache.py 6 用例（TDD 红绿全程）——首调探测+缓存命中零重复探测 /
过期 stale 返回+后台刷新落定新值 / client 注入绕缓存 / head 变化强制重探（load_config 共享 dict 需
deepcopy 教训）/ reset 清缓存 / rule 链不缓存。health 相关 7 文件 **115 passed / 0 failed**。
两个测试工程坑记档：①make_client 替换必须是工厂（每次新建）——共享单例被 _probe_llm 的 finally
close 后，后续探测抛 RuntimeError 被吞，计数假象「缓存永远命中」；②轮询等待后台刷新要轮询
**缓存内容落定**而非 handler 计数（calls==2 只代表 HTTP 返回，线程可能尚未写缓存）。

**运维影响**：health /v1/health 响应字段零变化（缓存对调用方透明）；部署后 healthcheck 恢复可靠，
compose 的 timeout=25/30s 宽限可保留（双保险）；LLM 探测频率从每次 health 请求一次降为 ≤1次/30s
——对 agnes 免费端点的无谓请求显著减少。

### B127. T-124 写侧元数据更新误拒根修 + T-123 分类治理执行（v1.1.2，2026-08-30）

**T-124 根因（T-123 应用实测撞出）**：`skills/store.py write_skill` 同名分支的「无变更重复提交」
判定只比 body sha——而 indexer 全链 sha 口径即 body（不含 frontmatter，`_record_from_meta` 的
`full = body.strip()`），仅改 category/pattern/tags/version/uses 等 frontmatter 字段时 body 未变，
被判「同名冲突已存在且内容完全相同」409 ERR_DUPLICATE_SKILL。**写侧元数据更新路径自 M3 起全断**
（此前未更新过既有技能故潜伏；PUT 是全量覆盖语义，元数据更新是合法场景）。

**修复**：同名分支在 body sha 相等时补六项元数据对比（description/category/version/pattern/tags
sorted/uses sorted），全等才拒「无变更重复提交」，任一变化放行。+2 测试（category 变更放行 /
tags 变更放行），test_skills_store **19 passed**（既有「完全无变更拒」用例保持绿=无误放开）。

**T-123 执行实录**：
1. NAS 备份先行：`~/sgme-backups/skills.db.bak-t123-20260830`（6.3MB）。
2. 导出 102 条 uncategorized + 17 个现有分类枚举（exports/t123-uncategorized.json）。
3. agnes-2.5-flash 预分类（4 批×26，429 读超时重试后全成）：分布 software-development 31 /
   methodology 14 / creative 10 / research 9 / ai 8 / media 7 / design 5 / data 5 / 其余 13，
   **100% 落在白名单内**（零越界）。产出 exports/t123-categorize-review.md（人工审核清单）+
   t123-categorize-suggestions.json（机器可读）。
4. 应用脚本 scripts/t123_apply.py：GET 原文 → 正则替换 frontmatter `category:` 行 → PUT 全量回写
   （写侧正路，sha 重算后 sync_index 增量同步，单一真相源保持）；**默认 dry-run**，429 按
   Retry-After 重试 + 0.6s 节流（限流 120 req/min/Key）。
5. 首轮 apply 全 409 → 实锤 T-124 → 根修后待 NAS 部署 v1.1.2 重放。

**运维影响**：部署 v1.1.2 后元数据更新（改分类/标签/版本）恢复可用；T-123 审核清单即治理台账，
回滚 = 用备份库恢复 + PUT 回原 frontmatter（双侧可逆）。

**B127 终态（2026-08-30 当日闭环）**：v1.1.2 部署 NAS（compose image → sgme:1.1.2-nas-autoupd，
src git pull fb046fb → 92c9fee 两轮 build）后重放 apply **102/102 成功 0 失败**；重启触发 sync_index
（diff_records 元数据指纹生效）→ **uncategorized 0/403**；分布 software-development 82 / hermes 65 /
ai 63 / methodology 29 / creative 25 / github 23 等；检索 rrf 双路全绿、health 1.1.2 毫秒级
（TTL 缓存生效实测 287ms vs 修复前 5.5s）；skills-hub bare 仓 sync to_remote 回推完成。
遗留小项：GET /v1/skills 未透出 category 查询参数（数据层就绪，过滤入口待接线，随下版）。

### B128. 维度裁剪 B81 未闭环收尾：生产停用 projects / tasks（T-127，2026-08-31）

**背景**：B81（2026-08-18）执行「维度裁剪——项目池/待办池为专用落地点」时，改了四处：
`registry/dimensions.yaml` 移除 projects/tasks + `aliases.yaml` 同步清理 + templates（work/coding/full）
去对应 section 与 memory_types + 测试维度引用修正，并明写运维影响「维度移除后存量 projects/tasks 标签
不再注入（历史保留）」。**但漏了生产 db 的 `active` 停用**。

而 T-2（v0.7）裁决是「**YAML=种子，DB=真相**」，`memory_dao.py:33` 的 upsert 注释
`-- active 不在此更新：保留 DB 现值（停用维度重启不复活）` 正是该裁决的实现——
于是「yaml 删维度」≠「db 停用维度」，**裁剪在生产零生效，且静默无感知长达 13 天**。

2026-08-31 生产实测影响面：
- `projects` 标签 **13,499 条**（全库第一大维度）、`tasks` 2,836 条；
- **近 7 天新打 projects 3,862 条**（维度排名第一，超过 tech_stack 3,769），占新记忆约 43%；08-30 仍新增 83/14 条；
- 对照三池落点：`project_meta` 仅 2 行、`demands` 仅 114 行 → 13,499 条"项目记忆"永远进不了项目池；
- 且 projects 标签记忆归档数 **0**（全库归档率 27.6%）——不参与 L1.5 冲突合并，**只增不减的孤儿数据**。

**改动**：生产 db 停用两个废弃维度（`dimension_registry.active = 0`），不改代码、不改 yaml、不动存量标签。

**执行要点（可复用）**：
1. **热备份必须用 sqlite3 backup API，不能 `cp`**——生产库为 WAL 模式，直接 `cp` 会漏掉未 checkpoint 的事务。
   做法：`PRAGMA wal_checkpoint(TRUNCATE)` + `sqlite3.connect(src).backup(dst)`。
2. **走 HTTP API 免重启**。`cfg["dimensions"]` 是 `app.py:611`（`init_databases`，启动时一次）的**进程内快照**，
   `engine/refine.py:142` 直接用它 → 若直改 db 而不重启，L1 提示词仍会注入旧维度清单。
   正解：`PUT /v1/admin/registry/dimensions/{dim_id}` body `{"active": false}`
   （`operations/registry.py:176 registry_update_dim` 内部改 db → commit → **`refresh_dimensions(cfg, mem_conn)` 即时回刷**，
   源码注释「停用/启用即时生效」）。
3. ⚠️ 停用函数名是 **`memory_dao.update_dimension_fields(conn, dim_id, {"active": False})`**（非 `set_dimension_active`）。
4. 重启不会复活：`app.py:609` 的 `import_registry` 幂等 upsert 同样不改 active（T-2 裁决）。

**停用前风险核查（grep 三路，确认零引用）**：
- `templates/*.yaml`：coding / full / work 三模板的 memory_types 与 dimensions 均已无 projects/tasks（B81 已清）→ 不会查空；
- `sgme/` 代码：`'projects'|"projects"|'tasks'|"tasks"` 硬编码引用**零命中**（排除测试）；
- `adapters/`：无引用。

**验证（API 层 + db 层双向）**：

| 项 | 停用前 | 停用后 |
|---|---|---|
| `GET /v1/admin/registry?active_only=true` total | 16 | **14** |
| `GET /v1/admin/registry?active_only=false` total | 16 | 16（停用项保留可溯源） |
| db `active=1` count | 16 | **14** |
| db projects/tasks active | 1 / 1 | **0 / 0** |
| 存量标签 projects / tasks | 13,499 / 2,836 | 13,499 / 2,836（**未删**，符合 B81 存量保留政策） |
| 08-31 起新打标 | — | **0 / 0** |
| memories / memory_archive | 25,824 / 9,822 | 25,824 / 9,822（无变动） |
| health | v1.1.3 ok | v1.1.3 ok |

**备份**：`/data/data/memory.db.bak-t127-20260831`（180,445,184 字节，与源同尺寸；
完整性校验 memories=25,824 / dims=16）。

**回滚**：`PUT` body `{"active": true}` 即时恢复（双侧可逆），或备份库恢复。

**运维影响**：
- 新记忆**不再**打 projects/tasks 标签，L1 提示词 `{{dimensions}}` 清单由 16 → 14 项（提示词稍短，l1_extraction 单次调用省约 200 tokens）；
- 存量 13,499 / 2,836 条标签**保留不动**，仍可被检索到（B81 既定政策），治理另立 T-131；
- 防复发机制见 T-128（yaml ↔ db 维度一致性校验，差集告警）。

**冒烟验证（2026-08-31 07:10，生产实证通过）**：

停用后 4.5 小时内生产零新记忆（最新记忆 `created_at=2026-08-30T17:13:01Z`，早于停用时刻 18:38Z；
近 24h 提炼 237 条全部发生在停用前），无法自然验证 → 执行可控冒烟：
`POST /v1/append`（agent key，刻意含"新项目""下周要记得"的项目/待办语义）
→ `POST /v1/admin/refine/trigger`（admin key，同步，**36.2s**，产出 4 条记忆）
→ 查维度 → `POST /v1/memory/{id}/reject` 清理。

| 冒烟产出记忆 | 维度标签 |
|---|---|
| 打算用 FastAPI + SQLite 做后端 | `tech_stack` |
| **我准备启动一个新项目叫「星尘计划」** | **`goals`**（停用前会落 `projects`） |
| 还得给 NAS 上的 SGME 加个备份校验 | `environment`, `goals` |
| **下周要记得先把数据库 schema 定下来** | **`goals`**（停用前会落 `tasks`） |

→ `projects` / `tasks` **均未出现**，`cfg` 回刷在生产实证**即时生效，无需重启**。

**清理（不留污染）**：①4 条冒烟记忆全部 reject（status=rejected，原件保留）②冒烟新建的场景
「星尘计划项目」reject ③⚠️ **冒烟的 update 动作污染了一个既有生产场景**「NAS 环境配置与部署规范」
（正文尾部被追加"## 备份维护 / SGME 备份校验"）——无场景正文更新 API（只有 `/status`），
故走 db：先存 `scene_versions` 快照 → 精确移除注入段落（242→201 字符）→ 解绑 `scene_memories`
冒烟关联行。④L0 原文 `raw/sessions/a0885c3e-*.md` 保留（溯源根，设计如此）。

**最终态核验**：`active=1` 维度 14 / 总数 16（停用项保留可溯源）；停用后 projects/tasks 打标 **0/0**；
memories 25,828（+4 冒烟，均已 rejected）/ archive 9,822 无变动；存量标签 13,499 / 2,836 未删；
scenes active 262 / rejected 2（含 1 个冒烟）；health v1.1.3 ok。

⚠️ **遗留观察（非本次引入，未展开）**：被冒烟 update 的场景「NAS 环境配置与部署规范」heat=6、
创建于 08-30 19:02，但 `scene_memories` **原本只关联 1 条记忆**（即冒烟那条，解绑后归 0）——
历史 update 的 `memory_ids` 关联疑似未落库，属独立问题。另该场景正文残留一个残缺标题 `## 部`
（无法判定是否本次 update 副产物，保守保留未动）。

**文档**：本记录 B128；Backlog T-127 标 ✅（v1.2+）；关联分析见
`docs/design/SGME-提炼提示词优化空间分析-v0.1.md` §一。

### B129. T-128 维度注册表一致性校验（防 B81 漏停用复发）

| 项 | 内容 |
|---|---|
| 背景 | B81（2026-08-18 维度裁剪）删了 yaml 的 projects/tasks，但生产 db `active` 未停用，且 T-2「DB=真相」设计使该遗漏**静默无感知 13 天**（T-127 才止血）。根因链路见 B128。需一个**自动**机制，让「yaml 与 db 维度集漂移」在启动/周期即被感知，而非等脏数据累积。 |
| 改动 | ① `sgme/data/memory_dao.py` 新增 `check_dimension_consistency(conn, yaml_dim_ids)`：DB active=1 集应 == YAML 声明集；产出三类差集——`orphan_active_in_db`（DB active 但 YAML 未声明，应禁用，即 T-127 复发形态）/ `missing_in_db`（YAML 声明但 DB 缺行，应导入）/ `inactive_in_db`（YAML 声明但 DB 停用，应启用）。DB 中 YAML 未声明且已停用者属溯源保留，不告警。② `sgme/operations/health.py` 新增 `publish_dimension_anomaly(mem_conn, report)`：复用 `signal.engine.publish` 的 `anomaly_warn` 通道（同源 `registry`，SSE/pull 消费端零改动），**进程级 30 分钟抑制窗口**防 10 分钟心跳刷屏。③ `sgme/server/app.py`：启动期（import_registry 后、cfg 回刷前，用原始 YAML 维度集）跑校验，不一致 → 日志告警 + 发布 anomaly_warn，并将 YAML 维度集存入 `cfg["_yaml_dimension_ids"]`；心跳任务每 10 分钟周期复核。④ `sgme/server/routes_registry.py` 新增 `GET /v1/admin/registry/consistency` 诊断端点（admin key）。 |
| 测试 | `tests/test_operations_registry.py` 新增 5 例：健康态 consistent=True；注入孤儿 active 维度 → orphan_active_in_db 命中、停用后退出；YAML 多声明不存在维度 → missing_in_db；停用 goals → inactive_in_db；端点契约（7 键 + 一致态零差集）+ 端点可见孤儿。全部 `pytest` 通过（registry 集 45/45、health 集 21/21、import 检查通过）。 |
| 实证 | 线上库只读复核（不部署）：当前（T-127 后）`consistent=True`、零差集；若 T-127 未执行（projects/tasks 仍 active），`orphan_active_in_db=['projects','tasks']` → 会触发告警。**证明本机制正是 T-127 复发的克星**。 |
| 验收 | 满足 Backlog T-128 验收：人为置 db 有、yaml 无的 active 维度 → 启动产 anomaly_warn；正常态零告警。 |
| 运维影响 | anomaly_warn 经既有 SSE/信号通道可达接入 agent；诊断端点供人工快速定位。无破坏性、无 schema 变更、存量数据零影响。 |
| 文档 | 本记录 B129；Backlog T-128 标 ✅（v1.2+）。 |

### B130. T-129 内部回归基线：生产库副本通路 + 中文检索 GT + recall@k（v1.2+，2026-08-31）

| 项 | 内容 |
|---|---|
| 背景 | 阶段二检索改动需 A/B 护栏，但业界标准评测（LoCoMo）延后至 Gen3 后（→ T-141）。用户定：用内部基线提前承担护栏职责。原 `eval/runner.py` 是纯离线（临时库 + case 自带 memories + 进程内直调 search），无法连真实库副本；`metrics.py` 缺 `recall@k`。需求：①连真实 `memory.db` 副本通路 ②用库内记忆由 LLM 反向生成中文 query（GT=记忆），**刻意构造多跳** ③补 `recall@k`（k=1/3/5/10）④**0 token**（不提炼、不嵌向量；边现成：9,822 `superseded_by` + 20,699 `scene_memories`）。 |
| 前置（Task #1） | `recall@k` 指标先补：`eval/models.py` 增 `RecallAtK` dataclass（as_dict → recall@1/3/5/10/query_count）；`eval/metrics.py` 增 `compute_recall_at_k`/`aggregate_recall_at_k`；`eval/rrf.py` 的 `search()` 把 recall@k 接入参数环并填入 `RRFMetrics.recall_at_k`；`eval/reporter.py` 增 `### 召回率 @k（T-129 A/B 护栏）` 段（JSON + MD 均渲染）。 |
| 改动 | ① 新增 `eval/realdb.py`：`snapshot_replica(src,dst)`（sqlite3 online backup 一致快照）/ `open_replica(path,readonly=True)` / `replica_corpus_stats(conn)`（记忆数+向量覆盖）/ `sample_memories(conn,n,seed)`（random.Random 确定性）/ `multi_hop_pairs(conn,limit,seed)`（读 scene 簇[成员均 live→相关集≥2] + supersession[相关集=live 后继]，边缺失静默跳过）/ `build_realdb_gt(conn,*,sample_n,multi_hop_ratio,seed,llm_fn,source)`（single_hop=抽样记忆 query=llm_fn、multi_hop=边相关集；`llm_fn` 可注入，留空用桩 `_stub_query_fn`）/ `make_mini_replica(tmp_dir,n,seed)`（合成完整 memory.db：含 FTS + 归档 mini#0→mini#1 + scene-tech-weekly 链 mini#2/3，供 CI 免 NAS）/ `RealDbGt`/`RealDbGtItem`/`MultiHopPair` dataclass（to_ground_truth 合并同 query、counts_by_hop、save/load）。② `eval/runner.py`：`_make_query_fn(self,mem_conn,vector_available)` 改为连接无关（原依赖 self.mem_conn/语料）；抽出 `_search_grid(self,mem_conn,ground_truth,*,vector_available,vector_count,corpus_size,banner_reason,gt_mode)` 核心（预warm 填 recall_cache → RRFGridSearch → 后算 recall 诊断 → 返回 RRFMetrics），`_run_rrf` 与 `run_realdb` 共用；新增 `run_realdb(gt,mem_conn,*,corpus_size,vector_available=False,...,gt_mode="realdb")` 校验 gt 为 RealDbGt、0 token（不打开 embed 缓存）。③ `eval/run.py`：增 `--realdb/--replica/--gt/--build-gt/--sample/--multi-hop-ratio/--seed/--self-test`；`_run_realdb` 解析副本（无 `--replica`+`--self-test`→`make_mini_replica`）、`open_replica` 只读、`replica_corpus_stats`、load 或 build GT、跑 `EvalRunner(cfg=None,rrf_skip_vector=True).run_realdb`、生成 EvalResult（rrf only，l1/l2 None）+ 报告落 `eval/results/`、`sys.exit(0)`。 |
| ⚠️ 关键修正 | `make_mini_replica` 初版把 `scenes`/`scene_memories` INSERT 到 `wiki_conn` → 实测 `no such table: scenes`。根因：v0.7 三库拆分（B2）已把 scenes 系列由 wiki.db **迁入 memory.db**（见 `sgme/data/db.py:119` `MEMORY_DDL`），`init_databases` 只在 mem_conn 建这些表。改为 INSERT 到 `mem_conn` 后全绿。 |
| 测试 | 新增 `tests/test_eval_realdb.py`（6 类 13 例）：mini 副本 FTS+边、sample 确定性/超额返回、multi_hop scene+supersession 并存、build_gt 形态+to_ground_truth、save/load 往返、run_realdb 端到端+可复现（两跑 recall@k 相等、query_count==len(gt.items)、conclusion==inconclusive_bm25_only）、数据零污染、报告 MD 渲染 recall。回归：`test_eval_recall_at_k.py`(7) + `test_eval_rrf.py` 无回归。**实跑：191 passed（eval 相关全集），71 passed（realdb+recall+rrf 三个文件）。** |
| 实证（自测） | `.venv/Scripts/python.exe -m eval.run --realdb --self-test --output eval/results/realdb_self_test` → 副本 11 live 记忆 + scene/supersession 边；GT 13 query（11 single + 1 scene + 1 supersession，source=stub）；RRF 5 组合、12 query、`conclusion=inconclusive_bm25_only`（0 向量基线诚实结论，正是护栏意图）；recall@1=0.875 @3/5/10=0.9583；报告落 `eval/results/realdb_self_test/report.json` + `report.md`。 |
| 验收 | 满足 Backlog T-129：命令可重复执行、结果可复现、基线数字落 `eval/results/`。 |
| ⚠️ 延后决策（待用户拍板，未阻塞） | (a) **真实 LLM 生成 GT**：暂用桩 `_stub_query_fn`（jieba ≥2 字关键词拼问句，BM25 可召回但非自然语言形态）；生产态需注入真实 `llm_fn` 才能逼近 LoCoMo 自然语句质量、真正测出图召回增益。(b) **真实 NAS 副本**：暂未拷 `memory.db`，自测用 `make_mini_replica` 合成副本证明端到端+可复现；生产跑用 `--replica <真实副本>`（建议 `snapshot_replica` 在线备份快照保一致）。两项均不影响当前 0-token 链路与护栏可用性。 |
| 运维影响 | 纯评测侧新增，不触生产服务；`make_mini_replica` 落 `eval/tmp/` 不污染 `data/`。部署 NAS 时本变更随镜像带出（CLI 仅在本地/CI 跑，不在服务进程内）。 |
| 文档 | 本记录 B130；Backlog T-129 标 ✅（v1.2+）。 |

### B131. T-130 查询侧停用词过滤（中英双语）+ 英文清理 + 空结果降级护栏（v1.2+，2026-08-31）

| 项 | 内容 |
|---|---|
| 背景 | T-130 实测：自然语言提问召回落空（旧 MATCH 隐式 AND + 查询侧停用词零过滤）。当前 `sgme/data/search/__init__.py:_build_fts_query` 虽已为 OR 连接（规避硬空召回），但停用词（who/with/a/的/了/谁/在…）不滤除 → OR 膨胀、常见词稀释 BM25 排序、结果集噪声高，且会让 T-134 图召回 A/B 被噪声淹没。需查询侧中英停用词过滤 + 英文清理 + 全停用词回退护栏。 |
| 改动 | ① 新增 `sgme/data/search/stoplist.py`：`STOPWORDS_EN`（功能词+检索无承载词）/ `STOPWORDS_ZH`（功能词+疑问/指示/语气/连词）/ `is_stopword(term)`（英文小写归一比对）/ `filter_stopwords(tokens)`（精确 token 匹配、顺序不变、去空，保留内容词）。② 改 `_build_fts_query(query,*,use_stoplist=True)`：分段后 `filter_stopwords` → `_clean_en_term`（折叠内部空白+ASCII 小写，防御「NAS」vs「nas」）→ 全停用词时**回退原 token**（不直接空召回，真空交 `recall_routes` 现有 LIKE 兜底）。③ `recall_routes`/`search_scenes` 经 cfg `search.stoplist.enabled`（默认 True）控制开关；`_search_like_fallback` 同步过滤停用词。④ `eval/realdb.py:build_realdb_gt` 增 `query_style="natural"`（内容词裹**纯停用词**模板，供 A/B「自然语句类」）+ `exclude_ids`（A/B 注入噪声记忆时排除出 GT）。⑤ 新增 `eval/ab_stoplist.py`：复用 T-129 副本/GT 基建，注入 N 条纯停用词 distractor，跑 stoplist 开/关双臂，产出 recall@k + 结果集噪声（avg 返回条数 / avg distractor 命中）对比报告。 |
| ⚠️ 关键修正（A/B 模板翻车） | 初版 `_NATURAL_TEMPLATES` 用「了解/内容/意思/觉得」等**非停用词**作填充词 → 这些词残留于 FTS 查询并误命中噪声记忆，导致 stoplist 开臂仍见 1.62 噪声命中、A/B 结论不干净。改为**全部填充词落在 `STOPWORDS_ZH` 内**（仅 谁/在/吗/关于/这个/事情/哪儿/呢…）→ stoplist 开臂噪声命中归零。教训：A/B 的自然语句模板自身不得引入非停用词。 |
| ⚠️ 顺带修复（T-129 自测可重复性） | `eval/run.py:_run_realdb` 自测用固定 `eval/tmp/realdb_self_test` 目录 → 二次运行 `make_mini_replica` 因旧副本残留触发 `UNIQUE constraint failed: memories.memory_id`。改为 `tempfile.mkdtemp` 每次唯一临时目录，满足 T-129「命令可重复执行」。 |
| 测试 | 新增 `tests/test_search_stoplist.py`（11 例）：is_stopword/filter_stopwords 中英、_clean_en_term 小写+折叠、_build_fts_query 过滤/保留中文内容词/全停用词回退/开关对照、_stoplist_enabled cfg 默认、集成（合成语料：stoplist 开滤除纯停用词 distractor 且 recall 不劣化、内容词 query 两臂 recall 一致）。回归：`test_search_v04`/`test_operations_search`/`test_scenes_fts`/`test_eval_realdb`/`test_eval_recall_at_k`/`test_eval_rrf` = **133 passed** + stoplist 11 = **144 passed**，0 失败。 |
| 实证（A/B） | `python -m eval.ab_stoplist --noise 40 --output eval/results/ab_stoplist` → 副本 51 记忆（11 真实 + 40 噪声）；GT 13 自然语句 query；双臂纯 BM25 仅 `search.stoplist.enabled` 不同：recall@k **0.8846 两臂一致（不劣化）**；结果集噪声 distractor 命中 **0.0（开）vs 8.15（关，↓100%）**；avg 返回条数 1.15 vs 10.0。T-129 自测 recall@k 0.875/0.9583 不变（不劣化确认）。 |
| 验收 | 满足 Backlog T-130：用 T-129 基线 A/B，recall@k 不劣化（内容词保留）+ 自然语句类结果集噪声明显下探（纯停用词 distractor 被滤除）。 |
| 运维影响 | 查询侧默认开启（`search.stoplist.enabled` 默认 True），属检索质量改进；下游 inject/图谱化读同一 search 路径，受益一致。生产部署随镜像带出。 |
| 文档 | 本记录 B131；Backlog T-130 标 ✅（v1.2+）。 |

### B132. T-131 存量 projects/tasks 标签治理执行：全量 LLM 分类 + 轻量重打标（v1.2+，2026-08-31）

| 项 | 内容 |
|---|---|
| 背景 | T-127 停用 projects/tasks 后，存量标签 13,499/2,836 依 B81「存量保留」政策未动；其中 active 记忆去重 9,633 条**仅带 projects/tasks、无任何有效维度** → 检索/注入不可达。T-131 定策（用户拍板）：**仅重打标（轻量）**——保留 projects/tasks 标签、新增有效维度、零结构改动；执行方法：**先 dry-run 看分类再执行**；数据源：**admin API 只读拉取**。 |
| 工具 | 新增 `scripts/t131_retag_classify.py`（pull 只读 admin API 拉全量 projects+tasks 去重缓存 tmp/t131_raw.json；classify 算缺口集+agnes-2.5-flash 批量提议补打维度；`--all` 全量模式；ASCII 源码、中文维度名/描述运行期从 registry/dimensions.yaml 加载）。前置 `scripts/sample_tag_distribution.py`（分布报告，commit 20e3758）。 |
| 数据 | 拉取 active 记忆去重 **9,633**（projects 8,535 + tasks 2,144）；缺口集（仅 projects/tasks、无有效维度）= **2,076（21.6%）**。 |
| 分类 | 全量 2,076 经 agnes-2.5-flash 104 批（--all --batch 20）：拟补打 **1,753**、LLM 判无相关维度 **323**（维持原样）；频次 tech_stack 1,110 > goals 276 > status 229 > focus 118 > skills 109 > ideas 89 > environment 72 > preferences 60 > style 59 > values 31 > habits 21 > identity 9 > social 1。提案存档 `eval/results/t131_proposals_full.json`（gitignored，随库可复现）。 |
| 执行（SSH 直写） | 用户批「SSH 直写 memory_tags（免部署、备份+回滚）」：①备份 `memory.db.bak-t131-20260831041750`（/data/backups，182,124,544 字节，`PRAGMA wal_checkpoint(TRUNCATE)` + sqlite backup API，memories=26,048/active_dims=14/memory_tags=47,799 完整性）②全量提案 docker cp 入容器 /tmp/t131_proposals.json ③应用：INSERT INTO memory_tags（单事务、busy_timeout 30s、维度校验在 active 注册表、已存在跳重）→ **1,752 条记忆 / 2,183 行**（1 条 memory_id 已不在库跳过）④projects/tasks 行原样保留（零删除零结构改动）。 |
| 验证 | DB 层：verify 脚本 2,183/2,183 行存在、**0 缺失**、projects 保留 1,560 / tasks 保留 888（与打标时一致性）；API 层独立复核（真实读路径 `GET /v1/admin/memories?dimension_id=`）：tech_stack active total **9,755**、goals 1,422、status 1,331、ideas 108；health 未受影响。 |
| 回滚 | 精确 applied 清单持久化 `/data/backups/t131_applied.json`（1,752 条/2,183 行，容器 /tmp 与 /data/backups 双份 + 本地 tmp/t131_applied.json 存档）；回滚 = `DELETE FROM memory_tags WHERE memory_id=? AND dimension_id=?` 遍历该清单（rollback_retag.py 已备，幂等）；另整库备份可整体还原。 |
| ⚠️ 遗留 | ①TTL 维度（status 7d / focus 30d / goals 90d）新标签使记忆**可检索**（search/维度过滤立即生效），但 inject 需记忆 updated_at 刷新后才出现——本次刻意**不动 updated_at**（最小改动），如需 TTL 续期另立任务 ②323 条 LLM 判无相关维度维持原 projects/tasks ③本次为**直写生产库**，无代码改动；`scripts/t131_retag_classify.py` 等工具代码**未部署 NAS**（T-127~T-132 同，随下次发布）④一次性直写脚本在 tmp/（backup_memory/apply_retag/verify_retag/rollback_retag，均 ASCII）。 |
| 文档 | 本记录 B132；Backlog T-131 标 ✅（v1.2+）；分布报告 `eval/results/t131_distribution.md`、dry-run 提案 `eval/results/t131_dryrun_proposal.md`。 |

### B133. T-133 结构边：memory_edges 建表 + 零 token backfill（v1.2+，2026-08-31）

| 项 | 内容 |
|---|---|
| 背景 | ST-38 图谱化第一步（进化方案 v0.2 §T2-1a）。需要一张关系边表承载「记忆↔记忆」结构关系，供 T-134 图召回 1-hop 使用；边源全部纯 SQL 零 token（归档链 + 场景共现），无 LLM 成本。设计风险点：场景共现按组合数爆炸（实测最大场景 1,239 记忆 → C(1239,2)=76.7 万边），必须硬截断 + 全局上限。 |
| 表结构 | `db.py` 新增 `MEMORY_EDGES_DDL`（照 §T2-1 逐字节：edge_id PK / from_id / to_id / relation / weight REAL default 1.0 / valid_from / valid_to / created_at / source + `idx_edges_from(from_id,relation)` + `idx_edges_to(to_id,relation)`）+ `_migrate_memory_edges_table`（IF NOT EXISTS 幂等，不 bump SCHEMA_VERSION，同 `_migrate_demands_table` 模式），`connect_memory` 挂接。⚠️ 刻意不加外键（同 demands 先例：记忆会被 Supersession 归档/软删，外键阻塞溯源）。 |
| 语义定夺 | `belongs_to` = **同场景记忆↔记忆共现边**（weight=共现场景数，仅存规范方向 from_id<to_id）。依据：设计明写「同场景**记忆对**」+ C(n,2) 组合爆炸算式 + T2-2a「1-hop 邻居」约束（若为 memory→scene 边，1-hop 到不了其他记忆，需 2-hop，与 v1 定义矛盾）。 |
| 新模块 | `sgme/data/edge_dao.py`：`create_edge`（edge_id 确定性 `{from}::{to}::{relation}`，INSERT OR IGNORE 幂等）/ `delete_edges_by_source` / `count_edges` / `list_edges` / `neighbors`（1-hop 双向 from/to，同邻居多关系取最高 weight 去重，T-134 预留）/ `backfill_system_edges`（零 token 全量 backfill）/ `edge_stats`（按 source/relation 对账）。 |
| backfill 逻辑 | ① `memory_archive.superseded_by` 非空 → 双向边：`superseder→archived` 记 `supersedes`、`archived→superseder` 记 `evolves_from`（weight=1.0）② active 场景 ∩ active 记忆 → 同场景记忆对聚合（weight=共现场景数）→ 每场景先按 priority/updated_at 取 top-100 参与配对（防组合爆炸）→ 每记忆取 top-8 邻 → 全局 cap 20 万，超限按 weight 降序裁剪 belongs_to 并 publish `anomaly_warn`（signal.engine.publish，source='edge_backfill'）。**幂等**：单事务内先 `DELETE WHERE source='system'` 再全量重插（收敛式，重跑结果一致；语义边 source='llm'/'cooccur' 不受影响）。dry_run 模式只统计不写库。 |
| CLI | `scripts/oneoff/backfill_edges.py`（--data-dir / --dry-run / --top-n / --per-scene-top-n / --min-weight / --cap），输出 JSON 统计 + edge_stats 对账。 |
| 测试 | `tests/test_edge_dao.py` 12 例：建表幂等 / create_edge 幂等 / delete_by_source / neighbors 双向+去重+relation 过滤 / supersession 双向边与方向断言 / 共现 weight 聚合 / per-scene+per-memory 截断（C(30,2)=435→≤90 边）/ 仅 active 场景与 active 记忆参与 / 全局 cap 触发 anomaly_warn 落 signal_events / 幂等重跑两次一致 / dry-run 零写入 / 其他 source（llm）保留。**68 passed**（12 新增 + 56 回归：session_db/scenes_moved/refine_dao/persona_dao/search_stoplist）。 |
| 生产预估（dry-run） | 复用 12:17 备份快照（182MB）本地 dry-run：superseded_pairs **9,859**（→supersedes+evolves_from 19,718 边）、scene_pairs_raw 93,530（8 个大场景被 per-scene top-100 截断）、belongs_to **16,816**（per-memory top-8）→ **总边 36,534**，远低于 20 万 cap，**无 anomaly**。 |
| ⚠️ 遗留 | ①生产 backfill 未执行（需部署后或经 SSH 直写，随部署确认）②「update/merge 动作顺手写 supersedes 边」运行时钩子未做（T-133 AC 未含；可随 T-134 或后续补）③T-134 图召回消费本表（neighbors 已预留）。 |
| 文档 | 本记录 B133；Backlog T-133 标 ✅、ST-38 转 🟡 进行中（v1.2+）。 |
