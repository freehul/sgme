# SGME 主动关怀决策链记录（2026-08-13）

> 记录 Care Engine（ST-25）从问题提出到设计定稿的完整决策过程
> 关联：docs/design/SGME-CareEngine设计-v0.1.md、docs/research/SGME-CareEngine-调研报告-20260813.md
> 类别：design（决策记录）

## 起因：用户三问（2026-08-13）

用户盘点 SGME 实际价值，连续三问：

1. 维度在实际使用中的作用有多大？
2. L1.5 和 L2 在实际使用中的作用有多大？
3. 作为个人记忆引擎，对个人的帮助有多大？核心的"主动关怀"功能有没有？

## 数据盘点（生产库实测，回答三问）

- 维度：11199 条记忆全部带标签（16 维分布：projects 6749 / tech_stack 6169 / environment 1874 / preferences 1852 / tasks 1391 / status 1293 / focus 1012 / goals 923 / values 661 / skills 616 / habits 565 / style 434 / identity 147 / social 110 / family 44 / ideas 7）——贯穿注入过滤、TTL 生命周期、L1.5 候选池、搜索过滤四条链路。
- L1.5：3572 次提炼累计动作——store 9704 / **update 2236 / merge 2121** / skip 421 / create 209。update+merge 占处理总量约 30%——每 3 条处理就有 1 条是修正/合并；memory_archive 2447 条归档可溯源。结论：L1.5 是最被低估、实际最值钱的层。
- L2：220 个场景已产出，但消费端较薄（不参与注入，主要靠搜索召回）——锦上添花。
- **信号总线：signal_events 2134 条事件（memory_updated 等）已发，consumed_at 全空——无消费者。**

## 关键结论：主动关怀未实现，但地基已铺好

用户核心诉求"主动关怀"（2026-07-06 提出：情绪洞察主动关心 + 工作计划提醒，Care Engine 方向确认过）**未落地**——代码无 care/情绪/提醒模块。但信号总线（2134 条事件）、Dream 定时器、事件流都已就绪，缺的正是消费者。

## 立项：ST-25（六步）

1. 建目录：SGME 子模块（与 Dream 同级），不独立建项目
2. Backlog 登记 ST-25（AC 草案 5 条）
3. 设计文档占位 docs/design/SGME-CareEngine设计-v0.1.md
4. SGME 登记 /v1/append（projects 维度，file_id c0eb1b96）
5. 需求池关联：demands 表为空，需求源头为记忆池确认记录
6. git commit（32bee87）

## 信号消费归属决策（用户拍板）

原设计参考 SCSM：由 SCSM 消费信号。SCSM 不存在时由谁执行？

**用户决策：由使用 SGME 的 agent 消费**（Hermes 等；SCSM 存在时接管）。
与历史决策一致（"SGME 只是记忆系统，不应具备 agent 决策能力"）：SGME 只发信号、只存记忆、只提供角色数据；关怀决策/触达全在消费方。业界验证（TencentDB 代码级"记忆引擎拒绝主动"）确认该架构正确。

## 调研（3 并行子代理，2026-08-13）

- 研究员A：Mem0/Letta/Zep/Hindsight/MemGPT 主动机制与角色
- 研究员B：Replika/Character.AI/星野/角色卡生态（CC V2 spec）
- 研究员C：TencentDB persona 代码级细节 + 主动推送先例（MemGPT heartbeat/OpenViking/OpenClaw/Mem0 ProMem）

完整报告见 docs/research/SGME-CareEngine-调研报告-20260813.md。核心发现：

1. 记忆层与动作层分离是行业铁律；
2. 主动触达成熟形态 = cron + heartbeat 双轨，防打扰是设计核心（默认静默/幂等去重/冷却期/退避）；
3. 角色系统 = 三层分离（角色卡/用户 persona/关系记忆），CC V2 是现成"皮"标准；
4. 差异化机会：C.AI 角色记忆不跨角色共享、Replika 单伴侣、星野羁绊 RAG——SGME"换角色只换皮，记忆完整延续"是竞品硬伤。

## 三个决策点拍板（用户确认）

1. **落地形态**：SGME 侧只做「信号增强 + 角色层数据结构」；关怀决策/触达全在消费方 agent。SGME 内核保持零决策、零推送通道。
2. **角色文件格式**：Character Card V2 兼容子集（name/description/personality/scenario/first_mes/mes_example/system_prompt/post_history_instructions/character_book + extensions 挂关怀策略）。
3. **零物化例外**：用户画像保持模板查询零物化（架构铁律不变）；**角色 persona 允许物化文件**（一次生成、稳定注入省 token）——角色是皮，物化无妨；芯（记忆池）保持零物化。

## Task 拆解（T-35~T-38）

| 编号 | 内容 | 侧 | 备注 |
|------|------|----|------|
| T-35 | 角色层数据结构（CC V2 兼容子集 + 角色 persona 物化生成） | SGME | 先做，是一切的地基 |
| T-36 | 关怀信号增强（信号类型枚举 + 事件标记规则 + 与 Dream 协同） | SGME | |
| T-37 | 角色注入装配（角色 persona + 画像模板查询 → 沟通提示词；读写闭环） | SGME | |
| T-38 | 关怀消费方（Hermes 插件 cron + heartbeat 双轨） | 消费方 | 涉及插件部署副本，需先设计 |

## 产品方向（用户确认）

- 核心不变：永远是"属于个人的记忆系统"——多 agent 可接入，但只为一个人服务；
- 角色 = 外皮：管家/伴侣/朋友等沟通方式封装，不改变记忆内核；注入模板（选什么记忆）与沟通角色（怎么说话）正交；
- 面向不同群体：角色系统是产品化抓手——同一引擎，不同角色适配不同用户群体。

## 实施期收敛项（未决）

1. 角色存储位置（roles/ 目录 vs 数据库）与 WebUI 角色管理页
2. 情绪标签来源：提炼提示词补情绪维度 vs 信号侧推断（费 token 权衡）
3. 推送通道形态：Hermes 消息 vs 桌面通知 vs 日报式沉淀（消费方定）
