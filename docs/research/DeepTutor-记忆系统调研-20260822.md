# DeepTutor 记忆系统调研报告

> 调研日期：2026-08-22
> 调研对象：HKUDS/DeepTutor（港大数据智能实验室开源，Agent-Native 终身个性化 AI 导师）
> 仓库：https://github.com/HKUDS/DeepTutor （v1.5.2，⭐~36.9k，主语言 Python）
> 源码本地路径：D:\GitHubDownloads\DeepTutor
> 调研动机：对照 SGME（ShiGuang Memory Engine）记忆引擎，提炼可借鉴的工程与提示词设计

---

## 1. 项目背景与定位

DeepTutor 是港大 2026-01 开源的 AI 教育项目，核心定位是「让 AI 真正辅助学习，而非聊天」。其论文《Towards Agentic Personalized Tutoring》提出 **Agent-Native 架构**：围绕学习目标设计一整套可协作的 Agent 工具链（解题、出题、深度研究、数学动画、持久化助教）。

记忆系统是它的「统一个性化底座」——论文称其为 *hybrid personalization engine*，把静态知识锚定与动态多分辨率记忆耦合，将交互历史提炼为持续演化的学习者画像。

**与 SGME 的同构性**：两者都追求「跨会话不丢上下文 + 持续演化的用户画像」，但实现路径不同——SGME 以 sqlite-vec 向量库为主，DeepTutor 以「纯 Markdown 文件 + 脚注引用溯源」为主，且带一套独立的 LLM 提炼流水线。

---

## 2. 总体架构：三层记忆

| 层 | 物理存储 | 内容 | 写入方 |
|---|---|---|---|
| **L1 原始事件** | `memory/<user>/trace/<surface>/<YYYY-MM-DD>.jsonl`（append-only）+ `snapshot/<surface>/`（实体全量 + fingerprint + `changes.jsonl`） | 每轮对话、每次测验、每次 KB 查询的原始记录 | 各 surface 事件钩子，纯落盘零 LLM |
| **L2 单面摘要** | `memory/<user>/L2/<surface>.md`（7 个 surface 各一个） | 每个 surface 的 Markdown 摘要，每条事实 ≤240 字符 + 脚注引用指向 L1 实体 id | LLM 增量提炼 |
| **L3 跨面画像** | `memory/<user>/L3/<recent|profile|scope|preferences>.md`（4 个槽） | 跨 surface 的用户画像、知识范围、近期动态 | 从 L2 新增条目再提炼一层 |

引用链（溯源链）：**L3 → L2 文件 → L1 原始 trace**。L3 引用刻意指向 L2 *文件*（surface 名）而非条目 id，设计者注释称这是为了给用户「干净的 7 级脚注链」（L3 → L2 md → L1 raw traces）。

7 个 surface：`chat / notebook / quiz / kb / book / partner / cowriter`
4 个 L3 槽：`recent（近期动态）/ profile（用户画像）/ scope（知识范围）/ preferences（偏好）`

**关键约束**：`preferences` 槽**永不自动提炼**，只能由 `write_memory` 工具在用户显式声明偏好时写入。

---

## 3. 各层详解

### 3.1 L1 — 原始事件层（永不丢失）
- `trace.append()`：每次事件以 JSONL 追加；**全程 try/except 包裹、log-and-swallow，永不抛出**，保证产生 surface 不被记忆写坏。
- 每 surface 一个 asyncio 写锁，防并发 JSONL 行交错。
- `snapshot`：当前实体全量（`state.json` 存 `{entity_id: fingerprint}` + `labels` + `last_refresh`）+ `changes.jsonl` 增量日志（指纹 diff 出新增/删除/更新）。
- `meta.json` 记录「已见 id 集合」，使 L2 提炼可**只处理新增事件**（增量而非全量重读）。

### 3.2 L2 — 单面摘要层
- 每个 surface 有专属 `focus`（提炼关注点）与固定 `section` 目录，例如：
  - `quiz`: 错误模式 / 强项 / 弱项
  - `kb`: 兴趣 / 高频查询 / 知识库缺口
  - `chat`: 持续误解 / 已掌握概念 / 反复出现的话题
- Markdown 每条事实形如：`- <text> [^1][^2] <!--m_xxx-->`，脚注 `[^1]: chat:def` 指向 L1 实体 id，`<!--m_xxx-->` 是条目锚点（存活于往返，供 audit/dedup/删除用）。

### 3.3 L3 — 跨面画像层
- `profile` 强制要求：**多条 L2 证据跨 surface 支撑**才允许写，禁止未直接证实的性格推测。
- `scope` 给每个已涉概念打置信标签：`familiar / practicing / unsure`，且必须绑 L2 证据。
- `recent` 是 1–4 周的滚动时间线，时间锚定、surface 归属。

---

## 4. Consolidator 提炼流水线

四个模式（`deeptutor/services/memory/consolidator/modes/`）：

| 模式 | 作用 | 关键设计 |
|---|---|---|
| **update** | 增量抽取事实（L1→L2 或 L2→L3） | chunk 边界切分 + 引用池校验；无新输入则只更新 meta 时间戳 |
| **audit** | 对照原始证据做行级编辑 | 渲染带原始全文标注的行视图，按 chunk 审计 |
| **dedup** | 迭代行级合并/删除 | 0 编辑即早停；迭代次数是上限而非配额 |
| **merge** | 无 LLM 的脚注合并 | collapse 重复引用为单一脚注，纯函数 |

### 4.1 Chunk 切分策略（`chunker.py`）
- 字符级切分，目标大小 = `clamp(ceil(len/budget), min, max)`。
- **边界扩展**：每个 chunk 右边缘向前扩展到下一个段落/句子边界，**绝不截断半句话**。
- 相邻 chunk 重叠 10%（`overlap_ratio`），跨切分的事实也能被完整读到。
- 纯函数、无 IO 无 LLM，易单测。

### 4.2 引用池校验（防幻觉核心）
- 每个 chunk 附带「本 chunk 可引用 id 清单」（`_chunk_with_ref_header`）。
- LLM 产出的每条事实必须带 ≥1 个**在当前 chunk 引用池内**的有效引用；无效引用的事实直接 `refs_dropped` 丢弃（`validate_fact_refs`）。
- 提炼 prompt 硬规则：**「No refs → do not emit」（无引用就不产出）**。

---

## 5. 提示词纪律（`prompts/{en,zh}.yaml`）

这是最值得 SGME 抄的部分——用规则把「AI 越描越夸张」的毛病压住：

1. **禁用绝对化词汇**：deeply / truly / mastered / expert / passionate / loves / hates / always / never / fully understands。除非是用户原话，且必须用「」包起来。
2. **强制对冲模板**（L3）：claim 必须形如「Across N `<surface>` interactions, the user X」——把观察**绑定到计数或 surface**，杜绝空泛定性。
3. **长度上限**：每条 `text` ≤240 字符，要求 terse（简洁）。
4. **动词短语优先**：用 "uses X" / "prefers X over Y" / "stuck on Z" 而非形容词。
5. **删除必须给理由**（枚举）：contradicted / superseded / stale / low-signal。
6. 空 `ops` 是合法且预期的输出（无变化时不必硬写）。
7. 只输出 JSON，禁 markdown 围栏、禁解释性散文。

---

## 6. 记忆读取与注入

### 6.1 工具
- `read_memory`：返回 `read_l3_concat()`（4 个 L3 槽拼接）。工具描述明确写「用于个性化语气/深度/举例——**不要每轮都调**，纯事实问题不需要」。
- `write_memory`：**唯一**的 chat 写入通道，只写 `preferences` 槽，且只在用户显式声明偏好时调用。

### 6.2 注入策略（token 成本控制）
- 记忆**不是每轮全量塞 system prompt**：由客户端 `memory_references` 按轮次**选择注入**（按需）。
- system prompt 中 `memory` 块仅在 `memory_context` 非空时追加（`prompt_blocks.py`），且 volatile 内容故意不进 system 块以保持前缀稳定。

### 6.3 Recall 模块（廉价安全读）
- `recent()` / `recent_queries()`：**只读标题/时间戳（stamps），不碰正文**，因此可放在交互路径（页面加载）而非仅提炼时调用。
- **`days_ago` 预计算**：返回时直接算好「几天前」，注释原话——让模型做日期算术不可靠。
- 去重：同一 (surface, label) 只保留一条，防止单话题刷屏挤掉其它内容。

---

## 7. 工程化细节

- **原子写**：所有落盘走 `tmp + os.replace()`（POSIX 天然原子）。
- **按文件粒度锁**：`_write_locks` 字典按路径分配 asyncio.Lock。
- **增量提炼**：`meta.json` 的 seen-id 集合避免重复处理历史。
- **幂等偏好写入**：`write_memory` 加重复检测短路——同文本 casefold 去重（issue #647：长会话中模型爱重复调用同一写入），返回已有条目而非追加副本。
- **前向兼容迁移**：v1→v2 把散落文件移入 `backup/<ts>/`；`tutorbot`→`partner` 改名时连 L2 正文/脚注/prose 里的 token 一并重写，且全部幂等（已存在则跳过）。
- **纯函数 + 测试友好**：`parse/serialize` 幂等往返（任意 `serialize` 产物都能 idempotent 还原），chunker 无 IO。

---

## 8. 对照 SGME：可借鉴点更正（2026-08-22 经注入 SGME 源码核对）

> **勘误说明**：本节初版在未注入 SGME 源码的情况下写成，部分「对 SGME 价值」映射有误——把 SGME **已有**的能力当成了缺口（如增量提炼、days_ago、场景化注入）。
> 2026-08-22 已实际注入 SGME 源码核对：`sgme/engine/{pipeline,refine,l1,l15}.py`、`sgme/profile/{inject,tier0}.py`、`sgme/raw/store.py`、`prompts/l1_extraction.txt`、`sgme/data/memory_dao.py`。
> 结论：**DeepTutor 真正值得 SGME 借鉴的只有 2~3 条；其余多数为 SGME 已有能力的重复或设计哲学差异，已逐条撤回。**

### 8.1 真正可借鉴（SGME 当前缺失或弱）

| # | 亮点 | DeepTutor 做法 | SGME 现状 | 结论 |
|---|---|---|---|---|
| A | **生成时引用池护栏** | 每条事实须引用「当前 chunk 实际存在的消息 id」，无效引用（`validate_fact_refs`）直接丢弃 | SGME 有**存储级**溯源：`source_message_ids` / `source_ref=file_id:seq` + L1.5 冲突裁决 + `anomaly_warn`，但未见「生成阶段强制引用池 + 丢弃未溯源断言」的硬约束 | **增量护栏**：可在 `l1_extraction.txt` 加「每条记忆须对应真实存在的 source_message_ids 序号，无法对应则丢弃」 |
| B | **反夸张 / 谦抑护栏** | 禁用绝对化词（deeply/truly/mastered/loves/never）+ 强制对冲模板「Across N interactions」+ 动词短语优先 + 删除须枚举理由 + ≤240 字符 | 已核对 `prompts/l1_extraction.txt`：仅有**提取完整性/格式**护栏（禁止空数组、禁止省略、宁缺毋滥、替代关系声明），**缺反夸张/谦抑这一层** | **直接可补**：把 DeepTutor 的语气谦抑 + 强制溯源护栏加入 SGME L1 prompt |
| C | **纯文件 + 7 级脚注溯源** | L3→L2→L1 裸 Markdown，可手编、可 git | SGME 用 DB + sqlite-vec | **表示层互补**：「向量召回 + 强制引用溯源的 Markdown 记忆体」方向成立 |

### 8.2 误判已撤回（SGME 已有同类能力，原编号对照）

| 原编号 | 原结论 | SGME 实际（源码证据） | 处理 |
|---|---|---|---|
| 原 #2 seen-id 增量 | SGME 批量 refine 更费 token | `refine_file` 用 `last_refined_seq` 增量提取（seq > last_refined_seq），原理完全相同 | **撤回** |
| 原 #4 days_ago 预计算 | SGME inject 未算时间差 | `profile/inject.py` `_relative_time` 已算「N天前/小时前/分钟前」 | **撤回** |
| 原 #7 按场景注入 | SGME inject 无场景控制 | `profile/inject.py` `inject()` 按 template sections（维度/TTL/time_window/priority 过滤）场景化注入；`tier0.py` 每日生成基本用户画像（48h 过期降级静态维度） | **撤回**（即用户指出的「SGME 本就按场景注入+基本画像」） |
| 原「提炼异步+预览 → SGME refine」 | 映射等价 | SGME `refine` 是**引擎从 L0 驱动**（`refine_trigger(async)`），非 agent 触发、无人工 preview-apply 闸门；DeepTutor 的预览-应用是 workbench UI 特性，两者不等价 | **撤回映射** |
| 原 #5/#6 幂等/迭代去重 | SGME 缺去重方案 | SGME 已有 `append_l0` 幂等（同 session_key+started_at 不重复）+ L1.5 `merge/supersede`；DeepTutor 的 write_memory 查重短路是 **agent 工具层**，SGME 无对应（agent 不写记忆） | **降权/不适用** |
| 原 #8 stamps-only 快读 | 主动关怀可借鉴 | SGME `inject` 读完整 blocks，未证实有 stamps-only 廉价路径；保留为**低置信候选**（可用于 care 信号消费便宜读） | **降级** |

### 8.3 SGME 反而领先 / 已覆盖

- **L1 写入稳健性 + 安全**：SGME `append_l0` 含 `redact_secrets` **密钥擦除**（2026-08-17 安全加固，防工具输出带 key 进原始层）+ `batch_scan` 崩溃只丢当前文件；DeepTutor 未提及密钥擦除。**SGME 在此领先**，初版「互相印证」表述弱化了 SGME 的安全设计，特此更正。
- **提取护栏已具备**：SGME `l1_extraction.txt` 的「禁止空数组 / 禁止省略 / 宁缺毋滥 / 替代关系声明」是 DeepTutor 没有的**覆盖完整性**护栏，方向互补。

### 8.4 关于「第 3 点：写入窄通道」的澄清（事实准确，映射应修正）

**事实核对（已验证）**：DeepTutor agent 唯一的写记忆通道是 `write_memory` 工具 → `store.write_preference`，该函数**硬编码** `path = paths.l3_file("preferences")`，即只写 L3 `preferences.md`；写入前先 `emit` 一条 `preference_stated` 的 L1 trace（溯源），并带 casefold 去重短路（issue #647）。其余 L3 槽（recent/profile/scope）由 consolidation 产出，agent 不可直写。→ **原事实描述准确**。

**但「对 SGME 意义 = SGME 可限制 agent 直写」映射错误**：SGME 的 agent **完全不写记忆**（零 agent 写入），偏好/persona 全部由引擎从 L0 经 L1/L1.5/L2 提取。因此这**不是**「SGME 该补的限制」，而是两种记忆哲学的差异：
- **DeepTutor**：信任用户*显式声明*——给 agent 一个**受溯源+去重约束的窄通道**主动记录偏好（`write_memory`）。
- **SGME**：信任*对话提取*——agent 不碰记忆，一切由提炼流水线从 L0 产出。

真正值得借鉴的是 DeepTutor 把「显式偏好」做成**窄通道 + 强制溯源 + 去重短路**的封装方式；若 SGME 未来想引入「用户主动声明→直接落库」的快通道（类似现有「创意池 API 主动记录」模式），这是现成范本。

### 8.5 更正后的核心启示

1. DeepTutor 与 SGME 在**架构哲学高度同构**（原始层 append + 异步提炼 + 限制 agent 直写 + 场景化注入 + 基本画像），多数「可借鉴点」是 SGME 已有能力的「另一种实现」，并非缺口。
2. SGME 真正可补的是 **A（生成时引用池硬约束）+ B（反夸张/谦抑护栏）**——两者都落在 **L1 提取 prompt** 这一层，改动小、收益明确。
3. 表示层上 **C（裸 Markdown + 脚注溯源）** 与 SGME 的「DB + 向量」互补，可作为长期演进方向，而非当下必做。

---

## 9. 关键代码路径索引

```
deeptutor/services/memory/
├── __init__.py            # 三层子系统总入口（L1 trace / L2+L3 doc / ops / paths / ids / store / consolidator）
├── store.py               # MemoryStore 门面（L1 emit / L2+L3 读写 / write_preference / 迁移）
├── paths.py               # 路径解析，ContextVar 实现多用户隔离 + Surface/L3Slot 枚举
├── trace.py               # L1 append-only JSONL（按 surface 每日一个文件，永不抛异常）
├── document.py            # Markdown + 脚注引用解析/序列化（幂等往返，HTML 注释条目锚点）
├── recall.py              # stamps-only 廉价读取 + days_ago 预计算
├── consolidator/
│   ├── __init__.py        # 四模式入口：run_update / run_audit / run_dedup / run_merge
│   ├── chunker.py         # 字符级边界扩展切分（段落/句子边界，10% 重叠）
│   ├── references.py      # 引用池校验 + 原始 trace 注解（防幻觉核心）
│   ├── guards.py          # banned-phrase 过滤
│   ├── modes/update.py    # 增量事实抽取（id-set diff + chunk + 引用校验）
│   ├── modes/dedup.py     # 迭代行级去重，0 编辑早停
│   └── prompts/{en,zh}.yaml  # 提炼 prompt（禁用词/对冲/长度/删除理由）
├── snapshot/              # 当前实体全量 + fingerprint + changes.jsonl（diff 源）
└── prompts/{en,zh}.yaml   # L2/L3 提炼 prompt 主定义
```

---

## 10. 结论

DeepTutor 的记忆系统最值得学的不是分层本身（SGME 已有记忆池 + 场景向量），而是它用**工程约束 + 提示词纪律把「记忆可信度」做成了可验证的东西**：

1. 每条记忆都能**溯源到原始事件**（引用池校验强制）；
2. 提炼过程是**增量、预算可控、可早停**的（不重读全量历史）；
3. 记忆内容是**被护栏约束的**——不准夸张、不准空泛、必须绑定证据计数；
4. 读取与注入是**按需、廉价、安全的**（stamps-only、按轮次注入）。

这四点正是 SGME 在「记忆质量」维度可以补强的地方。建议优先落地 #1（强制引用溯源）、#2（增量提炼）、#3（提示词护栏）三项。

---

*本报告由 WorkBuddy（吹吹水）基于 D:\GitHubDownloads\DeepTutor 源码实地阅读生成，非联网二手整理。*
