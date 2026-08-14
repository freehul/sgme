<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getConfig, updateConfig } from '../../api/admin'
import { getHealth } from '../../api/dashboard'
import { ApiError } from '../../api/client'

const logging = ref<Record<string, unknown>>({})
const version = ref('')
const saving = ref(false)
const loading = ref(true)
const error = ref('')
const saved = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [cfg, health] = await Promise.all([getConfig(), getHealth()])
    logging.value = (cfg.config.logging as Record<string, unknown>) || {}
    version.value = health.version
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
    await updateConfig('logging', logging.value)
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
  <div class="general">
    <div class="page">
      <h2 class="title">通用设置</h2>

      <p v-if="error" class="error">{{ error }}</p>
      <p v-if="loading" class="empty">加载中…</p>

      <div v-else class="form-grid">
        <div class="field">
          <label class="flabel">日志级别</label>
          <select v-model="logging.level" class="input">
            <option>DEBUG</option>
            <option>INFO</option>
            <option>WARN</option>
            <option>ERROR</option>
          </select>
        </div>
        <div class="field">
          <label class="flabel">语言</label>
          <select class="input">
            <option>中文</option>
            <option>English</option>
          </select>
        </div>
        <div class="field">
          <label class="flabel">系统版本</label>
          <input class="input" :value="version" readonly />
        </div>
        <div class="field">
          <label class="flabel">数据目录</label>
          <input class="input" value="/data/sgme/storage" readonly />
        </div>
      </div>

      <div class="row-end">
        <span v-if="saved" class="info">{{ saved }}</span>
        <button class="btn btn-primary" :disabled="loading || saving" @click="save">保存设置</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 20px; }
.title { margin: 0; font-size: var(--fs-lg); font-weight: 600; }
.row-end { display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.flabel { font-size: 13px; font-weight: 500; color: var(--text-muted); }
</style>
