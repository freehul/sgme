# SGME v1.0.0（正式版）— 拾光记忆引擎

给 AI 装上长期记忆——它记得你们聊过的每一件事，还会主动关心你。

> b4 → 1.0.0：beta 阶段收官，首个正式版。全链路免费模型化——提炼降级链三免费模型按序兜底，新用户零成本启动；版本检测与自动更新闭环，自升级不再靠人工。

## 本版重点

- **LLM 提炼免费链重构（2026-08-22）**：降级链改为三免费模型按序兜底——Agnes agnes-2.5-flash（主位，当前 $0/1M token，实测 1-4s）→ 硅基流动 DeepSeek-V4-Flash（免费，1-3s）→ 智谱 GLM-4.7-Flash（末位兜底，永久免费但慢 38s/次）。zhipu 从主位移到末位，快模型提速主位
- **新用户免费 Key 引导全面更新**：docs/guide/免费模型Key申请指南.md 重写（三 Key 申请流程 + 链位说明）；health 缺失提示 / agent_onboarding / README 三处文案同步为免费链
- **降级链参数调优**：max_retries 5→2（免费兜底就位后快速切换，不再等 5 次退避 ~2min 才降级）；退避 base 3s / max 60s / jitter 0.5s（1305 过载恢复数十秒级，多扛两轮减少降级）
- **版本检测与自动更新闭环（ST-34）**：GitHub Releases 检测（health 只增字段向后兼容）+ WebUI 提示条/确认弹窗 + 意图文件 + NAS 主机 cron 更新代理（无特权容器，失败自动回滚旧镜像）——NAS 代理已部署并端到端验证

## 变更清单

- **提炼链**：agnes 主位 / siliconflow 第二 / zhipu 末位兜底（原 zhipu 主 + deepseek 付费备用移除）；max_retries 2、退避 3s→60s、jitter 0.5s
- **引导**：免费模型 Key 申请指南重写（agnes/siliconflow/zhipu 三 Key）；MODEL_KEY_MISSING_NOTICE + agent_onboarding requirement + README 模型说明同步
- **自动更新（ST-34）**：update_check 检测模块 / health 4 新字段 / WebUI 提示条与确认弹窗 / 意图文件端点 / sgme-host-updater.sh 主机代理（cron 每 5 分钟轮询，git pull → build → 换 tag → compose up → 健康验证 → 失败回滚）
- **此前 beta 累计**：WebUI 管理面板 27 路由（ST-7）、记忆图谱 D3（ST-13）、wiki 知识库（W1-W7）、创意池/待办池/项目池（ST-14/15/16）、Care Engine 关怀信号（ST-27）、DSH 适配器 20 工具 + SSE 事件订阅、Docker 一键部署（ST-12）、dream 夜间整理（ST-10）、skills-hub 同步（ST-11）、检索术语别名（ST-19）、L1.5 幂等修复、向量多 provider 降级链
- **版本**：`pyproject.toml` / `sgme/__init__.py` / `sgme/operations/health.py` / `sgme/server/app.py` 及对应测试断言升至 `1.0.0`（正式版）

## 测试

- pytest：版本断言 6 文件 + 1.0.0 全绿；提炼链/health/update_check 相关模块全绿
- ST-34 真实冒烟：health 4 新字段 + 意图端点 200 + NAS 主机代理端到端复验通过（写入 pending 意图 → 识别已是最新 → 清请求 exit 0）
- 启动 `python -m sgme` → `/v1/health` 报 `version 1.0.0`

## 部署形态

- **Docker**：`docker compose up -d --build`（多阶段镜像 + healthcheck）
- **NAS（群晖）**：bind mount + env_file 密钥注入；生产实例升 1.0.0 可走自动更新（WebUI「立即更新」→ 主机 cron 代理执行）或手动 `deploy.sh`
- **DSH 插件**：`dsh plugin add dsh-sgme`（npm 0.3.x）

## 说明

- **版本**：`1.0.0` 正式版——接口契约稳定，向前兼容 beta 数据（memory.db/wiki.db 无需迁移）
- **许可证**：MIT（© 2026 freehul）
- 快速开始详见 [README.md](README.md) 与 [docs/runbook.md](docs/runbook.md)
