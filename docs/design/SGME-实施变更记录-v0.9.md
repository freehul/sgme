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
