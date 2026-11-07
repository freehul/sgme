# 免费模型 Key 申请指南（SGME 托底）

> 适用场景：新部署 SGME 且未配置模型 Key 的用户（免费托底方案）
> 核实日期：2026-11-07 ｜ 口径：官方文档 + 本机只读 API 实测（详见 SGME wiki「GLM-4.7-Flash 可用性与限制实测（2026-11-07）」）

## 为什么需要这些 Key？

SGME 免费运行需要三个平台的免费 Key（每个注册约 5-10 分钟，全程零充值）。LLM 提炼走免费降级链——按序兜底，任一个可用即可提炼；向量检索复用硅基流动的免费嵌入模型：

| 能力 | 服务 | 模型 | 费用 | 链位 |
|---|---|---|---|---|
| LLM 提炼 | Agnes AI | agnes-2.5-flash | 当前免费（$0/1M token，官方价目表） | **第一优先（主位）** |
| LLM 提炼 | 智谱 BigModel | glm-4-flash-250414 | 官方定价页标「免费」 | **第二优先（2026-11-07 新增）** |
| LLM 提炼 | 硅基流动 | THUDM/GLM-4-9B-0414 | 免费 | 第三优先 |
| 向量检索 | 硅基流动 | BAAI/bge-m3（1024 维） | 调用零费用 | — |

> **链序说明**：2026-11-07 用户拍板把智谱免费档接回第二级。选 `glm-4-flash-250414` 而非 `glm-4.7-flash` 的依据是本机实测：前者 0.4–2.4s、无思考、JSON mode 正常、10K 汉字长上下文末尾标记召回成功；后者首发成功率约 60%、单次 10–53s、`1305 模型拥塞` 频繁，且默认开启思考会把 `max_tokens` 吃光返回空正文。**4.7-Flash 暂不入链**。
>
> ⚠️ 智谱用 `ZHIPU_API_KEY`（有效）；`ZHIPU_API_KEY_FREE` 实测 401 已失效，勿用。
> ⚠️ 硅基流动账户 2026-11-07 实测所有免费模型（含 bge-m3）返回 `402 余额不足`，属实名/余额问题，完成支付宝刷脸实名后复测。
> 历史：2026-08-29 zhipu 因免费 Key 失效整体移出链（B121）；2026-09-01 DeepSeek-V4-Flash 转付费移出（B144）。

## 一、Agnes AI（LLM 提炼主位）

1. 打开 https://agnes-ai.cn （国内站）注册账号（邮箱即可）
2. 登录后进入 API 平台/开发者控制台 →「API Keys」→ 创建密钥（格式 `sk-xxxxxxxx...`，只显示一次，立即复制）
3. 把 Key 写入 SGME 的 `config/.env`：`AGNESAI_API_KEY=你的Key`

**免费说明（官方口径）**：
- agnes-2.5-flash 官方价目表当前显示 **$0 / 1M tokens**（输入输出均免费），官方声明「不限期免费开放」
- 上下文窗口 512K、最大输出 65.5K，支持工具调用
- 免费档有限流，SGME 降级链会自动退避/切换，正常提炼场景足够
- Base URL 国内节点 `https://apihub.agnes-ai.cn/v1`（providers.yaml 已内置）

## 二、智谱开放平台（LLM 第二优先，2026-11-07 回归）

1. 打开 https://open.bigmodel.cn 手机号注册（国内直连，无需绑卡）
2. 控制台 →「API Keys」→ 新建 → 复制保存
3. 写入 `config/.env`：`ZHIPU_API_KEY=你的Key`

**免费说明（官方口径 + 实测）**：
- `glm-4-flash-250414`：定价页标「免费」，128K 上下文 / 16K 最大输出，无 token 总量上限
- 同一把 key 也能调 `glm-4.7-flash`（200K、免费），但受 1305 拥塞影响，暂不入链
- ⚠️ **只有官方标「免费」的模型零费用**；`glm-5.x` 等付费模型用同一把 key 会产生真实账单
- `GET /api/paas/v4/models` 返回 200 可作健康探测，但该列表只列 12 个新模型、**不含历史免费模型**——列表里没有 ≠ 不可调用
- 并发上限约 2（实测 3 路出一个 `429/1302`）；链级 `throttle: 0.5 rps` 已覆盖

## 三、硅基流动（LLM 第三优先 + 向量检索）

1. 打开 https://cloud.siliconflow.cn 注册
2. 用户中心 → 实名认证 → 个人实名（支付宝扫码刷脸，几分钟）——**必做**，否则免费模型报 402
3. 右上角头像 →「API 密钥」→ 新建 API 密钥 → 复制保存
4. 写入 `config/.env`：`SILICONFLOW_API_KEY=你的Key`

**免费说明（官方口径）**：
- `deepseek-ai/DeepSeek-V4-Flash` 已转付费档（2026-09-01 B144，勿配置依赖）；LLM 备用现用 `THUDM/GLM-4-9B-0414` 免费档；`BAAI/bge-m3` 免费（向量，费用账单显示 0）
- 向量模型限流宽裕：RPM 2000-10000、TPM 50 万-1000 万（按账户维度，非 key）
- 2026-05-15 起未实名账户不能调用平台功能；免费体验名单会调整，以当日价格页为准

## 四、SGME 配置与生效

- `providers.yaml` 已内置 agnes / zhipu / siliconflow 三个供应商连接（仓库自带）
- 提炼链见 `llm.yaml`：`agnes → zhipu → siliconflow → rule(drop_batch)`
- 修改 `config/.env` 后**重启 Gateway 生效**（`load_env_file()` 用 `setdefault`，已存在的进程环境变量优先）
- 验证：`GET /v1/health` 看 `model_config` 是否提示缺失；调用 `/v1/search` 看语义检索是否命中
- 未配置任何 Key 时：LLM 提炼降级 `drop_batch`、向量检索降级纯 BM25——系统能跑但提炼不可用

## 五、排障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 / `1000` | Key 无效 / 未写对 .env | 检查变量名与值；`ZHIPU_API_KEY_FREE` 已失效，换新 key |
| 402 | 硅基流动账户余额不足 / 未实名 | 完成支付宝实名后复测 |
| 429 / `1302` | 账户级并发或 RPM 超限 | 降并发生效；SGME 降级链自动退避/切换 |
| 429 / `1305` | 模型级公共池拥塞（与你的频率无关） | 降频无用；退避重试、换模型或错峰 |
| `max_tokens` 用尽但正文为空 | GLM-4.7 系列默认思考吃光输出预算 | 换 `glm-4-flash-250414`，或显式 `thinking: {"type":"disabled"}` |

## 六、免费政策时效声明

免费政策随时可能调整，落地前以官方「价格/额度」页为准：
- Agnes：https://wiki.agnes-ai.com/zh-Hans/docs/agnes-25-flash
- 智谱：https://docs.bigmodel.cn/cn/guide/start/pricing （配合「模型概览」确认是否仍在线）
- 硅基流动：https://cloud.siliconflow.cn/models
