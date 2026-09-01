# SGME 记忆系统进化方案 v0.2 — 审查意见

> 审查人：吹吹水｜日期：2026-08-31 01:40｜对象：`SGME-记忆系统进化方案-v0.2.md`
> 结论：**有条件通过**——方案骨架（三阶段 / 拆步止损 / 检索改动必跑 A/B 铁律）认同；
> **2 项阻断需在 T1-1 启动前解决**，2 项重要修订需补进方案，1 项补充。
> 全部结论均有本地源码或 NAS 生产实测数据支撑，实证原始数据见文末附录。

---

## 零、结论速览

| 级别 | 编号 | 问题 | 处置建议 |
|---|---|---|---|
| 🔴 阻断 | **B1** | LoCoMo「50 会话全量档」**数据不存在** | 三档改两档：冒烟（1 conversation）→ 基线（10 conversation / 1,540 QA）。数据已下载 |
| 🔴 阻断 | **B2** | **检索查询侧无停用词过滤**，自然语言提问在 BM25 路上直接落空（**中英共有**） | 新增独立任务插到阶段一最前：查询侧停用词过滤 + 英文分词适配 |
| 🟠 重要 | **R1** | T1-1 混了**互斥的两条架构路线**（离线 corpus vs 在线灌库），文档未拆 | 拆 T1-1a（离线，沿用现有框架）／T1-1b（在线灌库，为图召回准备） |
| 🟠 重要 | **R2** | 在线路线的 **GT 对齐路径未定义**（evidence → memory_id） | 冒烟阶段先测「GT 覆盖率」，<70% 放宽到 session 级 |
| 🟡 补充 | **S1** | 规模与成本口径偏乐观（10 conversation ≠ 10 session） | 改为按 conversation 计量；先跑 1 conversation 冒烟实测外推 |
| 🟡 补充 | **S2** | T1-2 两项检查结论（尖峰已归因 / skip_conflict 生产不可观测） | 见 §四，建议补一个可观测标记 |

---

## 一、B1：LoCoMo 没有 50 会话档（阻断）

**文档原文**（§二 T1-1 ②）：「冒烟 1–2 会话 → 正式基线 10 会话 → **50 会话全量可选**」。
**附录**（附：审查实证数据）：「全套 LoCoMo = 50 会话 / 7,512 QA」。

**实测**：

- 仓库正确地址是 **`snap-research/locomo`**（小写，文档未给出且我之前按 `SnapResearch/LoCoMo` 克隆返回 404）；
- 仓库只发布 **`data/locomo10.json`** 一个数据文件，**即 10 conversation 版本**。官方 README Note 1 明确：
  「This release is a subset of the conversations released previously with our first Arxiv version in March 2024.
  **The initial release contained 50 conversations.**」——50 会话是初版口径，**当前未公开**；
- 数据已下载：`D:\GitHubDownloads\LoCoMo\data\locomo10.json`（2,805,274 字节）。

**实测统计（与文档数字对账）**：

| 项 | 实测 | 文档 | 结论 |
|---|---|---|---|
| conversation 数 | 10 | 10 | ✅ |
| QA 总数 | 1,986 | — | — |
| category 1（multi-hop） | 282 | 282 | ✅ |
| category 2（temporal） | 321 | 321 | ✅ |
| category 3（open-domain） | 96 | 96 | ✅ |
| category 4（single-hop） | 841 | 841 | ✅ |
| category 5（adversarial） | 446 | 约 500+ | ⚠️ 实测 446 |
| 剔除 adversarial | **1,540** | 1,540 | ✅ 完全吻合 |

**结论**：文档的四类数字全部核对无误，唯独「50 会话全量档」是**拿不到数据的档位**，应从方案删除（或标注「需另行获取，当前不可得」）。三档 → 两档即可。

---

## 二、B2：检索查询侧无停用词过滤，自然语言提问落空（阻断，且中英共有）

这是本次审查**最有价值的发现**——它是 SGME 的**现存产品缺陷**，与图谱化无关，
但会让 T1-1 的基线数字完全不可信。

### 2.1 现象

`memories_fts` 用 FTS5 默认 `unicode61` 分词器，MATCH 查询为**隐式 AND 语义**。
而 `sgme/data/search/` 与 `sgme/operations/search.py` **均无停用词过滤**（grep `stopword|停用词` 全库零命中）。

结果：**查询里只要含一个文档中不存在的虚词，整条查询返回空**。

### 2.2 实测（临时库 + 真实 `init_fts` + 真实 `jieba.cut_for_search`）

文档：`m1 = "Leo went to the park with his dog yesterday and played frisbee near the lake"`
（seg = `Leo   went   to   the   park   with   his   dog   yesterday   and   played   frisbee   near   the   lake`）

| 查询 | 结果 |
|---|---|
| `frisbee` | ✅ m1 |
| `frisbee park` | ✅ m1 |
| `NAS server` / `memory engine` | ✅ 命中 |
| **`who played frisbee with a dog`** | ❌ **空**（`who`/`with`/`a` 不在文档中） |

**中文同样中招**（文档 `m4 = "吹吹风昨天带着狗去公园玩飞盘"`）：

| 查询 | 结果 |
|---|---|
| `公园 飞盘` | ✅ m4 |
| `吹吹风 狗 公园`（实词堆叠） | ✅ m4 |
| **`谁带着狗去公园`** | ❌ **空**（`谁` 不在文档中） |
| **`吹吹风在哪个地方玩飞盘`** | ❌ **空**（`哪个`/`地方` 不在文档中） |

### 2.3 对方案的影响

- LoCoMo 的 1,540 条有效 QA **全部是自然语言疑问句**（Who / When / What did X do with Y…），
  虚词密度极高 → **BM25 路基本不贡献召回**，基线数字会系统性偏低；
- 由此产生的直接后果：**T1-1 基线不可信，且与 Mem0 66.9% 不可直接比**
  （Mem0 检索不走 jieba，无此缺陷）；
- 更糟的是：T2-2 图召回的 A/B 差异会被这个系统性缺陷**淹没**——
  两臂都烂在 BM25 上，测不出 graph 路的真实增量。

### 2.4 建议

**新增独立任务，插到阶段一最前面**（编号建议 T-127）：

1. **查询侧停用词过滤**：中英双语停用表（英文 `a/an/the/who/with/and/…`，中文 `的/了/谁/哪个/在/是/…`），
   在 `cut_for_search` 之后、拼 MATCH 之前过滤；
2. **英文分词适配**：jieba 对英文已按空格切词，但保留空格占位（实测 seg 里有多余连续空格），需清理；
   另需处理标点/大小写（`NAS` vs `nas` 的 unicode61 大小写敏感问题）；
3. **兜底策略**：过滤后若 MATCH 结果为空 → 降级为 OR 语义重查一次（或交由向量路兜底，已有）。

**为什么值得单独做**（不只是为了评测）：

- 中文侧实测已证明这是**线上真实缺陷**——用户自然语言提问「吹吹风在哪个地方玩飞盘」当前**召回为空**；
- 与 Gen3 图谱化**完全解耦**，可独立上线、独立验收，零架构风险；
- 成本极低（一张停用表 + 查询拼装处一个过滤函数 + 边界测试）；
- 做完之后 T1-1 的基线才有可比性，T2-2 的 A/B 才有信噪比。

---

## 三、R1 / R2：T1-1 需要拆成两条路，且 GT 对齐要先验证

### R1：现有 eval 框架是「离线」的，与文档的「在线灌库」是两套东西

核查 `eval/runner.py` 实际运行模式：

| 环节 | 实测实现 | 含义 |
|---|---|---|
| `_setup_eval_db()` | 建**临时库** | 不碰生产/实例库 |
| `_setup_retrieval_corpus()` | 由 **case 自带 memories** 构建 corpus（`retrieval_gt.build_corpus`） | **不经过提炼** |
| `_make_query_fn()` | **进程内直调** `search` 函数 | **不走 HTTP 服务** |
| memory_id | 确定性 `{case_id}#{idx}`（禁用 uuid4） | 离线自造 ID |

而文档 T1-1 ② 写的是「LoCoMo 会话 → `append` + `refine_trigger` 灌入（走真实提炼链路，不走 mock）」——
这是**一整套全新的在线通路**：需独立 SGME 实例 + 独立 DATA_DIR + embed 可用 + HTTP 灌库 + 提炼 + GT 映射。

**两条路对比**：

| | T1-1a 离线（沿用现有框架） | T1-1b 在线灌库（文档方案） |
|---|---|---|
| 成本 | 零 LLM，分钟级 | 小时级，吃满免费额度 |
| 依赖 | 无（现有框架即可跑） | 独立实例 + embed + LLM + 提炼链路 |
| 能测什么 | RRF / BM25 / 向量三路融合、**检索回归护栏** | 上述 + **图召回**（必须有提炼产物才有边） |
| GT 风险 | 低（corpus 自造，GT 直通） | 高（evidence → memory_id 映射未验证） |

**建议**：拆两步。**T1-1a 先做**——它便宜、快、立刻提供「检索回归护栏」这个阶段一真正要的东西；
**T1-1b 在 T2-1a（结构边）之前做**，因为图召回的 A/B 必须依赖提炼产物。
⚠️ 注意：T1-1a 测不了图召回（无提炼就无边），这个限制要在文档写明，不能指望它给 T2-2 出 A/B。

### R2：在线路线的 GT 对齐路径未定义

文档只写「LoCoMo QA 对转 `retrieval_gt` 格式」，但没写**怎么转**。实际链路是：

```
LoCoMo QA.evidence (dia_id 列表)
  → 灌库时 L0 文件的段位标记（source_ref）
  → memory_sources 表反查 memory_id
  → 构成 relevant_ids
```

风险点：一条 QA 的 evidence 是若干 dia_id，但 SGME 提炼后一条记忆可能聚合多个 turn，
也可能一个 turn 产出多条记忆 → **映射是多对多的，且存在「证据 turn 未产出任何记忆」的可能**。

**建议**：冒烟阶段（1 conversation）**先测「GT 覆盖率」**——
即 1,540 条 QA 中有多少能映射到 ≥1 条 memory。
覆盖率 <70% 需放宽策略（如从 dia_id 级放宽到 session 级，或接受 session 级 GT 的粗粒度）。
**这个数字不先测出来，10 conversation 全量跑完也可能白跑。**

---

## 四、S1 / S2：补充项

### S1：规模与成本口径修正

> **⚠️ 本节已于 2026-08-31 02:10 用生产实测重写。** 我上一版按 JSON 文件大小（2.8 MB）
> 估「70 万 tokens / 3–6 小时」，**两个数字都错了**：语料实际只有 18–21 万 tokens，
> 但总成本反而更高（350–380 万），因为**真凶不是吃全文的 l1_extraction，而是 l1_conflict**。

**文档原文**：「10 会话 ≈ 200 个 L0 文件 × 4 stage ≈ 800 次 LLM 调用，1 小时量级」。

**实测（解析 `locomo10.json` + 生产 `refine_runs` 累计）**：

| 项 | 数值 |
|---|---|
| 结构 | **10 conversation × ~27 session = 272 session**，共 **5,882 turns** |
| 对话文本量 | **726,756 字符 ≈ 18–21 万 tokens**（⚠️ 不是 70 万——JSON 里的 `event_summary`/`observation`/`session_summary`/QA 不算灌入内容） |
| 单 conversation | 中位 76,741 字符（≈1.9 万 tokens）/ 646 turns |
| 平均 turn / session | 123.6 字符 / 2,672 字符 |
| QA | 1,986 条，**1,982 条带 evidence**（`['D1:3']` 形态 → 即 dia_id） |

**生产各 stage 累计 token（2026-08-06 → 08-30，24 天）**：

| stage | 轮次 | 总 tokens | 占比 | 平均/次 | 最大/次 |
|---|---|---|---|---|---|
| **l1_conflict** | 2,693 | **317,511,039** | **85.7%** | 117,902 | 1,013,115 |
| l1_extraction | 8,368 | 27,873,191 | 7.5% | 3,331 | 16,633 |
| l2_scene | 1,941 | 24,921,176 | 6.7% | 12,839 | 174,612 |
| tier0_summary | 29 | **0** | 0% | 0 | 0 |

> 注：l1_conflict 均值 11.8 万被 08-12 预筛上线前的旧数据拉高；预筛生效后实测中位约 5 万
> （见 S2 ①）。最大 101 万是预筛前的历史峰值。

**LoCoMo 全量灌入成本推算**（按 ~378 条记忆产出，装箱 17 条/批 ≈ 23–47 批）：

| stage | 估算 tokens | 说明 |
|---|---|---|
| l1_extraction | **45–64 万** | 内容 18.2 万（固定）+ **提示词开销 27–46 万**（按调用次数计） |
| **l1_conflict** | **230–310 万** | **占 70%+，真凶** |
| l2_scene | 30–60 万 | 场景从 0 建起，前期较便宜 |
| **合计** | **约 350–380 万 tokens** | |

**反直觉的两点**：

1. **提示词比内容还贵**。中文 `l1_extraction.txt` 3,087 字符 ≈ 1,700 tokens，
   而 LoCoMo 平均 session 内容仅 2,672 字符 ≈ 670 tokens —— **提示词是内容的 2.5 倍**。
   按 session 灌（272 次）时提示词开销 46 万 > 内容 18 万。
   `config/sgme.yaml` 的 `chunk_size: 5000`（min_chunk 4,500）决定调用次数：
   按 session 灌 = 272 次，按 conversation 灌 = 160 次，调大 chunk_size 可直接省掉这笔重复开销。
2. **tier0_summary 不在提炼链路上**——它是 `profile/tier0.py` 由 **inject 时按需**调用，
   `refine_runs` 里那 29 条 tier0 记录 token 全 0。故方案所述
   「提炼链 `tier0_summary → l1_extraction → l1_conflict → l2_scene`」的顺序描述不准确。

**时间重估**：调用次数约 206–366 次（l1_extraction 160–272 + l1_conflict 23–47 + l2_scene 23–47），
按长 prompt 单次 8–45 秒估算 ≈ **1–2 小时**（顺利 1 小时；遇 429 限流走分批间隔则 2–3 小时）。
**先前的「3–6 小时」偏高，予以撤回。**

**省钱杠杆（按性价比排序）**：

1. **只做 T1-1a 离线基线 = 0 tokens**（不跑提炼，沿用现有 eval 框架，分钟级）——最大杠杆；
2. 线上跑的话，调大 `chunk_size`（5,000 → 20,000）减少提示词重复，省约 19 万 tokens（5%）；
3. 调低 l1_conflict 候选规模（`vector_top_k` 50 → 30、`dimension_top_n` 50 → 30）
   → l1_conflict 降约 40%，**单项可省 100 万+ tokens**；
4. 先跑 1 conversation 冒烟实测墙钟与额度消耗，再外推（避免全量跑到一半被限流）。

### S2：T1-2 两项检查 —— 已完成，结论如下

**① 尖峰归因：✅ 已完成，实锤确认**

拉取 NAS 生产 `/v1/admin/refine_runs?stage=l1_conflict`（38 条有效记录，08-29 11:05 – 08-30 17:13）：

- **`PEARSON(total_tokens, memories_count) = 0.968`（n=38）** —— 近乎完全线性；
- 尖峰样本（128,406 / 118,319 / 115,974 tokens）对应 `memories_count = 17 / 16 / 16`；
  低谷样本（13,847 / 13,863 tokens）对应 `memories_count = 1 / 1`；
- 拟合：**单次 token ≈ 7,000（固定开销）+ ~7,500 × 新记忆数**，装箱上限约 17 条 → 上限约 12.8 万。

**结论：文档推测正确**——尖峰是「多记忆大批次装箱」的正常行为，**非异常，无需止血**。
补充一点给运维：若后续要压降，调低装箱上限（17 → 12）即可把中位从 ~7 万压到 ~9 万上限、中位约 5 万。

**② `fallback=skip_conflict` 验证：⚠️ 逻辑有测试覆盖，但生产不可观测**

- 代码路径确认（`sgme/engine/l15.py:309-312`）：预筛降级 → 清空候选 → `resolve_conflicts` 短路
  → 新记忆直接 store，**不调 LLM、不丢数据**；
- 测试覆盖充分：`tests/test_l15_prescreen.py` 有 3 个用例
  （embed 失败清空候选 / 向量检索异常清空候选 / 短路不调 LLM）；
- **但生产侧无法复验**：当前 embed 正常（`bge-m3`，latency 260ms，`memory_vectors=23,044`），
  无法人为触发降级；
- **更关键的可观测性缺口**：`refine_runs.action_counts` 里的 **`skip` 与 LLM 判定「无变化 skip」
  同名**（实测 60 run 中 32 run 有 skip、共 102 次），而预筛降级**不写 `variant`、不写独立标记**
  → **降级真的发生时，事后也识别不出来**。

**建议**：补一个独立可观测标记（如 `action_counts["prescreen_skipped"]` 或 `variant="skip_conflict"`），
成本一行代码，换运维能一眼看到降级频次。建议登记为独立小任务（编号建议 T-132）。

---

## 五、建议的执行顺序（v0.3）

```
【新增·前置】T-127 检索查询侧停用词过滤 + 英文分词适配   ← B2，中英共有缺陷，独立可交付
【新增·前置】T-128 LoCoMo 数据落地与格式解析             ← B1，数据已下载，需解析/切片/冒烟集构造
      ↓
T1-1a 离线检索基线（沿用现有 eval 框架，分钟级，先有护栏）
T1-1b 在线灌库通路 + GT 覆盖率验证（1 conversation 冒烟）
      ↓
T1-1 正式基线（10 conversation，recall@k + J-score 双口径落盘）
      ↓
T2-1a 结构边（纯 SQL 零 token）→ T2-2a 图召回 v1 + A/B
      ↓
【止损点】A/B 无增益 → 重评估 T2-1b / T2-3
      ↓
T2-1b 语义边 + T2-3 三元组 → T2-2b 图召回 v2 + A/B
      ↓
T3-x 治理补齐
```

**登记建议**：审查通过后按项目规范登记 Backlog。已确认**当前最大 T 编号为 T-126**
（2026-08-30 关闭），可用编号自 **T-127** 起；本方案建议登记为 **v1.2+ Epic**。

---

## 附录：实证原始数据

### A. 生产环境（2026-08-31 01:28 实测）

```
GET /v1/health  (192.168.10.10:9910)
  version = 1.1.3    status = ok
  vector  = {"available": true, "engine": "sqlite-vec",
             "memory_vectors": 23044, "scene_vectors": 587,
             "connectivity": {"provider": "local", "model": "bge-m3", "latency_ms": 260}}
```

### B. `l1_conflict` token 分布（38 条有效记录）

```
min = 13,847   p50 = 71,388   max = 128,406   n = 38
PEARSON(total_tokens, memories_count) = 0.968
status=error 2 条（token=0，LLM 瞬时故障，对应文档所述 drop_batch）
```

### C. 各 stage action_counts（近 60 run）

```
l1_conflict    n=60  skip_runs=32  actions={"store":190, "skip":102, "update":101, "merge":98}
l1_extraction  n=60  skip_runs=0   actions={}
tier0_summary  n=29  skip_runs=0   actions={}
l2_scene       n=60  skip_runs=0   actions={"update":105, "create":13, "merge":2}
```

### D. LoCoMo 数据

```
来源：https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
本地：D:\GitHubDownloads\LoCoMo\data\locomo10.json（2,805,274 B）
samples=10  QA_TOTAL=1986  categories={1:282, 2:321, 3:96, 4:841, 5:446}
剔除 adversarial(5) → 1540
```

### E. 源码事实核对（文档断言 vs 实测）

| 文档断言 | 实测 | 结论 |
|---|---|---|
| eval 框架已齐（12 模块） | 12 个 .py（含 `__init__`/`__main__`/ab/embed_cache/loader/metrics/models/reporter/retrieval_gt/rrf/run/runner） | ✅ |
| metrics.py 无 recall@k | 函数清单仅 L1 F1 / strict match / subsidiary acc / l2 hitrate / inject，无 recall@k | ✅ |
| `META_RRF_K=60` @ `search.py:79` | 确认在 `sgme/operations/search.py:79` | ✅ |
| cases 仅 v001_baseline + v001_sample | 确认（`eval/cases/` 两个文件） | ✅ |
| `superseded_by` 归档链可用 | `memory_archive.superseded_by` 存在，`find_by_superseded_by` 已有 | ✅ |
| `scene_memories` 共现可用 | 表存在（db.py:147），有 `idx_scene_memories_memory` | ✅ |
| 提炼提示词全中文 | `prompts/l1_extraction.txt` 确认全中文 + 中文维度清单占位符 | ✅ |
| 生产 v1.1.3 / 23,043 向量 | 1.1.3 / 23,044（正常增长） | ✅ |
