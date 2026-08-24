<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getMemory, rejectMemory, unrejectMemory, type MemoryDetail, type MemorySource } from '../../api/memory'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id))
const detail = ref<MemoryDetail | null>(null)
const loading = ref(true)
const error = ref('')
const reason = ref('')
const busy = ref(false)

const STATUS_LABEL: Record<string, string> = {
  active: '有效',
  rejected: '已拒绝',
  expired: '已过期',
  archived: '已归档',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    detail.value = await getMemory(id.value)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function reject() {
  busy.value = true
  error.value = ''
  try {
    await rejectMemory(id.value, reason.value.trim() || undefined)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function unreject() {
  busy.value = true
  error.value = ''
  try {
    await unrejectMemory(id.value)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

watch(id, () => load(), { immediate: true })
</script>

<template>
  <div class="detail">
    <button class="back" @click="router.push('/memories')">← 返回列表</button>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <template v-if="detail">
      <div class="head">
        <h2>记忆详情</h2>
        <span class="status" :class="detail.memory.status">{{ STATUS_LABEL[detail.memory.status] || detail.memory.status }}</span>
      </div>

      <div class="panel">
        <pre class="content">{{ detail.memory.content }}</pre>
        <div class="meta">
          <span>ID: {{ detail.memory.memory_id }}</span>
          <span>类型: {{ detail.memory.memory_type }}</span>
          <span>优先级: P{{ detail.memory.priority }}</span>
          <span>发生: {{ fmtTs(detail.memory.occurred_at) }}</span>
          <span>更新: {{ fmtTs(detail.memory.updated_at) }}</span>
        </div>
        <div class="meta">
          <span v-for="d in detail.memory.dimensions" :key="d" class="dim-tag">{{ d }}</span>
          <span v-if="detail.memory.custom_flag" class="flag">标记: {{ detail.memory.custom_flag }}</span>
        </div>
        <p v-if="detail.memory.notes" class="note-text">备注: {{ detail.memory.notes }}</p>
      </div>

      <div v-if="detail.sources.length" class="panel">
        <h3>溯源引用</h3>
        <ul class="src-list">
          <li v-for="(s, i) in detail.sources" :key="i" class="src">
            <span class="src-type">{{ s.source_type }}</span>
            <code class="inline">{{ s.source_ref }}</code>
          </li>
        </ul>
      </div>

      <div v-if="detail.archive_chain.length" class="panel">
        <h3>归档链（supersession）</h3>
        <ul class="chain">
          <li v-for="(a, i) in detail.archive_chain" :key="i" class="chain-item">
            <code class="inline">{{ (a as Record<string, unknown>).memory_id }}</code>
            <span class="chain-ts">{{ fmtTs(String((a as Record<string, unknown>).archived_at || '')) }}</span>
          </li>
        </ul>
      </div>

      <div class="panel">
        <h3>操作</h3>
        <template v-if="detail.memory.status !== 'rejected'">
          <textarea v-model="reason" placeholder="拒绝原因（可选）" rows="2" />
          <button class="danger" :disabled="busy" @click="reject">标记为拒绝</button>
        </template>
        <button v-else :disabled="busy" @click="unreject">恢复为有效</button>
      </div>
    </template>
  </div>
</template>