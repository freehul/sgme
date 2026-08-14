<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listTemplates, updateTemplate, deleteTemplate, type TemplateItem } from '../../api/admin'
import { ApiError } from '../../api/client'

const rows = ref<TemplateItem[]>([])
const loading = ref(false)
const error = ref('')

const editing = ref<TemplateItem | null>(null)
const editContent = ref('')
const saving = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listTemplates()
    rows.value = data.items
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function open(item: TemplateItem) {
  editing.value = item
  editContent.value = item.content
}

function close() {
  editing.value = null
}

async function save() {
  if (!editing.value) return
  saving.value = true
  error.value = ''
  try {
    await updateTemplate(editing.value.name, { name: editing.value.name, content: editContent.value })
    await load()
    close()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function remove(item: TemplateItem) {
  if (!confirm(`删除模板 ${item.name}？`)) return
  error.value = ''
  try {
    await deleteTemplate(item.name)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

onMounted(load)
</script>

<template>
  <div class="templates">
    <div class="head">
      <h2>模板管理</h2>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length" class="empty">暂无模板。</p>

    <div v-else class="list">
      <div v-for="t in rows" :key="t.name" class="card">
        <div class="card-head">
          <span class="card-title">{{ t.display_name || t.name }}</span>
          <code class="inline">{{ t.name }}</code>
          <span v-if="t.builtin" class="builtin">内置</span>
          <span v-if="!t.valid" class="invalid">解析失败</span>
        </div>
        <div class="meta">
          <span v-for="mt in t.memory_types" :key="mt" class="dim-tag">{{ mt }}</span>
          <span v-if="t.token_budget != null">预算 {{ t.token_budget }}</span>
          <span>{{ t.sections.length }} sections</span>
        </div>
        <p v-if="t.error" class="err">{{ t.error }}</p>
        <div class="actions">
          <button @click="open(t)">编辑</button>
          <button v-if="!t.builtin" class="danger" @click="remove(t)">删除</button>
        </div>
      </div>
    </div>

    <div v-if="editing" class="overlay" @click.self="close">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">{{ editing.display_name || editing.name }}</span>
          <button class="close" @click="close">✕</button>
        </div>
        <textarea v-model="editContent" class="yaml" spellcheck="false" />
        <div class="drawer-foot">
          <button class="primary" :disabled="saving" @click="save">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>