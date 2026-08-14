# 第 1 批致命问题修复方案清单

**生成日期**：2026-08-14
**来源**：SGME 项目全面深度检查报告（2026-08-14）
**范围**：9 项致命问题（F-1 ~ F-9）
**目标**：消除阻断核心功能 / 跨机部署 / WebUI 关键流程的致命缺陷
**执行顺序**：后端组（F-1 → F-9 → F-3 → F-2）→ 前端组（F-4 → F-7 → F-8 → F-5 → F-6）

---

## 一、致命问题总览

| 编号 | 类别 | 问题 | 严重性 |
|---|---|---|---|
| F-1 | 代码 | eval/run.py dry_run 恒真，评测框架核心功能失效 | 评测框架永远跑 mock LLM |
| F-2 | 部署 | llm.yaml 降级链缺 lm-studio 兜底 | 离线场景无 LLM 兜底，违反架构约束 #9 |
| F-3 | 部署 | sgme.yaml backup.remote_dir 硬编码本机路径 | 跨机部署失败 |
| F-4 | UI | /sessions 会话原文路由存在但侧栏无入口 | 设计 §4 视图 11 不可达 |
| F-5 | UI | MemoryDetail / IdeaDetail 不响应路由参数变化 | 详情页跳转不刷新 |
| F-6 | UI | SearchView / WikiView 不响应 query 变化 | 全局搜索/Wiki 抽屉不刷新 |
| F-7 | UI | 路由无 404 兜底 + PlaceholderPage 孤儿 | 访问未定义路径白屏 |
| F-8 | UI | SkillsView 引用 FontAwesome 但未引入依赖 | 4 个统计卡图标空白 |
| F-9 | 部署 | 6 个脚本硬编码本机路径读密钥 | 跨机不可用，违反密钥纪律 |

---

## 二、后端组（4 项）

执行后需重启 SGME 服务：
```bash
netstat -ano | findstr 9910
taskkill /PID <pid> /F
python -m sgme
```

### F-1【代码】eval/run.py dry_run 恒真 ✅ 已完成

**根因**：`eval/run.py:145` `dry_run = args.dry_run or True` 是恒真表达式（`False or True = True`），`--dry-run` 参数完全失效。

**改动清单**：

| 文件 | 行号 | 改动 |
|---|---|---|
| `eval/run.py` | 145 | `dry_run = args.dry_run or True` → `dry_run = args.dry_run` |

**删旧件**：否（仅删 `or True` 三个字符）

**设计意图核实**：原注释「默认 dry-run（无 LLM 依赖时）」表达的是"无 LLM 时默认 dry"，但实现写错了。正确做法是让 `--dry-run` 显式控制。若想"无 LLM 依赖时默认 dry"，应检测 LM Studio 可达性后决定，超出本次修复范围。

**验证**：
```bash
# dry-run 模式（应跑 mock）
python -m eval --cases eval/cases/v001_sample.yaml --dry-run
# 真实 LLM 模式（应调真实 deepseek，需 DEEPSEEK_API_KEY 环境变量）
python -m eval --cases eval/cases/v001_sample.yaml
```
日志应分别打印 `模式: dry-run (mock LLM)` 和 `模式: 真实 LLM`。

**pytest**：`python scripts/test_fast.py eval`（应仍全绿，mock 模式行为不变）

**风险**：低。唯一行为变化是 `--dry-run` 不传时从"恒 dry-run"变为"真实 LLM"，这正是设计意图。

---

### F-9【部署】6 个脚本硬编码本机路径读密钥 ✅ 已完成

**根因**：`scripts/check_usage.py:5`、`deepseek_usage.py:5`、`test_dsv4.py:5`、`test_dsv4_nothink.py`、`test_dsv4_full.py`、`test_deepseek_l1.py` 均硬编码 `r"<用户目录>\AppData\Local\hermes\.env"`。

**采纳方案**（用户 2026-08-14 批准备选方案）：6 个一次性调试脚本全部归档 `scripts/oneoff/`，不再维护。如需复用从 `os.environ.get("DEEPSEEK_API_KEY")` 读取。

**归档清单**（已执行，见 `scripts/oneoff/README.md`）：

| 脚本 | 原用途 |
|---|---|
| `check_usage.py` | 查看 DeepSeek usage 返回结构 |
| `deepseek_usage.py` | DeepSeek token 用量/余额统计 |
| `test_dsv4.py` | DeepSeek V4-Flash 思考模式 + 上下文 + 提纯质量测试 |
| `test_dsv4_nothink.py` | DeepSeek V4-Flash 关思考验证 |
| `test_dsv4_full.py` | DeepSeek V4-Flash 完整 L1 提纯测试 |
| `test_deepseek_l1.py` | DeepSeek 提炼质量测试 v2 |

**删旧件**：6 个脚本从 `scripts/` 移至 `scripts/oneoff/`（git mv）。

**验证**：`scripts/` 下不再有这 6 个文件；`scripts/oneoff/README.md` 已登记。

**pytest**：无（脚本不参与测试）

**风险**：低。归档脚本不参与运行时。

---

### F-3【部署】sgme.yaml backup.remote_dir 硬编码本机路径 ✅ 已完成

**根因**：`config/sgme.yaml:35` `remote_dir: E:\SGME_Backup` 是用户本机路径，已入 git。

**改动清单**：

| 文件 | 行号 | 改动 |
|---|---|---|
| `config/sgme.yaml` | 35 | `remote_dir: E:\SGME_Backup` → `remote_dir: ''`（空字符串占位，部署时由 env 注入） |
| `sgme/config.py` | 93-95 | `ENV_OVERRIDES` 字典追加一行：`"backup.remote_dir": "SGME_BACKUP_REMOTE",` |

**删旧件**：否（保留 yaml 字段，仅清空值）

**ENV_OVERRIDES 机制说明**（config.py:88-95）：
- 读取时 env 值优先于 yaml（部署时 `set SGME_BACKUP_REMOTE=E:\SGME_Backup` 注入）
- 落盘（persist_config）恢复文件现值——env 注入值仅存于进程内存，防泄漏进 git
- 更新接口（apply_section）在 env 设置期间忽略该字段（env 优先）

这正是 ST-20（2026-08-11 GitHub 发布前脱敏）的既定机制，本次只是把 `backup.remote_dir` 纳入该机制。

**本机部署兼容**：用户本机需在 `config/.env` 或环境变量加 `SGME_BACKUP_REMOTE=E:\SGME_Backup`，否则 `push_remote` 会因 remote_dir 为空而跳过。

**空值风险核实**（用户 2026-08-14 补充确认）：`backup/manager.py:297` `if remote_dir is None: return {"ok": True, "skipped": True}` 已实现 None 跳过；`operations/backup.py:136` `remote_dir = cfg.get(...) or None` 把 yaml 空字符串转 None。链路完整，无需补兜底。

**验证**：
```bash
# 不设 env，remote_dir 应为空
set SGME_BACKUP_REMOTE= && python -c "from sgme.config import load_config; c=load_config(); print(repr(c['backup']['remote_dir']))"
# 期望输出：''

# 设 env，应注入
set SGME_BACKUP_REMOTE=E:\SGME_Backup && python -c "from sgme.config import load_config; c=load_config(); print(c['backup']['remote_dir'])"
# 期望输出：E:\SGME_Backup
```

**pytest**：`python scripts/test_fast.py config`

**风险**：中。需确认 backup/manager.py 对空 remote_dir 的处理（是否跳过 push_remote 而非报错）。若报错需补空值兜底。

---

### F-2【部署】llm.yaml 降级链缺 lm-studio 兜底 ⚠️ 已撤销

**撤销原因**（2026-08-14 用户纠正）：lm-studio 本地模型是用户主动决定取消的——本地模型能力不足（提炼质量不达标）+ 向量维度不够，才最终采用云端 DeepSeek 提炼。原 F-2 误判为"架构约束 #9 要求 lm-studio 兜底"，未查 SGME 记忆/git 历史核实该决策。

**撤销操作**：
- `config/llm.yaml`：移除 lm-studio 节点，降级链恢复 `deepseek → rule drop_batch`；注释改两级 + 决策溯源
- `config/providers.yaml`：移除 lm-studio 整个 provider 定义（WebUI 供应商卡片与健康探测不再显示）

**教训**：动手前应查 SGME 记忆/git 历史核实用户决策，不可仅凭架构约束文本推断。

---

## 三、前端组（5 项）

执行后需：
```bash
cd ui
npm run build
# 浏览器 Ctrl+F5 硬刷新
```

### F-4【UI】/sessions 会话原文侧栏无入口

**根因**：`router.ts:18` 已注册 `/sessions` 路由，但 `MainLayout.vue:80-102` nav 无对应链接。

**改动清单**：

| 文件 | 行号 | 改动 |
|---|---|---|
| `ui/src/views/layout/MainLayout.vue` | 82（scenes 链接后） | 插入一行 RouterLink |

**插入内容**（在 `<RouterLink to="/scenes">...</RouterLink>` 之后）：
```vue
        <RouterLink to="/sessions"><span class="nav-ico">📜</span><span class="lbl">会话原文</span></RouterLink>
```

**删旧件**：否

**分组归属**：归入「记忆闭环」主功能区（与 memories/scenes 同组，符合设计 §4 视图 11 定位）。

**图标选择**：📜（卷轴，与会话原文语义契合，与既有 emoji 风格一致：📖🗂🎭💗📈⚙📚🧰💡📁📋）

**验证**：`npm run build` → 浏览器 Ctrl+F5 → 侧栏应见「会话原文」入口 → 点击进入 `/sessions` 应显示 SessionView。

**风险**：极低。纯导航链接新增。

---

### F-7【UI】路由无 404 兜底 + PlaceholderPage 改造

**根因**：`router.ts:5-49` 无 `:pathMatch(.*)*` 兜底；`PlaceholderPage.vue` 是孤儿组件且文案过时（"该视图尚未实现"，但实际所有视图已实现）。

**改动清单**：

| 文件 | 行号 | 改动 |
|---|---|---|
| `ui/src/router.ts` | 46（`{ path: 'backup', redirect: '/settings' }` 之后） | 插入 404 兜底路由 |
| `ui/src/views/PlaceholderPage.vue` | 全文 | 改造为 404 页面 |

**router.ts 插入内容**（在 children 数组末尾，最后一个 `]` 之前）：
```ts
      // 404 兜底
      { path: ':pathMatch(.*)*', name: 'not-found', component: () => import('./views/PlaceholderPage.vue'), meta: { title: '未找到' } },
```

**PlaceholderPage.vue 改造**（全文替换）：
```vue
<script setup lang="ts">
import { useRouter } from 'vue-router'
const router = useRouter()
</script>

<template>
  <div class="not-found">
    <div class="nf-code">404</div>
    <h2>页面未找到</h2>
    <p class="empty">您访问的页面不存在或已被移动。</p>
    <button class="btn" @click="router.push('/dashboard')">返回总览</button>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
  gap: 12px;
}
.nf-code {
  font-size: 72px;
  font-weight: 700;
  color: var(--brand);
  line-height: 1;
}
</style>
```

**删旧件**：删除原 PlaceholderPage.vue 的"该视图尚未实现"文案（已无意义）。

**验证**：`npm run build` → 浏览器访问 `/nonexistent` → 应显示 404 页面 + 返回总览按钮。

**风险**：极低。兜底路由不影响既有路由。

---

### F-8【UI】SkillsView 引用 FontAwesome 图标但未引入

**根因**：`SkillsView.vue:70-73` statCards 用 `icon: 'fa-cubes'` 等，`:262` `<i class="fas" :class="c.icon" />`，但 `package.json` 无 FA 依赖。

**改动清单**（推荐方案：改用 emoji，与项目其他页风格一致）：

| 文件 | 行号 | 改动 |
|---|---|---|
| `ui/src/views/skills/SkillsView.vue` | 70-73 | statCards 的 icon 字段改 emoji |
| `ui/src/views/skills/SkillsView.vue` | 262 | `<i class="fas" :class="c.icon" />` → `<span class="stat-emoji">{{ c.icon }}</span>` |

**statCards 改动**（line 70-73）：
```ts
    { label: '总技能数', value: String(total), icon: '🧰', bg: 'rgba(59,130,246,.1)', color: '#3B82F6' },
    { label: '分类数', value: String(cats), icon: '🏷', bg: 'rgba(16,185,129,.12)', color: '#0f9d72' },
    { label: '含描述', value: String(described), icon: '✅', bg: 'rgba(245,158,11,.13)', color: '#b45309' },
    { label: '内容(KB)', value: (chars / 1024).toFixed(1), icon: '📄', bg: 'rgba(99,102,241,.1)', color: '#6366F1' },
```

**模板改动**（line 262）：
```vue
            <span class="stat-emoji">{{ c.icon }}</span>
```

**删旧件**：删除 `<i class="fas" :class="c.icon" />` 这一行（替换为 span）。

**备选方案**（不推荐）：`npm i @fortawesome/fontawesome-free` + main.ts `import '@fortawesome/fontawesome-free/css/all.css'`。但项目其他页全用 emoji，引入 FA 会破坏一致性。

**验证**：`npm run build` → 浏览器 Ctrl+F5 → 技能仓库页 4 个统计卡应显示 emoji 图标（🧰🏷✅📄）。

**风险**：极低。

---

### F-5【UI】MemoryDetail / IdeaDetail 不响应路由参数变化

**根因**：`MemoryDetail.vue:9` `const id = String(route.params.id)` 一次性赋值，`:66` `onMounted(load)` 只执行一次。`IdeaDetail.vue:8,38` 同病。

**改动清单**：

| 文件 | 行号 | 改动 |
|---|---|---|
| `ui/src/views/memory/MemoryDetail.vue` | 2 | `import { onMounted, ref }` → `import { onMounted, ref, watch, computed }` |
| `ui/src/views/memory/MemoryDetail.vue` | 9 | `const id = String(route.params.id)` → `const id = computed(() => String(route.params.id))` |
| `ui/src/views/memory/MemoryDetail.vue` | 27 | `detail.value = await getMemory(id)` → `detail.value = await getMemory(id.value)` |
| `ui/src/views/memory/MemoryDetail.vue` | 39 | `await rejectMemory(id, ...)` → `await rejectMemory(id.value, ...)` |
| `ui/src/views/memory/MemoryDetail.vue` | 52 | `await unrejectMemory(id)` → `await unrejectMemory(id.value)` |
| `ui/src/views/memory/MemoryDetail.vue` | 66 | `onMounted(load)` → `watch(id, () => load(), { immediate: true })` |
| `ui/src/views/ideas/IdeaDetail.vue` | 2 | `import { onMounted, ref }` → `import { onMounted, ref, watch, computed }` |
| `ui/src/views/ideas/IdeaDetail.vue` | 8 | `const ideaId = route.params.id as string` → `const ideaId = computed(() => route.params.id as string)` |
| `ui/src/views/ideas/IdeaDetail.vue` | 29 | `await getIdea(ideaId)` → `await getIdea(ideaId.value)` |
| `ui/src/views/ideas/IdeaDetail.vue` | 38 | `onMounted(load)` → `watch(ideaId, () => load(), { immediate: true })` |

**注意**：IdeaDetail.vue 后续还有 `idea.value.memory_id` 等用法，需全文 Grep `ideaId` 替换为 `ideaId.value`（load 函数内、promote 函数内等）。MemoryDetail 同理需 Grep `id` 在 load/reject/unreject 内的用法。

**删旧件**：否（仅改写法）

**验证**：
1. 进入 `/memories/A` 显示 A 详情
2. 不离开页面，地址栏改为 `/memories/B`（或从搜索结果点另一个记忆），详情应刷新为 B
3. IdeaDetail 同理

**风险**：低。watch + computed 是 Vue 标准模式。

---

### F-6【UI】SearchView / WikiView 不响应 query 变化

**根因**：`SearchView.vue:67` `onMounted(syncFromQuery)` 只首次执行，注释 line 58 说"响应后续查询参数变化"但实际没 watch。`WikiView.vue:80-85` `onMounted` 内读 `route.query.page_id` 只首次。

**改动清单**：

| 文件 | 行号 | 改动 |
|---|---|---|
| `ui/src/views/search/SearchView.vue` | 2 | `import { onMounted, ref }` → `import { onMounted, ref, watch }` |
| `ui/src/views/search/SearchView.vue` | 67 | `onMounted(syncFromQuery)` → `watch(() => route.query, syncFromQuery, { immediate: true })` |
| `ui/src/views/wiki/WikiView.vue` | 2 | `import { onMounted, ref }` → `import { onMounted, ref, watch }` |
| `ui/src/views/wiki/WikiView.vue` | 80-85 | `onMounted` 内的 `if (pid) openDetail(pid)` 抽出为独立函数 + watch |

**WikiView 具体改法**：
```ts
// 原 line 80-85
onMounted(async () => {
  await load()
  const pid = String(route.query.page_id || '')
  if (pid) openDetail(pid)
})

// 改为
onMounted(load)

watch(() => route.query.page_id, (pid) => {
  if (pid) openDetail(String(pid))
}, { immediate: true })
```

**注意**：`immediate: true` 时 onMounted 的 load 可能还没跑完，openDetail 会先请求 page 详情。这没问题（两个请求独立），但若想严格顺序可去掉 immediate 改为 onMounted 内首次调用 + watch 后续。考虑到 WikiView 列表与详情独立，保留 immediate 即可。

**SearchView 同理**：`watch(() => route.query, syncFromQuery, { immediate: true })`，syncFromQuery 内已判断 `q !== query.value.trim()` 才触发，避免重复请求。

**删旧件**：否

**验证**：
1. SearchView：在 `/search?q=A` 检索后，顶栏再搜 `B`，地址栏变 `/search?q=B`，结果应刷新为 B
2. WikiView：在 `/wiki?page_id=X` 打开 X 详情后，从统一检索点另一个 page，地址栏变 `?page_id=Y`，抽屉应切换为 Y

**风险**：低。

---

## 四、文件改动矩阵

| 文件 | 涉及问题 | 改动类型 |
|---|---|---|
| `eval/run.py` | F-1 | 删 `or True` |
| `scripts/oneoff/check_usage.py` | F-9 | 归档（原 `scripts/check_usage.py`） |
| `scripts/oneoff/deepseek_usage.py` | F-9 | 归档（原 `scripts/deepseek_usage.py`） |
| `scripts/oneoff/test_dsv4.py` | F-9 | 归档（原 `scripts/test_dsv4.py`） |
| `scripts/oneoff/test_dsv4_nothink.py` | F-9 | 归档（原 `scripts/test_dsv4_nothink.py`） |
| `scripts/oneoff/test_dsv4_full.py` | F-9 | 归档（原 `scripts/test_dsv4_full.py`） |
| `scripts/oneoff/test_deepseek_l1.py` | F-9 | 归档（原 `scripts/test_deepseek_l1.py`） |
| `scripts/oneoff/README.md` | F-9 | 新增归档说明 |
| `config/sgme.yaml` | F-3 | remote_dir 改空字符串 |
| `sgme/config.py` | F-3 | ENV_OVERRIDES 加 backup.remote_dir |
| `sgme/operations/backup.py` | F-3 | 空字符串转 None |
| `config/llm.yaml` | F-2 | ⚠️ 已撤销：移除 lm-studio 节点（原插入已回滚） |
| `config/providers.yaml` | F-2 | ⚠️ 已撤销：移除 lm-studio provider 定义（原补 vector_capable 已回滚） |
| `ui/src/views/layout/MainLayout.vue` | F-4 | 加 /sessions RouterLink |
| `ui/src/router.ts` | F-7 | 加 404 兜底路由 |
| `ui/src/views/PlaceholderPage.vue` | F-7 | 改造为 404 页面 |
| `ui/src/views/skills/SkillsView.vue` | F-8 | FA 图标改 emoji |
| `ui/src/views/memory/MemoryDetail.vue` | F-5 | id 改 computed + watch |
| `ui/src/views/ideas/IdeaDetail.vue` | F-5 | ideaId 改 computed + watch |
| `ui/src/views/search/SearchView.vue` | F-6 | syncFromQuery 改 watch |
| `ui/src/views/wiki/WikiView.vue` | F-6 | page_id 改 watch |

**共 21 个文件**，后端 12 个 + 前端 9 个。

---

## 五、验证清单（全部改完后）

### 后端验证
```bash
# 1. pytest 相关模块
python scripts/test_fast.py eval config llm

# 2. 重启服务
netstat -ano | findstr 9910
taskkill /PID <pid> /F
python -m sgme

# 3. 真实 LLM 冒烟（AGENTS.md 强制要求）
python scripts/e2e_smoke_v04.py
# 查 Server 日志无 "L1.5 输出解析失败" / "降级直存"

# 4. 降级链验证（关 LM Studio，临时改错 DEEPSEEK_API_KEY，应降级到 rule drop_batch）
```

### 前端验证
```bash
cd ui
npm run build
# 浏览器 Ctrl+F5 硬刷新
```

### 逐项功能验证
- F-1：`python -m eval` 不带 `--dry-run` 应跑真实 LLM
- F-2：health 端点应见 lm-studio 节点
- F-3：`SGME_BACKUP_REMOTE` env 注入生效
- F-4：侧栏见「会话原文」入口
- F-5：记忆/创意详情页路由参数变化能刷新
- F-6：检索/Wiki query 变化能刷新
- F-7：访问 `/nonexistent` 显示 404
- F-8：技能仓库 4 个统计卡显示 emoji
- F-9：3 个脚本不依赖硬编码路径

---

## 六、提交计划

按 AGENTS.md「逻辑分组提交」：

| 序号 | 提交信息 | 涉及问题 |
|---|---|---|
| 1 | `fix: 评测框架 dry_run 恒真导致无法跑真实 LLM` | F-1 |
| 2 | `fix: 脚本硬编码本机路径读密钥改为环境变量` | F-9 |
| 3 | `fix: 备份异地目录硬编码改为 env 注入` | F-3 |
| 4 | `fix: LLM 降级链补 lm-studio 离线兜底` | F-2 |
| 5 | `fix: WebUI 路由与导航致命问题` | F-4/F-5/F-6/F-7/F-8 合并 |

提交信息格式遵循 AGENTS.md「scene: git_message」规则：`<type>: <中文描述>`。

---

## 七、决策记录（2026-08-14 用户批准确认）

1. **F-9 处理方式**：✅ 采纳归档方案，6 个脚本全部移至 `scripts/oneoff/`（含补充的 test_dsv4_nothink/test_dsv4_full/test_deepseek_l1）。
2. **F-2 providers.yaml**：⚠️ 已撤销。用户纠正：lm-studio 本地模型是主动决定取消的（能力/向量维度不足），原 F-2 误判。已从 providers.yaml 移除整个 lm-studio provider 定义，从 llm.yaml 降级链移除 lm-studio 节点。
3. **F-3 空值兜底**：✅ 已核实 `backup/manager.py:297` None 跳过 + `operations/backup.py:136` 空字符串转 None，链路完整，无需补兜底。
4. **F-7 PlaceholderPage**：✅ 72px 大 404 + 返回总览按钮。
5. **F-8 图标方案**：✅ emoji（🧰🏷✅📄）。
6. **提交粒度**：5 个提交（按逻辑分组）。
7. **F-1 验证控费**：真实 LLM 冒烟用小样本（`eval/cases/v001_sample.yaml`）。
8. **文档登记**：修复完成后在 `docs/design/SGME-实施变更记录-v0.9.md` 记一条 B 系列（F-2/F-3 涉及架构约束 #9 与 ST-20 扩展）。

---

## 八、风险与回滚

### 整体风险
- 后端组：中等（涉及 LLM 降级链 + 配置注入机制，需实测）
- 前端组：低（纯 UI 改动，不影响数据）

### 回滚方案
- 所有改动均在 git 跟踪文件内，回滚用 `git revert <commit>` 即可
- 配置文件改动（sgme.yaml / llm.yaml / providers.yaml）回滚后需重启服务
- 前端改动回滚后需重新 `npm run build`

### 回滚验证
回滚后跑 `python scripts/test_fast.py` 全量快测，确认无回归。

---

*本清单为执行依据，确认后按后端组 → 前端组顺序动手。每完成一项跑相关验证，全绿后再进下一项。*
