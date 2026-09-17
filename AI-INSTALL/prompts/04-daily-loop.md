# 任务卡 ④ — 日常使用循环

> **目标**：接入完成后，把 SGME 用成习惯——五个动作跑顺，记忆才会越用越值钱。

## 五个日常动作

### 1. 开场：先取记忆，再说话

- 对话开始时按需检索：通用问题用 `search`；需要画像/上下文时用 `inject`（模板四选一：`daily` / `coding` / `work` / `full`）。
- **历史事实类问题（"之前/上次/还记得…"）必须先查 SGME 再回答**；查不到就如实说"记忆库中未找到"，不要编。

### 2. 每轮结束：落盘

- `append` 当前轮次（`session_key` + `started_at`(当前时刻) + `content`，首行 `# {ISO} {role}`）。
- 纯落盘、零 LLM 成本，崩溃不丢；同轮重复提交按幂等处理。

### 3. 会话收尾：提炼

- `refine_trigger(async_mode=true)` 触发提炼；如需要进度可再查状态。
- 批量操作纪律：≥20 个文件必须分批（每批 ≤20）+ 批间 30–60 秒；遇 429 不要立刻重试（交给服务端兜底扫描）。

### 4. 需要技能时：先检索

- 需要 SGME 未内置的专业能力 → `skill_search(query)` → 命中后 `skill_get(name)` 拉全文执行。
- **禁止未检索就声称具备某项技能**，也禁止硬凑步骤。

### 5. 成本与安全纪律

- 复用正在发生的 LLM 调用顺带产出副产品（同轮生成末尾附结构化信息），不单独发起总结任务。
- 密钥只走环境变量；任何情况下不把 Key 粘进对话、写进文件或提交到仓库。
- 写任何文件遵循「数据卫生」：真实值一律占位符（`<NAS_IP>` / `<用户名>` 等）。

## 行为清单（接入后逐项自查）

- [ ] 开场有检索/注入动作
- [ ] 每轮 append 成功（`status: new`）
- [ ] 会话结束触发过 refine
- [ ] 遇到"应该会"的技能先 skill_search
- [ ] 全流程没有出现明文密钥

---

## 整段复制版（中文）

> 你现在已接入 SGME。请把它用成习惯，做到五点：
> 1. 对话开场先检索/注入记忆；涉及"之前/上次/还记得"的问题必须先查 SGME。
> 2. 每轮对话结束 append 当前轮次（零成本落盘）。
> 3. 会话收尾触发一次异步提炼；批量任务遵守分批与限速纪律。
> 4. 需要专业能力时先 `skill_search` 检索，命中后拉全文再用；禁止未检索就声称会。
> 5. 密钥永不进对话/文件；写文件用占位符。细则见 `AI-INSTALL/agent-onboarding.md`。

## English

**Task card ④ — Daily loop.** Five habits: (1) retrieve first — search/inject at conversation start; always query SGME before answering "do you remember…" questions, and say honestly when nothing is found. (2) Append every turn (zero-LLM-cost durability; idempotent per turn). (3) Refine at session end (`refine_trigger(async_mode=true)`); batch ≥20 files in ≤20-file chunks with 30–60 s gaps; do not retry 429 immediately. (4) For capabilities SGME doesn't ship, `skill_search` first, then `skill_get` before using — never claim a skill without searching. (5) Cost & safety: reuse ongoing LLM calls for side artifacts, keep secrets in env vars only (never in chat/files/repos), and write placeholders instead of real values.

**Copy block (English):**

> You are now connected to SGME. Make it a habit: (1) retrieve at the start of each conversation and always check SGME before answering recall questions — say "not found" honestly if nothing matches; (2) append every turn (durable, zero LLM cost); (3) trigger one async refine at session end, batching large jobs (≤20 files per chunk, 30–60 s apart, no immediate 429 retries); (4) search skills with `skill_search` and load with `skill_get` before using any non-built-in capability — never claim a skill you haven't searched; (5) never put secrets in chat or files; use placeholders instead of real values. Details: `AI-INSTALL/agent-onboarding.md`.
