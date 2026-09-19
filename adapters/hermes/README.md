# SGME × Hermes 适配插件

SGME 作为 Hermes **原生 memory.provider** 的桥接插件（架构 §18）。
瘦桥接：不碰 LLM、不碰数据库，全部能力走 SGME Gateway HTTP（默认回环 `127.0.0.1:9910`，生产用 `SGME_BASE_URL` 指向远端 SGME，如 `http://<NAS_IP>:9910`）。

## 目录说明（项目文件管理原则）

| 位置 | 角色 |
|---|---|
| `adapters/hermes/`（本项目） | **源码唯一副本**，随 SGME 版本控制与分发 |
| `<HERMES_HOME>/plugins/sgme/`（Hermes 运行时） | 部署副本，由 `install.py` 生成，可随时重建 |
| `~/.sgme/install.json` | 服务发现清单（install.py 生成，见下） |

> 修改插件代码只改 `adapters/hermes/`，然后重跑 `install.py` 同步部署副本。

## 生命周期能力

- `system_prompt_block()` — SGME 画像摘要注入 system prompt
- `prefetch(query)` — 每轮 LLM 前召回相关记忆 + 场景（scopes `["memory","wiki"]`）
- `sync_turn()` — 每轮对话增量写入 SGME 原始层（后台异步，tool 消息指纹去重）
- `on_session_end()` — 补最后一轮增量 + 触发异步提炼
- `get_tool_schemas()` / `handle_tool_call()` — 40 个 `sgme_*` 工具（见下表）

## 版本口径

- `plugin.yaml` 的 `version` **跟随 SGME 版本号**（当前 `1.2.3` = 技能消费统计批次 T-174），
  由 `tests/test_hermes_adapter.py::test_plugin_yaml_version_follows_sgme_version` 守门。
- 适配器能力面唯一基准 = **SGME MCP 工具面**（`sgme/mcp_server.py`，41 个工具）；
  三个官方适配器（hermes / dsh / doubao）平级，对账走 `python scripts/adapter_parity.py`。
- 演进纪律：SGME 每新增一个 MCP 工具，本适配器必须在同批工作内补齐（或显式登记豁免/待补），
  否则 parity 脚本判为漂移（失败）。

## 工具清单（40 个 = 41 基准 − 2 豁免 + 1 自有）

Key 口径：**agent** = Agent Key（`SGME_AGENT_KEY`）；**admin** = 管理员 Key（`SGME_ADMIN_KEY`）；
**—** = 免鉴权。写侧/运维工具的 description 里都注明「需管理员 Key」，与实现一致（有测试守门）。

### 检索 / 注入（读）

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_memory_search` | 统一检索，默认 scopes `["memory","skills"]`（可传 scopes 覆盖）；命中技能只回名字，配合 `sgme_skill_get` 取全文 | agent |
| `sgme_conversation_search` | L0 原始会话层检索（scopes `["sessions"]`）——查「某次对话的原话」（**自有工具**，基准外） | agent |
| `sgme_inject` | 按场景模式拉画像（daily/coding/work/full） | agent |
| `sgme_answer` | 聚合型提问（计数/列举/时序），多一步服务端 LLM 合成 | agent |
| `sgme_health` | 健康自检：版本/LLM/提炼水位/向量水位 | — |

### 记忆治理

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_memory_get` | 单条记忆详情（维度/状态/溯源/归档链） | agent |
| `sgme_memory_reject` | 标记「不采用」（不删除、可恢复、幂等） | admin |
| `sgme_memory_unreject` | 撤销「不采用」，恢复 active | admin |

### wiki 知识库

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_wiki_search` | wiki 页面检索（执行通道，含 skill 手册） | agent |
| `sgme_wiki_pages` | 按 category 列目录（渐进式披露 L2 索引层） | agent |
| `sgme_wiki_page` | 按 page_id 拉全文 | agent |
| `sgme_wiki_page_add` | 建页/幂等 upsert（原样入库，不走 LLM） | admin |
| `sgme_wiki_page_update` | 追加（默认 `append=true`）或覆盖正文 + 元数据 | admin |
| `sgme_wiki_evolve_trigger` | 手动补触发自进化（会话经验 → wiki 手册） | agent |

### 技能层（SGME 1.1.0 范式：技能不预载，按需检索 → 拉全文 → 执行）

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_skill_search` | 技能检索（只回 name/description/category，**不含正文**） | agent |
| `sgme_skill_digest` | L1 摘要：字段 + 正文骨架 + uses 依赖（执行前审核） | agent |
| `sgme_skill_get` | L2 全文；`section` 只取一节省 token（带 `#` 前缀会自动剥离） | agent |
| `sgme_skill_list` | L0 索引列表（分页浏览全量） | agent |
| `sgme_skill_coldstart` | 冷启动包（技能检索协议 + SGME 操作手册） | agent |
| `sgme_skill_materialize` | L3 物化：写 `<dest_dir>/<name>/SKILL.md`，返回 path + sha256（**服务端路径**） | agent |
| `sgme_skill_put` | 写入/覆盖技能（lint 门禁 + 三层查重后落盘提交） | admin |
| `sgme_skill_delete` | 删除技能（默认软删 deprecated；`hard`/`force` 走 query） | admin |
| `sgme_skill_rename` | 墓碑制改名（旧位置留 superseded_by 墓碑） | admin |

### 关怀信号（谁消费谁标记）

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_signal_pull` | 拉未消费关怀信号（只拉取不消费） | agent |
| `sgme_signal_claim` | 原子认领（409 = 已被他人消费，跳过即可） | agent |
| `sgme_signal_ack` | 写消费回执（claimed/acked/failed） | agent |
| `sgme_signal_clear` | 批量清空未消费信号（幂等，破坏性，仅用户明确要求时用） | admin |

### 角色（换皮不换芯）

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_role_list` | 列角色模板（含当前角色标记） | agent |
| `sgme_role_assemble` | 装配角色沟通提示词（+可选画像） | agent |
| `sgme_role_active_get` | 读当前沟通角色 | agent |
| `sgme_role_active_set` | 设置当前沟通角色 | agent |

### 三池登记

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_idea_add` | 创意池（仅用户主动提出时记录） | admin |
| `sgme_demand_create` | 待办池（跨项目统一，会话中主动登记） | admin |
| `sgme_project_register` | 项目池（upsert，用户主动立项时登记） | admin |

### 运维 / 提炼

| 工具 | 作用 | Key |
|---|---|---|
| `sgme_refine_trigger` | 同步提炼（阻塞 + 消耗 LLM 额度，尽量用 batch） | admin |
| `sgme_refine_batch` | 异步批量提炼（排队即返） | admin |
| `sgme_refine_status` | 提炼批次记录（查「入库了但没变成记忆」） | admin |
| `sgme_stats` | 统计概览（记忆数/维度分布/提炼水位/已注册 agent） | admin |
| `sgme_config_get` | 读服务端运行时配置（整体或按段） | admin |
| `sgme_config_update` | 改服务端配置段（热生效 + 落盘，破坏面最大） | admin |

### 永久豁免（基准里有、本适配器不实现，已登记 `scripts/adapter_parity_map.yaml`）

| 基准工具 | 不实现理由 |
|---|---|
| `agent_onboarding` | Hermes 走 **memory.provider 槽位**接入（`config.yaml` → `memory.provider: sgme`），没有 MCP 握手/连接环节，也没有「连接即发现工具」的引导落点——工具面在插件加载时随 `get_tool_schemas()` 一次性注册给模型。 |
| `append` | Hermes 由 `sync_turn()` **每轮自动入库**（含 tool 消息指纹去重 + 增量导出游标）；让 agent 手写 L0 会破坏引擎的**查重与幂等语义**（同 session_key + 同 started_at 幂等丢弃 / 不同则追加），并可能把消息导重复（ST-23③ 实锤过的失效模式）。 |

## 服务发现清单 install.json

`install.py` 部署插件的同时写 `<HOME>/.sgme/install.json`（可用 `--install-json <path>` 或环境变量
`SGME_INSTALL_JSON` 覆盖落点），供其它 agent/工具发现「SGME 装在哪、用哪个环境变量取 Key」：

```json
{
  "schema_version": 1,
  "adapter": "hermes",
  "adapter_version": "1.2.3",
  "base_url": "http://127.0.0.1:9910",
  "http": { "host": "127.0.0.1", "port": 9910 },
  "mcp": { "port": 9913 },
  "keys": { "admin": "SGME_ADMIN_KEY", "agent": "SGME_AGENT_KEY", "bearer": "SGME_BEARER_TOKEN" },
  "agent_id": "hermes"
}
```

- `base_url` / `http.*` 从 `SGME_BASE_URL` 解析（未设则回环默认）；`mcp.port` 取 `SGME_MCP_PORT`（默认 9913）。
- `keys` **只写环境变量名引用**，绝不落明文密钥（项目铁律：密钥不落盘）；字段形态与
  `sgme/config.py::write_client_install_json`、`adapters/dsh/install.py` 对齐。
- 字段有测试守门（`tests/test_hermes_adapter.py` 的 install.json 三例：写入/回环默认/落点覆盖）。

## 安装

```bash
# 1. 部署插件 + 生成服务发现清单（自动探测 HERMES_HOME）
python adapters/hermes/install.py

# 2. 指定目录与清单落点（可选）
python adapters/hermes/install.py --home <HERMES_HOME> --install-json <path>

# 3. 确认配置启用（memory.provider: sgme）+ 环境变量提供密钥
# 4. 重启 Hermes
```

## 前提

- SGME Server 常驻运行（HTTP 9910 / MCP 9913）
- Key 配置：环境变量 `SGME_AGENT_KEY` / `SGME_ADMIN_KEY`（plugin.yaml 只存非敏感配置，不存密钥）
- 技能 / wiki 能力需服务端对应模块启用（`skills.enabled` / wiki 扩展），否则 `/v1/skills*`、`/v1/wiki/*` 整体 404（见常见坑 7）

## 验证（装完怎么算成功）

1. `hermes plugins list` → sgme 行状态应为 `enabled`（`not enabled` = 未加载，见常见坑 1）
2. 确认 `config.yaml` 的 `memory.provider: sgme`
3. 重启 Hermes 后，SGME Gateway（:9910）日志应出现 Hermes 的 `/v1/append` 与 `/v1/admin/refine/trigger_async` 调用
4. 会话内验证：问一个历史问题 → 应触发 `sgme_memory_search`；问「有没有现成技能」→ 应触发 `sgme_skill_search` → `sgme_skill_get`
5. 离线自证：`python -m pytest tests/test_hermes_adapter.py -q`（40 例，零网络零 LLM），或 `python scripts/test_fast.py hermes`

## 常见坑

1. **`plugins.enabled` 存成字符串**：`hermes config set plugins.enabled '[...]'` 会写成字符串而非 YAML list，加载器 `isinstance(list)` 校验失败 → 视为无插件加载。须手工把 config.yaml 里该键改回列表块（`- sgme` 形式）。
2. **插件目录位置**：用户级 provider 目录是 `<HERMES_HOME>/plugins/sgme/`（**不带 `memory/` 子目录**）；放错位置 → `find_provider_dir` 返回 None，静默不加载。install.py 已按正确路径部署，别手搬到 `plugins/memory/sgme/`。
3. **config.yaml 是安全敏感配置**：部分工具（如 patch）拒绝写 config.yaml，直接改时用 Python 脚本写并备份（`.bak-pre-sgme`）。
4. **改桥接代码要同步部署副本**：改 `adapters/hermes/__init__.py` 后必须重跑 install.py 同步到 `<HERMES_HOME>/plugins/sgme/`，否则 Hermes 加载的是旧副本。
5. **检索取不到技能（旧缺陷）**：v0.1.0 的 `sgme_memory_search` 请求体写死 `scopes: ["memory"]`，技能层与 wiki 层取不到（prefetch 却是双 scope）。现默认 `["memory","skills"]` 并支持 `scopes` 参数覆盖；自建 HTTP 调用请注意服务端 curl 缺省是 `["memory","skills"]`，显式传值会覆盖缺省。
6. **技能 `section` 契约**：服务端要**纯标题文本**（`前置条件`），而 `sgme_skill_digest` 的 `sections` 骨架给的是**带 `#` 的原样行**（`## 前置条件`）。插件已做归一化（剥 `#` 与空白），直接照抄骨架传进去也能命中；绕过插件手写 HTTP 调用则需自己剥，否则 404 且错误文案误导为「技能不存在」。
7. **技能/wiki 端点整体 404 ≠ 插件坏了**：`/v1/skills*`、`/v1/wiki/*` 由服务端模块开关控制（未启用时整个 router 不注册）。此时技能类工具会返回结构化错误，其它记忆能力不受影响。
8. **管理员 Key 缺失会 403**：17 个写侧/运维工具（`sgme_stats`、`sgme_config_*`、`sgme_refine_*`、`sgme_signal_clear`、`sgme_skill_put/delete/rename`、`sgme_wiki_page_add/update`、`sgme_memory_reject/unreject`、三池登记）走 `SGME_ADMIN_KEY`；未配置时这些工具报 403，而检索类工具（agent key）照常可用。这是「读得到、改不了」的预期形态，不是插件故障。
9. **`sgme_skill_materialize` 落盘在服务端**：返回的 `path` 是 **SGME 主机**上的路径；Hermes 与 SGME 不同机（如 Hermes 在本地、SGME 在远端容器）时本地拿不到该文件——此时改用 `sgme_skill_get` 取正文，别去找本地文件。
10. **`sgme_refine_trigger` 会阻塞且花钱**：同步提炼会一直等到完成并真实消耗 LLM 额度；批量补提炼用 `sgme_refine_batch`（异步排队即返），进度用 `sgme_refine_status` 看。

## 卸载

删除 `<HERMES_HOME>/plugins/sgme/`，config.yaml 改回 `memory.provider: holographic`（或其他），重启。
