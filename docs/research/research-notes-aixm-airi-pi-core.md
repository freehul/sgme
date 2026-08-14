# 研究笔记：aixm / airi / pi-core 三项目深度梳理

> 创建：2026-08-01 | 用途：跨项目关系与关键信息存档（临时研究笔记）
> 来源：D:\Projects\aixm\AIXM-设计方案-0.3.md（全文精读）+ 两路代码探查（airi / pi-core 实仓 grep+read）

---

## 0. 一句话关系图（⚠️ 2026-08-02 用户澄清后已修订）

> 重要更正：此前把「pi 是基座、AIXM 建在 pi 上」当成结论，是误读了 v0.3 文档措辞。
> **用户明确：AIXM 是原始、独立、通用的记忆系统；pi / airi 是 AIXM 的下游适配对象 / 客户端，不是基座。**

```
AIXM  (原始项目 · 独立通用记忆系统)
   = 事实提炼 + 存储 + 检索 核心引擎；对外暴露 HTTP API / MCP / CLI
   = 为不同 Agent 提供「专用适配器」（pluggable）：
        ├─ hermes-adapter   （更精准的触发，因 Hermes 是常用 Agent）
        ├─ zcode-adapter
        ├─ pi-adapter       （AIXM 作为 pi 的记忆扩展）
        └─ airi-adapter     （AIXM 作为 airi 的记忆扩展）
   = 桌面形态（host/runner 层，同一套服务管理+语音，不同 UI 表现）：
        ├─ desktop 窗口
        ├─ tray 托盘
        ├─ 灵动岛 Dynamic Island
        └─ 桌宠 desktop pet   ← airi 已实现，评估直接复用轮子

pi-core  (earendil-works/pi-mono) — 被 AIXM 适配的对象之一（非基座）
airi    (moeru-ai/airi, MIT)       — 被 AIXM 适配的对象之一 + 桌宠轮子来源
```

**核心结论（修订）**：AIXM 是中心、独立运行的通用记忆系统；pi 与 airi 是它要适配/接入的 Agent 生态，二者彼此独立、与 AIXM 也无代码关联。v0.3 文档把 AIXM「建在 pi 之上」的写法，正是用户认为「乱了」的部分之一。

---

## 1. AIXM（设计方案 v0.3，待审核）

> ⚠️ **用户澄清（2026-08-02）**：本节按 v0.3 文档原样记录，但其中「以 pi-agent-core 为运行时 / 建在 pi 扩展机制上」的设定与用户最新意图不符。用户要的是 **AIXM 独立运行**（见第 7 节）。这一定位是用户认为「乱了」的根源之一，将在重新设计中推翻。

- **本质（v0.3 原文）**：pi Agent 生态的**记忆与知识扩展集合**。以 pi-agent-core 为运行时，通过 pi 标准扩展机制提供记忆、知识库、个人资料库、Agent 学习能力；以常驻服务形态经 HTTP API / MCP / CLI 暴露给外部 Agent（Hermes、Trae、ZCode 等）。
- **核心愿景**：记忆桥梁（多 Agent 共享记忆）、持续进化、知识管理、遵循「极简核心 + 激进可扩展」的 pi 哲学（不 fork 不改内部）。
- **设计原则**：pi 原生 / 服务常驻 / 唯一写者（daemon 串行化所有 DB 写）/ 可追溯（session_id+entry_id）/ 渐进式注入（Level 0/1/2，注入路径零 LLM 调用）/ 幂等 / 单用户多 Agent（无 user_id，用 source_agent 区分）。
- **v0.3 相对 v0.2 的关键修正**：
  - S1 三层拆分：core（引擎库）/ service（常驻 daemon 唯一写者）/ pi-ext（薄客户端），解决「HTTP 寄生在 pi 扩展内」导致 pi 不跑则外部 Agent 全断、多实例抢端口双写 DB。
  - S2 每会话游标 + processed_entries 集合双保险（解决 pi 多会话 + 树形分支）。
  - S3 daemon 内 single-flight 串行提取队列（解决 agent_settled 重入竞态）。
  - S4 Phase 0 质量冲刺前置（黄金评测集 + prompt 迭代 + 质量门禁），因旧版 facts 提炼质量不满意而重做。
  - M1 提炼管线直接用 pi-ai 作为库（自带降级链），registerProvider 仅作交互可选项。
  - M2 facts=事件日志 / profile=物化状态，新增 fact_sources 关联表替代 JSON 数组。
  - M3 Supersession prompt 限幅 + key 复用 + JSON 校验重试 + pending_review 降级。
  - M4 Hermes state.db backfill 列为 Phase 2 一等交付物（两段式：抽样核对→全量）。

### 架构（三层）
- **客户端层**：多个 pi 实例 / Hermes(shell hooks→HTTP) / Trae·其他(MCP/HTTP) / CLI，都对接 @aixm/service。
- **@aixm/service（daemon, :7700 仅绑 127.0.0.1）**：HTTP API / MCP Server / 提取任务队列（single-flight）/ backfill / 生命周期（NSSM 自启 + 客户端探活兜底拉起，文件锁防双实例）。
- **@aixm/core（纯 TS 库，零 pi/网络依赖）**：FactEngine / SupersessionEngine / ProfileManager / Injector / KnowledgeEngine / VaultManager / LearningEngine / LlmClient(pi-ai 封装+降级链 local→deepseek→agnesai) / Tokenizer(CJK 预分词) / Store(better-sqlite3 + WAL + migrations)。
- **存储**：~/.aixm/aixm.db（SQLite WAL 单库）+ vault/ + knowledge_files/。

### 数据模型要点
- facts = append-only 事件日志（active/superseded/pending_review）；profile_entries = 物化当前画像（UNIQUE(domain,key)）；fact_sources 关联表可索引溯源；mem_cursors(每会话游标) + processed_entries(entry 级判重)；facts_fts FTS5（unicode61 + CJK 预分词）。
- 所有写操作在单 SQLite 事务内（facts 插入 + fact_sources + profile 应用 + FTS5 + 游标/processed 更新）；LLM 调用在事务外，失败整批不落库。

### 实施计划（约 14 周）
- Phase 0 质量冲刺（黄金集 ≥90% 准确率、重复率 <5%）→ Phase 1 地基(core/store/llm-client/eval) → Phase 2 daemon+提取管线+Hermes backfill → Phase 3 pi 扩展 → Phase 4 knowledge+learning → Phase 5 vault+desktop(openpi fork) → Phase 6 集成验收。

### 待决策
- CJK 分词(nodejieba vs jieba-wasm)、OCR(tesseract.js/PaddleOCR-ONNX/外部)、爬取(cheerio/playwright)、vault 加密(SQLCipher/应用层 AES)、sqlite-vec 向量检索（FTS5 先行）。

### 外部引用（文档中）
- pi-core 源码 `D:\Projects\pi-core`（pi-agent-core v0.82.1；extensions.md/custom-provider.md/sdk.md/packages.md）—— 已核实 pi 能力（agent_settled、turn_end、before_agent_start、sessionManager JSONL 树、registerProvider、pi-ai、pi install、createAgentSession）。
- openpi `D:\Projects\openpi`（desktop fork 基）。
- 旧版 aixm（Python）`D:\Projects\aixm`（FactEngine/tokenizer/ensure_aixm/backfill 逻辑参考）。

---

## 2. pi-core（earendil-works/pi-mono 本地改名，v0.82.1）

- **本质**：开源自扩展编码 Agent「Pi」（CLI/SDK）。维护者 Mario Zechner（badlogicgames / libGDX 作者），官网 pi.dev。
- **理念**：极简内核 + 扩展/技能/模板/主题定制；明确不内置 MCP、子 agent、权限弹窗、plan mode、内置 todo。
- **技术栈**：TypeScript（erasable 语法，Node strip-only；禁 enum/namespace/参数属性），Node≥22.19，ESM；Biome 2.3.5、vitest、esbuild、tsgo、npm workspaces、lockstep 版本（全包 0.82.1）；工具参数用 TypeBox。
- **packages/**：
  - `ai`（核心）：统一多供应商 LLM API（30+，OpenAI/Anthropic/Google/Bedrock…），自动鉴权、成本/Tokens、跨供应商 handoff、OAuth。
  - `agent`（核心）：有状态 agent 运行时；agent-loop + 事件流、AgentMessage、工具系统、compaction、session harness。
  - `coding-agent`（产品）：CLI `pi`（交互/print/JSON/RPC/SDK 四模式）；内置 read/write/edit/bash/grep/find/ls；ExtensionAPI/技能/模板/主题/pi-packages；session 树与信任模型。
  - `tui`（核心）：终端 UI 框架（差分渲染、Editor/Markdown/SelectList）。
  - `storage/sqlite-node`：agent-core 的 Node sqlite 会话后端。
  - `server`（实验）：supervisor/rpc/radius/ipc。`evals`：评估 harness。
  - 残留引用（已不存在）：agent-old、mom。
- **.pi 目录**：Pi agent 自身的「项目级 home」（对应运行期 ~/.pi/agent/），含 prompts/（斜杠命令脚本 cl/is/pr/sa/wr.md）、skills/、extensions/、git/、npm/。= 配置+协议资源+运行期状态混合体，**不是独立协议**。
- **核心抽象**：
  - pi-ai：`Models`/`Provider`/`Model`/`CredentialStore`（`Model` 可序列化纯数据，便于跨会话/进程/供应商）。
  - agent-core：`Agent`/`agentLoop`（事件 agent_start→turn_start→message_*→tool_execution_*→turn_end→agent_end）、AgentMessage+convertToLlm、transformContext(prune/compaction)、steering/follow-up、beforeToolCall/afterToolCall、AgentTool(parallel/sequential/terminate)。
  - coding-agent：`ExtensionAPI`、`AgentSession`/`ModelRuntime`/`SessionManager`(SDK)；session 为 JSONL 树(id/parentId)。
- **生态关联**：全仓 grep **未命中** AIXM / airi / PIPER / moeru-ai。文档仅引用 earendil-works/pi-mono、pi.dev、pi-chat（Slack/聊天自动化兄弟项目）、pi-share-hf（HF 会话分享）、rfc.earendil.com。「PI」非 Personal Intelligence/PIPER 缩写，仅为品牌名 pi.dev。
- **运行**：入口 `packages/coding-agent/src/main.ts`(Node) / `src/bun/cli.ts`(Bun)；SDK 用 createAgentSession/ModelRuntime；RPC=`--mode rpc`(LF 分隔 JSONL)。根目录：`npm install --ignore-scripts` → `npm run build` → `npm run check` → `./test.sh`。AGENTS.md 强制：未获要求不得直接 build/test，禁止跑完整 vitest（含 e2e）。

---

## 3. airi（moeru-ai/airi，@proj-airi/root v0.11.3，MIT）

- **本质**：开源「AI 虚拟角色灵魂容器」——复刻 Neuro-sama（能聊天、能玩 Minecraft/Factorio、跨浏览器/桌面/移动端运行的「赛博生命体 / AI 老婆」）。理念「以 Web 技术为先」（WebGPU/WebAudio/Web Workers/WASM/WebSocket），桌面经 candle 调原生 CUDA/Metal 本地推理。
- **技术栈**：pnpm@10.33 workspace + turbo@2.9；TypeScript 5.9、@antfu/eslint-config、oxlint、knip；Vue 3 + Vue Router + Pinia + VueUse + UnoCSS + Vite 8；桌面 electron-vite、移动 Capacitor、浏览器扩展 wxt。
- **LLM SDK**：`xsai`（`@xsai/*`、`@xsai-ext/providers`，moeru-ai 自研，类 Vercel AI SDK 但更轻），支持 30+ 厂商。**注意：用 xsai，不用 pi-ai。**
- **渲染/角色**：three + @pixiv/three-vrm(VRM)、@moeru/three-mmd(MMD)、pixi-live2d-display(Live2D)、spine-webgl、TresJS。
- **音频/视觉**：@ricky0123/vad-web、kokoro-js、@huggingface/transformers、onnxruntime-web、@mediapipe/tasks-vision。
- **数据**：duckdb-wasm/pglite + drizzle-orm。IPC/RPC：@moeru/eventa；DI：injeca；可靠 WS：better-ws。服务：hono/h3/srvx/crossws；游戏 mineflayer/factorio-rcon；MCP @modelcontextprotocol/sdk。
- **apps/**：stage-web（浏览器主入口）、stage-tamagotchi（Electron 桌面）、stage-pocket（Capacitor 移动）、component-calling（实时音频）、server、ui-server-auth（CF Workers 认证 UI）。
- **packages/**：core-agent（agent 运行时编排）、core-character（角色流水线：分句/情绪/延迟/TTS）、pipelines-audio/audio/audio-pipelines-transcribe、model-driver-lipsync/mediapipe、stage-ui（核心 stores/composables）、stage-ui-{three,live2d,mmd,spine,tachie}（可替换渲染后端）、stage-shared/layouts/pages、ui/ui-transitions/ui-loading-screens、server-runtime/sdk/shared/schema、memory-pgvector/duckdb-wasm/drizzle-duckdb-wasm、plugin-protocol/sdk/sdk-tamagotchi、better-ws/stream-kit/electron-*/cap-vite/i18n/ccc、font-*。
- **engines/**：stage-tamagotchi-godot（Godot 原生桌面舞台运行时引擎）。
- **integrations/**：vscode（vscode-airi）。**services/**：discord-bot、telegram-bot、twitter-services、satori-bot、minecraft、computer-use-mcp。**plugins/**：airi-plugin-bilibili-laplace、claude-code、game-chess、homeassistant、web-extension。
- **拟人化子系统**（packages/stage-ui/src/stores/modules/）：consciousness(意识循环)、hearing(听+STT)、speech(说+TTS)、artistry/artistry-autonomous(人格/自动表达)、airi-card(角色状态)、vision、web-search、gaming-{factorio,minecraft,module-factory}、discord/twitter。
- **记忆**：memory-pgvector + DuckDB WASM；规划中「Memory Alaya」(WIP) —— 是 airi 自有规划，非外部项目。
- **生态关联**：父组织 moeru-ai；直接实现依赖 xsai、unspeech(ASR/TTS)；衍生 hfup/xsai-transformers/demodel/inventory/mcp-launcher/velin/chat(WebXR)。全仓 grep **未命中** AIXM / pi-core / PIPER。结论：与 AIXM、pi-core 无依赖/实现/fork 关系。
- **运行**：`pnpm i`（postinstall 自动构建）→ `pnpm dev`（stage-web 主入口）；桌面 `pnpm dev:tamagotchi`；移动 `pnpm dev:pocket:ios|android`；服务端 `pnpm dev:server`；文档 `pnpm dev:docs`；`pnpm build`(turbo)；`pnpm test`(vitest)、`pnpm typecheck`。

---

## 4. 跨项目判断（重要）

| 关系 | 结论 |
|------|------|
| AIXM ⟷ pi-core | **用户澄清后修订**：v0.3 文档把 AIXM 写成「以 pi-agent-core 为运行时」，但用户明确 **pi 不是基座，AIXM 才是中心**。正确关系：AIXM 独立运行、对外暴露 HTTP/MCP/CLI；pi 是 AIXM 要适配的下游 Agent 之一（AIXM 作为 pi 的记忆扩展）。文档措辞是「乱了」的表现，重设时将推翻。 |
| AIXM ⟷ airi | **无代码关联，但有关联意图**：AIXM 文档未提 airi；airi 代码 grep 无 AIXM/pi-core 命中。用户看 airi 是因为它**已实现桌宠**，想直接复用其轮子；且未来 AIXM 可作 airi 的记忆扩展适配器。两者 LLM SDK 不同（pi-ai vs xsai）。 |
| pi-core ⟷ airi | **无关联**：pi-core 代码 grep 无 moeru-ai/airi/AIXM 命中；组织不同（earendil-works vs moeru-ai）。 |
| AIXM ⟷ openpi | AIXM 的 desktop app 计划 fork 自 `D:\Projects\openpi`（非 airi）。openpi 未纳入本次探查。 |

**推断（修订）**：用户把三者放一起，是为了给 AIXM 重新设计找「可复用的轮子 + 可适配的生态」——airi 提供桌宠实现参考、pi 提供扩展/Agent 机制参考；但 AIXM 自身是独立中心，pi/airi 都是下游适配对象。若做「统一记忆层」，AIXM 的方案思路（事实日志+物化画像+Supersession 判重+常驻 daemon）仍可独立借鉴。

---

## 5. 待确认 / 可追问方向

1. 用户已澄清真实意图（见第 7 节）：AIXM 独立通用记忆系统 + 专用适配器（hermes/zcode/pi/airi）+ 桌面形态（desktop/tray/灵动岛/桌宠）。下一步是重新设计。
2. `D:\Projects\openpi` 是什么？是否为 Pi 的开源桌面版？与 airi 是否撞名混淆？值得单开探查（尤其若桌面形态要 fork 它）。
3. AIXM 当前是否已有代码（aixm-monorepo 目录，packages/{core,service,pi-ext,eval}）？本次只给了设计文档，未给实现全貌。
4. Hermes / Trae / ZCode 三个「外部 Agent」是否也是本地项目？AIXM 的 backfill 核心语料来自 Hermes state.db；Hermes 是精准触发适配器的首要对象。
5. airi 的「Memory Alaya」与 AIXM 记忆层是否存在可对齐的设计理念（都含事实/画像/检索），作为 airi 适配器设计的参考。

---

## 6. AIXM 设计文档版本演进（0.1 → 0.2 → 0.3）

> 三版位置：0.1/0.2 在 `D:\Projects\aixm\docs\history\`，0.3 在 `D:\Projects\aixm\` 根目录。行数：0.1=788、0.2=564、0.3=492。

### 演进主线
- **v0.1**：通用 AI 记忆引擎，试图从零重写（TS 核心 fork pi-agent-core + Python 扩展，双库 user.db/agent.db）。被 K3 审查打回（45/100，5 致命）：无视已上线系统、语言栈撕裂、抛弃单库、偏离「同库渐进拆分」、违反 CLAUDE.md（缺测试门槛/migration UP+DOWN）。
- **v0.2**：按 K3 + 8 项变更重写。定位收敛为「pi 生态记忆/知识扩展集合」；100% TS、npm 依赖 pi 不 fork、单库、三层去重（游标+Supersession+溯源）、三层架构成型。但 HTTP 仍内嵌 pi 扩展、全局单一游标、agent_settled 重入无防护、提炼质量无闭环——4 项严重隐患遗留。
- **v0.3**：针对 v0.2 的 4 严重问题重构。三层拆分 core/service/pi-ext；HTTP 抽成独立 daemon（唯一写者）；每会话游标+processed_entries 集合+single-flight 串行队列；Phase 0 质量冲刺前置（黄金集+门禁）；提炼管线 pi-ai 库直调；Hermes state.db backfill 升 P0。当前 `D:\Projects\aixm` 仓库已含 packages/{core,service,pi-ext,eval} 实际实现，v0.3 进入落地。

### 维度对比表
| 维度 | v0.1 | v0.2 | v0.3 |
|------|------|------|------|
| 定位 | 通用记忆引擎（多 Agent 共享） | pi 生态记忆/知识扩展集合 | 同 v0.2（扩展集合） |
| 语言/依赖 | TS 核心 fork pi + Python 扩展（撕裂） | 100% TS，npm 依赖 pi 不 fork | 同 v0.2 |
| 架构分层 | 核心运行时+扩展层（2 层） | 接入层+pi-core+扩展层+存储（3 层） | core/service/pi-ext（3 层，service=常驻 daemon） |
| 存储 | 双库 user.db/agent.db+向量 BLOB | 单库 aixm.db（SQLite WAL+FTS5） | 单库 aixm.db（migration UP/DOWN 明确） |
| HTTP 暴露 | 寄生自研扩展系统 | 内嵌 aixm-memory 扩展开 pi 进程生死 | 独立 @aixm/service daemon（唯一写者，:7700 绑 127.0.0.1） |
| 提炼触发 | turn_end（但 Hermes 无此事件！） | agent_settled | agent_settled |
| 防重复 | 仅字段级（mention_count/superseded_by） | 全局单一游标+Supersession+溯源 | 每会话游标+processed_entries 集合+single-flight 队列+Supersession+溯源 |
| LLM 调用 | 自研 LLM Router（复制 pi） | pi-ai 库+registerProvider+LlmRouter 降级 | pi-ai 库直调（管线不经 registerProvider），降级链 |
| 注入 | Level 0/1/2, before_agent_start | 同，零 LLM | 同，直读 DB <10ms，注入路径零 LLM |
| 提炼质量 | 无 | 模块级，无评测闭环 | Phase 0 质量冲刺+黄金集+门禁 前置 |
| backfill | 未提 Hermes | 未列 P0 | Hermes state.db backfill 为 P0 两段式 |
| 待决策 | 大量 TBD（vault/OCR/爬虫） | 部分收敛 | 仍有选型待定（分词/OCR/爬取/加密/向量） |

### 关键教训（可复用）
- v0.1 的「从零重写」被 K3 否决 → 但**用户明确反对 K3 的「不要重写」立场**（2026-08-02）：「没有什么不可以重写，就是要重新设计」。因此「演进式优于推倒重来」对用户**不适用**——K3 的审查约束已被用户推翻，重新设计是目标而非禁忌。
- 防重复机制从「字段级」→「全局游标」→「每会话游标+集合+队列」，是逐步补全并发正确性的过程。
- 服务生命周期（HTTP 寄生 → 独立 daemon）是 v0.2 最痛的架构债，v0.3 才还清。
- 提炼质量被定位为「首要重做动因」，故 v0.3 把质量评测前置为 Phase 0 门禁。

---

## 7. 用户澄清后的正确架构定位（2026-08-02 深夜）

> 用户亲述四点，推翻此前「Pi 是基座」的结论，并给出重新设计的方向。

### 7.1 四点原意
1. **AIXM 是最初的项目**：通用记忆系统，但早期只适配了 Hermes 和 ZCode。
2. **为什么想重构/重设**：① 事实提炼结果不达预期；② 做统一设置接口时，想让 desktop + tray 统一管理「服务运行 + 语音对话」但没做完；想要「灵动岛」和「桌宠」——而这二者其实是 desktop 运行的**不同形态**（同一 host，不同 UI 表现）；由此看中 pi 的扩展能力 + agent 核心，但「想要的越多越乱」。看 airi 是因为它**已经实现了桌宠**，想直接复用轮子（反正还没开发完、设计可改）。
3. **当前设计乱了**：用户认为乱始于 K3 审查之后（审查说已上线不应重写）。**用户反对此约束**：没什么不能重写，就是要重新设计。
4. **AIXM 仍应独立运行**：通用记忆系统定位不变。对外提供 HTTP API / MCP / CLI 基本接口；对常用 Agent 提供**专用适配器**（如 Hermes 记忆扩展，更精准的触发）；也可适配为 pi 或 airi 的记忆扩展。这正是了解 pi 的原因（为架构设计打底）。

### 7.2 修订后的关系模型
- **AIXM（中心，独立）** = 通用记忆引擎（事实提炼 + 存储 + 检索）。对外：HTTP / MCP / CLI。
- **专用适配器（pluggable，下游）**：hermes（精准触发）/ zcode / pi（AIXM 作 pi 记忆扩展）/ airi（AIXM 作 airi 记忆扩展）。
- **桌面形态（host/runner 层）**：desktop 窗口 / tray 托盘 / 灵动岛 / 桌宠 —— 同一服务管理 + 统一设置 + 语音对话，不同 UI 表现。airi 的桌宠实现是可复用轮子。
- ⚠️ **重大修正（见第 11 节，2026-08-02 调查）**：经 GitHub/官方文档核实，**Hermes Agent 本身已是完整的「agent + 桌面形态 + 桌宠 + 电脑操控」平台**（195k★/MIT/Nous Research，原生支持 Windows 电脑操控与宠物前端 `hermey-the-pet`）。因此「让桌宠操作电脑」的 agent 角色应由 **Hermes** 承担，**pi-agent 在此目标下基本冗余**——它仅剩「可选的 TS 编码 Agent 适配器」价值。架构 v2 见第 11 节。
- **pi / airi** = 被 AIXM 适配的 Agent 生态，二者独立、与 AIXM 无代码关联。

### 7.3 对设计的隐含要求（重设时落实）
- 内核必须**零运行时耦合**到任何特定 Agent（不依赖 pi 的事件流/扩展机制作为生存条件）。
- 适配器层干净可插拔：通用 HTTP/MCP/CLI 先满足基本功能，专用 adapter 叠加精准触发。
- 桌面形态需要独立的「host」层：服务生命周期管理 + 统一设置 + 语音，再分出 4 种 UI 形态。
- 事实提炼质量仍是一等公民（用户重设的首要动因）。

### 7.4 下一步候选（待用户定）
- A. 起草 v0.4 架构：以「独立 AIXM + 适配器 + 桌面形态」重新定位（推翻 v0.3 的 pi 基座设定）。
- B. 深读 airi 的桌宠实现（stage-tamagotchi / plugin-protocol / stage-ui-*）评估复用可行性。
- C. 回收旧 Python 系统（DESIGN-v1.0-python.md + 代码）中可保留的好设计。

---

## 8. airi 桌宠复用可行性评估（2026-08-02）

> 用户选择先评估「桌宠轮子」能否直接拿来。两路探查（UI/窗口层 + Agent/语音/记忆耦合层）结论：可复用，且耦合度比预想低。

### 8.1 桌宠到底是什么形态
- **Electron 桌面**（apps/stage-tamagotchi）：多窗口 —— 主角色窗、常驻小宠物窗 `inlay`(450×150 贴底)、`caption`/`spotlight`/`widgets`/`settings`，以及全屏透明置顶点击穿透的 `desktop-overlay`（受 `AIRI_DESKTOP_OVERLAY=1` 开关）。
- **可选 Godot 原生渲染后端**（engines/stage-tamagotchi-godot，sidecar，h3+crossws WebSocket 通信）—— 非必须。
- **角色渲染后端多套**：`stage-ui-three`(three+@pixiv/three-vrm，VRM，默认 Web) / `live2d`(pixi-live2d-display) / `mmd` / `spine` / `tachie`(立绘)。

### 8.2 与「大脑」的耦合（关键，结论乐观）
- UI 不直接调 agent，经 Pinia store + **port 接口**间接连：`chat.ts` 注入 4 个 port（session/context/foregroundStream/llm）。
- 大脑↔UI 仅靠 `llm` + `foregroundStream` 两个 port 解耦；**换大脑 = 换 `llm` port 实现（约 1–2 文件）**。
- 语音/口型与 LLM 解耦：TTS provider 抽象完善（elevenlabs/azure/openai-compatible/kokoro），口型由音频实时算，文本喂入即可。
- **记忆零耦合**：桌宠当前完全没用记忆层（Memory Alaya 仅 README 标 WIP，无代码）。AIXM 接入是纯增量、零改造。

### 8.3 两种复用路径
- **路径 B（推荐，低成本）：fork airi 桌宠 + 换大脑**。保留 `stage-ui` UI/动画、`pipelines-audio`、`model-driver-lipsync`、`modules{consciousness,hearing,speech}`；改 `chat.ts:191` 的 `llm` port 指向 AIXM 的 HTTP/MCP 流式端点，或在 `providers.ts` 增加 AIXM 为 OpenAI 兼容 chat provider（现有 `buildOpenAICompatibleProvider`/`createModelProvider` 可复用）。**若 AIXM 暴露 `/v1/chat/completions`，可能零代码仅改配置**。风险：chat.ts hook 链与 `@xsai` 类型耦合，适配器需对齐 `StreamEvent` 类型。
- **路径 A（更干净，自拥）：抽取纯渲染层 + 自写壳**。`stage-ui-three` 仅依赖 `stage-shared/ui/vue/three`（无 core-agent），最易独立抽；自写 Electron/Tauri 外壳（参考 airi 的 `window-contract.ts`/`tray/index.ts` 的置顶/穿透/托盘）。更干净、AIXM 自拥桌面形态，但需重做窗口/inlay/托盘/overlay 外壳。

### 8.4 风险与许可红线（务必注意）
- **代码 MIT**，但两处雷：① `@esotericsoftware/spine-webgl` 是 **Spine Runtimes License**（非 MIT，商用需条款）；② **Live2D/VRM 模型资产**各有许可（Live2D 免费模型限非商业）。代码可搬，资产不可随意搬运，需自备或合规授权角色。
- 桌面形态映射：airi 已覆盖 desktop 窗 + tray + inlay 桌宠 + overlay；「灵动岛」可对应其 `caption`/`widgets` 紧凑浮窗形态（需自设计 pill 形态）。

### 8.5 结论
桌宠轮子可拿。最低成本路径 = fork + 把 `llm` port 指向 AIXM（OpenAI 兼容接口则近零代码）；长期更干净 = 抽取渲染层进 AIXM 自己的 host 层。记忆层现为零耦合，AIXM 接入无迁移负担，是纯增量价值。

---

## 9. GitHub 调研：pi 本体与社区扩展生态（2026-08-02）

> 用户要求搜索 GitHub 弄清 pi 及其社区扩展；并确认「pi-agent 也是 pi 的扩展项目」。结论：确属实，pi 是分层 monorepo，agent 运行时是建在底层 LLM 库之上的包；社区扩展（含记忆类）已非常活跃。

### 9.1 仓库身份（修正此前笔记）
- 仓库现名 **`earendil-works/pi`**（原 `badlogic/pi-mono`，作者 Mario Zechner / badlogicgames，libGDX 作者；组织 Earendil Inc./Works）。**本地 `pi-core` 即此仓库的副本**（之前记的「pi-mono v0.82.1」已过期：上游最新 v0.83.0，2026-07；★ 78.7k；MIT）。
- 官网 `pi.dev`，Discord 社区，Pi Packages 生态（2026-06 已 4600+ 个包）。

### 9.2 分层架构 —— 印证「pi-agent 也是 pi 的扩展」
垂直链（每层基于下一层构建，故 agent 运行时确为建在底层之上的「包/扩展」）：
```
pi-ai            (基础层, 零依赖)  统一多厂商 LLM API（20+ 厂商，流式/工具/成本追踪/跨厂商切换）
   ▲ 被依赖
pi-agent-core    (Agent 运行时)    有状态 Agent：工具执行、事件流、上下文管理、压缩、会话树
   ▲ 被依赖
pi-coding-agent  (产品层 CLI)      内置工具 + 会话 + 扩展系统 + 暴露 SDK/RPC
   │
pi-tui           (渲染层, 独立)    终端差分渲染
```
辅助包：`pi-server`（实验性 RPC 网关，由 orchestrator 改名）、`pi-storage-sqlite-node`（node:sqlite 会话后端）、`pi-web-ui`、`pi-mom`（Slack bot）、`pi-chat`（聊天自动化）、`pi-pods`（vLLM GPU）、`pi-proxy`（CORS 代理）。
**解读**：`pi-agent-core`（用户口中的「pi-agent」）是坐落于 `pi-ai` 之上的运行时包；`pi-coding-agent` 再在其上叠加扩展系统。AIXM 要「适配为 pi 的记忆扩展」，落点就是 `pi-coding-agent` 的**扩展层（ExtensionAPI）**，或发布为 Pi Package。

### 9.3 扩展机制（AIXM 作 pi 扩展的核心接口）
- 扩展 = 默认导出函数 `export default function(pi: ExtensionAPI)`，能：
  - `pi.registerTool({...})` 注册工具（TypeBox 参数校验，与内置工具并列，LLM 可调用）；
  - `pi.on("event", ...)` 订阅事件：`session:start/end/fork`、`message:user/assistant/system`、`tool:start/end/error`、`generation:start/end/stream`、`compaction:start/end`、`error`；
  - `ctx.ui.confirm/notify/setStatus` 注入 UI。
- **加载位置**：`~/.pi/agent/extensions/`（全局）、`.pi/extensions/`（项目）、settings `extensions` 数组、已安装包。支持热重载。
- ⚠️ **安全红线**：扩展在主进程以**完全系统权限**运行（与 pi 同权，可读文件/执行命令/联网）。第三方包须审源码。
- **事件名映射（修正 v0.3 文档）**：v0.3 引用的 `before_agent_start` 是真实钩子（pi-memoir 用它注入系统提示）；`agent_settled` 对应 pi 的 `session:end`。AIXM 的 pi 适配器应映射到 pi 真实事件枚举。

### 9.4 「不内置 MCP」是设计哲学 → 但可扩展加上
- pi 官方刻意**不内置** MCP / 子 Agent / 权限弹窗 / Plan 模式 / 内置 Todo / 后台 Bash，全靠扩展实现。
- AIXM 想暴露 MCP：两条路都通 —— ① AIXM 自起 MCP server，pi 侧装 `pi-mcp-adapter`（社区包，已存在）连上；② AIXM 直接发 `@aixm/pi-ext` Pi Package 注册记忆工具。前者更契合「AIXM 独立运行」诉求。

### 9.5 包分发（AIXM 适配器的发布通道）
- `pi install npm:<pkg>` / `git:github.com/user/repo` / `https://...` / 本地路径。
- 包用 `package.json` 的 `pi` 键声明 `extensions/skills/prompts/themes`，keyword `pi-package`。
- 全局 `~/.pi/agent/settings.json`；项目 `.pi/settings.json`（信任后启动时自动装缺失包）。`pi update/list/remove`。
- → AIXM 的 pi 适配器 = 发布 `@aixm/pi-ext`，用户 `pi install npm:@aixm/pi-ext` 即装上「记忆扩展」。

### 9.6 社区扩展生态（直接相关项目）
- **`pi-hermes-memory`（chandra447）** ⭐关键前车之鉴：把 **Nous Research 的 Hermes Agent 持久记忆系统**移植进 Pi 的社区扩展（368 测试，MIT）。能力：事实/偏好/纠错/失败记忆、SQLite FTS5 检索、全局+项目两级、密钥扫描、每 10 轮后台学习、自动合并、程序性技能、记忆老化。**这正是 AIXM 想做之事的已存在实现**，且命名直指 Hermes（AIXM 目标适配器之一）。→ AIXM 的「Hermes 适配器」应与它区分：pi-hermes-memory 是「给 pi 装 Hermes 式记忆」，而 AIXM 是「独立记忆引擎，给 Hermes/pi/airi 都供记忆」。可借鉴其设计，但定位不同。
- **`pi-memoir`（k1lgor）**：项目级持久记忆，「收割一次、反复查询」，省 ~95% token；用 `before_agent_start` 注入提示强制先查记忆。
- **`pi-mcp-adapter`（nicopreme）**：给 pi 加 MCP 支持。**AIXM↔pi 走 MCP 的桥**。
- **`pi-subagents` / `pi-crew` / `@quintinshaw/pi-dynamic-workflows`**：子 Agent / 多 Agent 编排（pi 不内置，靠社区补）。
- **`@aliou/pi-guardrails` / `@gotgenes/pi-permission-system`**：权限门控。
- **`oh-my-pi`**：把多个扩展打包的发行版（含 pi-hermes-memory、pi-crew、pi-guardrails）。
- **`pi-py`（encyc）**：pi 的 Python SDK 移植（对齐 v0.81.1），无 CLI/TUI。
- 更多：`pi-lens`(LSP/类型检查)、`pi-web-access`、`pi-web-tools`、`pi-webmcp`(WebMCP) 等，包目录 `pi.dev/packages` 持续扩张。

### 9.7 RPC / SDK 模式（host 层与 AIXM 的程序化接缝）
- **RPC 模式**：`pi --mode rpc`，stdin/stdout 上的 JSONL 协议（命令/响应/事件流）。适合嵌入其他应用、IDE、自定义 UI、桌面宠物（airi 类）。
- **SDK 模式（Node.js 同进程）**：`import { createAgentSession, createAgentSessionRuntime, AgentSessionRuntime, runRpcMode, SessionManager, ... } from "@earendil-works/pi-coding-agent"`。导出含 `createEventBus`、`defineTool`、`getAgentDir`、`getPackageDir`。
- **对 AIXM 的意义**：① 桌面形态 host（如 fork 的 airi 桌宠）可本身做 pi 的 RPC/SDK 宿主；② AIXM 若需主动驱动 pi，可用 SDK；③ AIXM 作 pi 扩展时，扩展内即可拿到 `ExtensionAPI` 直接读写记忆。

### 9.8 对 AIXM 重新设计的启示（落 v0.4 时吸收）
1. **双集成模式并存**：AIXM 既做独立 daemon（HTTP/MCP/CLI，安全隔离），又发 `@aixm/pi-ext` Pi Package（零延迟内存注入）。二者共用同一引擎，适配器层分流。
2. **记忆注入真实钩子 = `before_agent_start` + `session:end`**（非 v0.3 模糊的 `agent_settled`）；提炼触发挂 `session:end`。
3. **MCP 走 `pi-mcp-adapter`** 或自起 MCP server，避免 pi 不内置 MCP 的坑。
4. **参考 `pi-hermes-memory`/`pi-memoir` 的注入与检索实现**，但定位升级为「多 Agent 通用记忆后端」而非单 pi 扩展。
5. **扩展权限红线**：AIXM 的 pi 扩展须做最小权限，记忆读写经独立服务，不在扩展进程内持有 DB 写权（呼应 v0.3 的「唯一写者 daemon」设计）。

---

## 10. 待办 / 下一步
- （已完成）三项目定位 + 三版 AIXM 设计演进 + airi 桌宠可行性 + pi GitHub 生态调研。
- 候选：① 起草 v0.4 设计文档（独立内核+适配器+host 桌面层+桌宠复用+pi 扩展方案）；② 建最小 fork 验证（airi 桌宠 llm port 指 mock 跑空壳）；③ 回收旧 Python 系统好设计；④ 对照 `pi-hermes-memory` 源码提炼可借鉴的记忆注入/检索细节。
- **（新增，2026-08-02）重大方向修正**：Hermes 即 Agent（详见第 11 节）。v0.4 重心应转为「AIXM 作记忆后端 + Hermes 作 agent/桌面/电脑操控 + 可选 airi 3D 宠脸」，pi-agent 降级为可选适配器。

---

## 11. Hermes 即 Agent 的重大重设（2026-08-02 调查）

> 用户提问：pi-agent 在本系统的意义是什么？若所有桌面形态都能和 Hermes 深度结合，Hermes 就是这个 agent；设计里加 agent 就是为了桌宠能操作电脑帮我做事，若 Hermes 能替代呢？→ 调查结论：**能，且 Hermes 本来就是完整平台**。

### 11.1 关键事实（GitHub / 官方文档核实）
- **Hermes Agent** = Nous Research 2026-02 发布的开源自主 AI Agent，MIT，★195k，Python 82% + TS 13%。自我改进闭环：持久记忆（三层 working/episodic/semantic-skill）+ 自动生成技能（SKILL.md 开放标准，可分享 agentskills.io）。
- **电脑操控（Computer Use）跨平台**：经 `cua-driver`（TryCua）走 MCP，后台模式**不抢真实光标/焦点/虚拟桌面**；macOS / Windows / Linux 全支持。Windows 用 UIAutomation + SendInput + PostMessage（2026-06-23 起官方支持 Windows/Linux GUI）。⚠️ **WSL2 无 GUI 桌面控制**（仅终端控制）——用户是 Windows，须**原生 Windows 跑 Hermes**（非 WSL2）才能用电脑操控。
- **桌面形态完备**：原生 Electron 桌面应用（`hermes desktop` / `hermes gui`）、Ink TUI、Web dashboard、消息网关（Telegram/Discord/Slack/WhatsApp/Signal/Email/iMessage…）、desktop-plugins、TUI widgets。
- **桌宠原生支持（petdex）**：3200+ 像素精灵宠物，响应 agent 状态（idle/running/thinking/waving/celebrating/failing），CLI/TUI/桌面应用均可渲染，可弹出常驻最前浮窗；**纯装饰，不影响 token/行为**。
- **社区桌面宠前端 `hermey-the-pet`**（Ash-Blanc，Windows+Linux）：给本地 Hermes 套「脸」的小机器人，两模式（陪伴/工作）、三气泡入口（research/对话/cowork）、拖放交互。**明确「不新造 agent，只给已有 Hermes 前端壳」**——正是「桌宠驱动 Hermes 操作电脑」的形态。
- **MCP 原生支持**（stdio + HTTP）；记忆后端可插（文档提及 sqlite/postgres/redis，`config.yaml` 配 `mcp_servers`）。
- 多 profile 隔离、provider 无关（Claude/GPT/Gemini/本地 OpenAI 兼容端点）。

### 11.2 对原问题的直接回答
- **Q1：pi-agent 在本系统的意义？** 原设计中「agent」是为「桌宠操作电脑」而加。但 Hermes 已原生提供 agent + 电脑操控 + 桌面形态 + 桌宠 + 多平台，故 **pi-agent 在「操作电脑」目标下基本冗余**。pi-agent 仅剩潜在价值 = 一个 TS-native、极简、可 fork 的**编码 Agent 运行时**（扩展系统干净），作为**可选的次要 Agent 适配器**——而非驱动桌宠的 agent。Hermes 才是。
- **Q2/Q3：Hermes 能替代吗？** 能，且更完整。Hermes = 真·autonomous agent（computer-use + 工具 + 持久记忆 + 多平台 + 桌宠），本就具备 AIXM「设计里加 agent」想达成的全部能力。你的判断成立。

### 11.3 修订后架构（v2）
```
AIXM（独立通用记忆引擎：事实提炼 + 存储 + 检索）
   ├─ 对外：HTTP API / MCP server / CLI（基础、通用）
   └─ 适配器（pluggable）：Hermes（首要，MCP 记忆）/ ZCode / pi（可选 ext）/ airi（可选 ext）
        │
Hermes Agent（= 真·agent：computer-use + 工具 + 持久记忆 + 多平台）
   ├─ 桌面形态：原生 app / TUI / web / 网关 / petdex 宠物 / hermey-the-pet 前端
   └─ 可选升级宠脸：airi 的 Live2D/VRM 3D 渲染（若想要更精致的脸）
        │
桌宠（前端壳，驱动 Hermes，Hermes 操作电脑）
```
- **pi-agent 降级为可选**：仅当想要 TS 编码 Agent 或 fork 极简内核时保留；不再是必需组件，不再是「操作电脑的 agent」。
- **airi 桌宠降级为可选升级**：Hermes 已有 petdex + hermey-the-pet，3D 精致脸（airi Live2D/VRM）成为可选项而非刚需。

### 11.4 AIXM 与 Hermes 记忆的关系（核心张力）
- Hermes 自带三层持久记忆 + 自动技能，已很强。AIXM 的原始重设动因 = 「事实提炼质量不达标」→ AIXM = **更优的记忆/事实提炼层**。
- 集成路径（**须验证 Hermes 记忆是否可外部替换**）：
  - **路径 B（sidecar，推荐，侵入小）**：AIXM 经 Hermes 的 MCP/事件钩子做旁路——监听 Hermes 活动 → 提炼事实入库；在 Hermes 触发前注入 AIXM 记忆。不触碰 Hermes 内部记忆。
  - **路径 A（替换后端，更强但风险高）**：若 Hermes 内存后端可插（文档提 sqlite/postgres/redis env），AIXM 作为更强后端接管。需核实官方是否开放此钩子。
- 社区 `pi-hermes-memory` 是「给 pi 装 Hermes 式记忆」；AIXM 方向相反 = 「给 Hermes 装 AIXM 式更强记忆」。

### 11.5 待验证 / 风险（落地前必查）
1. **Hermes 记忆能否外部替换/增强？** 核实 `config.yaml` 记忆后端钩子或 MCP 记忆协议是否开放（lobehub 文档提 `HERMES_MEMORY_BACKEND`，系第三方衍生，需官方确认）。
2. **Windows 电脑操控成熟度**：Windows GUI control 2026-06-23 才官宣（via TryCua），仍在演进；用户须原生 Windows（非 WSL2）。先小范围验证再承诺架构。
3. **Hermes 宠物为纯装饰**：真正「驱动操作」的界面是 hermey-the-pet 这类前端壳，petdex 精灵只做状态反馈。架构里「桌宠」= hermey-the-pet 类前端，而非 petdex 精灵。
4. **AIXM 独立性不变**：仍对外 HTTP/MCP/CLI，不依赖 Hermes；Hermes 是首要消费者，pi/airi 是可选。
