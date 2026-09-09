# T-149 聚合答案与时序推理 实施计划

> **For agentic workers:** 按 Task 顺序执行，每任务 TDD（先测后码）。评测接入（Task 6）暂缓——用户拍板「评测置后，先完成开发」。

**Goal:** 补齐答案生成侧的跨 session 聚合与时序推理能力，消化检索 recall@8=0.8426 与 J-score=0.384 的剪刀差。

**Architecture:** 三段式——①检索层透传时间锚点与 facts（零 LLM 成本）；②新建 operations/answer.py 聚合答案操作（LLM 走 refinement 降级链，prompt 分聚合/时序两模板）；③HTTP/MCP 挂 /v1/answer。评测台改造（--qa-mode）延后至 Task 6。

**Tech Stack:** Python 3.11 / FastAPI / SQLite JSON1 / sgme.llm.chain.call_with_fallback（既有降级链）

## Global Constraints

- 模块边界：operations/answer.py 不 import fastapi；SQL 一律在 data 层；中文注释英文函数名
- LLM 调用统一走 `sgme.llm.chain.call_with_fallback(cfg, prompt, chain_name="refinement", client=client)`，禁止裸调 provider
- 密钥不落盘；新增 prompt 注册进 `sgme/resources/prompts/` + manifest.yaml（版本 v001）
- 运行时配置走 config（answer.enabled 默认 True 灰度开关；prompt_version 记录）
- 旧契约零破坏：/v1/search 响应新增字段为纯增量（occurred_at/facts 对旧客户端透明）
- 每任务完成即 `python scripts/test_fast.py answer` 或 pytest 该模块，全绿再提交

---

### Task 1: 检索层透传 occurred_at + facts

**Files:**
- Modify: `sgme/data/search/__init__.py`（BM25 两处 SELECT L881/L904、LIKE 兜底 L954、图召回回填 L428 加 `m.occurred_at, m.facts_json`；search_memories 尾部装饰循环补 facts 解析）
- Modify: `sgme/data/search/vector.py`（vector_search 两处 SELECT 加 `m.occurred_at, m.facts_json`）
- Test: `tests/test_search_answer_fields.py`（新建）

**Interfaces:**
- Produces: `search_memories()` 返回 dict 新增键 `occurred_at: str|None`、`facts: list[dict]`（经 facts_dao.parse_facts_json 解析）
- 不改 rrf_merge（它按整 dict 透传，多余字段自然保留）

- [ ] Step 1: 写失败测试——内存库插入 2 条记忆（一条带 occurred_at+facts_json），BM25 与 LIKE 两路检索断言新字段存在且值正确
- [ ] Step 2: 跑 `pytest tests/test_search_answer_fields.py -v` 确认 FAIL（KeyError）
- [ ] Step 3: 改 5 处 SELECT + 装饰循环（`r["occurred_at"] = r.get("occurred_at"); r["facts"] = parse_facts_json(r.pop("facts_json", None))`）
- [ ] Step 4: pytest 该文件 PASS + `pytest tests/test_search*.py tests/test_facts.py` 回归全绿
- [ ] Step 5: 提交 `feat(search): 检索结果透传 occurred_at 与 facts（T-149①）`

### Task 2: answer prompts 资源（3 模板 v001）

**Files:**
- Create: `sgme/resources/prompts/answer_aggregate.txt`（聚合计数/列举/多跳：上下文带 [n] 编号+每条 facts+occurred_at；指令=逐条核对证据、计数题先列证据再给数、禁臆造；NO CONTEXT 语义保留）
- Create: `sgme/resources/prompts/answer_temporal.txt`（时序：上下文额外带排序好的时间线（occurred_at + content）；指令=先定位各事件时间点，再算间隔/排序；日期差用真实日历计算，答案保留原始日期措辞）
- Create: `sgme/resources/prompts/answer_generic.txt`（兜底通用模板=现有 _ANSWER_PROMPT 语义 + facts 透传）
- Modify: `sgme/resources/prompts/manifest.yaml`（注册 3 个 stage，version v001）
- Test: `tests/test_prompts_answer.py`

- [ ] Step 1: 失败测试——PromptStore().get("answer_aggregate") 返回含 {{context}}/{{question}} 占位符且 version=v001
- [ ] Step 2: FAIL → 写 3 模板 + manifest 注册 → PASS
- [ ] Step 3: 提交 `feat(prompts): answer 三模板注册（T-149②）`

### Task 3: operations/answer.py 核心操作

**Files:**
- Create: `sgme/operations/answer.py`
- Test: `tests/test_answer.py`

**Interfaces:**
- Produces: `answer(mem_conn, session_conn, cfg, *, query, question_type=None, limit=8, client=None, llm_fn=None) -> OperationResult`
  - question_type: "temporal"|"aggregate"|None（None=自动分派：正则命中「多少天/先后/顺序/之前/之后/上次/最早/最近间隔」→ temporal；「几个/多少/哪些/列表/清单」→ aggregate；其余 generic）
  - data: {answer, evidence: [{memory_id, rank, occurred_at, facts_used}], question_type, provider, usage, prompt_meta}
  - llm_fn 注入点（测试 mock）；缺省走 call_with_fallback
- 复用: operations/search.search（scopes=["memory"], include_sources=False）取候选；facts/occurred_at 从 Task 1 透传字段读
- LLM 失败 → LLMUnavailable 时不炸：data.answer=None + note="llm_unavailable"（HTTP 200 语义，与 search 空结果同哲学）

- [ ] Step 1: 失败测试四件：①mock search 返回带 facts/occurred_at 的候选 → 断言 context 渲染含 [n] 编号与 facts 文本 ②temporal 分派断言（「How many days passed...」→ temporal）③mock llm_fn 返回固定答案 → 断言 evidence 结构 ④llm_fn 抛异常 → answer=None + note
- [ ] Step 2: FAIL → 实现 answer.py（分派器 + context 渲染器 + 时序时间线排序器（occurred_at 升序、None 排后）+ call_with_fallback 接线 + prompt_meta 记录）→ PASS
- [ ] Step 3: `pytest tests/test_answer.py tests/test_search_answer_fields.py` 全绿
- [ ] Step 4: 提交 `feat(operations): answer 聚合/时序答案操作（T-149③）`

### Task 4: HTTP /v1/answer 端点 + MCP answer 工具

**Files:**
- Modify: `sgme/server/routes_memory.py`（AnswerRequest 模型 + POST /v1/answer，require_agent_key，run_operation(answer_operation,...)）
- Modify: `sgme/server/app.py`（无状态新增则跳过；确认 mem_conn/session_conn 已在 app.state）
- Modify: `sgme/mcp_server.py`（answer 工具：入参 query/question_type/limit，注册进 ONBOARDING_TOOLS）
- Modify: `sgme/resources/config/sgme.yaml`（answer.enabled: true）
- Test: `tests/test_routes_answer.py`

- [ ] Step 1: 失败测试：POST /v1/answer mock llm_fn 断言 200 + data.evidence；enabled=false 时 404 语义（ERR_DISABLED）；MCP answer 工具冒烟
- [ ] Step 2: FAIL → 实现路由 + MCP + 配置开关（app 启动读 cfg，operations 层收 enabled 判断）→ PASS
- [ ] Step 3: `pytest tests/test_routes_answer.py tests/test_mcp*.py` + 全量 search/answer 相关回归
- [ ] Step 4: 提交 `feat(server): /v1/answer 端点与 MCP answer 工具（T-149④）`

### Task 5: 文档对齐 + Backlog 登记

**Files:**
- Modify: `docs/design/SGME-架构设计-v1.0.md`（模块表/接口契约加 answer；数据流注记 facts 消费方上线）
- Modify: `docs/requirements/SGME-Backlog-v0.2.md`（T-149 拆子任务登记：①-④ ✅ ⑤评测置后）
- Modify: `docs/design/SGME-实施变更记录-v0.9.md`（B159）
- Modify: `docs/agent-onboarding.md`（工具数对账 +40→41）
- [ ] Step 1: 程序化对账 MCP 工具数（B153 脚本法）
- [ ] Step 2: 提交 `docs: T-149 开发段文档对齐 + B159`

### Task 6（暂缓，待用户令）: 评测接入 A/B

`eval/longmemeval_eval.py` 加 `--qa-mode product|legacy`（product=调 answer 操作语义的本地复刻——评测台无生产 Server，需把 answer 渲染逻辑抽为可独立调用的纯函数 `render_answer_context()` 供评测台复用）；refined 臂 FIXED_TS 改为按 session 日期递增（时序锚点修复）。
