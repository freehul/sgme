<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { search, type SearchResult } from '../../api/memory'
import { ApiError } from '../../api/client'

const route = useRoute()
const router = useRouter()
const query = ref('')
const scopes = ref<string[]>(['memory', 'wiki', 'wiki_pages'])
const match = ref('any')
const limit = ref(10)
const results = ref<SearchResult[]>([])
const routes = ref<string[]>([])
const loading = ref(false)
const error = ref('')
const searched = ref(false)

const SCOPE_LABEL: Record<string, string> = { memory: '记忆', wiki: '场景', wiki_pages: '知识库' }

async function doSearch() {
  if (!query.value.trim()) return
  loading.value = true
  error.value = ''
  searched.value = true
  try {
    const data = await search({
      query: query.value,
      scopes: scopes.value.length ? scopes.value : ['memory'],
      match: match.value,
      limit: limit.value,
    })
    results.value = data.results
    routes.value = data.meta?.routes || []
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    results.value = []
  } finally {
    loading.value = false
  }
}

function openMemory(r: SearchResult) {
  if (r.source === 'wiki_pages' && r.page_id) {
    router.push({ path: '/wiki', query: { page_id: r.page_id } })
    return
  }
  if (r.memory_id) router.push(`/memories/${r.memory_id}`)
  if (r.scene_id) router.push(`/scenes/${r.scene_id}`)
}

// score 可能为 undefined，需判空后再格式化，避免渲染崩溃
function fmtScore(score: number | undefined): string {
  if (score == null) return '—'
  return typeof score.toFixed === 'function' ? score.toFixed(3) : String(score)
}

// 支持从顶栏全局搜索 ?q= 进入时自动检索，并响应后续查询参数变化
function syncFromQuery() {
  const q = String(route.query.q || '').trim()
  if (q && q !== query.value.trim()) {
    query.value = q
    doSearch()
  }
}

watch(() => route.query, syncFromQuery, { immediate: true })
</script>

<template>
  <div class="search">
    <div class="head">
      <h2>统一检索</h2>
      <div class="search-bar">
        <input v-model="query" placeholder="输入检索词…" @keyup.enter="doSearch" />
        <div class="opts">
          <label v-for="(label, key) in SCOPE_LABEL" :key="key" class="chk">
            <input v-model="scopes" type="checkbox" :value="key" /> {{ label }}
          </label>
          <select v-model="match">
            <option value="any">任一维度</option>
            <option value="all">全部维度</option>
          </select>
          <select v-model="limit">
            <option :value="5">5 条</option>
            <option :value="10">10 条</option>
            <option :value="20">20 条</option>
          </select>
        </div>
        <button class="btn btn-primary" :disabled="loading" @click="doSearch">检索</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">检索中…</p>
    <p v-else-if="searched && !results.length" class="empty">无结果。</p>

    <div v-if="routes.length" class="routes">路由: <span v-for="r in routes" :key="r" class="route">{{ r }}</span></div>

    <div v-if="results.length" class="list">
      <div v-for="r in results" :key="`${r.source}-${r.memory_id || r.scene_id || r.page_id}`" class="card clickable" @click="openMemory(r)">
        <div class="card-head">
          <span class="rank">#{{ r.rank }}</span>
          <span class="scope">{{ SCOPE_LABEL[r.source] || r.source }}</span>
          <span class="score">score {{ fmtScore(r.score) }}</span>
          <span v-if="r.dimensions.length" class="tag-group">
            <span v-for="d in r.dimensions" :key="d" class="dim-tag">{{ d }}</span>
          </span>
          <span v-if="r.source === 'wiki_pages' && r.category" class="dim-tag">{{ r.category }}</span>
          <span v-for="t in r.tags || []" :key="t" class="dim-tag">{{ t }}</span>
        </div>
        <p v-if="r.source === 'wiki_pages' && r.title" class="title">{{ r.title }}</p>
        <p class="content">{{ r.content }}</p>
        <div class="meta">
          <span v-if="r.updated_at">{{ new Date(r.updated_at).toLocaleString() }}</span>
          <span v-if="r.trace?.length" class="trace">trace {{ r.trace.length }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.title { font-weight: 600; font-size: 14px; color: var(--text); margin: 0 0 6px; }
</style>