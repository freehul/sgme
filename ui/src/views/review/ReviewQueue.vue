<script setup lang="ts">
// 待审候选页（needs_review=1 的记忆在此人工放行/拒绝，设计决策 D-30 / Backlog T-318）
// 页面结构照抄 IdeaList.vue：卡片列表 + 分页 + 弹层操作，样式复用全局设计系统
import { onMounted, ref } from 'vue'
import {
  AUTO_RESOLVE_DEFAULT_DAYS,
  approveMemory,
  autoResolveReview,
  listPendingReview,
  rejectMemory,
  type ReviewItem,
} from '../../api/review'
import { listRegistry } from '../../api/admin'
import { getMemory } from '../../api/memory'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'
import { confidenceBadge, dimClass, dimTagsOf, shortMemoryId } from './reviewMeta'
import ReviewDetail from './ReviewDetail.vue'

const rows = ref<ReviewItem[]>([])
const total = ref(0)
const limit = 20
const page = ref(1)
const dimFilter = ref('')
const dimOptions = ref<Array<{ id: string; display_name: string }>>([])
const loading = ref(false)
const error = ref('')
const msg = ref('')

// 弹层状态：放行备注可选；拒绝必须填理由
const approving = ref<ReviewItem | null>(null)
const approveNote = ref('')
const rejecting = ref<ReviewItem | null>(null)
const rejectReason = ref('')
// 单条放行/拒绝提交中（防重复点击）
const saving = ref(false)
// 详情弹层（ReviewDetail 组件）：卡片点击打开，操作回传后转对应弹层
const selected = ref<ReviewItem | null>(null)

// 超期消化（D-30）：默认 7 天，页面可调阈值；提交前二次确认
const resolveDays = ref(AUTO_RESOLVE_DEFAULT_DAYS)
const autoResolving = ref(false)

// 维度标签补全缓存（memory_id → dimensions）：翻页/重载复用，失败静默
const dimCache = new Map<string, string[]>()
const DIM_ENRICH_CONCURRENCY = 6

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
    // 维度标签补全不阻塞列表渲染（失败只是没有维度标签）；
    // 传 rows.value（响应式代理）而不是原始 data.items，补全后才能触发重渲染
    void enrichDimensions(rows.value)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

// 待审列表（后端 SELECT m.*）不返回 memory_tags 关联，故维度标签需按需补全：
// 复用既有 GET /v1/memory/{id}（memory_dao 会回填 dimensions），限并发、失败静默
// ——维度只是辅助信息，不阻塞放行/拒绝主流程。
async function enrichDimensions(items: ReviewItem[]) {
  const todo = items.filter((it) => !dimTagsOf(it).length && !dimCache.has(it.memory_id))
  for (let i = 0; i < todo.length; i += DIM_ENRICH_CONCURRENCY) {
    await Promise.allSettled(
      todo.slice(i, i + DIM_ENRICH_CONCURRENCY).map(async (it) => {
        const detail = await getMemory(it.memory_id)
        dimCache.set(it.memory_id, detail.memory?.dimensions ?? [])
      }),
    )
  }
  // 回填缓存（页面重载/翻页回来时直接复用，不再请求）
  for (const it of items) {
    if (dimTagsOf(it).length) continue
    const cached = dimCache.get(it.memory_id)
    if (cached?.length) it.dimensions = cached
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

// 超期消化（D-30 超期保守直存）：批量放行创建时间早于 N 天前的待审项，
// notes 追加「未经人工确认」标记（可溯源、可按标记复查）；提交前二次确认
async function runAutoResolve() {
  const days = Math.trunc(Number(resolveDays.value))
  if (!Number.isFinite(days) || days < 1) {
    error.value = '超期天数必须是 ≥1 的整数'
    return
  }
  if (!confirm(`确认超期消化待审项？\n\n将放行「创建时间早于 ${days} 天前」且仍待审的记忆（D-30 保守直存），并在备注追加「未经人工确认」标记——可溯源、可复查。`)) return
  autoResolving.value = true
  error.value = ''
  msg.value = ''
  try {
    const result = await autoResolveReview(days)
    msg.value = `超期消化完成：按 ${days} 天阈值本次消化 ${result.resolved ?? 0} 条待审项。`
    page.value = 1
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    autoResolving.value = false
  }
}

function openApprove(item: ReviewItem) {
  approving.value = item
  approveNote.value = ''
  msg.value = ''
}

async function saveApprove() {
  if (!approving.value || saving.value) return
  const item = approving.value
  const note = approveNote.value.trim()
  saving.value = true
  error.value = ''
  msg.value = ''
  try {
    await approveMemory(item.memory_id, note || undefined)
    approving.value = null
    msg.value = `已放行 ${shortMemoryId(item.memory_id)}${note ? '（备注已记入 notes）' : ''}，该记忆回到注入/检索可见状态。`
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function openReject(item: ReviewItem) {
  rejecting.value = item
  rejectReason.value = ''
  msg.value = ''
}

async function saveReject() {
  const reason = rejectReason.value.trim()
  if (!rejecting.value || !reason || saving.value) return
  const item = rejecting.value
  saving.value = true
  error.value = ''
  msg.value = ''
  try {
    await rejectMemory(item.memory_id, reason)
    rejecting.value = null
    msg.value = `已拒绝 ${shortMemoryId(item.memory_id)}（软删，不物理删除，可在记忆库恢复）。`
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="review">
    <div class="head">
      <h2>待审候选<span class="sub">needs_review=1 的记忆需人工放行后才会进入注入（D-30）</span></h2>
      <div class="filters">
        <select v-model="dimFilter" @change="search">
          <option value="">全部维度</option>
          <option v-for="d in dimOptions" :key="d.id" :value="d.id">{{ d.display_name }}（{{ d.id }}）</option>
        </select>
        <button class="btn btn-sm" :disabled="loading" @click="search">刷新</button>
        <label
          style="display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); color: var(--text-muted)"
          title="超期消化阈值：创建时间早于 N 天前的待审记忆才被消化"
        >
          超期天数
          <input v-model.number="resolveDays" type="number" min="1" max="365" style="width: 72px" />
        </label>
        <button class="btn btn-danger btn-sm" :disabled="autoResolving || loading" @click="runAutoResolve">
          {{ autoResolving ? '消化中…' : '超期消化' }}
        </button>
      </div>
    </div>

    <div v-if="error" style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px">
      <p class="error" style="margin: 0">{{ error }}</p>
      <button class="btn btn-sm" @click="load">重试</button>
    </div>
    <p v-if="msg" class="note-text">{{ msg }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length && !error" class="empty">
      暂无待审候选。L2 提炼产出低置信度（needs_review=1）的记忆后会出现在这里，放行后进入注入；超期未处理的会按 D-30 保守直存消化。
    </p>

    <div v-else-if="rows.length" class="list">
      <div v-for="item in rows" :key="item.memory_id" class="card clickable" @click="openDetail(item)">
        <div class="card-head">
          <span
            v-for="d in dimTagsOf(item)"
            :key="d"
            class="dim-tag"
            :class="dimClass(d)"
            :title="`维度 ${d}`"
          >{{ d }}</span>
          <span
            v-if="!dimTagsOf(item).length"
            class="dim-tag dimtag-neutral"
            title="待审列表接口不返回维度标签，补全失败时显示此占位"
          >维度 —</span>
          <span
            class="dim-tag"
            :class="confidenceBadge(item).tone"
            :title="confidenceBadge(item).hint"
          >置信度 {{ confidenceBadge(item).label }}</span>
          <span v-if="item.memory_type" class="type">{{ item.memory_type }}</span>
          <span v-if="item.type && item.type !== item.memory_type" class="type">{{ item.type }}</span>
          <span v-if="item.route" class="badge">{{ item.route }}</span>
          <p class="content">{{ preview(item.content) }}</p>
        </div>
        <div class="meta">
          <span v-if="item.subject">主体：{{ item.subject }}</span>
          <span v-if="item.scene_key">场景：{{ item.scene_key }}</span>
          <span class="mono" :title="item.memory_id">ID {{ shortMemoryId(item.memory_id) }}</span>
          <span class="ts">创建于 {{ fmtTs(item.created_at) }}</span>
        </div>
        <div class="actions">
          <button class="primary" :disabled="saving" @click.stop="openApprove(item)">放行</button>
          <button class="danger" :disabled="saving" @click.stop="openReject(item)">拒绝</button>
        </div>
      </div>

      <div class="pager">
        <button :disabled="loading || page <= 1" @click="page--; load()">上一页</button>
        <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
        <button :disabled="loading || page * limit >= total" @click="page++; load()">下一页</button>
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
          <button :disabled="saving" @click="approving = null">取消</button>
          <button class="primary" :disabled="saving" @click="saveApprove">确认放行</button>
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
          <button :disabled="saving" @click="rejecting = null">取消</button>
          <button class="danger" :disabled="saving || !rejectReason.trim()" @click="saveReject">确认拒绝</button>
        </div>
      </div>
    </div>
  </div>
</template>
