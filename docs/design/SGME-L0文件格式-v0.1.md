# SGME L0 原始层文件格式 v0.1

> 日期：2026-08-03
> 依据：SGME-架构设计-0.4.md §8（原始层自持文件）/ §9.1（L0 捕获与提炼调度）/ §14 #27；SGME-数据模型设计-v0.1.md（raw_files 索引表）
> 目标：AI 可读（LLM 直接喂入提炼）、人可读、可解析（消息切分）、追加安全（不可变 + 原子追加）

---

## 1. 目录组织

```text
raw/
├── sessions/    # 会话记录（source_type=session）
├── uploads/     # 用户喂入资料（source_type=upload，按资料粒度一文件）
└── external/    # 外部事件摄入（source_type=external，如 Agent Mail 邮件）
```

文件命名：`{file_id}.md`（UUID4）。session_key 等元数据在 frontmatter，不依赖文件名（防特殊字符/路径注入）。

## 2. 文件结构

```markdown
---
format_version: 1
file_id: a1b2c3d4-...
session_key: 20260803_191806_5b6c69
agent_id: hermes
source_type: session
started_at: 2026-08-03T11:18:06Z
metadata:
  app: hermes-desktop
---

# 2026-08-03T11:18:06Z user

> 用户消息正文，Markdown 原样保留（含代码块）

## 2026-08-03T11:20:00Z assistant

助手回复正文

## 2026-08-03T11:20:05Z tool

**tool**: read_file

工具输出（原样）

## 2026-08-03T11:25:00Z assistant

（……后续消息……）
```

### frontmatter 字段（不可变元数据）

| 字段 | 必填 | 说明 |
|---|---|---|
| format_version | 是 | 格式版本（当前 1），解析器按版本兼容 |
| file_id | 是 | 与 raw_files.file_id 一致（UUID4） |
| session_key | 是 | 来源会话 id（Agent 原样），行内字段（§16.2 L0） |
| agent_id | 是 | 来源 Agent（hermes / x…） |
| source_type | 是 | session / upload / external |
| started_at | 是 | ISO 8601 UTC |
| metadata | 否 | 附加元数据（app 版本、平台等） |

> **ended_at / status / refined_at 不入文件**——属可变状态，由 raw_files 表维护（追加时重写 frontmatter 不经济，且破坏"文件正文只追加"的不可变语义）。

### 消息块约定

- 标题格式：`# {ISO时间} {role}`（首块）或 `## {ISO时间} {role}`（后续块）；role ∈ user / assistant / tool / system
- 消息正文 = 标题行之后至下一个标题行之前的内容（空行忽略）
- tool 块正文首行为 `**tool**: {工具名}`，随后为工具输出
- **msg_id 不入文件**：解析器按出现顺序编号 `1..n`，溯源引用 `source_ref = {file_id}:{seq}`（追加只增序号，既有序号不变，溯源稳定）

## 3. 追加语义（/v1/append）

- 同一 session_key 后续 append：**文件尾部原子追加**新消息块；已有内容与序号不变
- 追加时 frontmatter 不动（ended_at 由 raw_files 表更新）
- 追加后 raw_files.status 置 new + `last_refined_seq` 保留（触发增量提炼）
- 首块消息用 `#` 标题、后续用 `##`——追加方只需记住"文件存在则用 ##"，无需解析全文

## 3.1 增量提炼段（v0.4 二轮修订，K3 审查补）

- 增量段界定：`seq > raw_files.last_refined_seq` 的消息块（msg_id = file_id:seq，见 §2）
- 追加后提炼只把增量段喂 L1，已提炼内容不动；提炼完成更新 `last_refined_seq` 与 `refined_at`
- 全量重提炼（prompt 升级 / 手动触发）时 `last_refined_seq` 置 0 重新走全文件

## 4. 解析规则（提炼 / 溯源读取）

1. 读 frontmatter（首两个 `---` 之间，YAML）
2. 按 `^#{1,2} .* (user|assistant|tool|system)$` 切消息块（正则匹配行首标题）
3. 每块：时间戳 + role + 正文；tool 块解析首行工具名
4. 序号按解析顺序 1..n 生成
5. 校验：frontmatter 缺失 / 格式版本不识别 → 文件标记 `status=error` 并产 `anomaly_warn`（§3 提炼健康自检）

## 5. 冷归档（§17）

- 超过 90 天未变更的文件：zstd 压缩为 `raw/archive/YYYY-MM/{file_id}.md.zst`，raw_files.status=archived
- 内容字节级不变（压缩是编码变换，解压后与原文一致），溯源仍可用（按需解压读取）
- 归档不影响 source_ref 稳定性（引用的是 file_id:seq）

## 6. 与接口 / 数据模型映射

- `/v1/append`（接口契约 4.1）content 字段即本格式正文；服务端补全 frontmatter 落盘
- raw_files 表字段对应：path（目录+文件名）、session_key / agent_id / source_type / started_at / ended_at 来自 frontmatter 或 append 请求、refined_at / status 由提炼调度维护
- L1 提炼输入 = 文件全文（消息块序列），单文件超当前模型上下文时走滑窗/分块（详细设计待办①）

## 7. 待实现期验证

- 追加并发（同文件两路 append）用文件锁串行化
- uploads/external 文件的"会话语义"（无消息结构时整文件作为一条内容处理）

*文档完。*
