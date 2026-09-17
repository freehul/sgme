# 任务卡 ③ — 初始化本地文件（SOUL / USER / MEMORY / AGENTS 等）

> **目标**：让 AI 为目标宿主建立「本地身份 + 工作记忆」文件，并声明与 SGME 的分工：
> **本地文件 = 工作记忆**（身份、准则、稳定事实，随会话加载）；
> **SGME = 长期记忆**（全部会话的沉淀、跨会话检索、跨 Agent 共享）。

## 步骤

### 1. 识别宿主结构

| 宿主 | 既有约定 | 本卡产出 |
|---|---|---|
| Hermes | Profile 下 `SOUL.md`（身份+准则+铁律）、`USER.md`（用户画像）、`MEMORY.md`（环境/项目索引）；工作区 `AGENTS.md`（协作规则） | 按下方模板生成/合并 |
| Claude Code 等 | 工作区规则文件（如 `CLAUDE.md`） | 合并「身份 + SGME 纪律」两节 |
| 无约定 | —— | 建立最小集：`identity.md`（我是谁）、`user-profile.md`（用户是谁）、`agent-notes.md`（环境与项目要点），放于 Agent 自己的工作目录 |

### 2. 生成 / 合并（纪律）

1. **先读后写**：文件已存在 → 读取后**追加/合并**，禁止整文件覆盖；保留用户已有内容。
2. **简洁**：这些文件每次会话都会被加载——只放"每次都用得到"的稳定信息；细节进 SGME。
3. **写前三问**（决定放本地还是 SGME）：①跨会话稳定？②六个月后还有用吗？③是"事实"而非"过程"？——全 Yes 才写本地文件；其余一律交给 SGME。
4. **数据卫生**：真实值一律占位符（`<NAS_IP>`、`<用户名>` 等）；密钥永不落文件。

### 3. 模板（Hermes 版骨架；其他宿主按此精神裁剪）

**SOUL.md（身份+准则）**要点：
- 我是谁（名称/角色/沟通风格）+ 准则（诚实、先计划后行动、先报备后动手）
- 一条主线：长期记忆在 SGME —— 检索优先、轮次落盘、会话提炼（引用 `../agent-onboarding.md`，不抄细则）

**USER.md（用户画像）**要点：
- 用户是谁：称呼、时区/作息、沟通偏好（语言、格式、禁忌）
- 偏好类信息：确认过的风格/习惯（例如"列表先从 1 开始编号"这种长期规则）

**MEMORY.md（环境与项目索引）**要点：
- 环境事实：设备/服务/端口的**占位符形式**；项目清单与路径（占位符）
- 只放基础设施级、高频使用的事实；写前三问不过关的不写

**AGENTS.md（工作区协作规则）**要点：
- 协作纪律（报备、原件不删、提交规范）+ SGME 使用纪律（下列第 4 步）

### 4. 接入 SGME 纪律（写进上述文件，按引用方式）

- 对话开场：按需检索/注入相关记忆；历史事实类问题**必须先查 SGME 再回答**
- 每轮结束：append 当前轮次（零 LLM 成本）
- 会话收尾：refine 触发提炼（异步）
- 技能：需要 SGME 未内置的能力时先 `skill_search` 检索；禁止未检索就声称具备
- 明细见 `../agent-onboarding.md`（本卡只列清单）

## 验收（全部满足才算完成）

- [ ] 目标文件已生成/合并（不覆盖既有内容），含「SGME 分工声明」一节点
- [ ] 一轮读写回环：append 一条 → search 读回
- [ ] 把文件清单与分工说明向用户确认（"这是我对你的认识，有错请纠正"）

## 失败处置

| 症状 | 处置 |
|---|---|
| 宿主无既定文件体系 | 按最小集建三文件，位置放 Agent 自己的目录，并在 SGME 写入一条"宿主结构"记忆备查 |
| 文件过大导致会话超载 | 拆分：稳定事实留文件，其余迁 SGME；文件只留目录与纪律 |
| 用户对画像有异议 | 立即修正文件 + 在 SGME 同步修正（删除旧值并写入新值） |

---

## 整段复制版（中文）

> 你是我的 AI 助手。请为我做 SGME 的「本地文件初始化」：
> 1. 识别你的宿主结构（如 Hermes 的 SOUL.md / USER.md / MEMORY.md / AGENTS.md；或你的工具对应的规则文件）。
> 2. 生成或合并这些文件：先读后写、绝不覆盖；内容参考 `AI-INSTALL/prompts/03-init-agent-files.md` 的模板与"写前三问"。
> 3. 在文件中声明分工：本地文件=工作记忆，SGME=长期记忆；并写入 SGME 使用纪律（开场检索、轮次写入、会话提炼、技能先检索）。
> 4. 完成后做一轮读写回环验证，并把"我对你的认识"念给我确认。过程中不要让我粘贴任何密钥。

## English

**Task card ③ — Initialize local files (SOUL / USER / MEMORY / AGENTS).** Bootstrap the host's local identity + working-memory files and declare the split: local files = working memory; SGME = long-term memory. Detect the host layout (Hermes profile files; CLAUDE.md-style rules; or a minimal `identity.md` / `user-profile.md` / `agent-notes.md` set). Merge, never overwrite; keep files lean (they load every session); apply the three-question test (stable across sessions? useful in six months? a fact, not a process?) and send everything else to SGME. Record the SGME usage discipline (retrieve first, append per turn, refine at session end, skill_search before claiming skills). Acceptance: files created/merged with the division declared, one append→search round-trip, and the profile read back to the user for confirmation.

**Copy block (English):**

> Act as my AI assistant and initialize your local files for SGME:
> 1. Detect your host's convention (e.g., Hermes profile files: SOUL.md / USER.md / MEMORY.md / AGENTS.md; or your tool's rules file).
> 2. Create or merge those files — read first, never overwrite; follow the templates and the three-question test in `AI-INSTALL/prompts/03-init-agent-files.md`.
> 3. Declare the split: local files = working memory, SGME = long-term memory; add the SGME usage discipline (retrieve at start, append per turn, refine at session end, search skills before claiming them).
> 4. Finish with one append→search round-trip and read your profile of me back for confirmation. Never ask me to paste secrets.
