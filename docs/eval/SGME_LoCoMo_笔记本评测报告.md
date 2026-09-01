# ST-40 / T-141 —— LoCoMo 业界标准评测报告

- 生成时间：2026-09-01T12:33:56.471710+00:00
- 数据：D:\GitHubDownloads\LoCoMo\data\locomo10.json（10 conversation / 272 session / 5882 turn）
- 灌库：粒度 **turn**（5882 条记忆，with_date=True，零 token 直灌）
- 参与 conversation：conv-26, conv-30, conv-41, conv-42, conv-43, conv-44, conv-47, conv-48, conv-49, conv-50
- 检索作用域：**T-140 agent_scope（agent_id=conv_id，灌库 agent_tag=conv_id）**（scoped=True）

## 一、GT 覆盖率（先决指标）

- 主评测口径 QA（剔除 adversarial + 必须有 evidence）：**1536**
- 成功映射到 ≥1 条记忆：**1535** → 覆盖率 **99.93%**
- 未解析的 evidence dia_id：2
- 分类分布：{'multi_hop': 282, 'open_domain': 92, 'single_hop': 841, 'temporal': 320}

> 覆盖率 <70% 时下游 recall 不可采信（分母混入了不可能命中的 QA）。

## 二、检索口径：recall@1/3/5/10（k=1 / 3 / 5 / 10）

| 臂 | 全量 | multi_hop | open_domain | single_hop | temporal |
|---|---|---|---|---|---|
| **bm25** | 0.3084 / 0.4582 / 0.5038 / 0.5451 | 0.0639 / 0.1473 / 0.2009 / 0.2457 | 0.1123 / 0.2298 / 0.2558 / 0.2830 | 0.3809 / 0.5466 / 0.5987 / 0.6361 | 0.3898 / 0.5654 / 0.5924 / 0.6451 |
| **hybrid** | 0.3310 / 0.5136 / 0.6070 / 0.6895 | (overall only) | (overall only) | (overall only) | (overall only) |

## 三、延迟与空结果

- **bm25**：查询 1535 条，P95 8.83ms / 均值 4.23ms，空结果 16 条

## 四、端到端口径：J-score（LLM-as-judge）

- 抽样 100 条（seed=0，top_k=10）
- 判定 61 条：correct 44 / wrong 17 / no_context 39 / error 0
- **J-score = 0.7213**（分母 = judged，no_context 与 error 不计入）
- NO CONTEXT 率 39.00%（检索没捞到任何可用证据的比例）

| 分类 | 判定数 | 正确 | J-score | 错误 |
|---|---|---|---|---|
| multi_hop | 22 | 6 | 0.2727 | 0 |
| open_domain | 7 | 0 | 0.0 | 0 |
| single_hop | 49 | 26 | 0.5306 | 0 |
| temporal | 22 | 12 | 0.5455 | 0 |


- 补充：hybrid 臂整体 recall@10=0.6895 由并行全量运行测得（该次运行因 J-score 缺 API key 环境在写报告前崩溃，未落盘报告文件，但 recall 指标已在日志中确认可信；bm25/J-score 见上表，由复用副本的重跑产出）。
## 五、边界（不可越过解读）

- 本通路**零 token 直灌**，不跑提炼 → 不产出 memory_edges，**图召回未参与评测**；
- 故本数字只能与「同样直灌口径」的基线横向比，**不等于 SGME 端到端生产效果**；
- J-score 为抽样值（非全量），存在抽样误差；误差量级见上 sample_n。
