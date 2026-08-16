---
name: wiki-skill-discovery
description: 发现并加载 SGME 知识库中的技能手册。当任务需要操作手册（如 SGME 运维、GitHub 操作、弱电流程）或不确定知识库有哪些手册时使用——按分类列目录、按描述判断、拉全文执行。
---

# Wiki Skill Discovery（知识库技能手册发现）

SGME wiki 是跨 Agent 共享知识库：技能手册、经验、知识统一以 wiki_pages 存储（category 分类 + FTS 检索）。本 skill 教你**发现并加载**手册——内容永远从 wiki 数据库拉取（检索快准），本地不留副本。

## 何时使用

- 任务需要"操作手册"类知识（SGME 运维、GitHub 操作、弱电工程流程等）
- 不确定知识库有没有相关手册
- 需要按分类浏览手册目录

## 使用步骤

### 1. 按分类列手册目录

调用 `wiki_pages` 工具，category 传 `skill/<domain>`（如 `skill/sgme`、`skill/github`）：

- 返回轻量列表：标题 / 分类 / 描述 / 标签 + page_id
- 不传 category 时列出全部（先看有哪些分类再定位）

### 2. 判断加载哪本

读列表的 title + description，匹配当前任务意图：

- description 是触发式描述（如"操作 SGME 时加载本手册"），直接按描述判断
- 不确定时用 `wiki_search` 全文搜索关键词（FTS5 BM25 + 中文分词）

### 3. 拉全文执行

调用 `wiki_page` 工具，传 page_id 拉取手册全文：

- 全文含 frontmatter（name/description/type）+ 正文步骤 + 末尾「踩坑记录」章节
- 按步骤执行；执行中遇到问题**先查手册踩坑记录**（可能有现成答案）

## 铁律

1. **手册内容永远从 wiki 拉取**，本地不缓存副本（数据库检索快准，且保证读到最新）
2. 执行完如发现新踩坑/新变化，**按自进化规范回写手册**：追加到「踩坑记录」章节，带来源会话 + 时间戳 + 去重 hash（只增不改）
3. 手册不存在时如实说明"知识库无此手册"，可提议创建（category=skill/<domain>）
