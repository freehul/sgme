# SGME ST-8 注入测试报告 v0.1（MEMORY.md hook 场景注入）

> 测试日期：2026-08-20 ｜ 测试环境：NAS 192.168.10.10:9910（SGME 1.0.0b3，生产实例）｜ 测试人：SGME 前端验证工程师
> 关联：Backlog ST-8、docs/design/SGME-架构设计-v0.9.md（模板引擎 §25 / 注入契约 §4.2）
> 结论：**端点可用、注入链路真实跑通**，仅只读验证，未写任何 SGME 数据

## 1. 背景与实施计划

ST-8 设计决策（2026-08-09，方向 1）：hook 内容由 SGME 设置确定——内容存 SGME（templates/config），hook 经 HTTP 拉取注入，SCSM 可改。

实施计划（2026-08-11 用户定）：hook 落地先接 SGME 默认模板（templates/*.yaml）跑通注入，待 ST-7 WebUI 模板管理完成后做注入测试。ST-7 验收完成（2026-08-20），本报告即注入测试环节。

## 2. 端点确认（sgme/server/ 代码核对）

| 端点 | 方法 | 鉴权 | 位置 | 用途 |
|------|------|------|------|------|
| /v1/admin/templates | GET | Admin Key | routes_admin.py:678 | 模板列表（分页 limit/offset，items 含 name/display_name/memory_types/token_budget/sections/content） |
| /v1/inject | POST | Agent Key | routes_memory.py:119 | 记忆注入（mode 模板注入 或 custom_filter 自定义查询 → blocks + stats + tier0） |

> 注：任务预期端点 /v1/templates/query 在代码中不存在（全局 grep 无匹配）；实际注入端点为 POST /v1/inject（body: {mode}），与架构契约 §4.2 一致。

## 3. 测试执行（Python requests / urllib，禁代理防 Clash 劫持；key 从 .env 读取不落盘不打印）

### 3.1 GET /v1/admin/templates → 200

返回 count=4 total=4，4 个模板全部可用：

| 模板 | display_name | memory_types（维度） | token_budget | sections |
|------|--------------|----------------------|--------------|----------|
| daily | 日常模式 | identity, family, social, habits, status, focus | 700 | 3（基本信息/近期节奏/当前状态） |
| coding | 编码模式 | tech_stack, style, skills | 600 | 2（技术栈与踩坑/工作方式） |
| work | 工作模式 | goals, social, focus | 600 | 2（目标/关键关系） |
| full | 全量模式 | identity, family, social, values, skills, tech_stack, preferences, habits, environment, style, focus, goals, status | 1200 | 4（用户画像/能力与技术栈/偏好与风格/当前状态） |

### 3.2 POST /v1/inject mode=daily → 200

返回结构（顶层 blocks / stats / tier0，与契约 §4.2 一致）：

```json
stats: {mode: daily, queries: 3, tokens_est: 300, tier0_present: true}
tier0: {present: true, content: <画像摘要 169 字符>}
blocks: 4
```

| block | title | items | 对应模板 section | 查询维度 |
|-------|-------|-------|------------------|----------|
| 0 | 画像摘要（tier0） | 1 | —（tier0 摘要块） | — |
| 1 | 👤 基本信息 | 1 | 基本信息 | identity, family |
| 2 | 📅 近期节奏 | 3 | 近期节奏 | habits |
| 3 | 🔥 当前状态 | 5 | 当前状态 | status, focus |

item 结构：{content, memory_id, relative_time}（画像摘要块 item 仅 content），维度信息体现在 block 按模板 section 聚合的组织上。注入返回**真实记忆数据**（如近期节奏/当前状态均命中最近沉淀的记忆）。

## 4. 维度覆盖验证

- daily 模式注入实际覆盖 **5 个维度**：identity、family、habits、status、focus——与 templates/daily.yaml 各 section query.dimensions 完全一致（基本信息=identity+family；近期节奏=habits；当前状态=status+focus）
- tier0 画像摘要 present=true（NAS 存在画像摘要）
- **注**：任务预期「identity/goals/status 等」——daily 模板的 section 查询**不含 goals**（goals 属 work/full 模板：work「🎯 目标」= goals、full「🔥 当前状态」= focus+goals+status）。按实际模板如实记录。
- 维度 id 全部为注册表 id（identity/goals/status/…），符合架构约束「API 请求侧不收中文名」

## 5. 只读性与副作用确认

- GET /v1/admin/templates：只读 ✓
- POST /v1/inject mode 分支：operations/inject.py 注明**副作用：无**（不写库、不调 LLM、不发信号；仅 custom_filter 分支会记录注入统计 record_inject，本测试未走该分支）✓
- 生产 NAS 未写入任何测试数据；测试脚本置于 worktree tmp/（gitignore，不入库）

## 6. 结论

ST-8 hook 场景注入链路**真实跑通**：hook 可经 HTTP GET /v1/admin/templates 获取模板配置、POST /v1/inject（mode=daily）拉取注入块（tier0 画像摘要 + 3 场景 section，5 维度覆盖，真实记忆数据）。端点/返回结构/维度覆盖三项全部验证通过。Backlog ST-8 状态 🟡 部分完成 → ✅ 已解决。
