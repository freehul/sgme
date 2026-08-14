<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getConfig, updateConfig } from '../../api/admin'
import { ApiError } from '../../api/client'

const config = ref<Record<string, unknown>>({})
const sections = ref<string[]>([])
const loading = ref(false)
const error = ref('')
const selected = ref('llm')
const text = ref('')
const saving = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getConfig()
    config.value = data.config
    sections.value = data.writable_sections
    if (sections.value.length && !sections.value.includes(selected.value)) {
      selected.value = sections.value[0]
    }
    syncText()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function syncText() {
  const section = config.value[selected.value]
  text.value = section ? JSON.stringify(section, null, 2) : '{}'
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    let parsed: unknown
    try {
      parsed = JSON.parse(text.value)
    } catch {
      throw new Error('JSON 解析失败，请检查格式')
    }
    await updateConfig(selected.value, parsed as Record<string, unknown>)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="config">
    <div class="head">
      <h2>系统配置</h2>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-else class="body">
      <div class="bar">
        <select v-model="selected" @change="syncText">
          <option v-for="s in sections" :key="s" :value="s">{{ s }}</option>
        </select>
        <button class="btn btn-primary" :disabled="saving" @click="save">保存</button>
      </div>
      <textarea v-model="text" class="json" spellcheck="false" />
    </div>
  </div>
</template>