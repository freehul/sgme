---
name: skill-registry-protocol
description: SGME 三大核心模块路由与技能按需注入协议——先判断 memory/wiki/skills 归属，再检索专业技能，不批量安装、不凭空编造。
tags:
  - skill
  - memory
  - skills
  - wiki
category: sgme
version: 2.0.0
pattern: auto
---

# SGME 核心能力与技能检索协议

## 核心原则：SGME 是能力平面

接入 SGME 后，Agent 不需要安装很多 skill。宿主只需要一个 SGME 适配器或通用 MCP；
记忆、知识和专业流程统一由 SGME 提供，按需读取。

| 任务信号 | 目标模块 | 首选动作 |
|---|---|---|
| 以前、上次、偏好、项目历史、用户事实 | **memory** | `inject` 或 `search(scopes=["memory"])` |
| 手册、设计、经验、踩坑、长期知识 | **wiki** | `wiki_search` / `wiki_pages` → `wiki_page` |
| 框架、工具链、专业流程、领域技能 | **skills** | `skill_search` → `skill_digest` → `skill_get` |

任务跨模块时按顺序：**memory 取上下文 → skills 取方法 → 执行 → wiki 沉淀稳定经验**。

## 何时检索技能

仅当任务需要 SGME 尚未直接提供的专业流程时检索技能，例如特定框架、工具链、部署方式或领域知识。
记忆查询和 Wiki 查询不需要先安装 skill；它们直接走对应核心模块。

## 检索与注入流程（MUST）

1. 调 `skill_search(query)`（MCP）或 `POST /v1/search` `scopes=["skills"]`（HTTP），传入自然语言需求。
2. 先看 `name / description / category / tags`，必要时调 `skill_digest(name)` 查看章节和依赖。
3. 只取需要的节：优先 `skill_get(name, section=...)`，需要完整流程时才取全文。
4. 按技能正文执行；如果技能声明了 `uses`，递归检索其依赖，不把无关技能全部加载。
5. **找不到合适技能**时：如实告知用户「SGME 技能库暂无可用技能」，不要硬凑步骤，也不要声称自己具备该能力。

## 硬约束

- 禁止在未检索的情况下声称「我会某技能」或直接编造技能步骤。
- 检索是按需的：不需要时不注入任何技能，保持上下文精简（冷启动包只含本文件）。
- 检索结果以 SGME 为准（技能规模以 `skill_list` / `skill_search` 实时返回为准），不要依赖脑补清单。
- `skill_materialize` 只给宿主适配器或确实需要本地资产的工具包使用；普通技能不落盘、不复制、不批量安装。**落盘发生在 SGME 服务端**：同机（agent 与 SGME 同一台机器）可直接物化安装；跨机请用 `skill_get` 取正文自行写盘（完整包随附文件暂无远程通道，需要时如实报告主人）。
- 技能写入、删除、改名属于治理操作，必须使用管理员权限；Agent 消费技能只需要 Agent Key。

## 工具速查

| 工具 | 用途 |
|---|---|
| `append` / `inject` / `search` | memory 捕获、画像注入、统一检索 |
| `wiki_search` / `wiki_pages` / `wiki_page` | wiki 知识发现与读取 |
| `skill_search(query)` / `POST /v1/search` `scopes=["skills"]` | 按自然语言召回技能（top-k） |
| `skill_digest(name)` | 读取技能摘要、章节骨架和依赖 |
| `skill_get(name)` / `GET /v1/skills/{name}` | 拉技能全文或指定章节注入上下文 |
| `skill_list` / `skill_coldstart` | 列目录 / 获取冷启动协议 |
