---
name: skill-governance
description: SGME 技能库治理：发现重复、统一命名、合并/弃用/删除、编写 frontmatter、渐进披露、校验和适配器分发。整理或新增技能时使用。
category: sgme-development
tags: [skill, sgme, governance, registry, naming, dedupe, lifecycle]
version: 1.0.0
pattern: auto
uses:
  - coding-workflow
---

# SGME 技能库治理

## 目录与命名

- 技能目录和 frontmatter `name` 必须完全一致，只用小写字母、数字和单连字符。
- 核心入口：`sgme`；SGME 专项：`sgme-<domain>`；宿主适配器：`adapter-<host>`；通用开发能力使用清晰的领域名，如 `coding-workflow`。
- 每个技能必须有简短、可区分的 `description`、`category`、`tags` 和版本；正文不重复项目总规范。

## 重复审查

比较触发条件、输入/输出、工具入口和维护真源：

- 触发条件和输出相同 → 合并为一个入口，旧名保留迁移别名或在 Backlog 标记弃用。
- 共享原则相同但操作细节不同 → 主技能保留路由，细节放 `references/` 或专用技能。
- 适配器技能不能与通用 SGME 手册合并；适配器必须保留宿主安装和生命周期差异。
- 历史记录、已失效路径和空占位不属于技能能力；确认无引用后删除，不把它们继续复制到 wiki。

## 生命周期

1. 新技能先加契约测试和最小 `SKILL.md`，再补真实流程；不要复制整本通用手册。
2. 大型内容按任务模式拆到 `references/`，入口只保留路由和不可违反的约束。
3. 修改前搜索引用和 `uses` 依赖；删除前确认没有索引、测试、文档或运行配置引用。
4. 普通技能由 SGME 按需 `skill_search → skill_digest → skill_get` 提供；只有适配器或确需本地资产才 `skill_materialize`。
5. 若宿主提供 `skill-creator`，用其 `quick_validate.py` 做通用 frontmatter 校验；SGME 仓库再运行目录契约测试和 `scripts/validate_skill_catalog.py`，适配器改动另跑发布脚本和平级对账。

## 真源边界

- 运行时只扫描 `skills.source_dirs` 指向的 git 技能工作区；本仓库的 canonical 入口是 `skills/`，Docker 镜像入口是 `/app/cache/skills/`。
- `exports/skills-migration-preview*` 是从历史 wiki 导出的临时审阅物，不是技能真源、热集或冷启动包；不得把它加入 `source_dirs`。
- wiki 中 `status != active` 的历史 skill 页只保留作溯源，不重新发布；误挂 `skill` 标签的知识页留在 wiki 并进入人工清单。
- 清理派生预览可以归档或删除，但原始 wiki 数据库、git 技能仓和用户配置不得直接删除。
