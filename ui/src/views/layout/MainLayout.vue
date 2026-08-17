<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getAdminKey, setAdminKey, clearAdminKey, getAgentKey, setAgentKey, clearAgentKey, autoFillKeys } from '../../api/client'
import { useTheme } from '../../theme'

const router = useRouter()
const { mode, theme, set } = useTheme()

// 三态循环：light → dark → system → light（暴露「跟随系统」模式）
function cycleTheme() {
  const order: Array<'light' | 'dark' | 'system'> = ['light', 'dark', 'system']
  const idx = order.indexOf(mode.value)
  set(order[(idx + 1) % order.length])
}
const themeIcon = computed(() => (theme.value === 'dark' ? '☀' : mode.value === 'system' ? '🖥' : '☾'))

const keyDraft = ref(getAdminKey())
const keySaved = ref(!!getAdminKey())
const keyVisible = ref(false)
const agentDraft = ref(getAgentKey())
const agentSaved = ref(!!getAgentKey())
const searchQ = ref('')
// 密钥输入区可折叠（默认收起，减少侧栏占用）
const keyCollapsed = ref(true)

// 首次打开自动填充 admin/agent key（后端仅本机来源可用）
onMounted(async () => {
  const { filledAdmin, filledAgent } = await autoFillKeys()
  if (filledAdmin || filledAgent) {
    keyDraft.value = getAdminKey()
    agentDraft.value = getAgentKey()
    keySaved.value = !!getAdminKey()
    agentSaved.value = !!getAgentKey()
  }
})

function saveKey() {
  setAdminKey(keyDraft.value.trim())
  keySaved.value = !!keyDraft.value.trim()
}

function clearKey() {
  clearAdminKey()
  keyDraft.value = ''
  keySaved.value = false
}

function saveAgent() {
  setAgentKey(agentDraft.value.trim())
  agentSaved.value = !!agentDraft.value.trim()
}

function clearAgent() {
  clearAgentKey()
  agentDraft.value = ''
  agentSaved.value = false
}

function goSearch() {
  const q = searchQ.value.trim()
  if (!q) return
  router.push({ path: '/search', query: q ? { q } : {} })
}
</script>

<template>
  <div class="layout">
    <aside class="side">
      <div class="side-head">
        <div class="brand">
          <span class="brand-mark">拾</span>
          <div>
            <div>SGME</div>
            <div class="brand-sub">拾光记忆引擎</div>
          </div>
        </div>
        <button
          class="btn btn-ico"
          :title="`主题：${mode}（点击切换）`"
          :aria-label="`主题：${mode}（点击切换）`"
          @click="cycleTheme"
        >
          {{ themeIcon }}
        </button>
      </div>

      <nav class="nav">
        <div class="group">
          <span class="group-label">总览</span>
          <RouterLink to="/dashboard"><span class="nav-ico">📈</span><span class="lbl">总览</span></RouterLink>
        </div>

        <div class="group">
          <span class="group-label">记忆闭环</span>
          <RouterLink to="/search"><span class="nav-ico">🔎</span><span class="lbl">统一检索</span></RouterLink>
          <RouterLink to="/profile"><span class="nav-ico">🧑</span><span class="lbl">用户画像</span></RouterLink>
          <RouterLink to="/memories"><span class="nav-ico">📖</span><span class="lbl">记忆浏览</span></RouterLink>
          <RouterLink to="/scenes"><span class="nav-ico">🗂</span><span class="lbl">场景管理</span></RouterLink>
          <RouterLink to="/sessions"><span class="nav-ico">📜</span><span class="lbl">会话原文</span></RouterLink>
        </div>

        <div class="group">
          <span class="group-label">创意与需求</span>
          <RouterLink to="/ideas"><span class="nav-ico">💡</span><span class="lbl">创意池</span></RouterLink>
          <RouterLink to="/projects"><span class="nav-ico">📁</span><span class="lbl">项目池</span></RouterLink>
          <RouterLink to="/demands"><span class="nav-ico">📋</span><span class="lbl">待办</span></RouterLink>
        </div>

        <div class="group">
          <span class="group-label">系统管理</span>
          <RouterLink to="/roles"><span class="nav-ico">🎭</span><span class="lbl">角色管理</span></RouterLink>
          <RouterLink to="/signals"><span class="nav-ico">💗</span><span class="lbl">关怀信号</span></RouterLink>
          <RouterLink to="/wiki"><span class="nav-ico">📚</span><span class="lbl">Wiki 知识库</span></RouterLink>
          <RouterLink to="/skills"><span class="nav-ico">🧰</span><span class="lbl">技能仓库</span></RouterLink>
          <RouterLink to="/settings"><span class="nav-ico">⚙</span><span class="lbl">设置</span></RouterLink>
        </div>
      </nav>

      <div class="key-box">
        <button class="key-toggle" @click="keyCollapsed = !keyCollapsed">
          <span class="key-toggle-lbl">{{ keyCollapsed ? '▸' : '▾' }} 访问密钥</span>
          <span v-if="keySaved || agentSaved" class="key-dot" title="已保存密钥" />
        </button>
        <template v-if="!keyCollapsed">
          <div class="key-row">
            <input
              v-model="keyDraft"
              :type="keyVisible ? 'text' : 'password'"
              placeholder="Admin Key"
              @keyup.enter="saveKey"
            />
            <button class="btn btn-ico" :title="keyVisible ? '隐藏' : '显示'" @click="keyVisible = !keyVisible">
              {{ keyVisible ? '🙈' : '🙉' }}
            </button>
          </div>
          <div class="key-row">
            <button class="btn btn-primary btn-sm" style="flex: 1" @click="saveKey">{{ keySaved ? '更新' : '保存' }} Admin</button>
            <button v-if="keySaved" class="btn btn-sm" @click="clearKey">✕</button>
          </div>
          <div class="key-row">
            <input
              v-model="agentDraft"
              type="password"
              placeholder="Agent Key（Wiki/检索）"
              @keyup.enter="saveAgent"
            />
          </div>
          <div class="key-row">
            <button class="btn btn-sm" style="flex: 1" @click="saveAgent">{{ agentSaved ? '更新' : '保存' }} Agent</button>
            <button v-if="agentSaved" class="btn btn-sm" @click="clearAgent">✕</button>
          </div>
          <span v-if="keySaved || agentSaved" class="key-hint">写入型操作需 Admin；Wiki/检索需 Agent</span>
        </template>
      </div>
    </aside>

    <main class="content">
      <div class="content-inner">
        <div class="topbar">
          <div class="global-search">
            <span class="gs-ico">🔎</span>
            <input
              v-model="searchQ"
              type="search"
              placeholder="全局检索记忆 / 场景…"
              @keyup.enter="goSearch"
            />
            <button class="btn btn-primary btn-sm" @click="goSearch">检索</button>
          </div>
        </div>
        <router-view />
      </div>
    </main>
  </div>
</template>