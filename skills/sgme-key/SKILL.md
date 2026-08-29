---
name: sgme-key
description: SGME 免费模型 Key 申请指南（Agnes agnes-2.5-flash 主链 / 硅基流动备用+向量）。
tags:
  - skill
category: sgme
---

# 免费模型 Key 申请指南（SGME 托底）

> 适用：新部署 SGME 且未配置模型 Key 的用户（免费托底）。当用户被告知「模型未配置、需申请免费 Key」时，按本指南转述注册地址与步骤。
> 核实日期：2026-08-29 ｜ 官方口径：wiki.agnes-ai.com / docs.siliconflow.cn
> 历史注记：智谱 GLM-4.7-Flash 曾为托底主链，2026-08-29 因免费 Key 失效整体移出降级链（B121），无需再申请智谱 Key。

## 一句话

两个免费 Key（各约 10 分钟，**全程零充值**）：LLM 提炼用 Agnes agnes-2.5-flash（免费主位），备用 + 向量用硅基流动（DeepSeek-V4-Flash 免费 / BAAI/bge-m3 1024 维调用零费用）。

## 一、Agnes AI（LLM 提炼主位）

1. 打开 https://agnes-ai.cn 邮箱注册
2. 进入 API 平台/开发者控制台 →「API Keys」→ 创建密钥（格式 `sk-xxx...`，只显示一次，立即复制）
3. 写入 SGME `config/.env`：`AGNESAI_API_KEY=你的Key`

免费说明：agnes-2.5-flash 官方价目表当前 **$0/1M tokens**（输入输出均免费），上下文 512K；免费档有限流，SGME 降级链自动退避/切换。

## 二、硅基流动（LLM 备用 + 向量）

1. 打开 https://cloud.siliconflow.cn 注册
2. 用户中心 → 实名认证 → 个人实名（支付宝刷脸）——解锁全部免费模型
3. 头像 →「API 密钥」→ 新建 → 复制保存
4. 写入 `config/.env`：`SILICONFLOW_API_KEY=你的Key`

免费说明：`deepseek-ai/DeepSeek-V4-Flash` 免费（LLM 第二优先）；`BAAI/bge-m3` 调用费用 0；向量模型 RPM 2000-10000、TPM 50万-1000万（宽裕）；不实名仅影响充值/开票。

## 三、生效与验证

- providers.yaml 已内置 agnes / siliconflow 条目（仓库自带）
- 改 .env 后重启 Gateway；GET /v1/health 看 model_config 提示；/v1/search 验证语义检索
- 未配 Key：LLM 提炼降级 drop_batch、向量降级纯 BM25（系统能跑但提炼不可用）

## 四、排障

| 现象 | 处理 |
|---|---|
| 401 | 检查 Key 与 .env 变量名 |
| 403 | 完成实名认证 |
| 429 | 降级链自动退避，降低并发 |

## 五、时效

免费政策随时调整，以官方价格页为准：Agnes https://wiki.agnes-ai.com/zh-Hans/docs/agnes-25-flash ｜ 硅基 https://cloud.siliconflow.cn/models
