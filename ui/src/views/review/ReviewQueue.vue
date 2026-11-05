<script setup lang="ts">
// 待审候选页（D-30 / T-318，2026-11-05 改造：表格化 + 批量 + 理由非必填）
//
// 改造背景（用户反馈）：卡片式信息密度太低、很多重复、缺批量操作、拒绝理由不该必填。
// 本版参照「近似重复」页：高密度表格 + 顶部常驻批量按钮 + 展开看详情。
//
// 批量只允许拒绝（不进主注入）；放行会真进注入与检索，强制单条确认。
import { computed, onMounted, ref, watch } from 'vue'
import { ApiError } from '../../api/client'
import {
  AUTO_RESOLVE_DEFAULT_DAYS,
  approveMemory,
  autoResolveReview,
  batchRejectMemory,
  listPendingReview,
  rejectMemory,
  type ReviewItem,
} from '../../api/review'
import { getMemory } from '../../api/memory'
import { confidenceBadge, dimTagsOf, shortMemoryId } from './reviewMeta'

const PAGE_SIZE = 30

const rows = ref<ReviewItem[]>([])
const total = ref(0)
const page = ref(1)
const dimFilter = ref('')
const loading = ref(false)
const error = ref('')
const msg = ref('')
const selected = ref<string[]>([])
const expanded = ref<string[]>([])
const saving = ref(false)

const acting = ref<ReviewItem | null>(null)
const actKind = ref<'approve' | 'reject'>('approve')
const actReason = ref('')

const resolveDays = ref(AUTO_RESOLVE_DEFAULT_DAYS)

const maxPage = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const allChecked = computed(
  () => rows.value.length > 0 && rows.value.every((r) => selected.value.includes(r.memory_id)),
)

function oneLine(text: string, n = 110): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n) + '…' : t
}

function toggleExpand(id: string) {
  const i = expanded.value.indexOf(id)
  if (i >= 0) expanded.value.splice(i, 1)
  else expanded.value.push(id)
}

function toggleAll() {
  if (allChecked.value) selected.value = []
  else selected.value = rows.value.map((r) => r.memory_id)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listPendingReview({
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
      dimension: dimFilter.value || undefined,
    })
    rows.value = data.items
    total.value = data.total
    selected.value = []
    void enrich();
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

// 维度标签需要按需补（list_pending 不返回 memory_tags 关联）
async function enrich() {
  const targets = rows.value.filter((r) => !r.dimensions?.length)
  for (let i = 0; i < targets.length; i += 6) {
    const batch = targets.slice(i, i + 6)
    await Promise.all(batch.map(async (r) => {
      try {
        const d = await getMemory(r.memory_id)
        r.dimensions = (d as unknown as { dimensions?: string[] })?.dimensions ?? []
      } catch { /* 静默 */ }
    }))
  }
}

function openAct(item: ReviewItem, kind: 'approve' | 'reject') {
  acting.value = item
  actKind.value = kind
  actReason.value = ''
}

function closeAct() {
  if (saving.value) return
  acting.value = null
  actReason.value = ''
}

async function submitAct() {
  const it = acting.value
  if (!it) return
  saving.value = true
  error.value = ''
  try {
    if (actKind.value === 'approve') {
      await approveMemory(it.memory_id, actReason.value.trim() || undefined)
      msg.value = '已放行 ' + shortMemoryId(it.memory_id)
    } else {
      await rejectMemory(it.memory_id, actReason.value.trim())
      msg.value = '已拒绝 ' + shortMemoryId(it.memory_id)
    }
    acting.value = null
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

// 批量拒绝走服务端 batch-reject（D2，2026-11-05）：一次请求处理整页，
// 不再循环 N 次单条调用（每页 30 条 = 30 次鉴权往返，失败还得逐条拼文案）。
async function bulkReject() {
  const ids = [...selected.value]
  if (!ids.length) return
  saving.value = true
  error.value = ''
  try {
    const r = await batchRejectMemory(ids)
    const fails = r.results
      .filter((it) => !it.ok)
      .map((it) => shortMemoryId(it.memory_id))
    msg.value =
      '批量拒绝：成功 ' + r.rejected + ' 条' +
      (r.failed ? '，失败 ' + r.failed + ' 条（' + fails.join('、') + '）' : '')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
  await load()
}

async function doAutoResolve() {
  if (!window.confirm('把超过 ' + resolveDays.value + ' 天未处理的待审记忆按保守直存放行？')) return
  saving.value = true
  try {
    const r = await autoResolveReview(resolveDays.value)
    msg.value = '超期消化完成：' + r.resolved + ' 条'
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

watch([page, dimFilter], () => { void load() })
onMounted(() => { void load() })
</script>

<template>
  <div class="review-page">
    <div class="page-head">
      <div>
        <h2>待审候选</h2>
        <p class="sub">
          提炼时置信度不足的记忆不进主注入，暂存这里等你判「准不准」。
          超期 {{ resolveDays }} 天未处理的会自动按保守直存放行。
        </p>
      </div>
      <button class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="toolbar">
      <input v-model="dimFilter" class="dim" placeholder="按维度过滤（如 identity）" />
      <span class="count">共 {{ total }} 条 · 本页 {{ rows.length }}</span>
      <button class="btn btn-sm btn-ghost" :disabled="saving" @click="doAutoResolve">
        超期消化（{{ resolveDays }} 天）
      </button>
      <div class="bulk">
        <span v-if="selected.length" class="picked">已选 {{ selected.length }}</span>
        <button class="btn btn-sm btn-danger" :disabled="!selected.length || saving" @click="bulkReject">
          批量拒绝
        </button>
      </div>
    </div>

    <div v-if="error" class="alert err">{{ error }}</div>
    <div v-if="msg" class="alert ok">{{ msg }}</div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!rows.length" class="empty">待审队列为空</div>

    <div v-else class="rows">
      <div class="row head">
        <span class="c-check"><input type="checkbox" :checked="allChecked" @change="toggleAll" /></span>
        <span class="c-id">编号</span>
        <span class="c-conf">置信</span>
        <span class="c-sum">摘要</span>
        <span class="c-dim">维度</span>
        <span class="c-time">时间</span>
        <span class="c-act"></span>
      </div>
      <template v-for="it in rows" :key="it.memory_id">
        <div class="row" :class="{ open: expanded.includes(it.memory_id) }">
          <span class="c-check"><input v-model="selected" type="checkbox" :value="it.memory_id" /></span>
          <span class="c-id">{{ shortMemoryId(it.memory_id) }}</span>
          <span class="c-conf">
            <span :class="['badge', confidenceBadge(it).tone]">{{ confidenceBadge(it).label }}</span>
          </span>
          <span class="c-sum" @click="toggleExpand(it.memory_id)">{{ oneLine(it.content) }}</span>
          <span class="c-dim">
            <span v-for="d in dimTagsOf(it).slice(0, 2)" :key="d" class="dimtag">{{ d }}</span>
          </span>
          <span class="c-time">{{ (it.created_at || '').slice(5, 16).replace('T', ' ') }}</span>
          <span class="c-act">
            <button class="btn btn-sm btn-ghost" @click="toggleExpand(it.memory_id)">
              {{ expanded.includes(it.memory_id) ? '收起' : '展开' }}
            </button>
            <button class="btn btn-sm btn-ghost" @click="openAct(it, 'reject')">拒绝</button>
            <button class="btn btn-sm btn-primary" @click="openAct(it, 'approve')">放行</button>
          </span>
        </div>
        <div v-if="expanded.includes(it.memory_id)" class="detail">
          <div class="col">
            <div class="col-title">完整内容</div>
            <p>{{ it.content }}</p>
          </div>
          <div class="col meta-col">
            <div class="col-title">元信息</div>
            <p class="kv">类型：{{ it.type || '—' }}</p>
            <p class="kv">主体：{{ it.subject || '—' }}</p>
            <p class="kv">确定度：{{ it.certainty || '—' }}</p>
            <p class="kv">备注：{{ it.notes || '—' }}</p>
          </div>
        </div>
      </template>
    </div>

    <div class="pager">
      <button :disabled="page <= 1" @click="page -= 1">上一页</button>
      <span>{{ page }} / {{ maxPage }}</span>
      <button :disabled="page >= maxPage" @click="page += 1">下一页</button>
    </div>

    <div v-if="acting" class="modal" @click.self="closeAct">
      <div class="modal-box">
        <h3>{{ actKind === 'approve' ? '放行' : '拒绝' }} {{ shortMemoryId(acting.memory_id) }}</h3>
        <p class="modal-hint">
          {{ actKind === 'approve' ? '放行后这条记忆进主注入与检索。' : '拒绝是软删（status=rejected），可恢复、不物理删除。' }}
        </p>
        <input v-model="actReason" :placeholder="actKind === 'approve' ? '备注（可选）' : '理由（可选）'" />
        <p class="modal-note">理由非必填；填了在记忆详情里可查。</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" :disabled="saving" @click="closeAct">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="submitAct">
            {{ saving ? '处理中…' : '确认' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 全部走设计令牌 */
.review-page { padding: var(--space-5) var(--space-6) 60px; max-width: var(--content-max); }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-4); margin-bottom: var(--space-3); }
.page-head h2 { margin: 0 0 var(--space-1); font-size: var(--fs-xl); color: var(--text); }
.sub { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; max-width: 720px; }

.toolbar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; margin-bottom: var(--space-3); }
.dim { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 5px 10px; font-size: var(--fs-xs); background: var(--surface); color: var(--text); }
.count { color: var(--text-faint); font-size: var(--fs-xs); }
.bulk { margin-left: auto; display: flex; align-items: center; gap: var(--space-2); }
.picked { font-size: var(--fs-xs); color: var(--brand-text); }

.rows { border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; background: var(--surface); }
.row { display: grid; grid-template-columns: 28px 84px 62px 1fr 130px 92px 210px; align-items: center; gap: var(--space-2); padding: 6px 12px; font-size: var(--fs-sm); color: var(--text); border-top: 1px solid var(--divider); }
.row.head { border-top: none; background: var(--surface-muted); color: var(--text-muted); font-size: var(--fs-xs); font-weight: 600; padding: 7px 12px; }
.row.open { background: var(--surface-hover); }
.c-check { display: flex; align-items: center; }
.c-id { color: var(--text-muted); font-family: var(--font-mono); font-size: var(--fs-xs); }
.c-sum { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.c-sum:hover { color: var(--brand-text); }
.c-dim { display: flex; gap: 4px; overflow: hidden; }
.dimtag { font-size: 11px; padding: 1px 6px; border-radius: 999px; background: var(--brand-soft); color: var(--brand-text); }
.c-time { color: var(--text-faint); font-size: var(--fs-xs); white-space: nowrap; }
.c-act { display: flex; gap: 4px; justify-content: flex-end; }

.detail { display: grid; grid-template-columns: 1fr 260px; border-top: 1px solid var(--divider); background: var(--surface-muted); }
.col { padding: 10px 14px; }
.col + .col { border-left: 1px dashed var(--border); }
.col-title { font-size: var(--fs-xs); color: var(--text-faint); margin-bottom: var(--space-1); }
.col p { margin: 0 0 4px; font-size: var(--fs-sm); line-height: 1.7; color: var(--text); white-space: pre-wrap; word-break: break-word; }
.kv { font-size: var(--fs-xs) !important; color: var(--text-muted) !important; }

.alert { padding: 8px 12px; border-radius: var(--radius-sm); font-size: var(--fs-sm); margin-bottom: var(--space-3); }
.alert.err { background: var(--danger-soft); color: var(--danger); }
.alert.ok { background: var(--success-soft); color: var(--success); }
.modal-hint { margin: 0 0 var(--space-3); color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; }
.modal-note { margin: var(--space-2) 0 var(--space-4); color: var(--text-faint); font-size: var(--fs-xs); }
</style>
