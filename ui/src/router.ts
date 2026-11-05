import { createRouter, createWebHistory } from 'vue-router'

// 按模块分组的信息架构（见 SGME-WebUI设计-v0.1 §2 与本次重组）
// 分区：总览 / 记忆闭环 / 创意与需求 / 系统管理
const routes = [
  {
    path: '/',
    component: () => import('./views/layout/MainLayout.vue'),
    children: [
      // 总览
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'dashboard', component: () => import('./views/dashboard/DashboardView.vue'), meta: { title: '总览' } },

      // 记忆闭环
      { path: 'memories', name: 'memories', component: () => import('./views/memory/MemoryList.vue'), meta: { title: '记忆浏览' } },
      { path: 'memories/:id', name: 'memory-detail', component: () => import('./views/memory/MemoryDetail.vue'), meta: { title: '记忆详情' } },
      { path: 'review', name: 'review', component: () => import('./views/review/ReviewQueue.vue'), meta: { title: '待审候选' } },
      { path: 'near-dup', name: 'near-dup', component: () => import('./views/review/NearDupQueue.vue'), meta: { title: '近似重复' } },
      { path: 'cards', name: 'cards', component: () => import('./views/cards/CardView.vue'), meta: { title: '关于你' } },
      { path: 'persona', name: 'persona', component: () => import('./views/persona/PersonaView.vue'), meta: { title: 'MBTI 与性格' } },
      { path: 'scenes', name: 'scenes', component: () => import('./views/scenes/SceneList.vue'), meta: { title: '场景管理' } },
      { path: 'scenes/:id', name: 'scene-detail', component: () => import('./views/scenes/SceneList.vue'), meta: { title: '场景详情' } },
      { path: 'graph', name: 'graph', component: () => import('./views/graph/GraphView.vue'), meta: { title: '知识图谱' } },
      { path: 'sessions', name: 'sessions', component: () => import('./views/sessions/SessionView.vue'), meta: { title: '会话原文' } },
      { path: 'search', name: 'search', component: () => import('./views/search/SearchView.vue'), meta: { title: '统一检索' } },
      { path: 'profile', name: 'profile', component: () => import('./views/profile/ProfileView.vue'), meta: { title: '用户画像' } },

      // 创意与需求（次要分组）
      { path: 'ideas', name: 'ideas', component: () => import('./views/ideas/IdeaList.vue'), meta: { title: '创意池' } },
      { path: 'ideas/:id', name: 'idea-detail', component: () => import('./views/ideas/IdeaDetail.vue'), meta: { title: '创意详情' } },
      { path: 'demands', name: 'demands', component: () => import('./views/demands/DemandList.vue'), meta: { title: '待办' } },
      { path: 'projects', name: 'projects', component: () => import('./views/projects/ProjectList.vue'), meta: { title: '项目池' } },

      // 设置（单入口 + 标签页，对齐参考设计）
      { path: 'settings', name: 'settings', component: () => import('./views/settings/SettingsView.vue'), meta: { title: '设置' } },

      // Wiki 知识库 / 技能仓库
      { path: 'wiki', name: 'wiki', component: () => import('./views/wiki/WikiView.vue'), meta: { title: 'Wiki 知识库' } },
      { path: 'skills', name: 'skills', component: () => import('./views/skills/SkillsView.vue'), meta: { title: '技能仓库' } },

      // 运维看板（2.0 可观测性：健康总览 / TTL 处置 / LLM 成本）
      { path: 'ops/health', name: 'ops-health', component: () => import('./views/ops/HealthView.vue'), meta: { title: '健康总览' } },
      { path: 'ops/ttl', name: 'ops-ttl', component: () => import('./views/ops/TtlDispositionView.vue'), meta: { title: 'TTL 处置' } },
      { path: 'ops/cost', name: 'ops-cost', component: () => import('./views/ops/CostView.vue'), meta: { title: 'LLM 成本' } },

      // Care Engine 角色（T-39：角色管理，挂记忆闭环组）
      { path: 'roles', name: 'roles', component: () => import('./views/care/RolesView.vue'), meta: { title: '角色管理' } },
      { path: 'signals', name: 'signals', component: () => import('./views/care/SignalsView.vue'), meta: { title: '关怀信号' } },

      // 旧配置入口兼容 → 设置页
      { path: 'templates', redirect: '/settings' },
      { path: 'registry', redirect: '/settings' },
      { path: 'agents', redirect: '/settings' },
      { path: 'prompts', redirect: '/settings' },
      { path: 'providers', redirect: '/settings' },
      { path: 'modules', redirect: '/settings' },
      { path: 'config', redirect: '/settings' },
      { path: 'backup', redirect: '/settings' },

      // 404 兜底（必须放最后）
      { path: ':pathMatch(.*)*', name: 'not-found', component: () => import('./views/PlaceholderPage.vue'), meta: { title: '未找到' } },
    ],
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})