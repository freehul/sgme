# 免费模型 Key 申请指南（SGME 托底）

> 适用场景：新部署 SGME 且未配置模型 Key 的用户（免费托底方案）
> 核实日期：2026-08-22 ｜ 口径：官方文档交叉验证（wiki.agnes-ai.com / docs.bigmodel.cn / docs.siliconflow.cn）

## 为什么需要三个 Key？

SGME 免费运行需要三个免费 Key（每个注册约 5-10 分钟，全程零充值）。LLM 提炼走免费降级链——**三个免费模型按序兜底**，任一个可用即可提炼：

| 能力 | 服务 | 模型 | 费用 | 链位 |
|---|---|---|---|---|
| LLM 提炼 | Agnes AI | agnes-2.5-flash | 当前免费（$0/1M token，官方价目表） | **主位（第一优先）** |
| LLM 提炼 | 硅基流动 | THUDM/GLM-4-9B-0414 | 免费 | 第二优先 |
| 向量检索 | 硅基流动 | BAAI/bge-m3（1024 维） | 调用零费用 | — |

> 链序说明（2026-08-22 用户定；2026-08-29 更新；2026-09-01 更新 B144）：实测 zhipu 慢（38s/次）+ 高峰期 1305 限流风暴，agnes/siliconflow 免费档 1-4s——把快的放前面。**2026-08-29 zhipu 免费 Key 失效，已整体移出降级链（B121），无需再申请智谱 Key**。**2026-09-01 `deepseek-ai/DeepSeek-V4-Flash` 已转付费档，移出降级链（B144），LLM 备用改用免费档 `THUDM/GLM-4-9B-0414`**。**最少只需一个 Key 即可跑提炼**；建议两个都申请，容错最稳。

## 一、Agnes AI（LLM 提炼主位）

1. 打开 https://agnes-ai.cn （国内站）注册账号（邮箱即可）
2. 登录后进入 API 平台/开发者控制台 →「API Keys」→ 创建密钥（格式 `sk-xxxxxxxx...`，只显示一次，立即复制）
3. 把 Key 写入 SGME 的 `config/.env`：`AGNESAI_API_KEY=你的Key`

**免费说明（官方口径）**：
- agnes-2.5-flash 官方价目表当前显示 **$0 / 1M tokens**（输入输出均免费），官方声明「不限期免费开放」
- 上下文窗口 512K、最大输出 65.5K，支持工具调用
- 免费档有限流（npm 限制），SGME 降级链会自动退避/切换，正常提炼场景足够
- Base URL 国内节点 `https://apihub.agnes-ai.cn/v1`（providers.yaml 已内置）

## 二、硅基流动（LLM 第二优先 + 向量检索）

1. 打开 https://cloud.siliconflow.cn 注册
2. 用户中心 → 实名认证 → 个人实名（支付宝扫码刷脸，几分钟）——实名后解锁全部免费模型
3. 右上角头像 →「API 密钥」→ 新建 API 密钥 → 复制保存
4. 写入 `config/.env`：`SILICONFLOW_API_KEY=你的Key`

**免费说明（官方口径）**：
- `deepseek-ai/DeepSeek-V4-Flash` 已转付费档（2026-09-01 B144，勿配置依赖）；LLM 第二优先现用 `THUDM/GLM-4-9B-0414` 免费档；`BAAI/bge-m3` 免费（向量，费用账单显示 0）
- 向量模型限流宽裕：RPM 2000-10000、TPM 50 万-1000 万（按账户维度，非 key）
- 不实名仅影响充值与开发票（纯免费用户无影响）；部分免费模型需实名后全量解锁

## 三、智谱开放平台（已移出降级链，2026-08-29）

> ⚠️ **zhipu 免费 Key 失效，已整体移出 SGME 降级链（B121），providers.yaml 已删该段**——本节仅留档，新用户无需申请智谱 Key。若日后智谱恢复免费档并重新入链，再按官方流程申请即可。

## 四、SGME 配置与生效

- `config/providers.yaml` 已内置 agnes / siliconflow 两个供应商连接（仓库自带）
- 修改 `config/.env` 后**重启 Gateway 生效**
- 验证：`GET /v1/health` 看 `model_config` 字段是否提示缺失；调用 `/v1/search` 看语义检索是否命中
- 未配置任何 Key 时：LLM 提炼降级 `drop_batch`、向量检索降级纯 BM25——系统能跑但提炼不可用，这正是需要申请 Key 的原因

## 五、排障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 | Key 无效 / 未写对 .env | 检查 Key 是否带空格、变量名是否一致 |
| 403 | 未实名调用需实名模型 | 完成实名认证 |
| 429 / 1302 | 触发限流 | SGME 降级链自动退避重试/切换下一链位，降低并发即可 |
| 1305 | 平台过载 | 稍后重试（agnes 已升主位，较少触发） |

## 六、免费政策时效声明

免费政策随时可能调整，落地前以官方「价格/额度」页为准：
- Agnes：https://wiki.agnes-ai.com/zh-Hans/docs/agnes-25-flash （价目表段）
- 硅基流动：https://cloud.siliconflow.cn/models
