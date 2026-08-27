---
name: sgme-key
description: SGME 托底免费模型 Key 申请指南（智谱 GLM-4.7-Flash / 硅基流动 BAAI/bge-m3）。
tags:
  - skill
category: sgme
---

# 免费模型 Key 申请指南（SGME 托底）

> 适用：新部署 SGME 且未配置模型 Key 的用户（免费托底）。当用户被告知「模型未配置、需申请免费 Key」时，按本指南转述注册地址与步骤。
> 核实日期：2026-08-18 ｜ 官方口径：docs.bigmodel.cn / docs.siliconflow.cn

## 一句话

两个免费 Key（各约 10 分钟，**全程零充值**）：LLM 提炼用智谱 GLM-4.7-Flash（永久免费无限调用），向量用硅基流动 BAAI/bge-m3（1024 维，调用零费用）。

## 一、智谱（LLM 提炼）

1. 打开 https://open.bigmodel.cn 手机号注册（国内直连）
2. 控制台 →「API Keys」→ 新建 → 复制保存
3. 写入 SGME `config/.env`：`ZHIPU_API_KEY=你的Key`

免费说明：GLM-4.7-Flash 属官方「免费模型」分类，永久免费、无限调用、不消耗 token 额度；注册送的 token 资源包是给付费模型体验用（与 Flash 无关）；有限流（并发按权益等级，错误码 1302）。

## 二、硅基流动（向量）

1. 打开 https://cloud.siliconflow.cn 注册
2. 用户中心 → 实名认证 → 个人实名（支付宝刷脸）——解锁全部免费模型
3. 头像 →「API 密钥」→ 新建 → 复制保存
4. 写入 `config/.env`：`SILICONFLOW_API_KEY=你的Key`

免费说明：BAAI/bge-m3 调用费用 0；向量模型 RPM 2000-10000、TPM 50万-1000万（宽裕）；不实名仅影响充值/开票。

## 三、生效与验证

- providers.yaml 已内置 zhipu / siliconflow 条目（仓库自带）
- 改 .env 后重启 Gateway；GET /v1/health 看 model_config 提示；/v1/search 验证语义检索
- 未配 Key：LLM 提炼降级 drop_batch、向量降级纯 BM25（系统能跑但提炼不可用）

## 四、排障

| 现象 | 处理 |
|---|---|
| 401 | 检查 Key 与 .env 变量名 |
| 403 | 完成实名认证 |
| 429/1302 | 降级链自动退避，降低并发 |
| 1305 | 平台过载，稍后重试 |

## 五、时效

免费政策随时调整，以官方价格页为准：智谱 https://open.bigmodel.cn/pricing ｜ 硅基 https://cloud.siliconflow.cn/models
