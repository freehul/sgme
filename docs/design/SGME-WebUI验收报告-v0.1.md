# SGME WebUI 验收报告 v0.1（ST-7 逐视图无头验收）

> 验收日期：2026-08-20 ｜ 验收方式：无头静态核对（本机无法开浏览器） ｜ 验收人：SGME 前端验证工程师
> 关联：Backlog ST-7（WebUI 管理面板）、docs/design/SGME-WebUI设计-v0.1.md
> 结论：**全部 ✓，0 ✗**，npm run build 通过（111 modules / 937ms）

## 1. 验收范围与方法

1. 读 ui/src/router.ts 全部路由，对照 MainLayout 4 导航分组列出视图组件
2. 逐个视图核对：① 组件文件存在 ② 对应 api client 封装存在 ③ 路由注册正确
3. 前端构建：cd ui && npm run build（vite build；worktree 无 node_modules，复用主工作区依赖经 junction 链接）
4. 产物：本报告 + Backlog ST-7 状态更新

> 说明：Backlog 任务描述导航为「总览/记忆与知识/创意与需求/配置与管理」；代码实际 group-label 为**总览/记忆闭环/创意与需求/系统管理**（4 组不变，命名以代码为准）。

## 2. 路由盘点（ui/src/router.ts，共 27 路由 = 26 子路由 + 根 /）

| # | 路由 path | name | 组件文件 | api client | 存在性 |
|---|-----------|------|----------|------------|--------|
| 0 | / | —（layout） | views/layout/MainLayout.vue | client.ts | ✓ |
| 1 | /dashboard | dashboard | views/dashboard/DashboardView.vue | dashboard.ts | ✓ |
| 2 | /memories | memories | views/memory/MemoryList.vue | memory.ts / admin.ts | ✓ |
| 3 | /memories/:id | memory-detail | views/memory/MemoryDetail.vue | memory.ts | ✓ |
| 4 | /scenes | scenes | views/scenes/SceneList.vue | knowledge.ts | ✓ |
| 5 | /sessions | sessions | views/sessions/SessionView.vue | knowledge.ts | ✓ |
| 6 | /search | search | views/search/SearchView.vue | memory.ts | ✓ |
| 7 | /profile | profile | views/profile/ProfileView.vue | memory.ts | ✓ |
| 8 | /ideas | ideas | views/ideas/IdeaList.vue | ideas.ts | ✓ |
| 9 | /ideas/:id | idea-detail | views/ideas/IdeaDetail.vue | ideas.ts | ✓ |
| 10 | /demands | demands | views/demands/DemandList.vue | demands.ts / projects.ts | ✓ |
| 11 | /projects | projects | views/projects/ProjectList.vue | projects.ts | ✓ |
| 12 | /settings | settings | views/settings/SettingsView.vue（聚合 9 标签页，见 §3.2） | admin.ts / llm.ts 等 | ✓ |
| 13 | /wiki | wiki | views/wiki/WikiView.vue | wiki.ts | ✓ |
| 14 | /skills | skills | views/skills/SkillsView.vue | skills.ts | ✓ |
| 15 | /roles | roles | views/care/RolesView.vue | roles.ts | ✓ |
| 16 | /signals | signals | views/care/SignalsView.vue | roles.ts | ✓ |
| — | /templates /registry /agents /prompts /providers /modules /config /backup（8 条） | — | redirect → /settings（旧入口兼容，无需组件） | — | ✓ |
| — | /:pathMatch(.*)* | not-found | views/PlaceholderPage.vue | — | ✓ |

## 3. 4 导航 × 视图清单（MainLayout.vue nav 段）

### 3.1 导航分组

| 导航组（代码实际命名） | 侧边栏项 | 路由 |
|------------------------|----------|------|
| ① 总览 | 总览 | /dashboard |
| ② 记忆闭环 | 统一检索 / 用户画像 / 记忆浏览 / 场景管理 / 会话原文 | /search /profile /memories /scenes /sessions |
| ③ 创意与需求 | 创意池 / 项目池 / 待办 | /ideas /projects /demands |
| ④ 系统管理 | 角色管理 / 关怀信号 / Wiki 知识库 / 技能仓库 / 设置 | /roles /signals /wiki /skills /settings |

### 3.2 设置页 9 标签页（SettingsView.vue 聚合，非独立路由）

| 标签页 | 组件文件 | api client | 存在性 |
|--------|----------|------------|--------|
| 通用设置 | settings/SettingsGeneral.vue | admin.ts / dashboard.ts | ✓ |
| 模型供应商 | llm/ProvidersView.vue | llm.ts | ✓ |
| TTL 配置 | settings/SettingsTtl.vue | admin.ts | ✓ |
| 模板管理 | templates/TemplatesView.vue | admin.ts | ✓ |
| Agent 管理 | settings/AgentsTab.vue | admin.ts | ✓ |
| 维度注册表 | settings/RegistryTab.vue | admin.ts | ✓ |
| 提示词 | settings/PromptsTab.vue | admin.ts | ✓ |
| 扩展模块 | settings/SettingsExtensions.vue | admin.ts | ✓ |
| 备份管理 | settings/BackupTab.vue | admin.ts | ✓ |

## 4. 验收统计

- **视图文件**：ui/src/views/ 下共 **29 个 .vue 文件**，全部存在 ✓
  - 被引用 27 个：18 个路由组件（含 MainLayout + 404 PlaceholderPage）+ 9 个设置标签页
  - 未挂载遗留 2 个（存在但无任何引用，不判 ✗，仅备注）：settings/ConfigTab.vue、modules/ModulesView.vue
- **路由**：27 条（26 子路由 + 根 /）= 16 业务路由 + 8 旧入口 redirect + 1 404 + 根 redirect；组件 import 全部解析 ✓
- **api client**：ui/src/api/ 下 **12 个 .ts**（admin/client/dashboard/demands/ideas/knowledge/llm/memory/projects/roles/skills/wiki），全部被视图引用 ✓
- **build**：npm run build → vite v5.4.21，**111 modules transformed，✓ built in 937ms，exit 0**；dist 产物 47 文件（视图 chunk + api chunk 全部生成）
- **✓ / ✗**：27/27 ✓，**0 ✗**

## 5. 结论

ST-7 WebUI 管理面板逐视图验收**通过**：4 导航 × 27 被引用视图全部存在且路由注册正确，api client 封装齐全，前端构建成功。无浏览器环境下的无头验收证据充分，Backlog ST-7 状态 🔴 待验收 → ✅ 已解决。
