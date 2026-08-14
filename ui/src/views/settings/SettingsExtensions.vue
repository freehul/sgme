<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getConfig, updateConfig } from '../../api/admin'
import { ApiError } from '../../api/client'

const wiki = ref({ enabled: true })
const skills = ref<Record<string, unknown>>({})
const config = ref<Record<string, unknown>>({})
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const saved = ref('')

const EXT_MODULES = [
  { key: 'wiki', label: 'Wiki 知识库', desc: '启用后侧栏显示知识库导航项' },
  { key: 'skills', label: 'Skills Hub 技能仓库', desc: '启用后侧栏显示技能仓库导航项' },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getConfig()
    config.value = data.config
    const wikiCfg = data.config.wiki as Record<string, unknown> | undefined
    wiki.value = { enabled: wikiCfg?.enabled ?? true }
    skills.value = (data.config.skills_hub as Record<string, unknown>) || {}
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function toggle(key: string, v: boolean) {
  try {
    if (key === 'wiki') {
      await updateConfig('wiki', { enabled: v })
      wiki.value.enabled = v
    } else {
      await updateConfig('skills_hub', { enabled: v })
    }
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
        <div class="ext-row">
          <div class="ext-info">
            <div class="ext-name">Wiki 知识库</div>
            <div class="ext-desc">启用后侧栏显示知识库导航项</div>
          </div>
          <button class="toggle-switch" :class="{ active: wiki.enabled }" @click="toggle('wiki', !wiki.enabled)" />
        </div>
        <div class="ext-row">
          <div class="ext-info">
            <div class="ext-name">Skills Hub 技能仓库</div>
            <div class="ext-desc">启用后侧栏显示技能仓库导航项</div>
          </div>
          <button class="toggle-switch" :class="{ active: !!skills.enabled }" @click="toggle('skills', !skills.enabled)" />
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
