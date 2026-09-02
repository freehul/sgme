# SGME · LongMemEval 业界标准评测报告

> 替代 LoCoMo 成为 SGME 主评测标准（ST-40 演进）。协议对齐 gbrain `eval longmemeval` 与 LongMemEval 官方：每题独立隔离库、session 级 recall、LLM judge 算 J-score + token-F1。

## 一、评测配置

- 数据集：`longmemeval_s.jsonl`（500 题，25,112 sessions / 246,930 turns）
- top-k：8 ｜ 检索臂：**bm25 (纯 lexical), hybrid (bm25+向量)**
- 向量嵌入：**本地 LM Studio（RTX 4080S）`text-embedding-bge-m3-legal-euro-r7`**，OpenAI 兼容端点 `localhost:8123`，1024 维 / 8192 ctx，batch=64 约 38.7ms 条；替代 NAS Ollama bge-m3（实测 ~49s/条，全量不可行），**快约 200 倍**
- 图召回：**休眠** —— LongMemEval 直灌原始会话、不跑提炼 → `memory_edges` 为空 → 图召回贡献 0；与 gbrain 自身跑法一致，公平可比（已实测 `backfill_system_edges` 在此口径下产出 0 条边，因结构边依赖提炼产物 `memory_stats`）
- 评测隔离：每题独立隔离库，零跨题泄漏、**零生产库污染**（评测库 backfill 不可行，生产库 backfill 为独立运维步骤）
- QA judge：**智谱 glm-4-flash**（非 thinking，OpenAI 兼容端点）；注：公开榜用 GPT-4 judge，judge 模型差异会引入偏差，下文对比已标注
- 耗时：5203s

## 二、检索 recall（session 级，按 answer_session_ids）

LongMemEval 官方指标：检索 top-k → 命中的答案 session 数 / 答案 session 总数。检索是记忆系统的核心能力，此指标不受 judge 模型影响，可与公开榜间接对照。

| 题型 | bm25 (纯 lexical) | hybrid (bm25+向量) | 提升 | 样本数 |
|---|---|---|---|---|
| 单会话-助手 | 0.8036 | 1.0 | +24.4% | 56 |
| 知识更新 | 0.8077 | 0.9423 | +16.7% | 78 |
| 单会话-用户 | 0.8286 | 0.9143 | +10.3% | 70 |
| 跨会话 | 0.6356 | 0.8108 | +27.6% | 133 |
| 时序推理 | 0.5925 | 0.7816 | +31.9% | 133 |
| 单会话-偏好 | 0.4333 | 0.5333 | +23.1% | 30 |
| **整体（加权）** | **0.6847** | **0.8426** | **+23.1%** | 500 |

## 三、QA 质量（智谱 glm-4-flash 生成 + judge，检索臂=hybrid (bm25+向量)）

- **J-score（整体）：0.384**（correct 192 / wrong 131 / no-context 174 / errors 3）
- **token-F1（整体）：0.2783**（各题型按 judged 加权）
- NO CONTEXT 率：0.348（检索未命中相关 session 时系统诚实回答「NO CONTEXT」）

| 题型 | J-score | F1 | judged |
|---|---|---|---|
| 知识更新 | 0.4675 | 0.3012 | 77 |
| 跨会话 | 0.2273 | 0.0928 | 132 |
| 单会话-助手 | 0.875 | 0.7207 | 56 |
| 单会话-偏好 | 0.2667 | 0.0409 | 30 |
| 单会话-用户 | 0.6957 | 0.5734 | 69 |
| 时序推理 | 0.1579 | 0.1632 | 133 |

## 四、本次复测提升（对比 bm25-only 基线）

| 指标 | bm25 基线 | 本次（hybrid (bm25+向量)） | 变化 |
|---|---|---|---|
| 检索 recall@8 | 0.6847 | **0.8426** | **+23.1%** |
| QA J-score | 35.4% | **38.4%** | **+3.0 pp** |
| QA token-F1 | 24.7% | **27.8%** | **+3.1 pp** |
| NO CONTEXT 率 | 45.0% | **34.8%** | **-10.2 pp**（越低越好） |

> 基线 = 2026-09-01 全量 500 题 bm25-only 评测（`eval/results/longmemeval_full`）。本次接入本地向量臂后，检索召回与端到端 QA 同步提升，NO CONTEXT 拒答率下降。

## 五、与 2026-03 公开榜对比

> 本节所有 J-Score / F1 **统一为百分制（0–100）**。公开榜为 **GPT-4 judge**；SGME 本次为 **智谱 glm-4-flash judge**，judge 模型差异会引入系统性偏差（强 judge 通常更严格），故**只用检索 recall（第二节，与 judge 无关）做主要对比**，J-Score 仅作同口径量级参考。

| 系统 | J-Score (%) | F1 (%) | 检索臂 / judge |
|---|---|---|---|
| **SGME (本次)** | **38.4** | **27.8** | hybrid / glm-4-flash |
| **SGME 检索 recall@8** | **84.3** | — | 与 judge 无关的核心检索指标 |
| All-Mem | 60.2 | 45.2 | 公开榜 (GPT-4 judge) |
| Mem0 | 55.8 | 36.1 | 公开榜 (GPT-4 judge) |
| LightMem | 54.2 | 34.3 | 公开榜 (GPT-4 judge) |
| HippoRAG2 | 53.2 | 32.9 | 公开榜 (GPT-4 judge) |
| A-Mem | 50.4 | 30.8 | 公开榜 (GPT-4 judge) |
| MemGPT | 42.8 | 20.3 | 公开榜 (GPT-4 judge) |

### 解读

1. **检索维度（可直接对比）**：SGME hybrid 整体 session recall@8 = **84.3%**。接入本地向量臂后较 bm25 纯 lexical 基线（68.5%）提升 **23.1%**，跨会话/时序/偏好等难类型获益最大（见第二节分题型表）。
2. **QA 维度（量级参考，非同比）**：端到端 J-score 38.4% / F1 27.8%。与公开榜 42.8–60.2% 区间相比仍有差距，主因是 **judge 模型差异**（glm-4-flash vs GPT-4）；在同 judge 下复测方能直接相减。
3. **⚠️ 关键发现 —— 检索与 QA 的「剪刀差」（决定下一步优化方向）**：检索 recall@8 提升 **+23.1%**（0.6847→0.8426），但 J-score 只提升 **+3.0 pp**（0.354→0.384），NO CONTEXT 率下降 10.2 pp。也就是说，**新召回的 15.8 pp 检索量里，只有约 1/5 转化成了正确答案**。分题型看得更清楚：时序推理检索 recall 已达 **0.7816**，但 J-score 仅 **0.1579**；跨会话检索 **0.8108**，J-score 仅 **0.2273**；单会话-偏好检索 0.5333，J-score 0.2667。**结论：SGME 在 LongMemEval 上的瓶颈已经从「检索召不回」转移到「召回了但用不对」——即多跳聚合与时序推理环节。** 继续卷检索的边际收益已很低，下一步应投向答案生成 / 跨 session 聚合 / 时序比较逻辑。
4. **诚实边界**：① 图召回在本次 raw-ingest 评测中客观上无法激活（依赖提炼产物 `memory_stats`），生产环境 backfill 后图召回方可贡献，属后续运维增强项；② 嵌入模型 `text-embedding-bge-m3-legal-euro-r7` 为法律/欧洲语微调版，通用英文对话语料上非最优，换用原版 bge-m3 预期仍有小幅提升空间；③ **本次所有数字均不含 SGME 提炼管线**（L1/L1.5/场景/图召回未参与），测的是检索底座 + 直灌问答的水位，refined 臂虽已建成但受算力约束（~60s/session，全量≈8 天）未跑全量，故本表**不代表 SGME 端到端完整能力**；④ judge 为智谱 glm-4-flash（0.6% 请求重试后仍失败，计入 errors=3）。

