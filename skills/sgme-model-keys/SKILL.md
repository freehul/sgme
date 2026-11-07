---
name: sgme-model-keys
description: SGME 免费模型 Key 申请指南（Agnes 主链 / 智谱 glm-4-flash-250414 第二级备用 / 硅基流动第三级+向量）。
tags:
  - skill
category: sgme
---

# 免费模型 Key 申请指南（SGME 托底）

> 适用：新部署 SGME 且未配置模型 Key 的用户（免费托底）。当用户被告知「模型未配置、需申请免费 Key」时，按本指南转述注册地址与步骤。
> 核实日期：2026-11-07 ｜ 官方口径：wiki.agnes-ai.com / docs.bigmodel.cn / docs.siliconflow.cn + 本机只读 API 实测
> 历史注记：2026-08-29 智谱免费 Key 失效整体移出链（B121）；**2026-11-07 用有效的 `ZHIPU_API_KEY` 接回第二级备用**，模型取 `glm-4-flash-250414`。

## 一句话

三个免费 Key（各约 10 分钟，**全程零充值**）：LLM 提炼用 Agnes `agnes-2.5-flash`（主位），第二级备用智谱 `glm-4-flash-250414`，第三级 + 向量用硅基流动（`THUDM/GLM-4-9B-0414` / `BAAI/bge-m3`）。

## 一、Agnes AI（LLM 提炼主位）

1. 打开 https://agnes-ai.cn 邮箱注册
2. 进入 API 平台/开发者控制台 →「API Keys」→ 创建密钥（格式 `sk-xxx...`，只显示一次，立即复制）
3. 写入 SGME `config/.env`：`AGNESAI_API_KEY=你的Key`

免费说明：agnes-2.5-flash 官方价目表当前 **$0/1M tokens**（输入输出均免费），上下文 512K；免费档有限流，SGME 降级链自动退避/切换。

## 二、智谱开放平台（LLM 第二级备用，2026-11-07 回归）

1. 打开 https://open.bigmodel.cn 手机号注册（国内直连，无需绑卡）
2. 控制台 →「API Keys」→ 新建 → 复制保存
3. 写入 `config/.env`：`ZHIPU_API_KEY=你的Key`

免费说明与限制：
- `glm-4-flash-250414` 官方定价页标「免费」，**128K 上下文 / 16K 最大输出**，无 token 总量上限；实测 0.4–2.4s、无思考、JSON mode 正常、10K 汉字长上下文末尾标记召回成功
- ⚠️ 用 `ZHIPU_API_KEY`（有效）；`ZHIPU_API_KEY_FREE` 实测 `401/1000 身份验证失败`，已失效
- ⚠️ 只有官方标「免费」的模型零费用；`glm-5.x` 等付费模型用同一把 key 会产生真实账单
- ⚠️ **不要入链 `glm-4.7-flash`**：实测首发成功率约 60%、单次 10–53s、`1305` 频繁；且默认思考会把 `max_tokens` 吃光返回空正文。若必须用，显式 `thinking: {"type":"disabled"}` + `max_tokens ≥ 1024`
- 并发上限约 2（3 路出一个 `429/1302`）；链级 `throttle: 0.5 rps` 已覆盖
- `GET /models` 返回 200 可用于健康探测，但列表只列 12 个新模型、不含历史免费模型（列表没有 ≠ 不可调用）

## 三、硅基流动（LLM 第三级 + 向量）

1. 打开 https://cloud.siliconflow.cn 注册
2. 用户中心 → 实名认证 → 个人实名（支付宝刷脸）——**必做**，未实名免费模型报 402
3. 头像 →「API 密钥」→ 新建 → 复制保存
4. 写入 `config/.env`：`SILICONFLOW_API_KEY=你的Key`

免费说明：`THUDM/GLM-4-9B-0414` 免费档（B144：原 DeepSeek-V4-Flash 转付费已移出）；`BAAI/bge-m3` 调用费用 0；向量模型 RPM 2000-10000、TPM 50万-1000万。
⚠️ 2026-11-07 实测本机账户**所有**免费模型（含 bge-m3）返回 `402 余额不足`，属账户实名/余额问题而非模型下架；完成实名后复测。

## 四、生效与验证

- `providers.yaml` 已内置 agnes / zhipu / siliconflow；提炼链 `agnes → zhipu → siliconflow → rule(drop_batch)`
- 改 `config/.env` 后重启 Gateway（`load_env_file()` 用 `setdefault`，已有进程环境变量优先）
- `GET /v1/health` 看 `model_config`；`/v1/search` 验证语义检索
- 未配 Key：LLM 提炼降级 drop_batch、向量降级纯 BM25（系统能跑但提炼不可用）

## 五、排障

| 现象 | 处理 |
|---|---|
| 401 / `1000` | Key 无效或变量名错；`ZHIPU_API_KEY_FREE` 已失效，换新 key |
| 402 | 硅基流动未实名 / 余额不足，完成实名后复测 |
| 429 / `1302` | 账户级并发超限，降并发生效，降级链自动退避 |
| 429 / `1305` | 模型级公共池拥塞，降频无用；退避 / 换模型 / 错峰 |
| 正文为空、`finish=length` | GLM-4.7 系列默认思考吃光 `max_tokens`；换 4-flash 或 `thinking:disabled` |

## 六、时效

免费政策随时调整，以官方价格页为准：Agnes https://wiki.agnes-ai.com/zh-Hans/docs/agnes-25-flash ｜ 智谱 https://docs.bigmodel.cn/cn/guide/start/pricing （配合「模型概览」确认在线）｜ 硅基 https://cloud.siliconflow.cn/models
完整版见 `AI-INSTALL/免费模型Key申请指南.md`；实测细节见 SGME wiki「GLM-4.7-Flash 可用性与限制实测（2026-11-07）」。
