# SGME-Skills管理模块设计-v0.2

> 状态：送审稿（2026-08-26 会话定案熔稿）｜前置：本会话全部讨论决策
> 定位：SGME 把 skill 管理纳入自身体系（吸收/调用/回写/新增全闭环），上线后 progressive-skill 插件卸载退场
> 命名纪律：索引库定名 **skills.db**（不再改名）；progressive-skill 本次 v3.2.0 为收官版，此后仅修 bug

## 一、双线定位（终局）

```
线1 progressive-skill v3.2.0（已交付，2026-08-26）
    无 SGME 的 agent 用：本地文件树 + 零数据库工具链（audit R1-R5/门禁钩子/budget）
    收官：不新增功能；架构红线=永不引入数据库，状态只限 git 树 + JSON 边车
    
线2 SGME skills 管理（本文档）
    有 SGME 的基础设施用：hub API 服务端加强版，思想同源、载体升级
    上线后 → progressive-skill 直接卸载（不做自动探测休眠等花活）
```

## 二、存储四层定稿（含实证修正）

| 层 | 载体 | 内容 | 丢失后果 |
|---|---|---|---|
| 真源 | skills-hub.git（NAS裸仓） | 技能字节+完整历史 | 不可接受（多clone容灾） |
| 索引 | **skills.db**（新建，可重建派生物） | name/tags/description/pattern/category/embedding/content_hash/revision/usage统计 | 无所谓，从git全量重建 |
| 检索 | SGME 统一搜索（ST-24 多源统一检索） | 记忆/wiki/技能统一语义搜索入口 | — |
| 运行时 | agent 本地 | 索引文件 + 读缓存 | 可随时丢弃 |

**与 wiki.db 的关系：分立，不共享。** 判据=生命周期相同才同居：
wiki_pages 是真身（content TEXT 全文在库，库丢数据丢——实证于 sgme/data/db.py L198），
skills.db 是可弃缓存（真身在 git）。备份语义、清理权限完全相反。
跨域查询用 SQLite ATTACH 免费解决。

**实证备注**：主人曾记忆「wiki.db 只存索引、原文在 wiki 目录」——经查证为设计讨论中的方案版本，
落地代码是全文入库（NAS data/ 下无 wiki/skills 目录，只有 raw/ 会话原件目录）。

**经验归属口诀**：跟着技能走的坑进技能本体；跟人走的偏好进 memory.db；世界知识进 wiki.db。

## 三、运行时契约（读侧）

agent 本地只保留：高频热集技能副本 + SGME 操作手册 + 索引文件。其余全部删除，
由 SGME 接管（安装了 SGME = 接管记忆/skill/wiki 三大功能）。

### 四级披露（token 纪律的落地形态）

```
L0 索引常驻     名称+标签+简介（受 budget 预算约束）
L1 摘要         skill_digest(name) → frontmatter+骨架+分册清单
L2 全文         skill_get(name, section?) → 注入上下文（显式调用，禁止预注入）
L3 物化         skill_materialize(name) → 字节精确落盘工作区（脚本执行用）
```

要点：
- 「先审核再注入」不成立（没看到内容无法审核），正确机制=L1摘要当审核媒介，误判成本被压缩
- 物化必须走专用端点而非 LLM 转写（字节保真 + 使用遥测顺带记录）
- 执行 hub 分发的脚本 = 信任 hub 内代码 → 写侧门禁必须严格
- 读缓存透明降级：取过的技能留本地只读副本，NAS 宕机无感退化

### 冷启动包

新 agent 装 SGME → 拉（索引+高频热集+操作手册）→ 即刻可用 → 其余按需检索 →
越用缓存越厚 → 重装零损失。本地目录从「资产」退化为「缓存」。

## 四、写侧治理

### 准入规格（lint 门禁，复用 audit 引擎 R1-R5 + 新增）

frontmatter 完整（name=目录名/version/pattern/category）、触发词在 description 前57字符窗口、
无断链、原子≤8K、名称 kebab-case 全库唯一、scripts/ 资产须在正文声明用途。

### 三层查重

```
同名冲突      目录名唯一性        → 拒绝
同内容异名    归一化SHA256        → 拒绝
语义近亲      embedding相似度     → 警告+人工裁决（分层重叠合法，不自动拦）
```

查重位置前移到**回写之前**：「先搜后写」纪律从源头减少重复产生。

### 修改/合并/删除

- 修改：直接改+commit；**结构性手术前必须先打快照 commit**（2026-08-26 基线快照救场实证）
- 合并：git 不管合并——它是重构式操作，走清单流程（职责对比→新结构→迁移→零丢失校验→登记更新）
- 删除：单向门走 remove_skill API（先扫入向引用，有引用列清单拒绝/--force 清理后删）；
  先软删（deprecated 标记+宽限期）再硬删；git 历史永存兜底
- 改名：永不原地改名，旧名留墓碑别名指向新名

### 索引联动

同步路径：所有变更走 API → 同一操作内完成 落盘+commit+skills.db刷新+引用图重算。
异步兜底：pre-receive 钩子 + cron 对账（git树 vs skills.db vs 登记清单三方比对，漂移即报）。
缓存失效：全局递增 revision / commit SHA 比对，落后即刷。

## 五、自进化闭环（含新增分支）

> 术语注：「沉淀」指已验证流程的格式化固化；「蒸馏」是 Furnace/仓颉域概念
> （大体积内容→压缩提炼），其产物如需入库走「吸收」门。两词不混用。

```
吸收（外部现成包）─┐
新增（沉淀）───────┼→ 统一入口 [lint→查重→登记→原子commit]
回写（patch坑）───┘          ↓
                    git真源 + skills.db 刷新
                             ↓
              调用（四级披露 digest→get→materialize）
                             ↓
                  执行 → 新经验 → 回顶部 ↻
```

三操作分工：

| 操作 | 输入 | 头部差异 | 尾部 |
|---|---|---|---|
| 吸收 | 外部技能包 | 解包+结构校验+登记 | 共用：lint→查重→commit |
| 回写 | 执行踩坑 | 先搜后写定位归属→patch | 同上 |
| 新增 | 跑通的全流程 | **沉淀**：已跑通流程的固化归位（非蒸馏——无压缩提炼环节，见注） | 同上 |

**沉淀触发**（新增分支的路由信号就是「先搜后写」的结果）：

- 任务≥5次工具调用且成功，且搜无归属 → 收尾一行话提议「固化成skill？」
- 主人明说「沉淀成skill」→ 直接触发
- 成本纪律：当前会话 LLM 顺带产出，不单独发起调用

**沉淀模板锚点**：video-analysis-pipeline v2.0.0（2026-08-26 实战重建件）——
frontmatter 触发词57字窗口 / 步骤含精确命令 / 踩坑节填本次真实教训 / 验证步骤。

## 六、纳管迁移（一次性）

两条不变量：

1. **入库先行，删除在后**：本地技能须上传 hub 且验证 skill_get 原样取回后，才允许删本地副本。单向门。
2. **冲突归档**：同名不同内容 → 较新版本入库，旧的以 git 历史/`name@date` 归档，不静默覆盖。

迁移后本地保留集：高频热集 + SGME 操作手册 + 索引文件。

## 七、路线图

| 阶段 | 内容 | 依赖 |
|---|---|---|
| M1 skills.db 建库 + 索引器 | audit 引擎扩展为 indexer（扫描→建表→embedding） | 无 |
| M2 四级披露端点 | digest/get/materialize + 读缓存协议 | M1 |
| M3 写回API + 门禁前移 | patch/remove/rename/merge + lint 内联 | M2 |
| M4 纳管迁移 | 两不变量流程跑通，本地瘦身 | M3 |
| M5 冷启动包 + progressive-skill 卸载 | 收官交接 | M4 |
| （远期）wiki 双轨收敛 | category=skill/* 页面迁出，知识页摘标签 | M4 后择机 |

## 八、评审待决

1. 沉淀触发默认「提议制」（收尾问一句）vs「全自动」，主人偏好哪种默认值？
2. 高频热集的初始名单（建议：coding-discipline、sgme-operations、video-analysis-pipeline、everything 类原子技能）
3. skills.db 的 embedding 模型选型（沿用现有向量管线 or 技能专用）
