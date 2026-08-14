# SGME v1.0.0b1（beta）— 拾光记忆引擎

给 AI 装上长期记忆——它记得你们聊过的每一件事，还会主动关心你。

首个公开测试版。单用户 Agent 记忆引擎 Server：把 Agent 会话提炼为标签化记忆，按场景注入画像，让 AI 不再失忆。

## 一句话

你的 AI，从此记得你——跨设备、跨 AI，一个不丢的记忆中枢，还会主动关心你。

## 核心能力

- **主动关怀（Care Engine）**：情绪洞察、待办提醒、过劳预警、每日问候——Dream 定时扫描产生信号 → SSE 实时推送 → agent 原子认领（谁消费谁标记）→ 关怀用户 → 回执。信号总线 + 三层消费模型 + TTL 归档闭环。
- **三库记忆引擎**：L0 原始文件 → L1 标签化记忆（memory.db）→ L1.5 冲突提炼 → L2 场景（wiki.db），单向数据流，全程可溯源。
- **角色模板**：管家 / 伴侣 / 朋友 / 导师四张预置角色卡（Character Card V2 兼容），换皮不换芯——换角色只改「怎么说话」，记忆池不动。
- **记忆可溯源**：AI 说出的每一句话都能追回原始对话。
- **多智能体共享记忆**：Hermes、Trae、Reasonix、DSH 等所有 AI 共享同一个记忆大脑。
- **按场景注入**：日常闲聊带身份近况，编程带技术栈踩坑，工作带计划进度——零 LLM 成本（纯结构化 SQL 查询）。
- **多源统一检索**：BM25 关键词 + 向量语义 + 标签过滤三重融合，记忆池 + 知识库一站式召回。
- **中文原生**：针对中文检索调优，jieba 分词 + FTS5 + LIKE 兜底。
- **自托管轻量**：单机 Python + SQLite，无 GPU、无外部数据库，数据永远在你手里。

## 部署形态

- **Windows 服务**：NSSM 注册，开机自启 + 崩溃自动重启。
- **Docker**：`docker compose up -d --build`（Dockerfile + docker-compose.yml + healthcheck）。
- **NAS（群晖）**：bind mount 数据卷 + env_file 密钥注入，已实测验收。

## 接入方式

- **HTTP API**（:9910）+ **MCP**（:9913）双协议入口，鉴权同规则（X-API-Key）。
- **连接即发现**：agent 连上后调 `agent_onboarding()` 自助接入，无需人工配置。
- **五条接入铁律** + 事件对接（SSE/pull/signal_pull 三接法）+ 通信渠道兜底。

## 技术栈

Python 3.11 · FastAPI · SQLite（FTS5 + sqlite-vec）· LLM 降级链（deepseek-v4-flash 主链）

## 测试

- pytest 全量 1644 passed / 0 failed
- vitest 65 passed（dsh 适配器）

## 说明

- **版本**：`1.0.0b1` beta——公开测试版，接口契约可能调整。
- **许可证**：MIT（© 2026 freehul）
- **合规**：Python 自研实现，仅借鉴 TencentDB-Agent-Memory（MIT）设计思想，未直接引用其代码或提示词。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  /  macOS/Linux: source .venv/bin/activate
pip install -e .[dev]
python -m sgme
```

详见 [README.md](README.md) 与 [docs/runbook.md](docs/runbook.md)。
