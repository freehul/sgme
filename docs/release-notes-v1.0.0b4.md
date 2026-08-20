# SGME v1.0.0b4（beta）— 拾光记忆引擎

给 AI 装上长期记忆——它记得你们聊过的每一件事，还会主动关心你。

> b3 → b4 迭代：功能密度最高的一版——记忆关系图谱、search 修复、会话检索、信号治理、DSH 适配器注入三件套、省钱方案全部落地；wiki 升为特色主推。

## 本版重点

- **记忆关系图谱可视化（ST-13）**：记忆不再是一堆孤立条目——维度关系图谱可视化，AI 与你都能一眼看到记忆之间的关联结构
- **search 召回质量修复（T-89）**：RRF 融合后按 content 去重（L1 重复落库稀释注入）+ 结果截断到 limit（两路召回超发），召回更干净
- **会话检索（ST-33）**：`/v1/search` 新增 `sessions` scope——历史会话也纳入统一检索，一个入口查全部
- **信号批量清空（T-87）**：信号历史清理官方通道（SSE 接入前），WebUI + MCP + HTTP 三入口
- **DSH 适配器注入三件套（T-88）**：首句对话内容驱动注入（命中 L2 场景优先注入）+ `inject` 工具（agent 主动按场景注入画像）+ 关怀事件提醒去重防上下文爆炸
- **省钱方案（B96）**：降级链移除 deepseek 备用、退避 60s/重试 5 次、batch_scan 60min——LLM 成本进一步压降
- **向量 embed 多 provider 降级链**：本地 Ollama 优先、云端免费降级，断网/停服自动切换
- **wiki 升级为特色主推**：README 新增「Shared knowledge — a wiki your AIs write together」卖点

## 变更清单

- **图谱**：D3 记忆关系图谱可视化（ST-13）
- **搜索**：召回去重 + limit 截断（T-89）；`sessions` scope（ST-33）；直查 SQL 收口 data 层（T-9）
- **信号**：批量清空端点 + MCP + WebUI（T-87）；`care_todo_due` 推导维度 tasks→goals 修复
- **DSH 适配器**：首句内容驱动注入、inject 工具（T-88）、关怀事件去重注入（B86）；dsh-sgme 0.3.0（20 工具 + SSE 事件订阅）
- **配置**：省钱方案（降级链去 deepseek 备用 + 退避 60s + batch_scan 60min）；`dimensions.boundaries` 加载保留全链路（T-11）
- **运维**：NAS 一键部署脚本 `deploy.sh`（ST-12）；`install.py` 生成 `~/.sgme/install.json` 服务发现清单（T-23）
- **评测**：模板注入检测（T-20）
- **文档**：README 补 wiki 特色主推介绍（中英双语）
- **版本**：`pyproject.toml` / `sgme/__init__.py` / `sgme/operations/health.py` / `sgme/server/app.py` 及对应测试断言升至 `1.0.0b4`

## 测试

- pytest：版本断言 6 文件 +1.0.0b4 全绿；search/signal/adapters 相关模块绿
- DSH 适配器：typecheck + 136 测试通过（rc.6/rc.7/rc.8 兼容矩阵验证）
- 启动 `python -m sgme` → `/v1/health` 报 `version 1.0.0b4`

## 部署形态

- **Docker**：`docker compose up -d --build`（多阶段镜像 + healthcheck）
- **NAS（群晖）**：bind mount + env_file 密钥注入；生产实例升 b4 需重新拉取构建（`sgme:1.0.0b4-nas-*`）
- **DSH 插件**：`dsh plugin add dsh-sgme`（npm 0.3.x）

## 说明

- **版本**：`1.0.0b4` beta——公开测试版，接口契约可能调整。
- **许可证**：MIT（© 2026 freehul）
- 快速开始详见 [README.md](README.md) 与 [docs/runbook.md](docs/runbook.md)。
