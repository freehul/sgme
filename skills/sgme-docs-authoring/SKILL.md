---
name: sgme-docs-authoring
description: SGME 设计、Backlog、接入和运维文档的现状对齐写作。修改 docs/、AI-INSTALL 或发布说明时使用。
category: sgme-development
tags: [skill, sgme, docs, design, backlog, onboarding, runbook]
version: 1.0.0
pattern: auto
uses:
  - coding-workflow
  - sgme-development
---

# SGME 文档编写与现状对齐

## 工作流

1. 读 Backlog 条目和目标章节，确认文档属于公开主仓还是私有文档仓。
2. 用代码、配置、测试和 CodeGraph 核实每个事实；不把历史结论当现状。
3. 先改设计/契约，再改实现；保持章节结构，优先小范围 patch，不重写无关内容。
4. 文档中的路径、地址、主机名、密钥和身份信息使用占位符或环境变量。
5. 用 `rg` 检查旧名称、旧端点和版本号漂移，运行相关契约测试。
6. 提交信息使用 `docs:`，并在 Backlog 记录完成状态、版本和验证证据。

## 文档边界

- 公开仓只放产品接入说明、用户手册和可运行示例；设计审计、研究、计划和评审进入 `SGME-internal`。
- `adapters/<host>/` 是代码真源；适配器分发技能由脚本生成，不在文档仓手工维护副本。
- 文档只描述已实现或明确标注为待办的能力；发现缺口时回写 Backlog，不用模糊措辞掩盖。
