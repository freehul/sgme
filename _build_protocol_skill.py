# -*- coding: utf-8 -*-
import os

content = """---
name: skill-registry-protocol
description: SGME 技能检索与按需注入协议——当你判断任务需要 SGME 未内置的专业能力（写代码框架/画图/运维/某工具链/领域知识）时，必须先检索、再注入、不凭空编。触发：需要某技能、不确定 SGME 有没有、想调用专业能力。
tags:
  - skill
category: sgme
---

# SGME 技能检索协议（按需注入）

## 何时用本协议
当你判断任务需要 SGME 未内置的专业能力（写代码框架、画图、运维脚本、某工具链、特定领域知识……）时，**先检索、不凭空编**。

## 检索与注入流程（MUST）
1. 调 `skill_search(query)`（MCP）或 `POST /v1/search` `scopes=["skills"]`（HTTP），传入自然语言需求。
2. 从返回的 top-k 中挑选最匹配项（看 name / description / category / tags）。
3. 调 `skill_get(name)`（MCP）或 `GET /v1/skills/{name}`（HTTP）拉该技能 L2 全文，注入当前上下文后，再按其步骤执行。
4. **找不到合适技能**时：如实告知用户"SGME 技能库暂无可用技能"，不要硬凑步骤，也不要声称自己具备该能力。

## 硬约束
- 禁止在未检索的情况下声称"我会某技能"或直接编造技能步骤。
- 检索是按需的：不需要时不注入任何技能，保持上下文精简（这也是冷启动包只含本文件、不预载全量技能的原因）。
- 检索结果以 SGME 为准（当前 403 个 canonical 技能），不要依赖脑补清单。

## 工具速查
| 工具 | 用途 |
|---|---|
| `skill_search(query)` / `POST /v1/search` `scopes=["skills"]` | 按自然语言召回技能（top-k） |
| `skill_get(name)` / `GET /v1/skills/{name}` | 拉技能全文注入上下文 |
| `skill_list` / `skill_coldstart` | 列目录 / 冷启动包（本文件即冷启动包唯一项） |
"""

fp = r"D:\Projects\SGME\sgme\skills\protocol\SKILL.md"
os.makedirs(os.path.dirname(fp), exist_ok=True)
with open(fp, "w", encoding="utf-8") as f:
    f.write(content)

# 验证：UTF-8 解码不抛错 + 含中文 + 无替换字符
with open(fp, "rb") as f:
    d = f.read()
t = d.decode("utf-8")
cn = sum(1 for c in t if 0x4E00 <= ord(c) <= 0x9FFF)
bad = t.count("\uFFFD")
print(f"SIZE={len(d)} CN={cn} BAD={bad} PATH={fp}")
