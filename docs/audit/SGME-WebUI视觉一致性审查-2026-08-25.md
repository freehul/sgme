# SGME WebUI 视觉一致性审查报告（2026-08-25）

> 审查人：UI 风格审查子代理（只读审查）；逻辑体检由并行子代理执行，结论已并入 Backlog T-104。

审查完成（只读，未改任何文件）。以下为风格问题清单。

# SGME WebUI 视觉一致性审查报告

设计令牌真源：`ui/src/style.css` v0.2。可用语义 token：`--brand/--success/--warn/--danger` 及对应 `-soft`、`--text/--text-muted/--text-faint`、`--surface*`、`--border/--border-strong/--divider`、状态类 `--st-*`。

---

## 1. DashboardView.vue（13 处）

**升级横幅区（L425–505）——最严重，蓝色 #3B82F6 与品牌紫 --brand 冲突**
- L428 `.pulse-dot background: #3B82F6` → `var(--brand)`
- L413/415 `border: rgba(59,130,246,.35)` / `background: rgba(59,130,246,.08)` → `color-mix(in srgb, var(--brand) 35%, transparent)` / `8%`
- L441 `.update-link color: #3B82F6` → `var(--brand)`
- L452–453 `.update-btn background:#3B82F6; color:#fff` → `var(--brand)` + `var(--brand-contrast)`
- L466 `.update-msg color:#3B82F6` → `var(--brand)`
- L469 / L501 `.update-msg.err / .badge-count color:#ef4444` → `var(--danger)`；L500 `rgba(239,68,68,.12)` → `var(--danger-soft)`
- L496–497 `.tabs-line button.active` 蓝色激活态 → `var(--brand)` / `var(--brand)`

**图表数据色（L123–126）可保留但建议注释标注**：`#3B82F6/#6366F1/#F59E0B/#10B981` 是管道阶段数据色。问题：深色主题下 #F59E0B 偏刺眼、浅色下尚可。建议至少换成 CSS 变量形式（`--chart-l0` 等，在 `[data-theme]` 两套定义），或对齐语义 `--brand/--success/--warn`。

## 2. RolesView.vue（9 处）——fallback 色体系整体漂移

该页用了 style.css 中**不存在**的变量名并配旧深色主题 fallback：
- L416 `var(--border, #444)` → 删 fallback，用 `var(--border)`
- L422/423/443 `var(--accent, #4a9eff)`（3 处）→ `var(--brand)`（#4a9eff 亮蓝与全局紫色系冲突）
- L429 同上 → `var(--brand-text)` 或 `var(--brand)`
- L437–438 `var(--ok, #4caf50)`（2 处）→ `var(--success)`（style.css 无 `--ok`，实际永远走 fallback #4caf50 Material 绿，与 --success 不一致）
- L446 `var(--bg2, #1a1a1f)` persona 文本框背景 → `var(--surface-muted)`（#1a1a1f 在浅色主题下会渲染成近黑色块，**这是浅色主题下必坏的点**）

## 3. SignalsView.vue（9 处）

- **L21–25 TYPE_LABEL 数据色**：`#ff9800/#e91e63/#f44336/#4caf50/#2196f3` 全是 Material 彩色标签，绕过语义体系且浅色主题下对比度不足（#ff9800 黄橙字在白底上 ≈2.2:1，不达 WCAG AA）。care_todo_due→`--warn`、care_overwork→`--danger`、care_daily→`--success`、memory_updated→`--brand`；care_mood 无对应，建议新增 `--mood` token 或统一走 `--st-*-soft` 底+深字组合
- L29 fallback `#9e9e9e` → `var(--text-faint)`
- L222 `var(--accent, #4a9eff)` → `var(--brand)`
- L235 `var(--ok, #4caf50)` → `var(--success)`
- L236 `var(--warn, #ff9800)`：变量名对但 fallback 错 → 删 fallback 用 `var(--warn)`（#b45309/#f0a83a）

## 4. SkillsView.vue（9 处）

- L71 `#3B82F6` + `rgba(59,130,246,.1)` → `var(--brand)` + `var(--brand-soft)`
- L72 `#0f9d72` → `var(--success)`（又一个自造绿，与 #0f766e/#4caf50 三绿并存）
- L73 `#b45309` 恰好等于浅色 `--warn` 值，但仍应显式用 token
- L74 `#6366F1` → `var(--brand)`
- L277–279 `.cap-active/.cap-green/.cap-red` → `--brand/--success/--danger` + 对应 `-soft` 背景
- L312 `.markdown-content :deep(pre) background:#1f2937; color:#f9fafb` —— **硬编码深色代码块**，浅色主题下是页面上突兀的黑块。WikiView 有同款（见下），应抽成共享样式并用 `var(--surface-muted)/var(--text)` 或新增 `--code-bg/--code-text`

## 5. WikiView.vue（8 处）

- L247 同上 pre 硬编码深色块 → 与 SkillsView 统一抽公共 markdown 样式
- L254 `.dim-tag.skill-tag` `#EF4444` + `rgba(239,68,68,.15)` → `var(--danger)` + `var(--danger-soft)`
- L256–260 分类标签 cat-blue/green/yellow/purple/red：这是**分类数据色**，性质类似图表色可保留色相，但文字色需按主题切换（浅色下 #B45309 尚可、#10B981 偏浅）。建议改为 `--cat-blue`…token 双主题定义，或统一用 `-soft` 底 + `--st-*-text` 字的既有模式

## 6. GraphView.vue（7 处）——大部分可保留

- L24–26 节点类型色 `#e07b39/#3a7bd5/#2e9e6b`：SVG 图谱数据色，**可保留**（D3 无法直接吃 CSS 变量的地方可用 `getComputedStyle` 读 token）
- L29 fallback `#888` 可接受（兜底灰）
- L93 `stroke:'#999'` 连线色：深色下偏暗、浅色下偏淡，建议读 `var(--divider)` 或 `--border-strong` 计算值
- L115 `.attr('stroke','#fff')` 节点描边白色——**浅色主题下白底白描边会让节点糊边**，应取 `var(--surface)` 计算值
- L333 `color:#fff` 需确认所在元素背景是否随主题变化，若是深色固定底则可留

## 7. ProfileView.vue（6 处）——GitHub 深色 fallback 残留

全部是 `var(不存在变量, GitHub-dark 色)` 形式，浅色主题下 fallback 生效即坏：
- L74 `var(--success, #3fb950)` → `var(--success)`（删 fallback 即可，变量存在）
- L75 `var(--warn, #d29922)` → `var(--warn)`
- L77/80 `var(--border, #30363d)` → `var(--border)`
- L77 `var(--bg-card, #161b22)` → `var(--surface)`（`--bg-card` 不存在，浅色下会出黑卡片）
- L83 `var(--muted, #8b949e)` → `var(--text-muted)`

---

## 8. 重复实现：fmtTs × 7

`views/care/RolesView.vue:62`、`care/SignalsView.vue:32`、`dashboard/DashboardView.vue:88`、`memory/MemoryDetail.vue:61`、`memory/MemoryList.vue:67`、`scenes/SceneList.vue:32`、`settings/AgentsTab.vue:62`。

实现完全相同（`!ts → '—'`，否则 `toLocaleString()`）；仅 SignalsView/AgentsTab 参数类型写成 `string | null`（少了 undefined），行为无差异。utils 目录已存在（markdown.ts），**抽取可行性高、零风险**：新建 `src/utils/format.ts` 导出 fmtTs，7 处改 import，签名统一 `(ts: string | null | undefined) => string`。建议顺带评估 `fmtBytes/fmtNum` 等同类小工具一并归入。

## 9. 布局规范

- **head/filters 结构**：25 个页面均含 `class="head"`，结构统一 ✅，无需整改。
- **空态文案**：全部复用全局 `.empty` 类 ✅。风格上分两派：「暂无 X。」极简型（MemoryList/SceneList/SessionView/BackupTab）vs「暂无 X。点击「Y」…」引导型（Roles/Signals/Demands/Ideas/Projects）。建议规范为：列表主空态一律引导型（说明下一步动作），嵌套子区块（日报/备注/事件）保持极简型。
- **Modal**：`style.css` L1139 已有全局 `.modal/.modal-box/.modal-actions`，demands/ideas/projects 共 9 个 modal 正确复用 ✅。**例外**：RolesView 详情编辑用右侧 `detail-pane` 分栏而非弹层（属布局选择，可接受但与其他页面"编辑=modal"交互不一致，值得产品层面确认）；SkillsView/WikiView/TemplatesView 等有 detail 弹层需求但未见 `class="modal"` 使用，若各自手写 overlay 则应归一到全局类。

## 总结优先级

| 优先级 | 项目 |
|---|---|
| P0 浅色主题实坏 | RolesView L446 黑块、ProfileView 6 处 GitHub-dark fallback、WikiView/SkillsView 深色 pre 块 |
| P0 品牌冲突 | DashboardView 升级横幅整片 #3B82F6 |
| P1 对比度 | SignalsView 彩色标签、RolesView `--ok/--accent` 幽灵变量 |
| P2 统一 | fmtTs 抽 utils、分类/图表色 token 化、空态文案规范 |

未修改任何文件。

---

## 修复状态（2026-08-25 同日闭环）

本报告全部 P0/P1 及 P2 的 fmtTs 抽取已由双工程师并行修复完成：逻辑五项（死路由/key 路径/token 标题/扩展开关/死代码删除）+ 视觉 token 化（幽灵变量清零/品牌冲突/code-bg 双主题变量/fmtTs 抽 utils）。验证：grep 幽灵变量全库归零、npm run build 通过、后端回归 75 passed。详见 Backlog T-104。
