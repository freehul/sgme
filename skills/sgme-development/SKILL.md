---
name: sgme-development
description: SGME 引擎开发规范：架构边界、memory/wiki/skills 模块、提炼管线、数据库变更、测试和文档对齐。修改 SGME 代码或设计时使用。
category: sgme-development
tags: [skill, sgme, architecture, python, database, refinery, testing]
version: 1.0.0
pattern: auto
uses:
  - coding-workflow
---

# SGME 引擎开发

## 开始前

先读 SGME 根目录 `AGENTS.md`、当前架构设计和 Backlog；不要凭记忆断言功能是否存在。仓库有 `.codegraph/` 时先用 CodeGraph 了解符号和调用影响。

设计与代码不一致时，先更新设计说明，再改实现；完成后把 Task 标记、测试证据和运行影响写回文档仓。

## 模块边界

```text
adapters → server / mcp_server → operations → engine / data / profile / llm / config
```

- `data/` 是唯一数据库 CRUD 层；禁止入口层直接拼业务 SQL。
- `operations/` 是 HTTP 与 MCP 共用的业务操作层；入口层只做鉴权、参数转换和响应投影。
- `engine/` 编排 L0→L1→L1.5→L2 提炼；`refinery/` 提供知识提炼基础设施。
- `memory` 是事实、偏好、项目上下文与提炼；`wiki` 是长期手册/经验；`skills` 是 git 真源的专业流程。
- 技能唯一来源是 `skills.source_dirs` 中的 `SKILL.md`；wiki 不再桥接为技能。
- `adapters/<host>/` 是适配器真源；`skills/adapter-<host>/` 是可重建的分发副本，不直接手改。

## 常见改动配方

### 新表或字段

按「DDL → 幂等迁移 → DAO → operations → HTTP/MCP 路由 → 恢复 → 测试」闭环实现。不要直接改生产库；迁移必须可重复执行并保留原始数据。

### 新操作或端点

复制 `sgme/operations/health.py` / `inject.py` 的三段式结构：业务函数返回协议无关结果，必要时分别提供 `http_payload` / `mcp_payload`，两入口不互相 import。

### 技能改动

先修改 `skills/<name>/SKILL.md` 真源并验证 frontmatter；新增适配器后运行 `scripts/publish_adapter_skills.py --check`，再发布分发副本。普通技能不批量 materialize。

## 验证

```powershell
$env:PYTHONUTF8 = "1"
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" scripts/adapter_parity.py --strict
git diff --check
```

提炼、适配器、MCP、技能索引改动还要跑对应模块测试；真实 LLM 链路改动必须补真实模型冒烟或明确记录外部依赖未验证。
