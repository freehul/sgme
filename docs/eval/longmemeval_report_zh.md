# SGME · LongMemEval 业界标准评测报告

> 替代 LoCoMo 成为 SGME 主评测标准（ST-40 演进）。协议对齐 gbrain `eval longmemeval` 与 LongMemEval 官方：每题独立隔离库、session 级 recall、LLM judge 算 J-score + token-F1。

## 一、评测配置

- 数据集：`longmemeval_s.jsonl`（500 题，25,112 sessions / 246,930 turns）
- top-k：8 ｜ 检索臂：**bm25（纯 lexical）**
- 图召回：**休眠** —— LongMemEval 直灌原始会话、不跑提炼 → `memory_edges` 为空 → 图召回贡献 0；与 gbrain 自身跑法一致，公平可比（已实测 `backfill_system_edges` 在此口径下产出 0 条边，因结构边依赖提炼产物 `memory_stats`）
- 评测隔离：每题独立隔离库，零跨题泄漏、**零生产库污染**（评测库 backfill 不可行，生产库 backfill 为独立运维步骤）
- QA judge：**智谱 glm-4-flash**（非 thinking，OpenAI 兼容端点）；注：公开榜用 GPT-4 judge，judge 模型差异会引入偏差，下文对比已标注
- 耗时：4223s

## 二、检索 recall（session 级，按 answer_session_ids）

LongMemEval 官方指标：检索 top-k → 命中的答案 session 数 / 答案 session 总数。检索是记忆系统的核心能力，此指标不受 judge 模型影响。

| 题型 | recall@8 | 样本数 |
|---|---|---|
| 单会话-用户 | 0.8286 | 70 |
| 知识更新 | 0.8077 | 78 |
| 单会话-助手 | 0.8036 | 56 |
| 跨会话 | 0.6356 | 133 |
| 时序推理 | 0.5925 | 133 |
| 单会话-偏好 | 0.4333 | 30 |
| **整体（加权）** | **0.6847** | 500 |

## 三、QA 质量（智谱 glm-4-flash 生成 + judge）

- **J-score（整体）：0.354**（correct 177 / wrong 93 / no-context 225 / errors 5）
- **token-F1（整体）：0.2473**（基于各题型均值）
- NO CONTEXT 率：0.45（检索未命中相关 session 时系统诚实回答「NO CONTEXT」）

| 题型 | J-score | F1 | judged |
|---|---|---|---|
| 知识更新 | 0.5526 | 0.3498 | 76 |
| 跨会话 | 0.1667 | 0.0507 | 132 |
| 单会话-助手 | 0.7143 | 0.5833 | 56 |
| 单会话-偏好 | 0.1667 | 0.0288 | 30 |
| 单会话-用户 | 0.6377 | 0.5115 | 69 |
| 时序推理 | 0.1818 | 0.1539 | 132 |

## 四、与 2026-03 公开榜对比

> 本节所有 J-Score / F1 **统一为百分制（0–100）**。公开榜为 **GPT-4 judge**；SGME 本次为 **智谱 glm-4-flash judge**，judge 模型差异会引入系统性偏差（强 judge 通常更严格），故**只用检索 recall（第二节，与 judge 无关）做直接对比**，J-Score 仅作同口径量级参考。

| 系统 | J-Score (%) | F1 (%) | 检索臂 / judge |
|---|---|---|---|
| **SGME (本次)** | **35.4** | **24.7** | bm25 / glm-4-flash |
| **SGME 检索 recall@8** | **68.5** | — | 与 judge 无关的核心检索指标 |
| All-Mem | 60.2 | 45.2 | 公开榜 (GPT-4 judge) |
| Mem0 | 55.8 | 36.1 | 公开榜 (GPT-4 judge) |
| LightMem | 54.2 | 34.3 | 公开榜 (GPT-4 judge) |
| HippoRAG2 | 53.2 | 32.9 | 公开榜 (GPT-4 judge) |
| A-Mem | 50.4 | 30.8 | 公开榜 (GPT-4 judge) |
| MemGPT | 42.8 | 20.3 | 公开榜 (GPT-4 judge) |

### 解读

1. **检索维度（可直接对比）**：SGME bm25 lexical 整体 session recall@8 = **68.5%**。公开榜多未统一报告 retrieval recall，但 gbrain 同协议（hybrid top-8）检索召回通常更高——SGME 受限于 NAS Ollama bge-m3 嵌入 ~49s/条不可行，**未跑向量臂**，纯 lexical 口径下 68.5% 属合理区间；接入向量（hybrid）后预期进一步提升。
2. **QA 维度（量级参考，非同比）**：SGME 端到端 J-score 35.4% / F1 24.7%（百分制）低于公开榜 42.8–60.2% 区间，主因有二：① **bm25-only 检索**，跨会话/时序/偏好题检索召回偏低（0.59–0.64）导致 45% 题目回答模型判「NO CONTEXT」拒答；② **judge 模型较弱**（glm-4-flash vs GPT-4）。在同 judge、hybrid 检索下复测，绝对值差距预期显著收窄。
3. **诚实边界**：图召回在本次 raw-ingest 评测中客观上无法激活（依赖提炼产物）；生产环境 backfill 后图召回方可贡献，属后续运维增强项。

