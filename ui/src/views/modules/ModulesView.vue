<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { getConfig, updateConfig } from '../../api/admin'
import { ApiError } from '../../api/client'
import ConfigSectionEditor from '../../components/ConfigSectionEditor.vue'

// 各模块配置段（可写段白名单见 config.py CONFIG_SECTIONS）
const SECTIONS = [
  { key: 'refine', label: '提炼调度', desc: 'refine_on_append / batch_scan' },
  { key: 'search', label: '检索与向量', desc: 'vector / rrf' },
  { key: 'dream', label: 'Dream 日报', desc: 'enabled / schedule / ttl' },
  { key: 'wiki', label: 'Wiki', desc: '知识库' },
  { key: 'skills_hub', label: '技能中心', desc: 'remote source' },
  { key: 'backup', label: '备份', desc: 'schedule / dir' },
  { key: 'logging', label: '日志', desc: 'level' },
  { key: 'l1', label: 'L1 抽取', desc: '分块' },
  { key: 'l2', label: 'L2 场景', desc: '生成' },
]

const active = ref('refine')
const cfg = ref<Record<string, unknown>>({})
const loading = ref(false)
const saving = ref(false)
const dirty = ref(false)
const error = ref('')
const saved = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getConfig()
    cfg.value = (data.config[active.value] as Record<string, unknown>) || {}
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ''
  saved.value = ''
  try {
    await updateConfig(active.value, cfg.value)
    dirty.value = false
    saved.value = '已保存'
    setTimeout(() => (saved.value = ''), 2000)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

watch(active, () => {
  dirty.value = false
  load()
})

onMounted(load)
</script>

<template>
  <div class="modules">
    <div class="head">
      <h2>模块配置</h2>
      <span class="sub">按模块结构化编辑运行配置（写入即热生效并落盘 sgme.yaml）</span>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div class="module-layout">
      <aside class="mod-nav">
        <button
          v-for="s in SECTIONS"
          :key="s.key"
          class="mod-item"
          :class="{ active: active === s.key }"
          @click="active = s.key"
        >
          <span class="mod-name">{{ s.label }}</span>
          <span class="mod-desc">{{ s.desc }}</span>
        </button>
      </aside>

      <div class="mod-body panel">
        <div class="mod-toolbar">
          <span class="mod-title">{{ SECTIONS.find((s) => s.key === active)?.label }}</span>
          <div class="filters">
            <span v-if="saved" class="info">{{ saved }}</span>
            <button class="btn btn-sm" :disabled="loading" @click="load">重载</button>
            <button class="btn btn-primary btn-sm" :disabled="!dirty || saving" @click="save">
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
        <p v-if="loading" class="empty">加载中…</p>
        <ConfigSectionEditor v-else :config="cfg" @dirty="dirty = $event" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.module-layout { display: flex; gap: 16px; align-items: flex-start; }
.mod-nav { width: 200px; flex-shrink: 0; display: flex; flex-direction: column; gap: 4px; }
.mod-nav .mod-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: left;
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  cursor: pointer;
  transition: background-color 0.12s ease, border-color 0.12s ease;
}
.mod-item:hover { background: var(--surface); }
.mod-item.active { background: var(--brand-soft); border-color: var(--brand); }
.mod-name { font-size: 14px; font-weight: 600; color: var(--text); }
.mod-item.active .mod-name { color: var(--brand-text); }
.mod-desc { font-size: 11px; color: var(--text-faint); }
.mod-body { flex: 1; min-width: 0; }
.mod-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.mod-title { font-weight: 600; font-size: 15px; }
@media (max-width: 720px) {
  .module-layout { flex-direction: column; }
  .mod-nav { width: 100%; flex-direction: row; flex-wrap: wrap; }
}
</style>