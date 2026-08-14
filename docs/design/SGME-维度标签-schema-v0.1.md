# SGME 维度标签 Schema 草案 v0.1（2026-08-03）

> **⚠️ 已被 SGME-数据模型设计-v0.1.md（D8）取代**（2026-08-04，K3 审查标注）——memories 内联 `archived_memory_id` / `source_message_ids` 字段在 D8 中已规范化为独立 `memory_archive` / `memory_sources` 表，建表以 D8 为准，本文仅存演进记录。
> 依据：SGME-架构设计-0.4.md §8.1/§8.2、已解决 #13（标签存储方案）/ #19（TTL）/ #26（TTL 起算点）
> 状态：**历史草案**（已被 D8 取代）

## 表结构

### dimension_registry（维度注册表）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 存储键，英文 snake_case（identity / tech_stack …） |
| display_name | TEXT | 中文展示名 |
| category | TEXT | static / pattern / dynamic（§8.1 三类） |
| time_velocity | TEXT | 默认 static / dynamic，记忆级可覆盖 |
| ttl_days | INTEGER NULL | 动态维度默认 TTL；NULL = 不过期 |
| description | TEXT | 边界定义（源自注册表 YAML） |
| created_at | TEXT | 注册时间 |
| active | INTEGER | 软删除位（0/1），新增维度可随时加行 |

### dimension_alias（别名归一化表）

| 字段 | 类型 | 说明 |
|---|---|---|
| alias | TEXT PK | 自然语言表述（中文为主） |
| dimension_id | TEXT FK → dimension_registry.id | 归一化目标 |

### memories（记忆池主表，TTL 字段扩展）

| 字段 | 类型 | 说明 |
|---|---|---|
| memory_id | TEXT PK | 判等锚点（§8.3 模式 A） |
| content | TEXT | 事实内容 |
| memory_type | TEXT | persona / episodic / instruction |
| priority | INTEGER | 0-100 |
| time_velocity | TEXT | static / dynamic（记忆级覆盖） |
| ttl_days | INTEGER NULL | 记忆级 TTL 覆盖；NULL 沿用维度默认或不过期 |
| created_at / updated_at | TEXT | **TTL 起算点 = updated_at**（update/merge 续期，§8.2） |
| source_message_ids / agent_tag | TEXT | 溯源（§8.3） |
| archived_memory_id | TEXT NULL | Supersession 归档链（旧值归档不删除） |

### memory_tags（标签关联表，已解决 #13 定案）

| 字段 | 类型 | 说明 |
|---|---|---|
| memory_id | TEXT FK → memories | 记忆 |
| dimension_id | TEXT FK → dimension_registry | 维度 |
| PK (memory_id, dimension_id) | — | 复合主键 |

## 索引

- `memory_tags(dimension_id, memory_id)` 复合索引——标签交集过滤走索引 JOIN（§8.1/§14 #13 已决，不用 JSON 函数或位图）
- `memories(updated_at DESC)` ——动态维度默认排序（§8.2）
- `memories(priority DESC)` ——静态维度默认排序（§8.2）
- TTL 过滤：`WHERE updated_at > datetime('now', '-' || ttl_days || ' days')`（动态维度 + ttl_days 非空），无需物化过期标记

## 查询语义（§8.2 映射）

- 模板 section：`SELECT ... FROM memories JOIN memory_tags ... WHERE dimension_id IN (AND 交集 / match:any 并集) [AND updated_at > 时间窗] [AND priority >= min] ORDER BY priority|updated_at DESC LIMIT n`
- TTL 过滤默认开启（动态维度），`ttl_filter: false` 显式关闭
- 向量/FTS5 不进入模板查询，仅 /search 使用（§8.2/§10）

## 待 D8 定稿点

- memory_tags 关联表是否冗余存 dimension 快照（维度改名时标签迁移策略）
- memories 是否拆分 archive 表（Supersession 归档量增长后的查询性能）
- JSONL 元数据（L0 原始层文件）与 SQLite 索引的衔接
