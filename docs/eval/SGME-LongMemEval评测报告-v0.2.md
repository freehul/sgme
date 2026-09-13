# SGME · LongMemEval 评测报告 v0.2

> 2026-09-11 ｜ 数据来源：笔记本评测台（本仓 `eval/`）实跑记录，逐题 checkpoint 可溯源
> 上一版：`docs/eval/longmemeval_report_zh.md`（v0.1，2026-09-02，原件保留）
> 配套：`docs/eval/README.md`（评测台用法与口径）、`docs/eval/longmemeval_refined_cost_v0.1.md`（refined 全量成本模型）

---

## 一、结论速览

1. **已完成的是「直灌臂」500 题全量**（2026-09-10）：会话原样入库 + bm25/向量混合检索，**不经过 L1→L1.5 提炼链路** → 它度量的是**检索台架上限**，不是 SGME 生产链路的成绩。
   - 检索 recall@8（加权，500 题）= **0.9608**；QA J-score = **0.654**（对 327 / 错 66 / 无上下文 107）
2. **走完整提炼链路的「refined 臂」目前只跑过小样**（最多 2 题）：修复召回映射缺陷后 **2/2 命中、J-score 1.0**，n=2 不构成统计证据。
3. **因此本报告的主结论仍未落地**：代表 SGME 生产能力的数字，需要 refined 臂跑满 500 题全量才有。现有 500 题数字只能作**参照上界**。
4. 口径上最重要的一条发现：**recall@8 已到 0.96，却有 21.4% 的题「拿不到可用上下文」**——瓶颈已从「召不回会话」转移到「召回了会话但答不出内容」（聚合/时序推理能力），这与 T-149 的靶心一致。

---

## 二、评测口径

| 项 | 设定 |
|---|---|
| 基准 | LongMemEval（Chen et al., ICLR 2025），`longmemeval_s.jsonl` |
| 规模 | 500 题 / 25,112 sessions / 246,930 turns / 278.0 MB（本机独立复核：278,025,796 字节、500 个 `question_id`、246,930 次 `role`）/ 单题均值 556 KB（十进制，= 543 KiB，≈154K token） |
| 题型 | single-session-user 70、single-session-assistant 56、single-session-preference 30、multi-session 133、temporal-reasoning 133、knowledge-update 78 |
| 隔离 | **每题独立隔离库**（重建 + 只灌本题 haystack），零跨题泄漏、零生产库污染 |
| 检索 | top-k = 8；recall@8 = 命中答案 session 数 / 答案 session 总数（按基准自带 `answer_session_ids`），**与 judge 模型无关，可与公开榜间接对照** |
| QA | LLM 生成答案 + LLM 判分（J-score，0/1/2 归一）+ token-F1；本次判分模型 **DeepSeek `deepseek-v4-flash`**（v0.1 用智谱 `glm-4-flash`） |
| 臂定义 | `hybrid` = **直灌**（原始会话入库 + bm25/向量混合检索）；`refined` = **走生产链路**（`append_l0` → `refine_one`：L1 提炼 + L1.5 冲突裁决 + 场景/边） |
| 图召回 | 休眠（直灌无 `memory_edges` → 贡献 0；与 gbrain 原始灌入跑法一致，保证可比性） |

> ⚠️ **可读性提醒**：`eval/results/` 下的 run 名都叫 `t150_*`，但 Backlog 里的 T-150 是「生产覆盖层 `llm.yaml` 过时」，与本评测无关。建议后续按内容命名（如 `lme500_direct_0910`）。

---

## 三、结果

### 3.1 直灌臂 500 题全量（run `t150_laptop_full`，2026-09-10 15:05 → 17:38）

- 配置：`--arms hybrid --limit 500 --qa --qa-mode legacy --judge-model deepseek-v4-flash --top-k 8 --workers 3`，`refine_backend=cloud`（直灌臂不走提炼）
- 收口：**完成 500/500、题级错误 0**，耗时 **9154 s ≈ 2 h 33 min**（含首轮向量端点 500 重试）

**检索 recall@8（session 级）**

| 题型 | recall@8 | 命中/总数 |
|---|---|---|
| single-session-assistant | **1.0000** | 56 / 56 |
| single-session-user | **1.0000** | 70 / 70 |
| knowledge-update | 0.9936 | 77.5 / 78 |
| single-session-preference | 0.9667 | 29.0 / 30 |
| multi-session | 0.9495 | 126.3 / 133 |
| temporal-reasoning | 0.9145 | 121.6 / 133 |
| **加权合计** | **0.9608** | 500 题 |

**QA 质量（DeepSeek 生成 + 判分）**

| 指标 | 值 |
|---|---|
| J-score | **0.654** |
| 判对 / 判错 | 327 / 66 |
| **无可用上下文（NO CONTEXT）** | **107（21.4%）** |
| 异常 | 0 |

| 题型 | J-score | 判分题数 | token-F1 |
|---|---|---|---|
| single-session-assistant | 0.9643 | 56 | 0.7296 |
| single-session-user | 0.8714 | 70 | 0.6858 |
| knowledge-update | 0.7179 | 78 | 0.3228 |
| multi-session | 0.6090 | 133 | 0.2222 |
| temporal-reasoning | 0.4812 | 133 | 0.2037 |
| single-session-preference | **0.3667** | 30 | 0.0378 |

### 3.2 与历史基线对照（同一直灌臂）

| 运行 | 判分模型 | recall@8 | J-score | token-F1 | NO CONTEXT | 耗时 |
|---|---|---|---|---|---|---|
| 2026-09-01 bm25 500 题 | —— | 0.6847 | 0.354 | 0.2473 | 0.450 | —— |
| 2026-09-02 hybrid 500 题（仅检索） | —— | 0.8426 | —— | —— | —— | 2118 s |
| 2026-09-02 hybrid + QA | 智谱 `glm-4-flash` | 0.8426 | 0.384 | 0.2783 | 0.348 | 5203 s |
| **2026-09-10 hybrid + QA** | **DeepSeek `deepseek-v4-flash`** | **0.9608** | **0.654** | —— | **0.214** | 9154 s |

> recall +11.8 pp、J-score +27.0 pp、NO CONTEXT −13.4 pp。**提升的来源尚未做归因实验**（候选：向量端点从 PC 改到笔记本本机、B145 口径修正、评测台代码变更）——在出对外数字前应补一次 A/B 定因，否则不能把这条曲线当"优化效果"引用。

### 3.3 refined 臂（走完整提炼链路）——仅有小样

| run | 日期 | 题数 | 提炼后端 | 并发 | 耗时 | recall@8 | J-score | 说明 |
|---|---|---|---|---|---|---|---|---|
| `longmemeval` | 09-10 23:50 | 2 | local | 1 | 61 s | 0.5（1/2） | —— | **召回映射未修复**，数字不可用 |
| `t150_refined_smoke3` | 09-11 00:27 | 1 | local | 1 | 1690 s | 1.0 | —— | 单题冒烟 |
| **`t150_rerun_verify`** | **09-11 01:16** | **2** | **local** | **2** | **2530 s** | **1.0（2/2）** | **1.0（2/2）** | **映射修复后复验，全对** |

- 提炼模型：笔记本本地 LM Studio（`qwen3.8-9b-distill`，关思考、128K 上下文）——**零云依赖、零 API 费用**。
- 单题耗时量级：**≈1265 s/题/并发**（2 题 2 并发 2530 s）→ 500 题 2~3 并发外推 **≈2~3 天**（与成本模型一致）。
- n=2（且全为 `single-session-user`）**不能作为 refined 优于直灌的证据**，只能证明"链路跑通、召回映射正确"。

### 3.4 全部 run 清点（含未成形的冒烟）

| run | 臂 | 题数/完成 | recall@8 | J-score | 结论 |
|---|---|---|---|---|---|
| `t150_laptop_smoke` | hybrid | 3/3 | —— | 0.0 | 冒烟失败（809 s，QA 全空） |
| `t150_laptop_smoke3` | hybrid | 3/3 | 1.0 | 0.3333 | 可用，2/3 无上下文 |
| `t150_laptop_full` | hybrid | **500/500** | **0.9608** | **0.654** | **本报告主数据（直灌参照）** |
| `longmemeval` | refined | 2/2 | 0.5 | —— | 映射缺陷，作废 |
| `t150_refined_smoke3` | refined | 1/1 | 1.0 | —— | 单题冒烟 |
| `t150_rerun_verify` | refined | 2/2 | 1.0 | 1.0 | 修复后复验通过 |

---

## 四、关键发现

1. **检索/生成剪刀差（最值钱的一条）**：recall@8 = 0.9608 却有 **21.4%** 的题判为「无可用上下文」。说明"召回到会话 ≠ 召回到可答内容"——直灌把整段会话丢给模型，模型找不到答案要点。**继续调检索的边际收益已经很低，该投的是聚合/抽取。**
2. **弱项题型即 T-149 靶心**：`single-session-preference`（J 0.3667 / F1 0.0378）、`temporal-reasoning`（0.4812 / 0.2037）、`multi-session`（0.6090 / 0.2222）三类垫底——正好是需要**跨会话聚合与时序推理**的题型。
3. **时序题检索也最弱**：temporal-reasoning recall@8 0.9145 为六类最低，与"时间锚点未被显式建模"一致（T-149 ⑥ 时序锚点已改，待全量验证）。
4. **观测量缺失**：评测代码**没有 token/费用计量**（日志无 `usage` 字段），成本只能靠数据集体量外推（QA 500 题 ≈ 输入 12.7M token ≈ **¥15–30**）。建议加一次埋点，此后每次评测自动出账。
5. **稳定性观察**：09-10 15:05 首轮批量嵌入全线 `500 Internal Server Error`（`http://localhost:1014/v1/embeddings`）后自动恢复——建议评测前加 embed 端点 preflight 探活，避免长跑空转。

---

## 五、局限与可信度边界（必须随数字一起引用）

1. **主结论缺口**：代表 SGME 生产链路的 refined 臂**未跑全量**，仅 2 题；本报告的 500 题数字来自直灌臂。
2. **判分模型不可比公开榜**：公开榜用 GPT-4 判分，本次用 `deepseek-v4-flash`（v0.1 用 `glm-4-flash`）→ **J-score 绝对值不可跨判分模型比较**；跨版本对照请看 **recall@8**（judge 无关）。
3. **单次运行、无重复测量**：未做方差估计；0.8426 → 0.9608 的跳变未归因。
4. **嵌入模型非最优**：`bge-m3-legal-euro-r7` 是法律/欧语微调，用于英文对话略吃亏（换成未微调 bge-m3 应略高）。
5. **小样本不得外推**：refined 的 2 题全对不能说明整体提升。

---

## 六、下一步（建议顺序）

1. **refined 臂 500 题全量**（唯一能补齐主结论的动作）：断点续跑与题级并发已具备（B146）；成本与工期见 `longmemeval_refined_cost_v0.1.md`——本地提炼 ¥0 / ≈2~3 天（2~3 并发）；免费云链 ≈8.5~12.5 天且**有降级成"无提炼"静默作废的风险**，不建议。
2. **`no_context` 107 题专项**：逐题看召回内容与答案要点差在哪（会话太长？要点在会话深处？），这是提升 J-score 最短的路径。
3. **给评测台加 token/费用埋点**：消除成本估算的不确定性。
4. **补一次归因 A/B**：定位 09-02 → 09-10 的 +11.8 pp 究竟来自哪里，再决定要不要写进对外材料。

---

## 附录 A：复现命令

> 说明：早期启动脚本未留档（`tmp/t150_*.bat` 已不在），以下为按 run 报告字段（臂/题量/top-k/并发/判分模型）与运行日志复原的**等效命令**。

```bash
# 直灌臂 500 题全量（本报告主数据）
python -m eval.longmemeval_eval --arms hybrid --limit 500 --qa \
    --qa-mode legacy --judge-model deepseek-v4-flash --top-k 8 --workers 3 \
    --run-id t150_laptop_full --output eval/results/t150_laptop_full

# refined 臂（走完整提炼链路；本地提炼、断点续跑）
python -m eval.longmemeval_eval --arms refined --refine-backend local --workers 2 \
    --run-id lme500_refined --resume --output eval/results/lme500_refined
```

## 附录 B：材料索引

| 材料 | 位置 |
|---|---|
| 直灌臂 500 题报告 / 逐题记录 | 笔记本 `eval/results/t150_laptop_full/longmemeval_report.{json,md}`、`checkpoint.jsonl`（501 行） |
| 直灌臂运行日志 | `tmp/laptop_t150_full.log`（PC 本地留档） |
| refined 复验报告 | 笔记本 `eval/results/t150_rerun_verify/` |
| 成本与工期模型 | `docs/eval/longmemeval_refined_cost_v0.1.md` |
| 评测台用法/口径 | `docs/eval/README.md` |
| 上一版报告（v0.1） | `docs/eval/longmemeval_report_zh.md`（2026-09-02，原件保留） |

## 术语

| 术语 | 释义 |
|---|---|
| 直灌臂 / `hybrid` | 原始会话直接入库 + bm25/向量混合检索，不跑提炼 |
| refined 臂 | 走 SGME 生产链路：L0 原始层 → L1 提炼 → L1.5 冲突裁决/场景 |
| recall@8 | 前 8 条检索结果覆盖答案 session 的比例（judge 无关） |
| J-score | LLM 判分归一化得分（对=1、部分=0.5、错=0） |
| token-F1 | 答案词元级 F1 |
| NO CONTEXT | 模型明示"给定上下文里没有答案"的题占比 |
