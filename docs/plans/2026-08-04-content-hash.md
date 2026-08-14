# SGME 内容哈希去重实施计划（Task 6）

> 状态机文件：本文件是任务状态的唯一权威来源，每完成一步更新 checkbox 并 git 提交。
> 依据：审计发现——架构 §9.1 Dedup 要求"内容哈希对比识别未变原始内容并跳过"，未实现。
> 创建：2026-08-04

**Goal:** raw_files 增加 content_hash 字段，提炼时对比哈希识别"未变跳过 / 已改全量重提炼"

**Global Constraints:**
- 每步 TDD：写测试→跑失败→实现→跑通过→提交
- 迁移兼容已有库（ALTER TABLE 兜底）；测试用 tmp 隔离

---

### Task 6: content_hash 去重

**Files:**
- Modify: `sgme/storage/db.py`（raw_files DDL 加 content_hash + 迁移）
- Modify: `sgme/storage/wiki_dao.py`（insert/update 支持 content_hash）
- Modify: `sgme/server/routes_memory.py`（append 时计算哈希存储）
- Modify: `sgme/engine/refine.py`（提炼前对比哈希：相同→增量游标；不同→全量重提炼）
- Test: `tests/test_raw.py` / `tests/test_server.py` / `tests/test_engine.py`

**Interfaces:**
- Consumes: `raw_store.file_size` / `raw_store.parse_file`
- Produces: `content_hash` 字段；`refine_file` 哈希对比逻辑

- [x] Step 1: 写测试（append 存哈希 / 未变跳过 / 修改全量重提炼）
- [x] Step 2: 跑测试确认失败（RED：3 个全失败）
- [x] Step 3: 实现（DDL+迁移 / DAO / append 存哈希 / refine 对比）
- [x] Step 4: 跑测试确认通过 + 全量回归（382 passed，GREEN）
- [x] Step 5: 提交（feat(refine)）

## 状态记录

- 最后更新：2026-08-04 Task 6 完成
- 当前进行：无
- 已完成：Task 6（内容哈希去重，全量 382 passed）
