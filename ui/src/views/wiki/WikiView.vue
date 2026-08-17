<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { createWikiPage, exportWikiPage, getWikiPage, listWikiPages, type WikiPage } from '../../api/wiki'
import { ApiError } from '../../api/client'
import { renderMarkdown, collapseBlankLines } from '../../utils/markdown'

const route = useRoute()
const pages = ref<WikiPage[]>([])
const total = ref(0)
const limit = 20
const offset = ref(0)
const loading = ref(false)
const error = ref('')

const detail = ref<WikiPage | null>(null)
const detailLoading = ref(false)
const detailOpen = ref(false)

const showNew = ref(false)
const newTitle = ref('')
const newContent = ref('')
const newCategory = ref('')
const newTags = ref('')
const newDescription = ref('')
const busy = ref(false)

const CATEGORY_COLOR: Record<string, string> = {
  skill: 'red',
  design: 'blue',
  research: 'purple',
  ops: 'green',
  // 旧分类（历史页面可能仍用，保留不删）
  identity: 'blue',
  projects: 'green',
  goals: 'green',
  tasks: 'yellow',
  ideas: 'purple',
  skills: 'red',
}

function catClass(cat?: string | null): string {
  const key = (cat || '').split('/')[0] || ''
  const c = CATEGORY_COLOR[key] || 'neutral'
  return `cat-${c}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listWikiPages({ limit, offset: offset.value })
    pages.value = data.pages
    total.value = data.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function openDetail(id: string) {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  error.value = ''
  try {
    detail.value = await getWikiPage(id)
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

async function doExport(p: WikiPage) {
  try {
    const html = await exportWikiPage(p.page_id)
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `wiki_${p.page_id}.html`
    a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function create() {
  if (!newTitle.value.trim() || !newContent.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    const tags = newTags.value.trim() ? newTags.value.split(',').map(t => t.trim()).filter(Boolean) : []
    await createWikiPage({
      title: newTitle.value.trim(),
      content: newContent.value,
      category: newCategory.value.trim() || null,
      description: newDescription.value.trim() || null,
      tags,
    })
    showNew.value = false
    newTitle.value = ''
    newContent.value = ''
    newCategory.value = ''
    newTags.value = ''
    newDescription.value = ''
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function relLabel(rel: string): string {
  const m: Record<string, string> = { references: '引用', related: '相关', similar: '相似', implements: '实现', supersedes: '取代' }
  return m[rel] || rel
}

onMounted(load)

// 支持从统一检索结果 ?page_id= 直达详情，并响应后续 page_id 变化（T-34 闭环）
watch(() => route.query.page_id, (pid) => {
  if (pid) openDetail(String(pid))
}, { immediate: true })
</script>

<template>
  <div class="wiki">
    <div class="head">
      <h2>Wiki 知识库</h2>
      <div class="filters">
        <span class="sub">共 {{ total }} 页</span>
        <button class="btn btn-primary btn-sm" @click="showNew = true">＋ 新建</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!pages.length" class="empty">暂无知识页。通过 /v1/wiki/ingest 提炼入库后会出现在这里。</p>

    <div v-else class="list">
      <div v-for="p in pages" :key="p.page_id" class="card clickable" @click="openDetail(p.page_id)">
        <div class="card-head">
          <span class="card-title">{{ p.title }}</span>
          <span v-if="p.category" class="tag" :class="catClass(p.category)">{{ p.category }}</span>
          <span v-for="t in p.tags || []" :key="t" class="dim-tag" :class="{ 'skill-tag': t === 'skill' }">{{ t }}</span>
        </div>
        <p class="content line-clamp-2">{{ collapseBlankLines(p.description || p.content) }}</p>
        <div class="meta">
          <span>更新 {{ new Date(p.updated_at).toLocaleString() }}</span>
          <button class="btn btn-sm" @click.stop="doExport(p)">导出 HTML</button>
        </div>
      </div>

      <div class="pager">
        <button :disabled="offset <= 0" @click="offset -= limit; load()">上一页</button>
        <span>第 {{ offset / limit + 1 }} 页 / 共 {{ total }} 条</span>
        <button :disabled="offset + limit >= total" @click="offset += limit; load()">下一页</button>
      </div>
    </div>

    <div v-if="showNew" class="overlay" @click.self="showNew = false">
      <div class="modal-box">
        <h3>新建 Wiki 页面</h3>
        <label>标题
          <input v-model="newTitle" placeholder="页面标题（必填）" />
        </label>
        <label>分类
          <input v-model="newCategory" placeholder="category（可选，如 ops / design / skill/sgme）" />
        </label>
        <label>摘要
          <input v-model="newDescription" placeholder="description（可选，列表展示用摘要）" />
        </label>
        <label>标签
          <input v-model="newTags" placeholder="逗号分隔（可选）" />
        </label>
        <label>内容
          <textarea v-model="newContent" rows="10" placeholder="Markdown 内容（必填）" />
        </label>
        <div class="modal-actions">
          <button class="btn" @click="showNew = false">取消</button>
          <button class="btn btn-primary" :disabled="busy || !newTitle.trim() || !newContent.trim()" @click="create">
            {{ busy ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="detailOpen" class="overlay" @click.self="closeDetail">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">{{ detail?.title || 'Wiki 页面' }}</span>
          <button class="close" @click="closeDetail">✕</button>
        </div>
        <div class="drawer-body">
          <div v-if="detailLoading" class="empty">加载中…</div>
          <template v-else-if="detail">
            <div class="drawer-meta">
              <span v-if="detail.category" class="tag" :class="catClass(detail.category)">{{ detail.category }}</span>
              <span v-for="t in detail.tags || []" :key="t" class="dim-tag" :class="{ 'skill-tag': t === 'skill' }">{{ t }}</span>
              <span class="d-ts">更新 {{ new Date(detail.updated_at).toLocaleString() }}</span>
            </div>
            <p v-if="detail.description" class="desc">{{ detail.description }}</p>
            <div class="markdown-content" v-html="renderMarkdown(detail.content)" />
            <div v-if="detail.links?.length" class="wiki-links">
              <h4>关联页面（{{ detail.links.length }}）</h4>
              <div class="link-list">
                <a v-for="lk in detail.links" :key="lk.page_id" class="link-item" @click="openDetail(lk.page_id)">
                  <span class="rel">{{ relLabel(lk.rel_type) }}</span>
                  <span class="link-title">{{ lk.title }}</span>
                </a>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.drawer-body { padding: var(--space-5); }
.drawer-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.d-ts { font-size: 12px; color: var(--text-muted); margin-left: auto; }
.doc-content { margin: 0; white-space: pre-wrap; font-family: inherit; font-size: 14px; line-height: 1.7; color: var(--text); }
.markdown-content { font-size: 14px; line-height: 1.7; color: var(--text); word-break: break-word; }
.markdown-content :deep(h1), .markdown-content :deep(h2), .markdown-content :deep(h3), .markdown-content :deep(h4) { margin: 16px 0 8px; font-weight: 600; }
.markdown-content :deep(h1) { font-size: 22px; padding-bottom: 6px; border-bottom: 1px solid var(--divider); }
.markdown-content :deep(h2) { font-size: 18px; }
.markdown-content :deep(h3) { font-size: 16px; }
.markdown-content :deep(p) { margin: 8px 0; }
.markdown-content :deep(code) { background: var(--surface-muted); padding: 2px 6px; border-radius: 4px; font-family: var(--font-mono); font-size: 13px; color: var(--danger); }
.markdown-content :deep(pre) { background: #1f2937; color: #f9fafb; padding: 14px; border-radius: 8px; overflow-x: auto; margin: 12px 0; }
.markdown-content :deep(pre code) { background: transparent; color: inherit; padding: 0; }
.markdown-content :deep(ul), .markdown-content :deep(ol) { padding-left: 22px; margin: 8px 0; }
.markdown-content :deep(li) { margin: 4px 0; }
.markdown-content :deep(blockquote) { border-left: 4px solid var(--brand); padding: 8px 14px; color: var(--text-muted); background: var(--brand-soft); border-radius: 0 8px 8px 0; margin: 10px 0; }
.markdown-content :deep(a) { color: var(--brand-text); }
/* 类别颜色胶囊（对齐参考设计） */
.dim-tag.skill-tag { background: rgba(239,68,68,.15); color: #EF4444; }
.desc { color: var(--text-muted); font-size: 13px; margin: 0 0 10px; line-height: 1.6; }
.tag.cat-blue { background: rgba(59,130,246,.1); color: #3B82F6; }
.tag.cat-green { background: rgba(16,185,129,.1); color: #10B981; }
.tag.cat-yellow { background: rgba(245,158,11,.12); color: #B45309; }
.tag.cat-purple { background: rgba(99,102,241,.1); color: #6366F1; }
.tag.cat-red { background: rgba(239,68,68,.1); color: #EF4444; }
.tag.cat-neutral { background: var(--surface-muted); color: var(--text-muted); }
</style>