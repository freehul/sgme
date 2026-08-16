# SGME v1.0.0b2（beta）— 拾光记忆引擎

给 AI 装上长期记忆——它记得你们聊过的每一件事，还会主动关心你。

> b1 → b2 迭代：知识库渐进式披露、提炼成本治理、dsh-sgme 插件 0.2.0、Docker 部署固化、适配器收敛。

## 本版重点

- **dsh-sgme 插件 0.2.0**：`/sgme status` 连接自检 + 插件启动探测（装错/没装本体立即提示）+ README 装本体双路径指引（Docker 一键 / Python venv）+ 故障排查表
- **wiki 渐进式披露共享知识库**（W1-W7）：技能/手册/经验统一入 wiki_pages，多 agent 共享；自进化 evolve 管线——会话踩坑由 LLM 提炼后自动回写手册
- **提炼成本治理**：L1.5 向量预筛 + 熔断，l1_conflict 单次成本降 98%（全量召回 67-100 万 tokens → 约 4 万）
- **提炼 key 隔离**：提炼链接线至专用 `DEEPSEEK_API_KEY_SGME`，与 Hermes/DSH 用量彻底分开，可按 key 归因

## 变更清单

- **插件（dsh-sgme）**：0.1.0 → 0.2.0，新增 status 自检、启动探测、装本体指引、去品牌化（ST-22 提供商无关）
- **知识库**：wiki_pages 目录/wiki_page 全文/自进化写回三大工具，skill 分类 + description 摘要 + supersession
- **Docker**：多阶段镜像（WebUI 入镜像）+ entrypoint 首次启动物化 sgme.yaml + NAS 部署模板入 git
- **记忆去重**：content 清理 + memory_sources 唯一约束（幂等写入防重复记忆）
- **适配器收敛**：官方只维护 hermes + dsh，删除 reasonix/trae/workbuddy（走 MCP/自研）
- **安全**：append 写入 L0 前脱敏明文密钥（sk-/ark-/sgme_*）
- **成本治理**：prescreen 熔断（fallback=skip_conflict）+ 动态链采样参数继承

## 测试

- pytest：config/provider 相关 154 passed / 0 failed（去品牌化后）
- vitest：dsh-sgme 103 passed（9 文件）

## 部署形态

- **Docker**：`docker compose up -d --build`（多阶段镜像 + healthcheck）
- **NAS（群晖）**：bind mount + env_file 密钥注入，生产实例已跑 v1.0.0b2
- **DSH 插件**：`dsh plugin add dsh-sgme`（npm 0.2.0）

## 说明

- **版本**：`1.0.0b2` beta——公开测试版，接口契约可能调整。
- **许可证**：MIT（© 2026 freehul）
- 快速开始详见 [README.md](README.md) 与 [docs/runbook.md](docs/runbook.md)。