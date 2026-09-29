---
name: sgme-operations
description: SGME Gateway 运维与日常操作：健康检查、记忆检索写入、提炼、wiki 知识沉淀、技能索引和安全排障。服务异常或需要执行 SGME 操作时使用。
category: sgme-operations
tags: [skill, sgme, operations, gateway, memory, wiki, refine, troubleshooting]
version: 1.0.0
pattern: auto
uses:
  - sgme
---

# SGME Gateway 运维

## 先判断问题类型

- **记忆**：历史事实、偏好、项目上下文 → `inject` / `search(scopes=["memory"])`；用户要求记住 → `append`，会话结束再异步 `refine_trigger`。
- **Wiki**：手册、设计、经验、踩坑 → 先 `wiki_search` / `wiki_pages`，再 `wiki_page`；稳定经验用 `wiki_page_add` 或 `wiki_page_update` 沉淀。
- **技能**：专业方法 → `skill_search` → `skill_digest` → `skill_get`；普通技能不 materialize。

## 健康检查

1. 调 `health` 或 `GET /v1/health`，记录 `status`、`version`、LLM 可用性和提炼水位。
2. 连接失败时先检查服务发现地址、端口和 `X-API-Key`，HTTP 客户端访问本机服务使用 `trust_env=False`。
3. 服务仍不可达才检查进程/容器和日志；重启前保留日志与数据，不清空数据库。
4. health 正常但技能搜不到时，先检查 `skills.source_dirs`，再执行技能索引自愈/重建端点并查看 `fts_drift_detected`。

## 写入和提炼

- `/v1/append` 的正文首行必须是 `# {ISO8601} {role}`；只提交真实 user/assistant 消息，不把工具回执写成对话。
- 单轮写入保持幂等；批量提炼使用异步入口，按服务端返回的任务状态轮询，429 使用退避，不立即重试。
- 记忆纠错用 `memory_reject` / `memory_unreject`，不直接删除原始会话或记忆归档。

## 安全边界

- Agent Key 只读/写 Agent 端点；技能写入、删除、改名、配置和重建使用 Admin Key。
- 密钥只从环境变量或部署配置读取，不在聊天、技能、日志或仓库中粘贴明文。
- 清库、恢复、迁移属于高风险操作：先备份并核对精确目标，保留可回滚凭证，禁止对原始会话做不可逆删除。
