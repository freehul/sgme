# SGME 提炼质量评测基线 PRD v0.1

> 版本：v0.1
> 日期：2026-08-06
> 依据：SGME-架构设计-0.4.md §14 #32（提炼质量评测基线）、#33（提示词版本管理 A/B 裁判依赖评测集）
> 范围：评测集设计 + L1/L2 质量度量定义 + RRF 参数调优方案 + 基线目标
> 约束：仅定义「要做什么」「怎么做算对」「怎么度量质量」，不涉及架构设计或代码实现

---

## 1. 项目信息

- Language：中文
- Programming Language：Python 3.11（评测脚本与评测集工具链）
- Project Name：sgme_eval_baseline
- 原始需求：建 50~100 条用例小评测集，做 L1/L2 质量基线 + RRF 参数调优；须含「维度标注准确率」一项；画像质量 = 维度标注准 × 模板查询对

## 2. 产品定义

### 2.1 产品目标

评测基线要回答四个核心问题：

1. **L1 维度标注质量如何？** — 提炼管线给记忆打的维度标签有多准？（维度名称对 + 维度值/标签对）
2. **L2 场景聚合质量如何？** — 场景标签是否正确？模板查询匹配是否把对的记忆输出到对的 section？
3. **RRF 参数哪个最优？** — k 值、融合权重、向量/BM25 占比等参数在 SGME 数据上的最佳取值是什么？
4. **提示词版本 A vs B 哪个更好？** — 依据 #33 的 A/B 分流机制，评测集充当 ground truth 裁判标准，给出客观对比结论

### 2.2 User Stories

- As a **系统开发者**，I want 一套标准评测集和度量脚本 so that 每次改提示词/改参数后跑一遍就知道质量涨了还是跌了
- As a **提示词调优者**，I want L1 维度标注准确率（逐维度 Precision/Recall/F1）so that 知道哪些维度容易标错、需要优化提示词
- As a **架构决策者**，I want L2 场景聚合与模板查询匹配的正确率 so that 确认「画像 = 模板查询」这条架构决策在质量上成立
- As a **参数调优者**，I want RRF 参数对检索质量影响的量化对比 so that 不再凭感觉调 k 值和权重
- As a **#33 提示词版本管理者**，I want A/B 对比的 ground truth 标准 so that 不再靠目测判断哪个版本更好

---

## 3. 评测集设计（50~100 条用例）

### 3.1 用例结构

每条用例为一个 JSON 对象，字段定义如下：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `case_id` | string | 是 | 唯一标识，格式 `eval-{NNN}`（如 `eval-001`） |
| `source` | string | 是 | 来源标记：`real`（真实会话脱敏）/ `synthetic`（合成）/ `edge`（已知困难 case） |
| `difficulty` | string | 是 | `easy` / `medium` / `hard` |
| `conversation` | string | 是 | 输入会话文本片段（模拟 L0 原始层的一段对话，行内标注 msg_id 序号） |
| `expected_l1` | object | 是 | **L1 ground truth**：应提取的记忆列表与维度标注 |
| `expected_l1.memories[]` | array | 是 | 每条：`{content, dimensions[], memory_type, priority, time_velocity, source_message_ids[]}` |
| `expected_l15` | object | 否 | **L1.5 ground truth**：预期冲突提炼结果（可选，L1.5 独立评测时使用） |
| `expected_l15.actions[]` | array | 否 | 每条：`{new_memory_index, candidate_ids[], action, merged_content?, reason}` |
| `expected_l2` | object | 否 | **L2 ground truth**：预期场景标签与模板查询匹配（可选，L2 独立评测时使用） |
| `expected_l2.scene_labels[]` | array | 否 | 每条记忆应归属的场景主题标签列表 |
| `expected_l2.template_section` | object | 否 | daily/coding/work/full 模式下，每条记忆预期落入哪个模板 section |
| `notes` | string | 否 | 标注者备注（为什么这么标、歧义点说明） |

**维度标注正确性定义**（ground truth 的 `expected_l1.memories[].dimensions` 字段）：

- 每条记忆标注 **1~3 个维度 id**（英文 snake_case，对应 `registry/dimensions.yaml`）
- 标注时遵循维度边界的 `boundaries` 说明（如"护理行为可双标签 家庭+习惯"）
- ground truth 维度 = 人类标注者达成共识后的**最小完备维度集**（不多标、不漏标）

### 3.2 用例来源策略

| 来源 | 目标数量 | 策略 |
|---|---|---|
| **真实会话（脱敏）** | 30~40 条 | 从 Hermes 历史会话中选取典型片段，替换人名/敏感信息为占位符（如 `[用户]`/`[同事A]`）；覆盖 daily/coding/work 三种模式下的真实对话 |
| **合成用例** | 30~40 条 | 人工构造覆盖边界/歧义/多维度场景：含维度边界模糊（如"技术栈 vs 项目"）、多维度重叠（一条记忆同时涉及 4+ 个维度但只应标 2~3 个）、time_velocity 判断困难（"刚开始学"是 status 还是 skills）、优先级边界（50 分附近的记忆该不该收）、非中文内容（英文技术讨论） |
| **已知困难 case** | 10~20 条 | L1 归一化边界（别名表未覆盖、相似度兜底冲突）、L1.5 冲突提炼易错（该 merge 误判 store、该 skip 误判 update）、L2 场景 split/merge 敏感 |

### 3.3 标注流程

1. **初标**：产品经理（Alice）或领域专家初标所有用例的 ground truth
2. **交叉校验**：另一名标注者独立标注 20% 用例，计算标注者间一致率（Inter-Annotator Agreement, IAA）——目标 IAA ≥ 0.85（Cohen's Kappa）
3. **仲裁**：不一致用例由两人讨论达成共识；无法共识的标记 `difficulty=hard` 并在 `notes` 中记录分歧
4. **版本管理**：评测集纳入 git（`evals/cases/` 目录），每次修改走 PR 评审；评测集版本号与提示词版本号解耦
5. **质量保证**：每条用例的 ground truth 至少经过 1 人标注 + 1 人复核（可同一人隔天复核）

### 3.4 典型用例示例（5 条）

| case_id | source | difficulty | 会话片段摘要 | L1 ground truth 关键维度 | 测试目标 |
|---|---|---|---|---|---|
| `eval-001` | real | easy | 用户说"我叫张明，在深圳做后端开发，用 Python 和 Go" | identity（身份+职业+所在地）、skills（Python+Go）、tech_stack（后端） | 基础单维度标注 + 多维度拆分 |
| `eval-002` | synthetic | medium | "SGME 这个项目架构从 Fork 改自研了，用 Python 重写，参考了 TencentDB-Agent-Memory 的设计" | projects（SGME）、tech_stack（Python 自研+参考底座）、values（借鉴不照抄） | 维度边界：项目 vs 技术栈 vs 价值观 三标签 |
| `eval-003` | edge | hard | "每周三早上有固定安排，我得六点起。对了那个 sgme_ 工具前缀要不要改成别的？" | family（家庭安排）、habits（周三六点起）、projects（SGME 工具命名讨论） | 情境切分 + 家庭/习惯双标签 |
| `eval-004` | synthetic | medium | "最近有点累，但 ComfyUI 那个工作流终于跑通了，用 qwythos-9b 模型效果不错" | status（有点累）、focus（ComfyUI）、tech_stack（qwythos-9b） | time_velocity 判断：status=dynami、focus=dynami、tech_stack=static |
| `eval-005` | edge | hard | "以后代码注释全部用中文，变量名用英文。这个跟之前说的文档中文一致" | preferences（代码注释中文）、style（命名规范英文） | preference 更新 vs 新增：已有"文档中文"偏好，本条是关联还是独立 |

### 3.5 评测集规模与分布

- **总规模**：80 条（预留 20 条扩展空间，首版目标 50~80 条）
- **难度分布**：easy 30% / medium 45% / hard 25%
- **来源分布**：real 40% / synthetic 40% / edge 20%
- **维度覆盖**：每条维度至少出现在 3 条用例的 ground truth 中（15 维 × ≥3 = ≥45 条覆盖）
- **模式覆盖**：daily 场景 ≥20 条 / coding 场景 ≥20 条 / work 场景 ≥15 条 / full 场景 ≥10 条 / 跨模式 ≥10 条

---

## 4. L1 质量度量（维度标注准确率）

### 4.1 度量定义

维度标注准确率分两层：

**第一层：维度名称准确率（Dimension ID Accuracy）**

判断 L1 输出的 `dimensions[]`（归一化后的维度 id 列表）与 ground truth 是否一致。

- **Strict Match**：预测的维度 id 集合 == ground truth 的维度 id 集合（完全一致才算对）
- **Relaxed Match**：预测 ∩ ground truth 的维度数 / ground truth 的维度数 ≥ 阈值（默认不采用，仅作诊断参考）

**第二层：维度值/标签准确率（Dimension Value Accuracy）**

判断维度标注的附属字段（memory_type、priority、time_velocity）是否正确。

- `memory_type` 准确率：persona/episodic/instruction 三分类正确率
- `priority` 偏差：预测 priority 与 ground truth priority 的 MAE（Mean Absolute Error），以及 ±10 容差内的命中率
- `time_velocity` 准确率：static/dynamic 二分类正确率

### 4.2 计算方式

| 指标 | 公式 | 说明 |
|---|---|---|
| **维度 Precision（微平均）** | TP / (TP + FP) | 所有用例的所有预测维度汇总计算；衡量"标出来的维度有多少是对的" |
| **维度 Recall（微平均）** | TP / (TP + FN) | 所有用例的所有 ground truth 维度汇总计算；衡量"该标的维度有多少被标出来了" |
| **维度 F1（微平均）** | 2 × P × R / (P + R) | **主指标**——L1 维度标注质量的综合度量 |
| **维度 Precision（逐维度）** | TP_d / (TP_d + FP_d) | 每个维度 id 单独计算，暴露哪些维度容易标错（如"风格"易与"偏好"混淆） |
| **维度 Recall（逐维度）** | TP_d / (TP_d + FN_d) | 每个维度 id 的漏标率，暴露哪些维度容易被忽略（如"价值观"低频但重要） |
| **维度 F1（逐维度）** | 2 × P_d × R_d / (P_d + R_d) | 逐维度诊断用；低于 0.7 的维度需提示词优化 |
| **Strict Match Rate** | 严格匹配的用例数 / 总用例数 | 完全标对的用例占比（严格标准） |
| **memory_type Accuracy** | 正确的 memory_type 数 / 总记忆数 | 辅助指标 |
| **priority MAE** | Σ\|pred - gt\| / 记忆总数 | 辅助指标；目标 < 10 |
| **time_velocity Accuracy** | 正确的 time_velocity 数 / 总记忆数 | 辅助指标 |

其中 TP/FP/FN 的判据：

- **TP（True Positive）**：预测的维度 id ∈ ground truth 的维度 id 集合
- **FP（False Positive）**：预测的维度 id ∉ ground truth 的维度 id 集合（多标/标错）
- **FN（False Negative）**：ground truth 的维度 id 未被预测（漏标）

> **与维度注册表的对照规则**：预测维度先经归一化层映射到注册表 id，再与 ground truth 比较。归一化失败的维度（未知标签丢弃）直接计为 FP。

### 4.3 度量优先级

- **P0（必须）**：维度微平均 F1、Strict Match Rate、逐维度 F1（热力图）
- **P1（应该）**：memory_type Accuracy、time_velocity Accuracy
- **P2（可以）**：priority MAE、Relaxed Match Rate（诊断用）

---

## 5. L2 质量度量（场景/模板）

### 5.1 场景标签准确率

L2 场景聚合不直接对应 ground truth（场景是 LLM 动态生成的，无"标准答案"），改为间接度量：

- **场景一致性（Scene Coherence）**：同一条 ground truth 记忆在多次运行中是否落入了同一场景（稳定性度量）
- **场景粒度（Scene Granularity）**：场景总数是否在合理范围（与记忆总数比例在 1:5 ~ 1:20 之间，过细/过粗告警）

> L2 场景准确率暂不设硬性 ground truth 比对（场景主题是 emergent 的），未来迭代可引入"场景合并/拆分质量"的人工评估（抽样 10 条）。

### 5.2 模板查询匹配正确率

**定义**：对于给定的 Memory Mode（daily/coding/work/full），L1 产出并入库的记忆，是否能被模板查询正确地分配到预期的 section。

**度量方法**：

1. 从评测集中选取 **模板查询验证子集**（~20 条用例，涵盖 4 个模式）
2. 每条用例跑完 L1 → L1.5 → 入库后，分别以 4 个模式执行模板查询
3. 比对查询结果中每条记忆落入的 section 是否与 `expected_l2.template_section` 一致

| 指标 | 公式 | 说明 |
|---|---|---|
| **Section 命中率** | 命中预期 section 的记忆数 / 应有记忆总数 | 主指标 |
| **Section 误入率** | 进入错误 section 的记忆数 / 查询返回记忆总数 | 不应出现的记忆出现在该 section |
| **Section 漏出率** | 未出现在任何 section 的记忆数 / 应有记忆总数 | 该出来的记忆没出来（被 TTL/priority_min/time_window 过滤） |

### 5.3 画像质量综合评价公式

依据架构 §14 #32 原文"画像质量 = 维度标注准 × 模板查询对"：

```
ProfileQuality = L1_Dimension_F1 × L2_Section_HitRate
```

其中：
- `L1_Dimension_F1`：维度标注微平均 F1（§4.2 主指标）
- `L2_Section_HitRate`：模板查询 Section 命中率（§5.2 主指标）

两者乘积范围 [0, 1]，综合反映「记忆打对标签 → 标签驱动查询 → 查询输出对」的端到端质量。

---

### 5.4 模板注入效果度量（T-20）

> 依据 T-20：现状评测框架只测提炼质量（L1 F1）与检索排序（RRF），
> 4 个场景模板（daily/coding/work/full）的**注入效果**无检测手段，靠人判断。
> 本节定义注入相关性/有用性度量，补「画像 = 模板查询」这条架构决策的效果证据。

#### 5.4.1 度量定义

注入效果 = 注入的画像块对「用户后续对话」的相关性/有用性。两个主指标：

| 指标 | 公式 | 说明 |
|---|---|---|
| **注入命中率（Injection Hit Rate）** | 与后续对话相关的注入画像块数 / 注入画像块总数 | 主指标：注入的画像里有多大比例是后续对话真正用得上的 |
| **引用覆盖率（Reference Coverage）** | 被后续对话引用的记忆中被注入的比例 = 命中且被引用的记忆数 / 被引用记忆总数 | 主指标：该注入的记忆有没有被模板查询捞出来 |

辅助指标：

| 指标 | 公式 | 说明 |
|---|---|---|
| 画像块总数 | 注入响应中 present=true 的 block 数 | 分母（注入命中率） |
| 相关块数 | 与后续对话相关的 block 数 | 分子（注入命中率） |
| 引用记忆数 | GT 标注的后续对话引用记忆数 | 分母（引用覆盖率） |
| 命中且引用数 | 注入命中且被后续对话引用的记忆数 | 分子（引用覆盖率） |

#### 5.4.2 相关性与引用判定（ground truth 驱动，零 LLM）

判定不依赖 LLM，由评测集人工标注：

- **引用记忆**：评测用例新增 `expected_inject.referenced_memory_indices`——标注
  「后续对话引用了哪几条 GT 记忆」（按 `expected_l1.memories` 索引）。
- **注入命中**：模板查询（零 LLM 纯 SQL）落库后执行注入，得到画像块；
  画像块含 `memory_id`（确定性 `{case_id}#{idx}`），按 `idx` 回查 GT 记忆；
  `idx ∈ referenced_memory_indices` 的记忆视为「被引用且注入命中」。
- **相关画像块**：块内含 ≥1 条被引用且注入命中的记忆 → 该块与后续对话相关。

#### 5.4.3 评测数据形态

每条注入评测用例在既有 L1/L2 ground truth 之外附加：

```yaml
expected_inject:
  mode: coding                          # 注入模式（daily/coding/work/full）
  subsequent_conversation: |            # 用户后续对话（注入画像之后的对话）
    [msg#2] 2026-01-02T10:00:00Z user:
      那个 Rust 项目 CI 挂了，帮我看看怎么办
  referenced_memory_indices: [1]        # 后续对话引用的 GT 记忆索引（按 expected_l1.memories）
```

执行链路（与 §5.2 模板查询验证子集同构）：
1. GT 记忆落库到 eval DB（确定性 `memory_id = {case_id}#{idx}`，与 RRF 语料同一套 id 规则）
2. 按 `expected_inject.mode` 执行模板查询（`sgme.profile.inject`，纯 SQL 零 LLM）
3. 组装画像块（`build_inject_blocks`），按 §5.4.2 判定相关块
4. 计算注入命中率 + 引用覆盖率

#### 5.4.4 与既有指标的关系

- **L2 Section 命中率（§5.2）** 测「记忆被分到正确 section」——查询正确性；
- **注入命中率（本节）** 测「注入画像对后续对话有用」——效果/有用性；
- 两者互补：前者是过程质量，后者是结果质量。画像质量综合评价公式（§5.3）
  不变，注入度量作为独立维度进评测报告。

---

## 6. RRF 参数调优方案

### 6.1 被调参数列表

基于 §16.2 底座借鉴的 RRF 融合实现与 SGME `/search` 接口设计：

| 参数 | 默认值 | 搜索范围 | 说明 |
|---|---|---|---|
| `rrf_k` | 60 | [10, 30, 60, 90, 120] | RRF 标准常数，越小排名靠前的条目权重越大 |
| `bm25_weight` | 0.5 | [0.3, 0.4, 0.5, 0.6, 0.7] | BM25 在融合中的权重（向量权重 = 1 - bm25_weight） |
| `top_k` | 20 | [10, 20, 30, 50] | 检索返回条数 |
| `bm25_k1` | 1.2 | [0.8, 1.0, 1.2, 1.5, 2.0] | BM25 term frequency saturation 参数 |
| `bm25_b` | 0.75 | [0.5, 0.6, 0.75, 0.9] | BM25 length normalization 参数 |

> 注：RRF 参数调优仅影响 `/search` 接口（Tier 2/3 检索补充），不影响模板查询（纯结构化 SQL，无 RRF）。

### 6.2 调优策略

**推荐方案：网格搜索（Grid Search） + 固定评测集**

- 参数空间约 5×5×4×5×4 = 2000 个组合，本地 LM Studio 零边际成本可接受
- 评测集：从 80 条中选取 **检索评测子集**（~30 条），每条附带 `expected_search_results`（人工标注的相关记忆 id 排序列表）
- 目标函数：**NDCG@10**（Normalized Discounted Cumulative Gain）——度量检索排序质量，相关记忆排越前得分越高
- 辅助指标：MRR（Mean Reciprocal Rank）、Recall@10

**不推荐贝叶斯优化的理由**：参数空间小（离散枚举值）、本地零成本、网格搜索可复现且易于解释。

### 6.3 与 #33 提示词版本 A/B 裁判的关系

```
┌──────────────────────────────────────────────────────┐
│                    #32 评测集（本 PRD）                 │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ L1 评测   │  │ L2 评测   │  │ RRF 检索评测      │   │
│  │ 维度标注   │  │ 模板查询   │  │ NDCG@10          │   │
│  │ F1 主指标  │  │ 命中率     │  │                   │   │
│  └─────┬────┘  └─────┬────┘  └────────┬──────────┘   │
│        │             │               │               │
│        ▼             ▼               ▼               │
│  ┌─────────────────────────────────────────────────┐ │
│  │          ground truth = 裁判标准                  │ │
│  │  #33 A/B 对比时：分别跑 A 版和 B 版 → 比 F1 差分  │ │
│  └─────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

- #33 的 `refine_runs` 表记录每次提炼的 `(version, variant, provider, ...)`，提供观测数据
- #32 的评测集提供 **ground truth 裁判标准**：对 A 版本和 B 版本分别评测 L1 F1，差分即为版本优劣的量化依据
- **不做自动裁决**（与 #33 决策一致：结论留人工），但评测脚本输出 A/B 对比报告

---

### 6.4 RRF 网格搜索实测结论（增量更新 v0.1.1，commit 7e1fb47）

> 本小节为增量结论，基于 RRF 网格搜索真实接入 `/v1/search` 后的实测数据（p0-runA，两次独立运行逐字段相等）。**推翻了 PRD 早期版本中"rrf_k 零区分度 = 两路信号同源（词面耦合、Jaccard 高）"的假设。**

#### 6.4.1 实测数据（p0-runA）

| 字段 | 值 |
|---|---|
| corpus / query | 88 条记忆 / 50 查询（message 模式） |
| rrf_k 候选 | [10, 30, 60, 90, 120]，全部取值 NDCG@10 恒等 |
| best_ndcg10 | 0.9546 |
| ndcg_spread / param_sensitivity.spread | 0.0 / 0.0 |
| discriminative / rank_sensitive_ratio | false / 0.0 |
| route_overlap_jaccard | 0.0746 |
| gt_mode（NDCG 天花板） | message（0.977） |
| vector_available / vector_coverage | true / 1.0（embed_cache 138/138 命中，零网络） |
| bm25_avg / median / max recall | 1.74 / 1 / 7 |
| vector_avg recall | 20.0（恒满） |
| route_overlap_avg | 1.52 |
| queries_with_empty_bm25 / dual_route_queries | 16/50 / 34/50 |
| recommended_k / conclusion | null / inconclusive |

#### 6.4.2 根因结论：spread=0 是数学必然，非样本不足

实测 Jaccard=0.0746（两路重叠率极低，解耦确实发生），但 k 仍零区分度——原"同源"假设因果链断裂。经机理实验（exp_verify_mechanism.py）与数据分解，当前 `spread=0` 由两层原因叠加，均为结构/度量层必然：

1. **BM25 召回近退化（结构根因）**。50 条查询中 16 条 BM25 完全空召回（仅剩单路，k 数学上无作用点）；其余 34 条 BM25 均值仅 ≈2.56 条。更关键：BM25 命中里有 **87.4% 同时出现在向量结果集**（交集 1.52 / BM25 1.74），即 BM25 召回集近乎被向量集合**包含**。报表 Jaccard=0.0746 是**列表长度悬殊伪影**——向量恒满 20 条撑爆分母（重算 1.52/(1.74+20−1.52)=0.0752，与报告吻合）。两路并非"各说各话"，恰恰高度一致；真实问题是 **BM25 召回太窄**，这正是架构师早期"FTS5 `unicode61` 按标点整段切 token"假设的实证支撑（对应 P1-5）。

2. **k 仅对"双路命中且两路 rank 错位"的文档有作用点**。机理实验：
   - |BM25|=1 时，1/(k+1) 恒为该路最大分，叠加后压过所有纯向量文档，排序与 k 无关（M1）。
   - Jaccard=1.0 但两路排序一致时，k 仍失效（M3）——高重叠端真正决定变量是两路 **rank 不一致程度**，不是集合重叠率。
   - 仅当存在"双路命中且 rank 错位"文档（如 A：BM25 r0/向量 r10，B：BM25 r3/向量 r4）时，k 才翻转二者顺序（M2）。当前这类文档极少（BM25 退化为 1~2 条且多被向量覆盖、排序一致），故 k 无作用点。
   - 倒 U 型规律成立：k 仅在**中等重叠 + rank 错位**时有区分度；当前落在"BM25 退化、可重排跨路文档稀缺"的左端，spread=0 必然。

3. **度量天花板封顶（指标层放大）**。message 模式 NDCG@10 天花板 0.977、实测 best 0.9546，headroom ≈0.02。即便 k 能重排个别文档，也落在已饱和区间，无法被度量捕捉。related 模式（天花板 0.352、headroom 充足）才是未来能观测 k 区分度的场景。

**结论**：`rrf_k` 在当前语料下是**结构惰性参数**——不是"调不对"，而是"在当前检索结构下没有可作用点"。根因是 **BM25 召回退化**（将被 P1-5 中文分词修复），而非两路同源。

#### 6.4.3 `conclusion` 语义定性

**保留"把'测了没效果'与'根本没测成'显式分开"的方向，不合并为单一 `inconclusive`。** 理由：把"参数结构惰性（确实测了、向量也起来了、就是没区分度 = 确定结论）"与"数据不足（无查询 / 向量没起 = 根本没测）"混为一谈会误导运维。诚实红线要求区分二者。

当前代码实际落的三值是 `discriminative` / `inconclusive` / `no_queries`（见 `tests/test_eval_rrf.py:333/344/362` 断言）；本结论将其演进为**五值枚举**。`conclusive` 不复用代码现名 `discriminative`——因 `RRFMetrics` 已有一个同名布尔字段 `discriminative`，字符串枚举再用同名会造成"同名不同义"双字段陷阱。

| conclusion | 触发条件 | 语义 | recommended_k |
|---|---|---|---|
| `conclusive` | vector_available=true 且 ndcg_spread ≥ NDCG_SIG(0.01) | 存在真实最优 k（对应旧 `discriminative`） | best_k |
| `inconclusive_no_effect` | vector_available=true 且 ndcg_spread < NDCG_TIE(1e-9) 且 route_overlap_jaccard < J_LOW(0.20) | 低重叠（BM25 退化）致 k 无作用点，**确定结论** | null |
| `inconclusive_below_noise` | vector_available=true 且 0 < ndcg_spread < NDCG_SIG(0.01) | 微弱灵敏度落噪声内，无法确认 | null |
| `inconclusive_bm25_only` | vector_available=false 且有查询 | 向量未起、融合没跑，**无数据**（区别于 `no_queries`：后者无查询） | null |
| `no_queries` | ground_truth 为空（无查询） | 根本无查询可评，**无数据** | null |

当前 p0-runA 映射 **`inconclusive_no_effect`**（vector 起、spread=0、Jaccard 0.0746<0.20）。阈值 J_LOW=0.20 取"低重叠"经验界；辅以 `bm25_avg_recall < 3` 作 BM25 退化辅助判据（当前 1.74）。

**代码迁移映射（供 engineer 对齐，不在本文档改动范围）**：

| 代码现名 | 目标枚举 | 说明 |
|---|---|---|
| `discriminative` | `conclusive` | 仅改名，语义一致（有区分度 = 存在最优 k） |
| `inconclusive` | `inconclusive_no_effect` / `inconclusive_below_noise` / `inconclusive_bm25_only` | 按 `vector_available` 与 `ndcg_spread` 分流：向量未起→`inconclusive_bm25_only`；向量起且 0<spread<NDCG_SIG→`inconclusive_below_noise`；向量起且 spread=0 且低重叠→`inconclusive_no_effect` |
| `no_queries` | `no_queries` | 保持不变 |

> 注：实测 report.json 中 `conclusion` 字段值仍为旧单值 `"inconclusive"`，系接入前占位；按上表应分流为 `inconclusive_no_effect`。代码是否应用五值枚举需 engineer 对齐（不在此 PRD 改动范围，仅提示）。

#### 6.4.4 运维建议

- **维持 `search.rrf.k = 60` 不变**。k 在当前语料为惰性参数，任意取值 NDCG 恒等，默认 60 无风险；盲选"并列结果"充数违反诚实红线（6.4.5）。
- **重新评估触发条件**（满足任一即重跑网格搜索）：
  1. 语料显著增长（记忆数 88 → 上百/上千），跨路重叠文档基数增大；
  2. P1-5 中文分词落地，BM25 中位召回 > 3、空召回查询 → 0，Jaccard 抬升；
  3. `route_overlap_jaccard` 进入 0.20~0.30，跨路可重排文档充足，k 开始有区分度；
  4. GT 切换 related / headroom 模式或扩大查询集（message 模式天花板封顶）。
- **监控**：将 `route_overlap_jaccard`、`bm25_avg_recall`、`ndcg_spread` 纳入评测报告总览，作"是否值得调 k"的前置信号。

#### 6.4.5 诚实性红线（不变）

- `recommended_k` **恒为 null**；仅当 `conclusion == conclusive` 才输出 best_k。其余四态一律 null，**绝不从并列/无区分度结果里挑一个充数**。
- 报告横幅须明示结论语义（如 `inconclusive_no_effect` + 原因 `low_overlap_bm25_degenerate`），不得渲染为"已选出最优 k"。
- 此红线由以下真实测试守护（已逐行确认）：
  - `tests/test_eval_rrf.py:303-364`（诚实诊断测试类）：L326 零区分度 ⇒ `conclusion` 非确定态且 `recommended_k is None`；L339 有区分度 ⇒ `recommended_k` 落在 {10,30,60,90,120}；L357 双 case 均 None；L359 空 GT ⇒ `no_queries` 且 `recommended_k is None`。
  - `tests/test_eval_rrf.py:374` `test_rrf_search_no_data_pollution`：评测零污染 `data/`。
  - `tests/test_eval_qa_acceptance.py:745` `test_rrf_search_implemented_and_reproducible`：真实接入 + 可复现。

---

## 7. 基线目标

### 7.1 首次运行基线目标

评测集建成后的首次评测运行，设定以下目标值（达到即视为 L1/L2 管线质量可用）：

| 指标 | 目标值 | 说明 |
|---|---|---|
| L1 维度微平均 F1 | ≥ 0.75 | 维度标注整体准确率 |
| L1 Strict Match Rate | ≥ 0.50 | 严格完全匹配率（多维度场景天然低） |
| L1 memory_type Accuracy | ≥ 0.85 | 记忆类型判断相对容易 |
| L1 time_velocity Accuracy | ≥ 0.80 | static/dynamic 判断 |
| L2 Section 命中率 | ≥ 0.70 | 模板查询 section 分配正确 |
| 画像质量综合分 | ≥ 0.50 | F1×命中率（0.75×0.70≈0.525） |
| RRF NDCG@10 | ≥ 0.60 | 检索排序质量基准 |

### 7.2 基线报告格式

首次评测运行后产出的基线报告应包含：

1. **总览面板**：所有 P0 指标的当前值 vs 目标值（红/黄/绿状态）
2. **维度热力图**：15 个维度的逐维度 F1 矩阵（颜色越深越差），标注低于 0.6 的"问题维度"
3. **混淆矩阵**：维度标注的混淆对（如"风格"→"偏好"混淆频率），用于提示词优化指引
4. **RRF 参数敏感度**：各参数对 NDCG@10 的影响曲线，标注最优组合
5. **A/B 默认对比**：`@working` vs `v001` 的差分报告（如已有 #33 基础设施）
6. **误差分析**：hard 难度用例的典型失败模式分类（维度边界混淆 / 归一化失败 / 漏标 / 多标）

---

## 8. 与现有系统衔接

### 8.1 评测集格式对接提炼引擎

评测用例的 `conversation` 字段模拟 L0 原始层文件内容（行内 msg_id 序号标注），可直接喂入 L1 提炼管线：

```
评测用例 .conversation → 临时 L0 MD 文件 → refine_file() → L1 提取 → L1.5 冲突提炼 → 入库 → L2 聚合 → 模板查询
```

评测脚本串联上述流程，在每个环节捕获中间输出并与 ground truth 比对。

### 8.2 评测脚本输入/输出接口

**输入**：
- `--cases`：评测集 JSON 文件路径（`evals/cases/eval_set_v1.json`）
- `--stages`：评测阶段选择 `l1|l15|l2|search|all`（默认 `all`）
- `--prompt-version`：指定提示词版本（`@working` / `v001` / `v002`），不指定则走 manifest 的 active
- `--mode`：模板查询模式 `daily|coding|work|full`（L2 评测时使用）

**输出**：
- `evals/results/{timestamp}/report.json`：所有指标的结构化结果
- `evals/results/{timestamp}/report.md`：人类可读的基线报告（含热力图文字描述）
- `evals/results/{timestamp}/per_case.csv`：逐用例明细（用于误差分析）
- 退出码：全部 P0 指标达标 → 0；任一不达标 → 1（用于 CI 集成）

### 8.3 评测集成入 CI/开发流程

```
                    ┌──────────────┐
                    │ 改提示词/参数  │
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │ pytest 全绿   │
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │ 评测脚本跑分  │
                    │ python -m     │
                    │ evals.run     │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         达标（绿）    接近（黄）    不达标（红）
         可合并        需 review     阻止合并
```

- 评测脚本作为**可选 CI 检查**（非 blocking，但变更提炼相关代码时强烈建议跑）
- 真实 LLM 依赖（LM Studio 在线），不适用于纯 mock CI 环境
- 评测结果存入 `evals/results/` 目录，纳入 git 追踪形成历史趋势

---

## 9. 待确认问题

1. **评测集标注人力**：80 条用例的 ground truth 标注预计需要 2~3 个工作日（含交叉校验）。标注者是谁？是否可以由产品经理初标 + 架构师复核的两人协作模式推进？

2. **真实会话来源授权**：30~40 条真实会话脱敏用例需要从 Hermes 历史会话中选取。用户是否授权使用？脱敏到什么程度（人名→[用户]/[同事A]，具体数字/日期是否保留）？

3. **L2 场景 ground truth 取舍**：当前设计对 L2 场景聚合采用间接度量（场景一致性/粒度），不做硬性 ground truth 比对（因为场景主题是 emergent 的）。是否接受这个取舍？还是希望抽样 10 条做人工 L2 质量打分？

4. **RRF 参数调优的优先级**：RRF 调优只影响 `/search` 接口（Tier 2/3），不影响模板查询（纯 SQL）。在当前阶段（最小闭环），/search 尚未实现——RRF 调优是否同步推进还是等 /search 实现后再做？

5. **评测集存放与版本策略**：评测集文件（`evals/cases/`）是否纳入 git 管理？建议纳入（评测集是 code-like artifact，需要版本追踪），但需确认 ground truth 变更的评审流程（PR 评审还是直接改）。

---

*文档完。v0.1 初版，待团队评审后修订。*
