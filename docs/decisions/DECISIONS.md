# SGME 决策记录（DECISIONS）

> 记录关键架构/产品决策：选择 + 原因 + 代价。格式：日期 | 决策 | 原因 | 代价 | 关联条目。

---

## D-2026-08-09-01：pre_llm_call hook 注入内容由 SGME 设置确定（方向 1：内容存 SGME，hook 经 HTTP 拉取）

**日期**：2026-08-09
**关联**：ST-8 / Hermes memory provider 体系

### 决策

pre_llm_call hook 的注入内容**由 SGME 设置确定**，实现采用**方向 1**：

```
SGME 侧（内容权威）：
  templates/*.yaml（注入内容定义：模式/sections/预算）
  config/sgme.yaml 或 DB（选哪个模式、开关）
        ↑ HTTP（/v1/inject 或未来 /v1/hook_config）
Hermes pre_llm_call hook（纯执行者）：
  拉取 → 注入 user message（零内容逻辑）
```

**否决方向 2**（SGME 设置界面直接写 Hermes hook 脚本文件）。

### 原因

1. **单一真相源**：注入内容只在 SGME，改设置只改一处；方向 2 内容散落到 Hermes 侧
2. **多实例共享**：PC/NAS/笔记本多个 Hermes 实例从同一 SGME 拉取，设置一次到处生效
3. **换 Agent 可移植**：任何 Agent（Claude Code/Codex 等）的 hook 都能从 SGME 拉同一份内容——符合"SGME 产品化、不限于 Hermes"定位
4. **SCSM/WebUI 天然可改**：设置界面写 SGME 配置/模板，hook 行为自动跟着变
5. **脚本纯净**：hook 脚本只是管道，升级/覆盖不丢内容

### 代价

- hook 依赖 SGME daemon 在线（fail-open 兜底：daemon 挂 = 不注入，不影响对话）
- 需要 SGME 侧提供配置读取接口（现有 /v1/inject 已覆盖模板注入；如需 hook 专属配置要加端点）
- 实现成本高于方向 2（多一个 HTTP 拉取层）

### 当前状态

- Hermes 侧：hook 已停用（config.yaml `hooks: {}`），因为 SGME 插件（active memory provider）的 prefetch 已覆盖每轮场景检索
- 脚本 `$HERMES_HOME/scripts/pre_llm_call_sgme.py` 保留，未来按此决策改造（内容从 SGME 拉取，而非脚本内置检索逻辑）
- 待办：ST-8 范围内——实施计划已定（2026-08-11 用户定）：hook 落地先接 SGME 默认模板（templates/*.yaml）跑通，待 SCSM/WebUI 模板管理（ST-7）完成后做注入测试

---

## D-2026-08-09-02：五层价值演化链 + 双层存储架构（创意池/需求池/项目/问题/PR）

**日期**：2026-08-09
**关联**：ST-14~ST-17 / 设计文档 `docs/design/SGME-创意池与需求池设计-v0.1.md`

### 决策

建立**五层价值演化链**：创意池 → 需求池 → 项目 → 问题（issues）→ PR。
**双层存储**：SGME（全局层：创意池/需求池/项目注册表/项目投影）+ 项目目录（工程层：代码/.issues/PR/文档，git 管理）。

### 关键子决策

1. **创意池命名优于需求池**：需求带"必须解决"的承诺包袱，创意是"可能性种子"无门槛。创意池=对话副产品全量留存；需求池=用户明确想要的宽泛功能
2. **捕获自动化 + 升格人工**：SGME 提炼提示词加"创意/需求提取"方向（自动捕获，解决漏记）；人工在 UI 修正/完善/标记/升格（唯一人工动作）
3. **创意长期保存（无 TTL）**：想法不设过期，区别于普通记忆（TTL 7d~90d）
4. **项目级问题追踪 = git 当库 + 文件当 issue**：`.issues/NNN-title.md` 文件（frontmatter）+ commit 规范（Closes #N）；**不建 SQLite 物化视图**（YAGNI，AI 查询自然通道是文件/检索非 git 命令；物化视图触发条件=单项目 100+ 问题且查询痛点出现）
5. **项目不搬进 SGME，SGME 登记项目 + 记忆项目**：项目注册表（轻量元数据）+ 项目投影（提炼记忆）；SGME 存"关于项目的事实"，项目目录存"项目本身"
6. **删除=标记 discarded 可恢复**（Supersession 原则，非物理删除）
7. **追踪终点 = issue closed；体系闭环 = 记忆回写 SGME**（环形无绝对终点）

### 原因

- 对话是思维闪光第一现场，1G 会话库是静态存档，人工记录会漏——需要自动捕获层
- AI 查询自然通道是读文件/检索，git log 反直觉——git 回归版本控制本职
- 多 Agent 协作（Hermes/WorkBuddy/Trae）写共享状态，文件+commit 是唯一无锁方案
- SGME 提炼链路已存在，加提取方向成本最低；产品化定位要求全局视野

### 代价

- 提炼提示词改造有提炼质量风险（需要测试）
- 文件即 issue 在 100+ 问题规模时查询变慢（触发条件已定义）
- 创意池 UI 依赖 SCSM/WebUI（ST-7 未完成前靠 AI 手动操作）

### 当前状态

- 设计文档 v0.1 已产出；Backlog ST-14~17 已建
- 待办：评审设计 → 拆任务（提炼提示词改动/维度注册/UI）
