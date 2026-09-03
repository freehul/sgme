# SGME 生产 facts 回填方案与成本测算 v0.1

> 数据快照：2026-09-03 11:35，生产库 `/data/data/memory.db`（NAS 192.168.10.10，v1.1.4）
> 背景：B147 修复 `refine.py` 归一化漏 `facts` 字段后，**新增**记忆可正常产出三元组，但**存量** 26,265 条记忆 `facts_json` 全空 —— T-136 原子事实/符号层精确查询对存量完全不可用。

## 一、结论先行

| 项 | 结论 |
|---|---|
| 回填范围应砍掉 | **rejected 6,369 + expired 717 = 7,086 条（27%）不参与检索，无需回填** → 候选池 26,265 → **19,179** |
| 成本被高估的原因 | 存量记忆是**已提炼的短句**（p50 = 61 字符、mean = 74 字符），全量仅 1.42M 字符 ≈ 0.9M tokens，远低于原始 session 语料 |
| 方法决定成本 | **单条重跑复用 `extract_l1`：全量 ~40 小时 / 8,690 万 tokens**；**批量抽取（20 条/次，需新 prompt + 新代码）：全量 ~3 小时 / 560 万 tokens** —— 差 **15 倍** |
| 推荐 | 批量法 + 推荐层 **L4（static ∩ (episodic\|persona)，10,253 条）**：约 **2.1 小时**、约 **300 万 tokens** |
| 最大外部约束 | LongMemEval 全量 500 题评测正在跑（免费 agnes 档，ETA ~2026-09-12，实测 ~7 RPM）。回填会与其**抢同一免费额度** → 建议要么**排队等评测结束**，要么**改用付费链 glm-4-flash**（约 ¥5，2 小时内完成，互不干扰） |

## 二、生产库基线

| 指标 | 值 |
|---|---|
| memories 总数 | 26,265 |
| status 分布 | active 19,179 / rejected 6,369 / expired 717 |
| memory_type（active） | episodic 8,874 / persona 5,266 / instruction 5,039 |
| time_velocity（active） | static 13,531 / dynamic 5,648 |
| priority（active） | 集中在 p60~p85（p75:3,850、p70:3,543、p80:2,699、p85:2,461、p65:2,000） |
| 内容长度（active） | min 1 / p25 38 / **p50 61** / p75 92 / p90 135 / max 4,826 / mean 74 |
| 内容字符合计（active） | 1,423,582 字符（≈ 0.9M tokens） |
| 创建时间 | 2026-08：26,081 条；2026-09：184 条（存量高度集中于 8 月） |
| agent_tag | 无标签 18,985；hermes 172 / hermes-nas 15 / hermes-soul 4 / hermes-pc 3 |

## 三、分层候选集

| 层 | 定义 | 条数 | 内容字符 |
|---|---|---:|---:|
| L0 | 全量 active | 19,179 | 1.42M |
| L1 | 排除 instruction | 14,140 | 1.11M |
| L2 | 仅 static（稳定事实） | 13,531 | 1.00M |
| L3 | priority ≥ 75 | 11,012 | 0.88M |
| L4 | static ∩ (episodic\|persona) | 10,253 | 0.79M |
| L5 | static ∩ priority ≥ 75 | 8,288 | 0.64M |
| L6 | (episodic\|persona) ∩ priority ≥ 75 | 7,690 | 0.66M |

> 分层逻辑：`instruction` 型（5,039 条）多为「用户偏好/行为准则」，三元组结构化收益低，可降级；`time_velocity=dynamic`（5,648 条）是易变状态，facts 会快速过期，回填性价比低。

## 四、两种执行路径成本对比

**A. 单条重跑（复用现成 `extract_l1`，零开发量）**

| 层 | 调用次数 | tokens | 串行耗时 | workers=2 |
|---|---:|---:|---:|---:|
| L0 全量 active | 19,179 | 87M | 53h | 39h |
| L1 排除 instruction | 14,140 | 64M | 39h | 29h |
| L2 仅 static | 13,531 | 61M | 38h | 28h |
| L3 priority ≥ 75 | 11,012 | 50M | 31h | 23h |
| L4 static ∩ (episodic\|persona) | 10,253 | 46M | 28h | 21h |
| L5 static ∩ priority ≥ 75 | 8,288 | 38M | 23h | 17h |
| L6 (episodic\|persona) ∩ prio ≥ 75 | 7,690 | 35M | 21h | 16h |

**B. 批量抽取（20 条/次，需新增 `facts_batch_extraction` prompt + 回填脚本）**

| 层 | 调用次数 | tokens | 串行耗时 | workers=2 |
|---|---:|---:|---:|---:|
| L0 全量 active | 959 | 5.56M | 4h | 3h |
| L1 排除 instruction | 707 | 4.10M | 3h | 2h |
| L2 仅 static | 677 | 3.92M | 3h | 2h |
| L3 priority ≥ 75 | 551 | 3.19M | 2h | 2h |
| L4 static ∩ (episodic\|persona) | 513 | 2.97M | 2h | 2h |
| L5 static ∩ priority ≥ 75 | 415 | 2.41M | 2h | 1h |
| L6 (episodic\|persona) ∩ prio ≥ 75 | 385 | 2.23M | 2h | 1h |

**测算基准**：

- 单条法参数来自 LongMemEval 微基准实测：prompt 3,582 tok / completion 949 tok / ~10s per call（3.45 calls/session，session mean 42.6s）。
- 批量法估参：模板 tokens 不变（3,582）+ 20 条 × 50 tok 内容；输出 20 × 60 tok；单 call 延迟按 15s 估（待抽样实测校准）。
- 并发加速按 B146 实测 **workers=2 → 1.35x**（agnes 免费档并发降速，非线性）。
- 免费档 RPM 20~30；评测进行中实测仅 ~7 RPM（瓶颈为推理延迟而非节流）。

## 五、关键风险与约束

| 风险 | 说明 | 缓解 |
|---|---|---|
| **与全量评测抢额度** | LongMemEval 500 题正在跑（ETA 2026-09-12），回填会双倍消耗免费额度并互相拖慢 | ① 排队至评测结束；② 或走付费链 glm-4-flash（已配 judge key，约 ¥5） |
| 批量法准确率未知 | 20 条/次的批量抽取是否与单条等价，未经验证 | **50 条抽样门禁**：同批记忆双跑（单条 vs 批量），三元组 F1 ≥ 0.9 才放量 |
| 写入生产库 | 直接 UPDATE `facts_json` | 回填前备份 `memory.db`（已有备份惯例 `/data/backups/memory.db.bak-*`）；**只写 facts_json，不动 content / 向量 / 边**，不触发 L1.5 冲突裁决，风险面极小 |
| 批量法代码位置 | 容器内无 `scripts/` 目录 | 沿用既有范式：`docker exec -i sgme python3 -` + stdin 内联脚本 |

## 六、建议路径

1. **先做 50 条抽样门禁**：写批量抽取 prompt，对同一批记忆分别用单条 `extract_l1` 与批量法产出 facts，比对 F1。
2. **门禁通过后按 L4 层放量**（10,253 条 / ~2.1h / ~297 万 tokens）。
3. **回填脚本必备**：分批提交 + `facts_json` 断点续跑（复用 B146 的 checkpoint 范式）+ 失败条目落单不中断。
4. **执行窗口**：等 LongMemEval 全量评测结束后再跑，或直接切付费链 glm-4-flash 并行。

## 七、待你拍板

| # | 决策点 | 选项 |
|---|---|---|
| 1 | 执行路径 | A 单条重跑（零开发，~40h）/ **B 批量抽取（需开发，~3h）** |
| 2 | 回填范围 | L0 全量 / **L4 精选 10,253 条** / L2 仅 static / 自定义 |
| 3 | 模型与时机 | 免费 agnes 排队等评测结束 / **付费 glm-4-flash 立即跑（约 ¥5）** |
