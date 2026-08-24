<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getConfig, updateConfig } from '../../api/admin'
import { ApiError } from '../../api/client'

const config = ref<Record<string, unknown>>({})
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const saved = ref('')

const EXT_MODULES = [
  { key: 'wiki', label: 'Wiki 知识库', desc: '启用后侧栏显示知识库导航项' },
  { key: 'skills', label: 'Skills Hub 技能仓库', desc: '启用后侧栏显示技能仓库导航项' },
  { key: 'persona', label: 'Persona 用户画像', desc: '启用后画像页可编辑用户画像' },
  { key: 'care', label: 'Care Engine 关怀引擎', desc: '启用后侧栏显示角色/关怀信号导航项' },
]

// 各模块当前开关状态（key → enabled）
const moduleState = ref<Record<string, boolean>>({})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getConfig()
    config.value = data.config
    const next: Record<string, boolean> = {}
    for (const m of EXT_MODULES) {
      // skills_hub 在配置里是 skills_hub，其余与 key 同名
      const cfgKey = m.key === 'skills' ? 'skills_hub' : m.key
      const cfg = data.config[cfgKey] as Record<string, unknown> | undefined
      next[m.key] = cfg?.enabled ?? true
    }
    moduleState.value = next
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function toggle(key: string, v: boolean) {
  try {
    await updateConfig(key === 'skills' ? 'skills_hub' : key, { enabled: v })
    moduleState.value[key] = v
    saved.value = '已保存'
    setTimeout(() => (saved.value = ''), 2000)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

onMounted(load)
</script>

<template>
  <div class="extensions">
    <div class="page">
      <h2 class="title">扩展模块</h2>

      <p v-if="error" class="error">{{ error }}</p>
      <p v-if="loading" class="empty">加载中…</p>

      <div v-else class="ext-list">
        <div v-for="m in EXT_MODULES" :key="m.key" class="ext-row">
          <div class="ext-info">
            <div class="ext-name">{{ m.label }}</div>
            <div class="ext-desc">{{ m.desc }}</div>
          </div>
          <button
            class="toggle-switch"
            :class="{ active: moduleState[m.key] }"
            @click="toggle(m.key, !moduleState[m.key])"
          />
        </div>
      </div>

      <div class="row-end">
        <span v-if="saved" class="info">{{ saved }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 20px; }
.title { margin: 0; font-size: var(--fs-lg); font-weight: 600; }
.row-end { display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
.ext-list { display: flex; flex-direction: column; gap: 12px; }
.ext-row { display: flex; align-items: center; justify-content: space-between; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.ext-info { display: flex; flex-direction: column; gap: 4px; }
.ext-name { color: var(--text); font-weight: 500; }
.ext-desc { font-size: 12px; color: var(--text-muted); }
.toggle-switch { width: 44px; height: 24px; border-radius: 9999px; background: var(--surface-muted); border: 1px solid var(--border); position: relative; cursor: pointer; transition: background-color 0.2s ease; flex-shrink: 0; }
.toggle-switch.active { background: var(--brand); border-color: var(--brand); }
.toggle-switch::after { content: ''; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: transform 0.2s ease; }
.toggle-switch.active::after { transform: translateX(20px); }
</style>
