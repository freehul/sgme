<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listSessions, getSessionRaw, type SessionItem, type SessionRaw } from '../../api/knowledge'
import { ApiError } from '../../api/client'

const rows = ref<SessionItem[]>([])
const total = ref(0)
const page = ref(1)
const limit = 20
const keyFilter = ref('')
const agentFilter = ref('')
const statusFilter = ref('')
const loading = ref(false)
const error = ref('')

const detail = ref<SessionRaw | null>(null)
const detailLoading = ref(false)
const detailOpen = ref(false)

const STATUS_LABEL: Record<string, string> = { new: '待提炼', refined: '已提炼', archived: '已归档' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listSessions({
      page: page.value,
      limit,
      session_key: keyFilter.value || undefined,
      agent_id: agentFilter.value || undefined,
      status: statusFilter.value || undefined,
    })
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function apply() {
  page.value = 1
  load()
}

async function openDetail(fileId: string) {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  error.value = ''
  try {
    detail.value = await getSessionRaw(fileId)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailOpen.value = false
  detail.value = null
}

function fmtSize(size: number): string {
  if (!size) return '—'
  if (size < 1024) return `${size}B`
  return `${(size / 1024).toFixed(1)}KB`
}

onMounted(load)
</script>

<template>
  <div class="sessions">
    <div class="head">
      <h2>会话原文</h2>
      <div class="filters">
        <input v-model="keyFilter" placeholder="session_key 子串" @keyup.enter="apply" />
        <input v-model="agentFilter" placeholder="agent_id" @keyup.enter="apply" />
        <select v-model="statusFilter" @change="apply">
          <option value="">全部状态</option>
          <option v-for="(label, key) in STATUS_LABEL" :key="key" :value="key">{{ label }}</option>
        </select>
        <button @click="apply">检索</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length" class="empty">暂无会话。</p>

    <div v-else class="list">
      <div v-for="s in rows" :key="s.file_id" class="card clickable" @click="openDetail(s.file_id)">
        <div class="card-head">
          <span class="status" :class="s.status">{{ STATUS_LABEL[s.status] || s.status }}</span>
          <code class="inline">{{ s.file_id }}</code>
          <span class="key">{{ s.session_key }}</span>
        </div>
        <div class="meta">
          <span v-if="s.agent_id">agent: {{ s.agent_id }}</span>
          <span>{{ fmtSize(s.size) }}</span>
          <span>{{ new Date(s.started_at).toLocaleString() }}</span>
        </div>
      </div>

      <div class="pager">
        <button :disabled="page <= 1" @click="page--; load()">上一页</button>
        <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
        <button :disabled="page * limit >= total" @click="page++; load()">下一页</button>
      </div>
    </div>

    <div v-if="detailOpen" class="overlay" @click.self="closeDetail">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">{{ detail?.session_key || '会话原文' }}</span>
          <button class="close" @click="closeDetail">✕</button>
        </div>
        <div v-if="detailLoading" class="empty">加载中…</div>
        <pre v-else-if="detail?.content" class="raw">{{ detail.content }}</pre>
        <p v-else class="empty">（空会话或无内容）</p>
      </div>
    </div>
  </div>
</template>