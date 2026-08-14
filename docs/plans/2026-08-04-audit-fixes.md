# SGME 审计修复实施计划

> 状态机文件：本文件是任务状态的唯一权威来源，每完成一步更新 checkbox 并 git 提交。
> 任何会话（无论是否中断）先读本文件，从第一个未勾选项继续。
> 创建：2026-08-04 ｜ 依据：审计报告（registry 缺失/契约对齐/Key 吊销/MCP 收尾）

**Goal:** 修复审计发现的 4 个 🔴 问题 + MCP 收尾提交，每项独立可验证

**Global Constraints:**
- 每任务必须：写测试 → 跑失败 → 实现 → 跑通过 → git 提交（信息带任务号）
- 不碰无关代码；测试用隔离环境（SGME_MCP_DISABLED=1、tmp data）
- 文档与代码一致（接口契约同步更新）

---

### Task 1: `/v1/admin/registry` 维度注册表 CRUD 端点（🔴）

**Files:**
- Create: `sgme/server/routes_registry.py`
- Modify: `sgme/server/app.py`（注册路由）
- Test: `tests/test_registry_api.py`

**Interfaces:**
- Consumes: `memory_dao.dimension_registry` / `dimension_alias` 表
- Produces: `GET /v1/admin/registry`（列维度+别名）、`POST /v1/admin/registry/dimensions`（新增/停用）、`POST /v1/admin/registry/aliases`（维护别名）

- [x] Step 1: 写测试 test_registry_api.py（CRUD + 鉴权 + 幂等）
- [x] Step 2: 跑测试确认失败
- [x] Step 3: 实现 routes_registry.py + 注册路由
- [x] Step 4: 跑测试确认通过 + 全量回归（364 passed）
- [x] Step 5: 提交（feat(registry)）

### Task 2: 接口契约对齐（backup/events/config 路径兼容）（🔴）

**Files:**
- Modify: `sgme/server/routes_backup.py`（补 `POST /v1/admin/backup` 兼容路径）
- Modify: `sgme/server/routes_events.py`（补 `GET /v1/events?after=` 兼容）
- Modify: `docs/design/SGME-接口契约-v0.1.md`（trigger_async/config PUT/MCP 端口入文档）
- Test: tests/test_server_v04.py 补兼容路径用例

- [x] Step 1: 写兼容路径测试
- [x] Step 2: 实现兼容路径
- [x] Step 3: 契约文档更新
- [x] Step 4: 全量回归（367 passed）+ 提交

### Task 3: agents/register 补 revoke/disable 端点（🔴）

**Files:**
- Modify: `sgme/server/routes_admin.py`
- Test: tests/test_server_v04.py

- [x] Step 1: 测试（签发→吊销→失效）
- [x] Step 2: 实现（DELETE /v1/admin/agents/{agent_id}）
- [x] Step 3: 回归（371 passed）+ 提交

### Task 4: MCP 收尾（测试 + 提交）（🟡）

**Files:**
- Create: `tests/test_mcp_server.py`
- Modify: `sgme/mcp_server.py`（如有问题）
- Modify: `docs/runbook.md`（MCP 端口/用法）

- [x] Step 1: MCP 真实握手验证（uvicorn + streamablehttp_client，9 工具全通）
- [x] Step 2: 写 test_mcp_server.py（SDK 级 6 个）
- [x] Step 3: runbook 更新（§8.1 MCP 章节）
- [x] Step 4: 全量回归（377 passed）+ 提交

### Task 5: /v1/search 支持 wiki 场景层检索（🔴，规模最大）

**Files:**
- Modify: `sgme/search/__init__.py`、`sgme/server/routes_memory.py`
- Test: tests/test_search_v04.py

- [x] Step 1: 测试（scopes=wiki 返回场景）
- [x] Step 2: 实现（search_scenes + routes 双路）
- [x] Step 3: 回归（379 passed）+ 提交

---

## 状态记录（每次更新后立即提交）

- 最后更新：2026-08-04 全部 5 个 Task 完成
- 当前进行：无（全部完成）
- 已完成：Task 1-5（registry CRUD / 契约对齐 / Key 吊销 / MCP 收尾 / wiki 场景检索）
