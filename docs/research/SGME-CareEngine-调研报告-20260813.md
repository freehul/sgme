# SGME Care Engine 调研报告：市面主流记忆系统的主动关怀与角色机制

> 调研时间：2026-08-13
> 调研方式：3 个并行子代理（研究员A/B/C），官方文档 + GitHub README + 论文，附来源链接
> 调研目的：为 ST-25 Care Engine（主动关怀引擎）设计提供业界参照——主动关怀动作怎么实现、角色系统怎么落地
> 关联：docs/design/SGME-CareEngine设计-v0.1.md

## 核心结论（三句话）

1. **记忆层与动作层分离是行业铁律**——Mem0/Zep/Hindsight/TencentDB 全部只做存储/召回/事件，主动触达全部外置；TencentDB 代码级把宿主 agent 的 cron/heartbeat 会话当噪声过滤（"记忆引擎拒绝主动"是代码级事实）。
2. **主动关怀的成熟形态 = cron + heartbeat 双轨**——cron 做确定性定时（早安问候/待办提醒），heartbeat 做条件式巡检（周期消费信号队列，有新信号才决定是否打扰）；防打扰是设计核心。
3. **角色系统 = 三层分离**——角色定义（角色卡）、用户身份（persona）、关系记忆（lorebook/Facts）各成一层，业界早有"换皮不换芯"先例。

## 一、主动关怀的实现模式（按系统）

### Mem0
- 主动机制：无内建主动推送，纯被动召回。官方把"主动记忆"定义为应用层模式（三种触发：事件驱动/时间驱动/状态驱动）；提醒类示例靠独立后台轮询 + Slack 推送 + `last_notified_at` 幂等去重实现，全部绕开记忆层、由外部 agent 完成。
- 角色：无角色卡机制；companion 示例将 User/Companion 记忆分池存储、分开注入提示词，人格一致性由提示词维护。
- 启示：验证"记忆引擎只发信号、agent 消费并决定触达"的架构方向；提醒需幂等去重防重复打扰。
- 来源：github.com/mem0ai/mem0；mem0.ai/blog/proactive-memory-in-ai-agents-a-developer-s-guide；mem0.ai/blog/building-a-reminder-agent-that-actually-remembers

### Letta / MemGPT
- 主动机制：强主动。heartbeat 机制——工具调用 `request_heartbeat=true` 请求继续循环（默认 false = 克制优先，防脱轨），agent 可自主连续行动、用户不在时后台运行；演进出 sleep-time agents（空闲期后台整合记忆）；LettaBot cron 定时主动发消息（Morning Briefing "0 8 * * *"）+ heartbeat interval 周期性 check-in。
- 角色：memory blocks 机制——core memory 分 persona 块（自我概念/性格/行为准则）与 human 块（用户偏好/事实），全部注入 system prompt；persona 块即事实上的"角色卡"，agent 可自编辑。
- 启示：主动行动需要"可打断的自主循环 + 显式继续请求"——默认不动作、显式请求才继续，天然防打扰。
- 来源：github.com/letta-ai/letta；letta.com/blog/letta-v1-agent；letta.com/blog/memory-blocks；letta.com/blog/sleep-time-compute；arxiv.org/abs/2310.08560；arxiv.org/abs/2504.13171

### Zep（Graphiti）
- 主动机制：基本纯被动召回（graph/语义/全文混合检索，sub-200ms）。唯一主动面是 Webhook 事件推送（episode 处理完成等事件实时 POST 下游）——事件总线模式。
- 角色：无角色卡。个性化靠 User Summary Instructions（开发者定义常驻问题，随交互持续刷新 user 摘要）+ Context Templates（声明式组装注入）。
- 启示：记忆更新事件的先例就是 webhook 事件总线——SGME 的 signal_events 同构；User Summary Instructions ≈ 模板注入思路，可作关怀信号源。
- 来源：help.getzep.com/graphiti；help.getzep.com/webhooks；arxiv.org/abs/2501.13956

### Hindsight（Vectorize）
- 主动机制：被动 retain/recall + 主动 reflect（对存量记忆深层分析、生成新观察/见解——记忆自我演化机制，可支撑"定期反思→产出关怀信号"）。集成层有 Claude Code hooks 事件驱动自动写入记忆。
- 角色：无角色卡。dispositions（信念/偏好随经历持续更新）+ preference-conditioned reasoning，与 world facts/experiences 双通路分开存储；dispositions 最接近"关系/情绪状态"的持久化实现。
- 启示：reflect 可作为关怀触发器蓝本（定期反思记忆池→生成情绪关怀/待办信号）；dispositions 模式提示可为角色外皮维护"关系/情绪状态"标签。
- 来源：github.com/vectorize-io/hindsight；arxiv.org/abs/2512.12818

### OpenViking VikingBot（volcengine）
- 主动机制：多通道 agent（Telegram/Feishu/Slack 等）+ 内置 cron 工具 + HEARTBEAT.md 心跳检查（默认 600s 间隔）：agent 周期性读 HEARTBEAT.md 待办，主动经通道推送——"记忆 + 定时主动触达"结合最完整的开源样例。
- 来源：github.com/volcengine/OpenViking/bot/README.md

### TencentDB-Agent-Memory
- 主动机制：**无主动推送**。Chat Memory 纯被动管道：capture 记录 L0 → 异步提炼 → auto-recall 每轮用户消息前自动检索注入。代码级证据：session-filter.ts 把 cron/heartbeat/automation 会话（SKIP_TRIGGERS）明确排除出采集——系统主动把宿主 agent 的主动会话当噪声过滤；gateway 只有 /capture /recall /search 端点，无任何 push/notify/webhook。"主动"完全由宿主（OpenClaw 的 cron/heartbeat）承担，记忆引擎本身拒绝主动。
- 角色：团队 Memory Hub——Fixed Binding + ACL（private/team/restricted/agent 四级可见性）、Agent Loadout 资产绑定决定谁的记忆进谁的上下文。
- 启示：与 SGME"信号总线产出事件、消费者 agent 消费"的设计同构——记忆引擎保持被动，关怀逻辑必须放消费者侧。
- 来源：github.com/TencentCloud/TencentDB-Agent-Memory（README + 本地克隆代码级验证）

## 二、角色系统（"皮"）的实现模式

### 角色卡生态（Character Card V2 / SillyTavern）
- 角色卡是事实上的行业标准"角色定义格式"：JSON 按 chara_card_v2 spec 嵌入 PNG 的 tEXt chunk（关键字 chara），一张图即一个角色。
- V1 字段：name / description / personality / scenario / first_mes / mes_example；V2 新增 system_prompt（角色级系统提示）、post_history_instructions（回复后指令）、alternate_greetings（多条开场白）、character_book（内嵌世界书）、extensions（任意应用数据命名空间）。
- SillyTavern 组装顺序：description→scenario→personality→system→persona（用户）→lore→示例对话→聊天历史，{{char}}/{{user}} 宏替换。
- 记忆机制：上下文窗口 / Summarize（LLM 压摘要，最可靠）/ Vector RAG（质量不稳）/ Lorebook 世界书（关键字触发、token 预算、优先级）。世界书=世界记忆、角色卡=角色定义、persona=用户身份，三层完全分离。
- 主动联系：SillyTavern 无主动推送，生态有 Auto-Reply 等准主动扩展——真正主动依赖"调度器+推送通道"基建，角色卡格式不包含这部分。
- 来源：github.com/malfoyslastname/character-card-spec-v2；SillyTavern 文档

### Replika
- 人设：平台托管单一伴侣（官方系统提示词 + 用户可调性格维度，底层 prompt 不可编辑）；换名字/换形象不丢记忆——"皮"（形象/声音/名字）与"芯"（记忆）在用户侧已解耦。
- 记忆：Memory tab 显式事实条目（用户可问"你都知道我什么"、可手动增删）+ Diary 日记（AI 自动写、可回看）+ 深层模式提取；短期上下文窗口很窄（约 25 条消息）。
- 主动联系：成熟先例——用户开启通知后全天发消息：check-in、跟进上次话题、心情问候；推送在应用关闭时也到达；主动消息内容必须基于记忆生成（跟进上次聊过的事）才不显廉价——调度+推送+个性化内容三者缺一不可。
- 来源：help.replika.com（memory/notifications 文档）

### Character.AI
- 人设：角色定义=创作者填写的结构化字段（name/greeting/definition 自由文本 ≤32k 字符），官方建议按人格特质/说话风格/背景故事/动机/行为规则组织；业界经验"行为指令优于形容词堆砌"。Scene 场景机制把"情境"与"角色"分离——任何角色可套入同一场景（官方"换皮不换芯"先例）。
- 记忆：Chat Memories（用户手写 ≤400 字符长期注入）+ Pinned/Story Memory（Pin 锁定关键情节）+ Facts（自动抽取，分 tab 展示，可编辑/禁用/删除）；新开聊天可"复制 Facts 到新对话"跨会话延续。**记忆以角色为单位绑定，跨角色不共享（硬伤）**。
- 主动联系：已上线推送通知与"离线收到角色消息"；Meta 跟进同类功能并点名 C.AI/Replika：只允许用户发起对话后 14 天内、且用户至少发了 5 条消息时 bot 才能主动跟进，无回应即停止——频控与退避策略是必备设计。
- 来源：blog.character.ai/memory；techcrunch.com/2025/07/03/meta-has-found-another-way...

### 星野 / Talkie（MiniMax）
- 人设：用户"捏"角色，自由定制形象/声音/人设/技能四要素；角色即 UGC 内容资产（可交易卡牌"星念"）。
- 记忆：核心机制"羁绊"——基于 RAG 的外挂记忆库：建立羁绊后角色隔 N 天回来还记得你是谁，否则聊 20 来回就"出戏失忆"；Talkie 官方含"回溯/记忆/重启/评价/事件簿"，事件簿显式记录关键事件、用户可控。
- 主动联系：最激进先例——AI 深夜（约 22:30 后）主动拨打语音电话；主动联系在中国市场被证明是强留存引擎，但引发隐私与打扰争议。
- 来源：品玩/凤凰网/36kr 报道

## 三、persona 生成方法论（TencentDB 代码级细节）

- L3 Persona 不是简单摘要：完整 LLM agent（CleanContextRunner，开工具、180s 超时、沙箱）直接写 persona.md。
- 触发五条件：P1 Agent 显式请求更新 / P2 冷启动（首次提取完成且有 scene 文件）/ P2.5 恢复（persona.md 丢失）/ P3 首个 Scene Block 生成 / P4 阈值（新记忆数 ≥ 50 条）。
- 生成：先读 scene index，按 last_persona_time 找"自上次更新后变化的场景"做 **delta diff**，只把变化场景全文喂给 LLM。
- 提示词：Persona Architect - Incremental Evolution Protocol 四层深度扫描——L1 基础锚点（事实/破冰话题）→ L2 兴趣图谱（谈资/活跃度分级）→ L3 交互协议（沟通习惯/雷区）→ L4 认知内核（决策逻辑/驱动力）。
- 输出约束：固定模板（Archetype 一句话 + Basic Info + Long-term Preferences + 4 章叙事含 Emergent Traits 3-7 个标签），≤2000 字符、禁止过度推测、内容只准来自场景数据。
- 更新：增量式（edit 局部替换；首次 write 整体）；写前备份 3 份；写后 sanitize；checkpoint 推进 last_persona_at。
- 注入优先级：L3 persona + L2 场景导航 → system prompt 尾部（稳定内容、每轮必带）；L1 原子记忆 → 用户消息前（每轮按 query 检索）；需具体事实时 BM25+向量+RRF 回落到 L1/L0。

## 四、启示清单（对 SGME）

1. 记忆层与动作层分离：SGME 只发布记忆更新事件（signal_events），关怀决策留给消费 agent——与 Zep webhook / TencentDB 同构，引擎不内嵌调度。
2. 主动触达防打扰：默认静默、按信号阈值触发（冲突裁决/高情绪标签/到期待办）、幂等去重（last_notified_at）、情绪信号冷却期、无回应指数退避。
3. 角色外皮 = 独立 persona 块/角色卡注入 system prompt，与用户记忆严格分池——SGME 增设角色层数据结构，切换角色整体替换注入，记忆池不动。
4. 结构化状态支撑个性化：维护"用户画像摘要 + 关系/情绪状态"标签层，既作关怀信号源又作角色外皮上下文。
5. 事件驱动与定时调度混合：写路径事件（记忆新增/裁决后立即通知）+ 定时心跳（每日关怀扫描/待办检查）统一成信号流，两种触发共用同一关怀决策管线。
6. 角色卡 V2 兼容子集作为"皮"的标准格式；extensions 命名空间挂关怀策略（问候模板/触发规则/频率档位）。
7. 差异化卖点：C.AI 角色记忆不跨角色共享、Replika 单一伴侣、星野靠羁绊 RAG 部分挽回——SGME 记忆独立于角色，"换角色只换皮，个人记忆完整延续"。
8. 情感信号最小权限：情绪/脆弱类记忆默认 private，仅用户显式装备的关怀 agent 可读——关怀系统不能变成隐私泄露面。
9. 物化画像省 token：SGME 用户画像保持模板查询零物化；角色 persona 场景可物化（一次生成、稳定注入）。

## 来源索引

- Mem0: https://github.com/mem0ai/mem0 | https://mem0.ai/blog/proactive-memory-in-ai-agents-a-developer-s-guide
- Letta: https://github.com/letta-ai/letta | https://www.letta.com/blog/letta-v1-agent | https://www.letta.com/blog/memory-blocks
- Zep: https://help.getzep.com/graphiti | https://help.getzep.com/webhooks
- Hindsight: https://github.com/vectorize-io/hindsight | https://arxiv.org/abs/2512.12818
- OpenViking: https://github.com/volcengine/OpenViking
- TencentDB: https://github.com/TencentCloud/TencentDB-Agent-Memory
- Character Card V2: https://github.com/malfoyslastname/character-card-spec-v2
- Replika: https://help.replika.com | Character.AI: https://blog.character.ai/memory
- MemGPT 心跳: https://arxiv.org/abs/2310.08560 | https://arxiv.org/abs/2504.13171
- 星野: https://www.pingwest.com/a/294725 | https://m.36kr.com/p/2587566019115905
