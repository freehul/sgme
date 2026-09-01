# SGME 记忆系统进化方案 v0.2

> 状态：**待审查**｜日期：2026-08-31｜基于 v0.1 + 源码/NAS 生产实证审查修订
> 目标：把 SGME 从 **Gen2.8** 推进到 **Gen3（记忆图谱 + 图检索）**，先补上「评测安全网」
> 现状基线（2026-08-31 生产实测）：**v1.1.3**｜23,043 条记忆向量 / 587 条场景向量（active 269）｜单次 `l1_conflict` **1.4万–12.8万 tokens（中位 ~5万）**｜提炼链 `tier0_summary → l1_extraction → l1_conflict → l2_scene`

---

## 修订记录（v0.1 → v0.2）

| # | 修订点 | 依据 |
|---|---|---|
| 1 | **T1-2 从「阶段一主体任务」降级为「T1-1 内顺手检查两项」** | 预筛已由 T-25（2026-08-12）/T-68（2026-08-16）落地；08-30 生产实测单次 `l1_conflict` 1.4万–12.8万 tokens（中位 ~5万）。v0.1 所述「80 万+」为预筛上线前（08-11/12）旧数据 |
| 2 | **D6 LoCoMo 档位改为三档**（1–2 冒烟 → 10 基线 → 50 可选） | 全套 LoCoMo = 50 会话 / 7,512 QA；Mem0 等论文惯用口径 = **10 会话 / ~1,540 QA**，10 会话档才与 SOTA 数字直接可比 |
| 3 | **T2-1 新增 Phase 0 结构边（纯 SQL，零 token）** | `superseded_by` 归档链 + `scene_memories` 共现可直接 backfill；图召回价值验证可提前到语义边之前 |
| 4 | **T2-3「与 T2-1 共用同一次 LLM 调用」约束修正** | 二者分属 `l1_extraction` / `l1_conflict` 不同 stage，不是同一次调用；正解 = 各自搭现有 stage 调用顺风车 |
| 5 | **T2-2 拆 v1（结构边）/ v2（语义边）两步上线，补种子去重设计** | 止损点前移；RRF 自耦合（种子重复进融合）风险 |
| 6 | **新增英文语料适配检查 + J-score 双口径要求** | LoCoMo 英文对话 vs SGME 全中文提炼提示词；Mem0 66.9% 为端到端 LLM-judge 口径，非纯检索 recall |

---

## 一、方案总纲

| 阶段 | 主题 | 内容 | 前置 | 预期收益 | 风险 |
|---|---|---|---|---|---|
| **阶段一** | **基座：评测安全网** | T1-1 LoCoMo 评测基线（含 T1-2 残留两项检查） | 无 | 回归护栏 + 与 SOTA 可比数字 | 低 |
| **阶段二** | **攻坚：图谱化** | T2-1a 结构边（零 token）→ T2-2a 图召回 v1 → T2-1b 语义边 + T2-3 三元组 → T2-2b 图召回 v2 | 阶段一完成 | 多跳/联想检索；**价值验证前移、可止损** | 中（拆步后由中高降为中） |
| **阶段三** | **治理补齐** | T3-1 有效期间 / T3-2 Guardrail / T3-3 多 Agent scope | 阶段二稳定 | 事实过期语义、合规、隔离 | 低-中 |

**铁律（不变）**：阶段二任何检索改动，**必须**在阶段一评测基线上跑 A/B 回归对比，劣化即回滚。

---

## 二、阶段一：基座（评测安全网）

### T1-1 评测基线接入 LoCoMo（阶段一唯一主体任务）

**为什么第一做**：阶段二要动检索，没有基线等于盲改。`eval/` 框架已齐（`runner` / `metrics` / `retrieval_gt` / `ab` / `loader` / `reporter` 等 12 模块），缺的只是公开基准 case 与指标对齐（现有 cases 仅 `v001_baseline` + `v001_sample` 两组自建 case；`metrics.py` 现有 L1 F1 / inject / l2 hitrate / strict match，**无 recall@k 与 J-score**）。

| 项 | 内容 |
|---|---|
| 涉及文件 | `eval/cases/`（新增 `locomo_*.yaml`）、`eval/loader.py`、`eval/retrieval_gt.py`、`eval/metrics.py`、`eval/runner.py` |
| 数据迁移 | 无（评测旁路，不碰 memory.db；灌入走独立评测实例或备份恢复，**不动生产库**） |
| 主要工作 | ① **数据档位三档**：冒烟 1–2 会话 → 正式基线 **10 会话（对齐 Mem0 口径，数字直接可比）** → 50 会话全量可选<br>② 构造注入器：LoCoMo 会话 → `append` + `refine_trigger` 灌入（**走真实提炼链路，不走 mock**）；**遵守批量提炼纪律**——≥20 文件分批（≤20/批）+ 批间 30–60 秒、429 不立即重试（交 batch_scan 兜底）、永远 async<br>③ GT 标注：LoCoMo QA 对（single-hop 841 / multi-hop 282 / temporal 321 / open-domain 96，共 1,540 有效对）转 `retrieval_gt` 格式，重点保 multi-hop 类（图召回的靶子）<br>④ `metrics.py` 补 recall@k（k=1/3/5/10）+ **双口径**：检索 recall@k（纯检索，无生成成本，主指标）+ 端到端 J-score（含答案生成 + LLM-judge，走免费降级链，费用门禁，对齐 Mem0 66.9% 口径用）<br>⑤ **英文语料适配检查（新增）**：LoCoMo 英文对话 vs SGME 全中文提炼提示词（`prompts/*.txt`）+ 中文别名表——冒烟阶段人工抽检 ≥30 条提炼产物（维度打标 / 别名召回是否劣化）；必要时提示词双语化，走架构 §27 提示词版本递增 |
| 成本估算 | 10 会话 ≈ 200 个 L0 文件 × 4 stage ≈ 800 次 LLM 调用，agnes RPM 20-30 + 批间间隔 ≈ 1 小时量级；50 会话 ≈ 5 小时 |
| 验收标准 | ① 10 会话档基线数字（recall@1/3/5/10 + J-score）落 `eval/results/`<br>② 真实 LLM 冒烟通过 + 查日志无降级（mock 全绿不算）<br>③ 英文语料抽检结论落盘（允许劣化，但要有数字）<br>④ 命令可重复执行、结果可复现 |
| 风险 | 低。口径问题在文档写明：recall@k 与 J-score 是两种口径，勿混比 |
| 工作量 | 中 |

### T1-2 残留检查（并入 T1-1 顺手完成，非独立任务）

**背景修订**：v0.1 所述「prescreen 默认关闭、单次 80 万+ tokens、当天可闭环止血」已过时——T-25（2026-08-12 PR#4）落地向量预筛（候选 = 向量 Top-K ∪ 维度 Top-N）、T-68（2026-08-16）落地 `fallback=skip_conflict` 熔断，生产已生效。**08-30 生产实测 15 次运行：单次 1.4万–12.8万 tokens，中位 ~5万**——v0.1 验收目标「<10万」已基本达成。

剩余两项小检查：
1. **尖峰归因**：08-30 有 3 次 10.9万–12.8万 的批次，大概率是多记忆大批次的正常装箱行为，确认并落盘结论即可；
2. **`fallback=skip_conflict` 单独验证**：embed 不可达时跳过冲突检测直接 store，确认不丢数据（T-68 已有测试，生产侧复验一次）。

> ⚠️ **验证运维坑（记录备用）**：`GET /v1/admin/config` 按 `writable_sections` 白名单过滤返回段（top keys 仅 backup/dream/l1/l2/logging/refine/scene_gc/search/skills_hub/wiki，**不含 l15/care/skills/update_check 段**）——不能据此判断生产配置缺失；判断 l15 预筛是否生效，要看 `/v1/admin/refine_runs?stage=l1_conflict` 的真实 token 用量。

---

## 三、阶段二：攻坚（图谱化）

### T2-1 记忆关系边 `memory_edges`

**表结构（沿用 v0.1，source 枚举扩展）**

```sql
CREATE TABLE IF NOT EXISTS memory_edges (
  edge_id    TEXT PRIMARY KEY,
  from_id    TEXT NOT NULL,
  to_id    TEXT NOT NULL,
  relation   TEXT NOT NULL,        -- similar / causes / supersedes / belongs_to / contradicts / evolves_from
  weight     REAL NOT NULL DEFAULT 1.0,
  valid_from TEXT, valid_to TEXT,  -- 预留，阶段三启用
  created_at TEXT NOT NULL,
  source     TEXT NOT NULL         -- 'llm' | 'cooccur' | 'scene' | 'system'（v0.2 新增 system=结构性边）
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON memory_edges(from_id, relation);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON memory_edges(to_id, relation);
```

关系类型初始集 ≤6 种，沿用 v0.1：`similar` / `causes` / `supersedes` / `belongs_to` / `contradicts` / `evolves_from`。

**T2-1a Phase 0：结构边（纯 SQL，零 token，零 LLM）** ← v0.2 新增

| 来源 | 做法 | 成本 |
|---|---|---|
| ① `memory_archive.superseded_by` 归档链 | 存量数据一次性 backfill → `evolves_from`/`supersedes` 边；此后 update/merge 动作顺手写边 | 零 token（纯 SQL） |
| ② `scene_memories` 场景共现 | 同场景记忆对 → `belongs_to` 边，weight=共现场景数 | 零 token（纯 SQL） |

- **边量控制（必做）**：scene 共现按组合数膨胀（某场景 100 条记忆 = C(100,2)=4,950 边）——每场景按 weight 取 top-N + 总边量上限（建议 ≤20 万），超限记 anomaly_warn；
- **附带收益**：`fallback=skip_conflict` 生效期 L1.5 整体跳过、语义边不产出时，结构边兜底；
- 涉及文件：`sgme/data/db.py`（幂等迁移，`CREATE TABLE IF NOT EXISTS` 先例）、`sgme/data/memory_dao.py`、`scripts/oneoff/backfill_edges.py`（一次性回填脚本，幂等可重跑）。

**T2-1b 语义边（LLM，搭 `l1_conflict` 顺风车）**

- v0.1 方案 (c)（复用候选池）保留，但**落地方式修订**：不新增独立判定调用，而是**在 `l1_conflict` 现有 prompt 里加一列关系判定输出**——该 stage 已经看到新记忆 + 候选池全文，增量 token 小；产出候选对的 `similar`/`causes`/`contradicts` 边（weight = LLM 置信 × 相似度合成）；
- 脏边控制沿用 v0.1：weight 阈值 + 采样人工抽检 + `source` 字段可溯源关闭某一路；
- **D5 增量优先**不变；补充：可借 Dream 低峰对「高频被检索命中的记忆」定向补语义边（复用 batch_scan 模式）；
- 涉及文件：`sgme/engine/l15.py`、`sgme/prompts/manager.py`、`prompts/l1_conflict.txt`（版本递增）。

### T2-2 检索侧图召回（拆两步上线）

在 `operations/search.py` 现有三层之上增加 graph 候选路，与原候选一起进 RRF 融合，`routes` 增加 `"graph"` 标记（routes 数组新增值，客户端按数组处理不受影响）——兼容性结论沿用 v0.1。

**T2-2a v1（只用 T2-1a 结构边）**：结构边上线即跑 A/B，先验证 1-hop 图召回有无增益——**若 A/B 显示无增益，T2-1b/T2-3 的投入可及时止损**（v0.2 拆步的核心目的）。

**T2-2b v2（纳入语义边）**：v1 稳定后接入；2-hop 仍在 v2 之后评估（D2 不变，先 1-hop）。

**种子设计（v0.2 新增必答项）**：graph 路 = BM25/向量命中的 top-N 记忆做种子 → 扩展 1-hop 邻居进 RRF。**必须按 memory_id 去重**——种子已在原候选里，重复进 RRF 会自耦合推高排序；graph 路只贡献「邻居中不在原候选里的」增量记忆。

| 项 | 内容 |
|---|---|
| 涉及文件 | `sgme/operations/search.py`、`sgme/data/search/rrf.py`、`sgme/data/memory_dao.py`、`sgme/operations/graph.py`（复用取边逻辑） |
| D3 | 沿用 `META_RRF_K=60`（`search.py:79`，逐字节等价保留，勿动）；graph 权重独立新配置键（如 `search.graph.weight`，默认与既有路等权起步） |
| 验收标准 | ① LoCoMo 10 会话基线上 recall@5 不劣化且 multi-hop 类提升；② P95 检索延迟增幅 <100ms（1-hop 两条索引 SQL 可达成）；③ `eval/ab.py` A/B 报告落盘 |
| 风险 | 中（拆步 + 止损点后由 v0.1 的中高降级） |

### T2-3 原子事实三元组

**关键约束修订（v0.2）**：v0.1「必须与 T2-1 共用同一次 LLM 抽取调用，否则 token 成本翻倍」**不成立**——T2-1 语义边在 `l1_conflict` stage（输入 = 新记忆 + 候选池），T2-3 在 `l1_extraction` stage（输入 = 原始会话），二者不是同一次调用。正解 = **各自搭现有 stage 调用顺风车**：

- T2-3 = 扩展 `l1_extraction` 输出 schema（加 `facts` 三元组字段；`sgme/refinery/extract.py` 的 `output_schema` 已支持字段类型校验，可直接扩展），**不新增调用次数**；
- T2-1b = 扩展 `l1_conflict` prompt 输出（见 T2-1b）；
- 提示词改动均走架构 §27 版本递增。

| 项 | 内容 |
|---|---|
| D4 | 先 JSON 列 MVP（`memories` 加 JSON 列，免迁移）→ 验证价值后再迁 `memory_facts` 表（可索引、可 SQL 精确过滤，需 backfill）。不变 |
| 验收标准 | 三元组抽取成功率 ≥ 阈值（实施期定，建议 ≥85%，免费模型 agnes-2.5-flash 输出结构稳定性需冒烟确认）；符号层精确查询（如「XX 在哪家公司」）命中率对比基线提升 |
| 风险 | 提示词膨胀 + 免费模型输出结构稳定性——冒烟阶段人工抽检 JSON 合法率 |

---

## 四、阶段三：治理补齐（沿用 v0.1，无修订）

| 任务 | 涉及文件 | 要点 | 风险 |
|---|---|---|---|
| **T3-1** 有效期间 | `sgme/data/db.py`（`memories` ADD COLUMN `valid_from`/`valid_to`）、`memory_dao.py`、`search.py` | `occurred_at` 已覆盖事件时刻，本次只补有效期语义（事实何时失效）；NULL = 永久有效，天然向后兼容 | 低-中（检索过滤改变召回，需基线回归） |
| **T3-2** Guardrail | append / refine 写前 + search 召回后加过滤层 | 规则匹配优先（快），LLM 方案兜底（慢）；注意误脱敏 | 低-中 |
| **T3-3** 多 Agent scope | `sgme/server/routes_admin.py` 鉴权、`memories.agent_tag`（已有基础） | 影响 Hermes/DSH/Trae/WorkBuddy 共存，**必须灰度 + 保留当前全通行为为默认** | 中 |

---

## 五、待决事项决策清单（v0.2 修订版）

| 编号 | 决策点 | v0.1 建议 | v0.2 修订 |
|---|---|---|---|
| **D1** | 记忆边来源 | (c) 复用候选池 + 轻量判定 | (c) 保留，但**前置 Phase 0 结构边**（零 token 先行）；语义边搭 `l1_conflict` 车，不新增独立调用 |
| **D2** | 图召回跳数 | 先 1-hop | ✅ 不变；2-hop 在 T2-2b 稳定后评估 |
| **D3** | RRF 权重 / `META_RRF_K=60` | 沿用 60 + 新增可调 | ✅ 不变；graph 权重独立新配置键 |
| **D4** | 三元组存储 | 先 JSON 列 | ✅ 不变 |
| **D5** | 历史记忆 backfill | 增量优先 | ✅ 补充：Phase 0 结构边**全量 backfill**（零 token）；语义边增量 + Dream 低峰定向补高频记忆 |
| **D6** | LoCoMo 数据规模 | 先 50 会话冒烟 | ❌ **改三档：1–2 会话冒烟 → 10 会话基线（Mem0 可比口径）→ 50 全量可选** |
| **D7** | 登记 Backlog | 拆 Epic，v1.2+ | ✅ 必须；⚠️ **登记前重读最新版 Backlog**（生产已 v1.1.3、T 编号已过 T-126），确认可用 ST/T 编号 |

---

## 六、风险与缓解总表（v0.2）

| 风险 | 影响 | 缓解 |
|---|---|---|
| 检索改动劣化现有召回 | 高 | T1-1 基线 + `eval/ab.py` A/B，劣化即回滚（不变） |
| LLM 抽取 token 成本失控 | 中 → **低** | 语义边/三元组均搭现有 stage 调用，**零新增调用次数**（v0.2 结构性改善） |
| 脏边污染检索 | 中 | weight 阈值 + 采样抽检 + `source` 溯源关闭某一路（不变）；结构边天然无 LLM 噪声 |
| 历史记忆无边（冷启动） | 中 → **低** | Phase 0 结构边全量 backfill 直接消除大半冷启动；语义边增量 + Dream 定向补边 |
| 边量爆炸（v0.2 新增） | 中 | scene 共现边每场景 top-N 截断 + 总边量上限 ≤20万 + anomaly_warn |
| RRF 自耦合：种子重复进融合（v0.2 新增） | 中 | graph 路按 memory_id 去重，只贡献增量邻居 |
| 英文语料提炼劣化（v0.2 新增） | 中 | 冒烟抽检 ≥30 条产物；必要时提示词双语化（§27 版本递增） |
| 多 Agent 权限改动波及共存 | 中 | 默认保持全通，灰度开启（不变） |
| 评测 mock 全绿 ≠ 真实可用 | 中 | 验收强制真实 LLM 冒烟 + 查日志无降级（不变） |

---

## 七、验收与度量（v0.2）

| 阶段 | 核心度量 | 目标 |
|---|---|---|
| 阶段一 | LoCoMo 10 会话基线 | recall@1/3/5/10 + 端到端 J-score 落盘；英文抽检结论落盘 |
| 阶段一 | `l1_conflict` 单次 tokens | 现状 1.4万–12.8万（中位 ~5万）已达标；尖峰批次归因结论落盘 |
| 阶段二a | 结构边规模 | backfill 幂等完成，总边量 ≤20万 |
| 阶段二a | recall@5（10 会话基线 A/B） | 不劣化且 multi-hop 类提升；P95 增幅 <100ms |
| 阶段二b | recall@5（multi-hop 类） | v2 相比 v1 再提升或持平 |
| 阶段三 | 事实过期准确率 | 过期事实不再被召回（不变） |

---

## 八、执行顺序（v0.2）

```
T1-1 评测基线（含 T1-2 残留两项检查顺带做）
 → T2-1a 结构边（纯 SQL，零 token）
 → T2-2a 图召回 v1 + A/B
 → 【止损点：A/B 无增益则重评估 T2-1b/T2-3 投入】
 → T2-1b 语义边 + T2-3 三元组（并行搭车改造）
 → T2-2b 图召回 v2 + A/B
 → T3-x 治理补齐
```

**登记**：审查通过后按项目规范登记 Backlog——先重读最新版 Backlog 确认可用 ST/T 编号（生产已 v1.1.3，T 编号已过 T-126），提交信息引用 `Closes ST-x / T-x`；本方案建议登记为 **v1.2+ Epic**。

---

## 附：审查实证数据（2026-08-31 01:10 核验）

- NAS 生产 health：v1.1.3，vector available（sqlite-vec，bge-m3，latency 261ms），memory_vectors=23,043，scene_vectors=587
- 生产 `/v1/admin/refine_runs?stage=l1_conflict`（08-30，15 次）：prompt 13,235–126,642 tokens，中位 ~47,000；含 1 次 `drop_batch`（全链降级失败，LLM 瞬时故障）
- repo `config/sgme.yaml`：`l15.prescreen = {enabled: true, vector_top_k: 50, dimension_top_n: 50, fallback: skip_conflict}`；`l2.prescreen` 亦开启（T-97）
- LoCoMo 规模（联网核实）：全套 50 会话 / 7,512 QA（single-hop 841 / multi-hop 282 / temporal 321 / open-domain 96 / adversarial 约 500+，主评测惯例剔除 adversarial）；Mem0 口径 10 会话 / 1,540 有效 QA
