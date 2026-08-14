# QA 验收报告：#33 蒸馏提示词版本管理

> 验收人：Edward（QA Engineer）
> 日期：2026-08-05
> 范围：#33 提示词版本管理（T01–T05 全量交付物）
> 依据：`docs/design/SGME-提示词版本管理-v0.1.md`（v0.1 设计稿）、`AGENTS.md`（12 条架构约束）
> 方法：独立编写验收测试（`tests/test_prompts_qa_acceptance.py`，18 例）+ 独立核实工程师交付物 + 全量回归 + 红线/约束核对

---

## 1. 验收结论

**IS_PASS = YES** ✅

**智能路由判定：NoOne**（全部测试通过；无业务代码缺陷，无测试缺陷；无路由到工程师/QA 的必要）

- 工程师声明 458 passed → 独立复核基线 **458 passed / 0 failed**（EXIT_CODE=0）
- 新增 QA 独立验收测试 18 例 → **全部通过**
- 含新测试的全量回归：**476 passed / 0 failed**（EXIT_CODE=0）
- 红线 5 项全部通过；约束 4 项全部通过；无业务代码改动（仅新增 `tests/` 与 `docs/`）

---

## 2. 交付物

| 交付物 | 路径 | 状态 |
|---|---|---|
| QA 独立验收测试（新建） | `tests/test_prompts_qa_acceptance.py`（18 例） | ✅ 全部通过 |
| 本验收报告（新建） | `docs/QA_提示词版本管理_验收报告.md` | ✅ |
| 既有测试适配 | 无（工程师已适配；本次未改既有测试） | — |

---

## 3. 独立功能验收证据表

> 断言均引用具体测试名 + 关键断言行；全部 PASSED。

### 3.1 热更新语义

| 验收点 | 测试名 | 关键断言 |
|---|---|---|
| @working 编辑后引擎渲染立即生效（无缓存） | `test_working_hot_reload_at_engine_render` | `out1 != out2`；`"# 新增指令" not in out1`；`"# 新增指令" in out2` |
| 进行中任务不受影响（渲染即不可变字符串） | `test_inflight_render_frozen_not_affected_by_edit` | 已渲染文本 `"# 新指令" not in rendered`；下一渲染 `"# 新指令" in pv2.text` |
| activate 钉版后取不可变快照（不受草稿编辑影响） | `test_pinned_snapshot_immune_to_working_edits` | 编辑工作副本后 `pinned2.version == "v001"` 且 `pinned2.text == STAGE_TEXTS["l1_extraction"]`；切回 @working 才见草稿 |

### 3.2 A/B 语义

| 验收点 | 测试名 | 关键断言 |
|---|---|---|
| sha256 确定性分流公式与设计 §1.4 完全一致 | `test_ab_bucket_formula_matches_sha256` | `pv.variant == _expected_bucket(key, split)`（split∈{0,0.2,0.5,0.8,1.0}，key 含中文）；`pv.version` 与变体对应 v001/v002 |
| 不同 key 按比例分流（统计分布） | `test_ab_distribution_across_keys` | 200 key：`set(variants) == {"A","B"}`；`0.25 <= ratio_a <= 0.75` |
| bucket_by 默认 file_id（红线 §6 #2） | `test_ab_bucket_by_defaults_file_id` | `m["stages"]["l1_extraction"]["ab"]["bucket_by"] == "file_id"`（configure_ab 不传 bucket_by） |
| variant 正确落账（engine 链路 refine_runs） | `test_ab_variant_recorded_in_refine_runs` | `r["version"] == meta["version"]`；`r["variant"] == meta["variant"]`；`r["bucket_key"] == key`；`r["status"] == "ok"` |

### 3.3 版本可观测

| 验收点 | 测试名 | 关键断言 |
|---|---|---|
| refine_runs 逐批记录（L1 分块每块一条） | `test_extract_l1_records_run_per_chunk` | `len(runs) >= 2`；全部 `status=="ok"`、`version.startswith("working-")` |
| refine_runs 记录 error（LLM 全挂不丢观测） | `test_refine_runs_records_error_status` | `runs[0]["status"] == "error"`；`runs[0]["error"]` 非空 |
| metrics 不做自动裁决（红线 §6 #1） | `test_metrics_no_automatic_adjudication` | `set(body.keys()) == {"stage","since","groups"}`；组键集合精确等于观测字段；无 winner/recommend/conclusion/decision 等裁决字段 |
| 旧库 NULL 不追溯（红线 §6 #4） | `test_metrics_ignores_null_prompt_version` | 带版本行计入 `memories_rows == 1`、`avg_priority == 90.0`；NULL 行不计入 |
| 全链路：trigger 响应含 prompt_versions + memories.prompt_version 落 L1 钉版 | `test_refine_trigger_prompt_version_end_to_end` | `l1_meta["version"] == "v001"`；所有 memories 行 `prompt_version == "l1_extraction:v001"`；refine_runs `runs[0]["version"] == "v001"` |

### 3.4 向后兼容

| 验收点 | 测试名 | 关键断言 |
|---|---|---|
| manifest 缺失 → 引擎渲染走默认 @working | `test_manifest_missing_engine_render_works` | `"会话" in out`；`pv.version.startswith("working-")` |
| 全新库 schema v3 | `test_fresh_init_schema_v3` | 双库 `schema_version == 3`；`refine_runs` 表存在；memories 含 `prompt_version` 列 |
| 重复连接/初始化幂等安全 | `test_connect_memory_twice_idempotent` | 二次 connect 后 `schema_version == 3` 无异常 |
| insert_memory 旧签名（无 prompt_version）兼容 | `test_insert_memory_old_signature_backward_compat` | `row["prompt_version"] is None`（旧调用行为不变） |

### 3.5 最小侵入红线 + 维度联动

| 验收点 | 测试名 | 关键断言 |
|---|---|---|
| 4 渲染点全部经 PromptStore（无 read_text 直读残留） | `test_all_render_points_route_through_promptstore` | 依次调用 `calls == ["l1_extraction","l1_conflict","l2_scene","tier0_summary"]`（L1/L1.5/L2 render + tier0 generate_summary 均经 PromptStore.get） |
| 维度注册表写库后 cfg['dimensions'] 即时刷新 | `test_refresh_dimensions_after_registry_write` | `refresh_dimensions` 后 `"qa_dim_live" in ids`；停用后再次刷新 `not in ids2` |

### 3.6 T01 基线与 CLI

| 验收点 | 方法 | 结果 |
|---|---|---|
| 4 个 v001 快照 = 原 4 txt 字节级原样 | `diff -q prompts/<s>.txt prompts/versions/<s>/v001.txt` | 4/4 IDENTICAL |
| manifest sha256 与磁盘文件实算一致 | venv python 复算 sha256 | 4/4 OK |
| scripts/prompts_cli.py 可用 | `python scripts/prompts_cli.py --help` | list/publish/activate/ab 子命令齐全 |
| 出厂 manifest：tier0 A/B 默认不启用（红线 §6 #5） | 读 `prompts/manifest.yaml` | 4 stage 均 `ab.enabled=false`、`active=@working` |

---

## 4. 全量回归

```
命令：cd <项目根> && .venv/Scripts/python -m pytest tests/ -q -o tmp_path_retention_policy=all
（未加 --basetemp / --junitxml；结果重定向文件 + 统计进度点，规避 safe-delete 覆盖）

工程师基线（不含 QA 新测试）：458 passed / 0 failed / 0 error（EXIT_CODE=0，独立复核）
QA 新测试单独运行：         18 passed / 0 failed（EXIT_CODE=0，-rA 逐条 PASSED）
含 QA 新测试全量回归：      476 passed / 0 failed / 0 error（EXIT_CODE=0）
  （收集数 = 各文件测试数之和 = 476，与执行进度点一致；无 F/E 标记）
```

---

## 5. 红线核对（设计 §3/§6）

| 红线 | 核对结果 | 证据 |
|---|---|---|
| 不做自动裁决 | ✅ | metrics 仅返回原始观测（runs/error_runs/memories_count/memories_rows/avg_priority/action_dist），无任何裁决/结论字段；测试 `test_metrics_no_automatic_adjudication` |
| bucket_by 默认 file_id | ✅ | `configure_ab` 不传 bucket_by 时 manifest 落 `file_id`；`_VALID_BUCKET_BY=("file_id","memory_id","random")`；测试 `test_ab_bucket_by_defaults_file_id` |
| tier0 A/B 默认不启用 | ✅ | 出厂 manifest 4 stage 均 ab.enabled=false；store 默认 stage_config ab.enabled=false |
| 旧库 NULL 不追溯 | ✅ | `summarize` 按 `prompt_version IS NOT NULL AND LIKE '<stage>:%'` 过滤；测试 `test_metrics_ignores_null_prompt_version` |
| 最小侵入（4 渲染点只改读取行，无 read_text 直读残留） | ✅ | `sgme/engine/{l1,l15,l2}.py` 与 `sgme/profile/tier0.py` 中 prompt 读取仅经 `PromptStore().get()`；静态 grep 无对 `prompts/*.txt` 的 `read_text` 残留（tier0.py:128 的 read_text 是 tier0_summary.json 读取，非提示词）；测试 `test_all_render_points_route_through_promptstore` |

---

## 6. 约束核对

| 约束 | 核对结果 | 证据 |
|---|---|---|
| 模块边界（prompts 只依赖 config） | ✅ | `sgme/prompts/manager.py` 仅 `from sgme import config` + stdlib/hashlib/yaml |
| LLM 调用 trust_env=False 未被破坏 | ✅ | `sgme/llm/provider.py:48` 仍 `httpx.Client(..., trust_env=False)` |
| 无密钥硬编码 | ✅ | grep `sk-[a-zA-Z0-9]{20}` 于 sgme/scripts/config 无命中；鉴权沿用 env `SGME_ADMIN_KEY` / dev 默认值告警 |
| 未动 sgme/mcp_server.py、adapters/、config/sgme.yaml | ✅（有说明见 §7） | `git diff --name-only` 无 mcp_server.py / adapters/；config/sgme.yaml 的差异为**测试污染**（见 §7，非 #33 改动） |
| 未 commit | ✅ | 全部为工作区改动，无 #33 提交 |

---

## 7. 待遗留事项 / Known Issues

1. **（既有问题，非 #33 引入）测试污染 config/sgme.yaml**：`tests/test_server_v04.py::test_contract_config_post_alias` 通过 config API 写回**真实** `config/sgme.yaml` 的 `backup.dir`（指向 pytest tmp 路径）。每次全量回归都会改写该文件（基线复核时 `pytest-352` → 本次回归后 `pytest-360`）。**建议**：该测试改用 monkeypatch 隔离 config 文件路径（如临时副本）而非写真实配置文件。此项与 #33 无关，属既有测试卫生缺陷，建议转工程师后续修复（本次按约束未改 config/）。
2. **e2e_smoke.py 适配**：`scripts/e2e_smoke.py` 的 `fake_extract_l1` 已适配为 3 元组返回（工程师 T05 范围内改动，符合设计 §4 "extract_l1 返回 3 元组"）。真实 LLM 冒烟（LM Studio 在线时 `python scripts/e2e_smoke_v04.py`）本次未执行（环境无 LM Studio 在线），已由 mock 全链路（engineer test_e2e/test_e2e_v04 + QA 全链路用例）覆盖到链路层。
3. **bucket_by=memory_id 语义**：设计 §6 #2 注明 L2 按批聚合时 memory_id 分流会导致批内混合变体，默认 file_id 已实现；memory_id/random 保留为临时实验能力，未做工程验证（符合设计预期，不阻塞验收）。
4. **tier0 A/B 未启用**：机制统一支持但默认不启用（设计 §6 #5），本次未对 tier0 A/B 真实分流做端到端验证（符合设计预期）。

---

## 8. 结论

#33 蒸馏提示词版本管理实现满足设计 v0.1 全部验收要求：热更新/钉版/A-B/版本可观测/向后兼容/维度联动均独立验证通过；红线与约束全部满足；全量回归 **476 passed / 0 failed**。**IS_PASS=YES，路由判定 NoOne。**

*报告完。*
