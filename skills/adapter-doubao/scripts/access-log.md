# SGME 接入日志（豆包工作 / Doubao Work）

> 接入时间：2026-09-19 ｜ Agent：豆包工作（Doubao Work，Windows 桌面）｜ 版本：SGME 1.2.2
> 本文件为模板；部署副本 `<DOUBAO_SKILLS_DIR>/sgme-bridge/scripts/access-log.md` 由 install.py 累积维护。
> （文档内地址/路径/姓名一律占位符：`<NAS_IP>` / `<DOUBAO_SKILLS_DIR>` / `<project-root>` / 用户。）

## 身份登记

| 项 | 值 |
|---|---|
| 名称 / 版本 | 豆包工作（Doubao Work）官方适配器 / adapters/doubao |
| 运行平台与进程 | Windows 桌面客户端（豆包工作 agent 会话），无常驻进程（短连接型 + 可选 care_watch 轮询） |
| 调用面 | HTTP API `http://<NAS_IP>:9910`（scripts/sgme_client.py，零依赖）+ MCP `http://<NAS_IP>:9913/mcp`（同客户端 MCP 层，需 mcp 库） |
| 配置来源 | 环境变量 `SGME_AGENT_KEY`（agt_*，管理员签发）+ `SGME_ADMIN_KEY`（写侧/管理能力）；地址见部署副本 `scripts/client.env` 或环境变量 `SGME_HTTP_URL`/`SGME_BASE_URL` |
| 身份文件 | `adapters/doubao/SKILL.md`（真源）→ 部署 `<DOUBAO_SKILLS_DIR>/sgme-bridge/SKILL.md`（含 SGME-ONBOARDING-v2 官方模板 + 41 能力矩阵） |

## 权限边界

- **能做（agent key）**：health / append / inject / search / answer / memory_get / memory_reject / memory_unreject / events_pull / events_after / skill_list / skill_coldstart / skill_search / skill_digest / skill_get / skill_materialize / wiki_search / wiki_pages / wiki_page / wiki_page_add / wiki_page_update / wiki_raw（HTTP）；agent_onboarding / refine_trigger / refine_batch / refine_status / stats / config_get / idea_add / demand_create / project_register / signal_pull / signal_claim / signal_ack / role_list / role_assemble / role_active_get / role_active_set / wiki_evolve_trigger（MCP）。
- **能做（需 SGME_ADMIN_KEY）**：skill_put / skill_delete / skill_rename（MCP 服务端强制管理员 Key）；config_update / signal_clear（有管理员 Key 时自动使用）。
- **不能做**：admin HTTP 接口（`/v1/admin/*`）——agents 注册/撤销、HTTP 端 refine、待办状态流转等，须管理员 Key（HTTP 403）。
- **网络**：忽略代理环境变量（防代理劫持内网），仅访问内网 SGME。

## 能力面覆盖（2026-09-19，对齐 MCP 基准 41 个）

| 项 | 状态 |
|---|---|
| 方法层（sgme_client.py class SGME） | 41/41（基准业务方法）+ 5 项自有扩展（events_pull / events_after / wiki_raw / mcp_raw / describe 诊断） |
| CLI 命令层（能力面 = 命令面） | 41/41 基准子命令 + 6 个扩展命令（events-pull / events-after / wiki-raw / mcp / capabilities / env-info）= 47 个 |
| 离线自证 | `python scripts/sgme_client.py capabilities` → `41/41` |
| 自检 | `python scripts/selfcheck.py` → 静态矩阵 41/41 + 连通性 14 项全绿 |

## 连通性自检（2026-09-19）

| # | 检查项 | 结果 |
|---|---|---|
| 1 | health（服务可达） | ✅ status=ok，向量库与提炼水位正常 |
| 2 | search（记忆检索） | ✅ 返回记忆并带溯源 |
| 3 | inject（画像注入） | ✅ 返回画像块摘要（含用户画像/项目关键词） |
| 4 | append（L0 写入） | ✅ 接入记录落盘并提炼为记忆「Doubao Work（豆包工作）正式接入 SGME。」 |
| 5 | skill_search（技能检索） | ✅ 技能索引可检索（四位数规模） |
| 6 | wiki_search（知识库） | ✅ |
| 7 | events_pull（事件游标） | ✅ |
| 8 | MCP stats | ✅ |
| 9 | MCP config_get | ✅ |
| 10 | MCP refine_status | ✅ |
| 11 | MCP role_list | ✅ |
| 12 | MCP role_active_get | ✅ |
| 13 | MCP signal_pull | ✅ |
| 14 | 静态能力矩阵（41 基准） | ✅ 41/41 双覆盖 |

## 变更登记

- 2026-09-19：首次接入（sgme-bridge 技能 + 接入记录）。
- 2026-09-19：升级为官方适配器 `adapters/doubao/`（客户端 sgme_client.py 双层通道 + care_watch + selfcheck + install + 测试），部署副本由 install.py 管理。
- 2026-09-19：**登记进 SGME 官方适配器清单**（`AI-INSTALL/agent-onboarding.md` §6 + README 中英两版放置位置表），agent_id=`doubao-work`（声明式，未注册 admin agents 表——注册需管理员 Key）。
- 2026-09-19：**agent_id 正式注册 + key 轮换**——用管理员 Key 调 `POST /v1/admin/agents/register` 注册 `agent_id=doubao-work`（role=agent，scope=doubao-work），签发专用 `agt_*` key 并轮换为 `SGME_AGENT_KEY`（密钥只存环境变量，不落仓库；User 级环境变量已持久化）。自检 9/9 通过（新 key 实测）。
- 2026-09-19（入库前整改）：**数据卫生清理 + 能力面补齐**——
  - 数据卫生：源码默认地址由真实内网地址改为回环 `http://127.0.0.1:9910`（生产地址走环境变量/部署配置注入）；部署路径改 `os.path.expanduser("~")` 推导；文档地址/路径/姓名全部占位符化。
  - 地址保活：`install.py` 部署时把解析出的地址写入部署副本 `scripts/client.env`；解析顺序「环境变量 → 既有部署配置 → 旧部署副本地址（迁移沿用）→ 回环默认」，重跑部署不会让豆包侧失联；仓库内不出现真实地址。
  - 能力面：补齐 9 个缺失基准能力（wiki_evolve_trigger / stats / config_update / signal_clear / role_active_get / role_active_set / skill_put / skill_delete / skill_rename），CLI 命令面由 15 个扩到 41 个基准全覆盖；并修正 3 处与基准签名不符的调用（idea_add / project_register / signal_pull）。
  - 自检与测试：selfcheck 增静态能力矩阵核对（41/41）+ 实测 14 项；补 CLI 命令面与新增方法的单元测试。
