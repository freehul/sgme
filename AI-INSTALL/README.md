# AI-INSTALL — 让 AI 独立完成 SGME 的部署、接入与初始化

> 本目录是**写给 AI 读的操作体系**：把本目录交给你的 AI（Hermes / DSH / Claude Code / 任意 Agent），它就能自主完成 —— 部署 SGME → 接入 → 初始化本地文件 → 进入日常使用循环 —— 全程不需要用户读技术文档。
>
> 中英双语：每个文件 = 中文正文 + English section。

## 快速开始

把下面这句话发给你的 AI（推荐）：

> 「读 `AI-INSTALL/README.md`，然后按 `prompts/` 里任务卡的顺序，帮我部署并接入 SGME；每步完成后按 `selfcheck.md` 自检并向我汇报。」

## 任务卡清单

| # | 文件 | 任务 | 完成判据 |
|---|------|------|----------|
| ① | `prompts/01-install.md` | 部署 / 发现 SGME | 健康探针 200 + 版本号 |
| ② | `prompts/02-connect.md` | 接入与验证（通用 / Hermes / DSH 三路） | 4 项联通自检全绿 |
| ③ | `prompts/03-init-agent-files.md` | 初始化本地身份文件（SOUL / USER / MEMORY / AGENTS 等） | 文件生成 + 一轮读写回环 |
| ④ | `prompts/04-daily-loop.md` | 日常使用循环（检索 / 写入 / 提炼 / 技能） | 行为清单逐项勾选 |
| ✔ | `selfcheck.md` | 接入后自检 | 6 项全绿 |

## 本目录文件

- `agent-onboarding.md` —— **协议层唯一真相**（服务发现 / 连接 / 写入 / 提炼 / 技能），所有任务卡以它为准，本目录不复制其内容。
- `免费模型Key申请指南.md` —— 免费 LLM / 向量 Key 申请（两款免费平台，零充值）。
- `prompts/` —— 可整段复制给 AI 的提示词（每份含中文 + English 两个复制块）。
- `selfcheck.md` —— 自检清单与常见失败处置。

## 三条原则

1. **写给 AI**：指令式、步骤化、每步带验收；不允许跳步。
2. **单一真相源**：协议细节以 `agent-onboarding.md` 为准，其余文件只做编排与引用。
3. **安全默认**：密钥只走环境变量；AI 不得要求用户把 Key 贴进对话；写入内容遵循「数据卫生」规范（真实值一律占位符）。

---

## English

**AI-INSTALL — hand your SGME setup to an AI.** This directory is a set of AI-facing instructions: give it to any agent (Hermes / DSH / Claude Code / any) and it can deploy SGME → connect → initialize the agent's local identity files → run the daily memory loop — without the user reading technical docs. Every file is bilingual (Chinese body + English section).

**Quick start** — send this to your AI:

> "Read `AI-INSTALL/README.md`, then follow the task cards under `prompts/` in order to deploy and connect SGME for me; self-check against `selfcheck.md` after each step and report back."

**Task cards**: `01-install` (deploy/discover → health 200 + version) → `02-connect` (connect & verify; generic / Hermes / DSH → 4 green checks) → `03-init-agent-files` (initialize SOUL / USER / MEMORY / AGENTS → files created + one read/write round-trip) → `04-daily-loop` (retrieve / append / refine / skills → checklist).

**Files**: `agent-onboarding.md` (protocol source of truth), `免费模型Key申请指南.md` (free API keys), `prompts/` (copy-paste prompts), `selfcheck.md` (post-setup checks).

**Principles**: AI-first instructions with acceptance criteria per step; single source of truth (this directory references, never duplicates, the protocol doc); secrets only via environment variables.
