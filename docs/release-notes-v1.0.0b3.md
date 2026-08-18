# SGME v1.0.0b3（beta）— 拾光记忆引擎

给 AI 装上长期记忆——它记得你们聊过的每一件事，还会主动关心你。

> b2 → b3 迭代：免费模型托底产品化收尾——文档与代码对齐（架构/接口契约/runbook 同步为智谱 GLM-4.7-Flash 主链 + 硅基流动 BAAI/bge-m3 向量托底），版本号升至 b3。

## 本版重点

- **免费双件套全面落地（T-55 收尾）**：提炼主链默认智谱 GLM-4.7-Flash（永久免费、注册零充值），备用 deepseek；向量检索默认硅基流动 BAAI/bge-m3（1024 维，零费用）。Key 缺失时 `/v1/health` 的 `model_config.missing_keys` 给出引导，附 `docs/guide/免费模型Key申请指南.md`
- **文档与代码对齐（文档第一公民）**：架构 v0.9 §24 降级链、§23 向量模型描述、接口契约「两个模型」表、runbook §16.3 全部同步为当前免费托底现状，消除「doc 仍写 doubao/volc/deepseek 主链」的缺陷
- **版本号 1.0.0b2 → 1.0.0b3**

## 变更清单

- **配置（已落地，本版仅文档化）**：`config/providers.yaml` 新增 `zhipu` / `siliconflow` / `nvidia`；`config/llm.yaml` 提炼链 `[zhipu主→deepseek备→rule]`；`config/sgme.yaml` `search.vector` 切 `siliconflow/BAAI/bge-m3`
- **文档**：架构 v0.9（§24 降级链示例+正文、§23 向量模型、§2 核心约束第 9 条）、接口契约 v0.1（向量 Key）、runbook §16.3（search.vector 描述）
- **版本**：`pyproject.toml` / `sgme/__init__.py` / `sgme/operations/health.py` / `sgme/server/app.py` 及对应测试断言升至 `1.0.0b3`

## 测试

- pytest：health/server 版本断言 6 文件 +1.0.0b3 全绿；config/provider 相关模块绿
- 启动 `python -m sgme` → `/v1/health` 报 `version 1.0.0b3`、向量 `available:true`（siliconflow/BAAI/bge-m3）

## 部署形态

- **Docker**：`docker compose up -d --build`（多阶段镜像 + healthcheck）
- **NAS（群晖）**：bind mount + env_file 密钥注入；生产实例升 b3 需重新拉取构建
- **DSH 插件**：`dsh plugin add dsh-sgme`

## 说明

- **版本**：`1.0.0b3` beta——公开测试版，接口契约可能调整。
- **许可证**：MIT（© 2026 freehul）
- 快速开始详见 [README.md](README.md) 与 [docs/runbook.md](docs/runbook.md)。
