<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listRegistry, updateDimension, type Dimension } from '../../api/admin'
import { ApiError } from '../../api/client'

const dims = ref<Dimension[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const saved = ref('')

// 可配置 TTL 的动态维度：time_velocity=dynamic 且有 ttl_days（排除 ideas 等
// 长期保存无 TTL 的维度——它们不出现在 TTL 配置页）
const dynamic = () => dims.value.filter((d) => d.time_velocity === 'dynamic' && d.ttl_days != null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listRegistry()
    dims.value = data.dimensions
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
    for (const d of dynamic()) {
      await updateDimension(d.id, { ttl_days: d.ttl_days })
    }
    saved.value = '已保存'
    setTimeout(() => (saved.value = ''), 2000)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="ttl-page">
    <div class="row-between">
      <h2>TTL 配置</h2>
      <button class="btn btn-sm" @click="load">恢复默认值</button>
    </div>
    <p class="desc">动态维度记忆随时间过期（起算 updated_at，更新即续期）；静态维度（identity / preferences / skills 等）永久有效，无需配置。</p>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-else class="ttl-grid">
      <div v-for="d in dynamic()" :key="d.id" class="card ttl-card">
        <div class="ttl-card-head">
          <span class="ttl-name">{{ d.display_name }}</span>
          <code class="inline">{{ d.id }}</code>
        </div>
        <div class="row-center">
          <input v-model.number="d.ttl_days" type="number" class="input flex-1" min="1" />
          <span class="muted">天</span>
        </div>
      </div>
    </div>

    <div class="row-end">
      <span v-if="saved" class="info">{{ saved }}</span>
      <button class="btn btn-primary" :disabled="loading || saving" @click="save">保存配置</button>
    </div>
  </div>
</template>

<style scoped>
.ttl-page { display: flex; flex-direction: column; gap: 16px; }
.row-between { display: flex; align-items: center; justify-content: space-between; }
.row-between h2 { margin: 0; font-size: var(--fs-lg); font-weight: 600; }
.row-center { display: flex; align-items: center; gap: 8px; }
.row-end { display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
.flex-1 { flex: 1; min-width: 0; }
.muted { font-size: 13px; color: var(--text-muted); white-space: nowrap; }
.desc { margin: 0; font-size: 13px; color: var(--text-muted); overflow-wrap: break-word; }
.ttl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.ttl-card { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.ttl-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.ttl-card-head .ttl-name { font-weight: 600; font-size: var(--fs-md); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ttl-card-head + .row-center { padding-top: 2px; }
</style>
