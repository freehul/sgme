<script setup lang="ts">
// 待审候选页（needs_review=1 的记忆在此人工放行/拒绝，设计决策 D-30 / Backlog T-318）
// 页面结构照抄 IdeaList.vue：卡片列表 + 分页 + 弹层操作，样式复用全局设计系统
import { onMounted, ref } from 'vue'
import {
  approveMemory,
  listPendingReview,
  rejectMemory,
  type ReviewItem,
} from '../../api/review'
import { listRegistry } from '../../api/admin'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'
import ReviewDetail from './ReviewDetail.vue'

const rows = ref<ReviewItem[]>([])
const total = ref(0)
const limit = 20
const page = ref(1)
const dimFilter = ref('')
const dimOptions = ref<Array<{ id: string; display_name: string }>>([])
const loading = ref(false)
const error = ref('')

// 弹层状态：放行备注可选；拒绝必须填理由
const approving = ref<ReviewItem | null>(null)
const approveNote = ref('')
const rejecting = ref<ReviewItem | null>(null)
const rejectReason = ref('')
// 详情弹层（ReviewDetail 组件）：卡片点击打开，操作回传后转对应弹层
const selected = ref<ReviewItem | null>(null)

function openDetail(item: ReviewItem) {
  selected.value = item
}

function detailApprove(item: ReviewItem) {
  selected.value = null
  openApprove(item)
}

function detailReject(item: ReviewItem) {
  selected.value = null
  openReject(item)
}

const CONTENT_PREVIEW_LEN = 80
function preview(content: string): string {
  return content.length > CONTENT_PREVIEW_LEN ? content.slice(0, CONTENT_PREVIEW_LEN) + '…' : content
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listPendingReview({
      limit,
      offset: (page.value - 1) * limit,
      dimension: dimFilter.value || undefined,
    })
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// 加载维度候选（注册表 active 维度），失败不阻塞列表
async function loadDims() {
  try {
    const data = await listRegistry()
    dimOptions.value = data.dimensions
  } catch (e) {
    console.error('维度列表加载失败', e)
  }
}

onMounted(() => {
  load()
  loadDims()
})

function search() {
  page.value = 1
  load()
}

function openApprove(item: ReviewItem) {
  approving.value = item
  approveNote.value = ''
}

async function saveApprove() {
  if (!approving.value) return
  try {
    await approveMemory(approving.value.memory_id, approveNote.value.trim() || undefined)
    approving.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function openReject(item: ReviewItem) {
  rejecting.value = item
  rejectReason.value = ''
}

async function saveReject() {
  if (!rejecting.value || !rejectReason.value.trim()) return
  try {
    await rejectMemory(rejecting.value.memory_id, rejectReason.value.trim())
    rejecting.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function dimClass(d: string): string {
  const map: Record<string, string> = {
    identity: 'dimtag-yellow',
    goals: 'dimtag-green',
    status: 'dimtag-blue',
    focus: 'dimtag-purple',
    preferences: 'dimtag-blue',
    ideas: 'dimtag-purple',
    care: 'dimtag-red',
  }
  return map[d] || 'dimtag-neutral'
}
</script>

<template>
  <div class="review">
    <div class="head">
      <h2>待审候选<span class="sub">needs_review=1 的记忆需人工放行后才会进入注入</span></h2>
      <div class="filters">
        <select v-model="dimFilter" @change="search">
          <option value="">全部维度</option>
          <option v-for="d in dimOptions" :key="d.id" :value="d.id">{{ d.display_name }}（{{ d.id }}）</option>
        </select>
        <button @click="search">检索</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length" class="empty">暂无待审候选。提炼产生 needs_review=1 的记忆后会出现在这里，放行后进入注入。</p>

    <div v-else class="list">
      <div v-for="item in rows" :key="item.memory_id" class="card clickable" @click="openDetail(item)">
        <div class="card-head">
          <span v-if="item.dimension" class="dim-tag" :class="dimClass(item.dimension)">{{ item.dimension }}</span>
          <span v-if="item.memory_type" class="type">{{ item.memory_type }}</span>
          <span v-if="item.type && item.type !== item.memory_type" class="type">{{ item.type }}</span>
          <span v-if="item.route" class="badge">{{ item.route }}</span>
          <p class="content">{{ preview(item.content) }}</p>
        </div>
        <div class="meta">
          <span v-if="item.subject">主体：{{ item.subject }}</span>
          <span class="mono">ID {{ item.memory_id }}</span>
          <span class="ts">{{ fmtTs(item.created_at) }}</span>
        </div>
        <div class="actions">
          <button class="primary" @click.stop="openApprove(item)">放行</button>
          <button class="danger" @click.stop="openReject(item)">拒绝</button>
        </div>
      </div>

      <div class="pager">
        <button :disabled="page <= 1" @click="page--; load()">上一页</button>
        <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
        <button :disabled="page * limit >= total" @click="page++; load()">下一页</button>
      </div>
    </div>

    <!-- 详情弹层 -->
    <ReviewDetail :item="selected" @close="selected = null" @approve="detailApprove" @reject="detailReject" />

    <!-- 放行弹层（备注可选） -->
    <div v-if="approving" class="modal">
      <div class="modal-box">
        <h3>放行记忆</h3>
        <p class="content">{{ approving.content }}</p>
        <label>备注（可选）</label>
        <textarea v-model="approveNote" rows="2" placeholder="放行原因/说明，可留空"></textarea>
        <div class="modal-actions">
          <button @click="approving = null">取消</button>
          <button class="primary" @click="saveApprove">确认放行</button>
        </div>
      </div>
    </div>

    <!-- 拒绝弹层（理由必填） -->
    <div v-if="rejecting" class="modal">
      <div class="modal-box">
        <h3>拒绝记忆</h3>
        <p class="content">{{ rejecting.content }}</p>
        <label>拒绝理由 *</label>
        <textarea v-model="rejectReason" rows="2" placeholder="必填：如与既有记忆冲突/信息过期/误提炼"></textarea>
        <div class="modal-actions">
          <button @click="rejecting = null">取消</button>
          <button class="danger" :disabled="!rejectReason.trim()" @click="saveReject">确认拒绝</button>
        </div>
      </div>
    </div>
  </div>
</template>
