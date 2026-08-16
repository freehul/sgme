# 拾光记忆引擎（SGME）— wiki 渐进式披露共享知识库改造方案

> 版本：v0.1（送审稿，已由 v0.2 修订稿取代，本文档存档留痕）
> 日期：2026-08-16
> 地位：送审稿存档。审查意见见 v0.2 头部「审查修订记录」表。

---

## 1. 背景与问题

1. 技能/知识碎片化：Hermes（417+82 技能）与 DSH（29 技能）各维护独立技能库，布局不兼容（Hermes 三级分类 vs DSH 两级发现），无法直接共享目录；双维护、版本漂移。
2. 上下文膨胀：技能全量注入 system prompt 是固定开销（Hermes ~6000 tok/轮），渐进式披露是业界共识解法。
3. 多 agent 共享需求：SGME 已服务 DSH/Hermes/reasonix/trae/workbuddy 五类 agent。
4. 自进化需求：Hermes 有 hindsight；SGME 侧应提供同等能力且写入共享知识库。

## 2. 设计目标（5 个）

① md 内容为主体 ② 渐进式披露（L1 常驻 + L2 按需） ③ 多 agent 共享 ④ 自进化 ⑤ 缓存友好

## 3. 架构总览

wiki_pages 唯一事实源；本地技能保留 L1 常驻与索引引导；自进化写回 wiki（L2），L1 冻结。

## 4. 决策记录（v0.1 原稿要点）

- D1 skill 当知识存 wiki_pages（tags 标记、category 分类）
- D2 raw 不混入
- D3 加 description 字段
- D4 统一搜索过滤 skill
- D5 索引 skill 只写搜索引导
- D6 自进化直接写 wiki（闸门形态待确认 A）
- D7 skills_hub 保留禁用
- D8 官方 skill 插件先不动
- 调研修正：B（author/status/supersedes，待确认）/ C（FTS5 主路径）/ D（L1 稳定排序）

## 5. 详细设计（v0.1 原稿）

- 5.1 迁移 0002：ALTER 加 description；FTS 重建为 fts5(title, description, content, content=wiki_pages) ← **审查 P0-1：此 FTS 写法破坏中文分词，v0.2 已改为保留 content_seg + 新增 description_seg**
- 5.2 检索语义：search.py 排除 tags LIKE skill（← 审查 P1-3 改精确判断）
- 5.3 PATCH /v1/wiki/pages/{id} + MCP wiki_page_update
- 5.4 自进化管线（复用 refinery/llm ← 审查 P0-2：refinery 不接会话，v0.2 改独立 evolve 管线 + 独立游标）
- 5.5 bridge 补三工具
- 5.6 索引 skill 写 ~/.agents/skills/（← 审查 P1-6：改 adapters/dsh/skills/ + install 部署）
- 5.7 手册规范

## 6. 实施任务清单（W1-W7）

## 7. 验证与试点方案

## 8. 风险与回退

## 9. 待审查确认项（A/B/登记/试点）

## 10. 调研参照
