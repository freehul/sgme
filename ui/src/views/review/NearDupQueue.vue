<script setup lang="ts">
// 近似重复复核页（2026-11-05 B313，返工版：对齐全局设计系统 + 高密度 + 排序 + 批量）
//
// 为什么是对照视图：判断「这两条是不是同一件事」必须左右并排看——左边是被拦下
// 的新内容，右边是它撞上的既有记忆原文。抽掉右边就没有判断依据。
//
// 返工背景（用户 2026-11-05 反馈）：初版自造了一套硬编码色值，深色主题下正文与背景
// 同色→看不见；卡片过大密度低；无排序；批量操作藏在选中后才出现的浮条里。
// 本版全部改用设计令牌（--surface/--text/--border/--brand…）+ 全局组件类。
import { computed, onMounted, ref, watch } from 'vue'
import { ApiError } from '../../api/client'
import {
  discardQuarantine,
  inBand,
  listQuarantine,
  restoreQuarantine,
  type QuarantineItem,
  type QuarantineStatus,
  type ScoreBand,
} from '../../api/nearDup'

const PAGE_SIZE = 30

const rows = ref<QuarantineItem[]>([])
const total = ref(0)
const page = ref(1)
const status = ref<QuarantineStatus>('pending')
const band = ref<ScoreBand>('all')
const sortBy = ref<'score_desc' | 'score_asc' | 'time_desc' | 'time_asc'>('score_desc')
const loading = ref(false)
const error = ref('')
const msg = ref('')
const selected = ref<number[]>([])
const expanded = ref<number[]>([])

const acting = ref<QuarantineItem | null>(null)
const actKind = ref<'discard' | 'restore'>('discard')
const actReason = ref('')
// 放行方式：true=替代旧记忆（默认），false=两条并存
const actSupersede = ref(true)
const saving = ref(false)

const maxPage = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

// 分档筛选 + 排序都在前端做：一页 30 条，不值得多开往返
const shown = computed(() => {
  const list = rows.value.filter((it) => inBand(it.score, band.value))
  const s = sortBy.value
  return [...list].sort((a, b) => {
    if (s === 'score_desc') return b.score - a.score
    if (s === 'score_asc') return a.score - b.score
    if (s === 'time_asc') return a.created_at.localeCompare(b.created_at)
    return b.created_at.localeCompare(a.created_at)
  })
})

const allChecked = computed(
  () => shown.value.length > 0 && shown.value.every((it) => selected.value.includes(it.id)),
)

function bandClass(score: number): string {
  if (score >= 0.95) return 's-high'
  if (score >= 0.90) return 's-mid'
  return 's-low'
}

function pct(score: number): string {
  return (score * 100).toFixed(1) + '%'
}

function shortId(id: string | null): string {
  if (!id) return '—'
  return id.length > 14 ? id.slice(0, 8) + '…' + id.slice(-4) : id
}

function oneLine(text: string, n = 110): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n) + '…' : t
}

function toggleExpand(id: number) {
  const i = expanded.value.indexOf(id)
  if (i >= 0) expanded.value.splice(i, 1)
  else expanded.value.push(id)
}

function toggleAll() {
  if (allChecked.value) selected.value = []
  else selected.value = shown.value.map((it) => it.id)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listQuarantine(status.value, PAGE_SIZE, (page.value - 1) * PAGE_SIZE)
    rows.value = data.items
    total.value = data.total
    selected.value = []
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function openAct(it: QuarantineItem, kind: 'discard' | 'restore') {
  acting.value = it
  actKind.value = kind
  actReason.value = ''
  actSupersede.value = true
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
    if (actKind.value === 'discard') {
      const r = await discardQuarantine(it.id, actReason.value.trim() || undefined)
      msg.value = r.ok ? '已丢弃 #' + it.id : '#' + it.id + ' 当前状态 ' + r.status + '（无需重复操作）'
    } else {
      const r = await restoreQuarantine(it.id, actReason.value.trim() || undefined, actSupersede.value)
      msg.value = '已放行 #' + it.id + '，新记忆 ' + shortId(r.memory_id)
    }
    acting.value = null
    actReason.value = ''
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

// 批量只允许丢弃：放行会真写记忆池 + 占向量，误操作代价大，强制单条确认
async function bulkDiscard() {
  const ids = [...selected.value]
  if (!ids.length) return
  saving.value = true
  error.value = ''
  let ok = 0
  const fails: number[] = []
  for (const qid of ids) {
    try {
      const r = await discardQuarantine(qid)
      if (r.ok) ok += 1
      else fails.push(qid)
    } catch {
      fails.push(qid)
    }
  }
  msg.value = '批量丢弃：成功 ' + ok + ' 条' + (fails.length ? '，跳过 ' + fails.length + ' 条（状态已变）' : '')
  saving.value = false
  await load()
}

watch([status, page], () => { void load() })
onMounted(() => { void load() })
</script>

<template>
  <div class="near-dup">
    <div class="page-head">
      <div>
        <h2>近似重复</h2>
        <p class="sub">
          提炼时判定「像重复」的新记忆不进记忆池；相似度 ≥0.92 已自动丢弃（可在「已丢弃」里翻案），这里只留需人工裁决的条目。
          展开可见左右对照：左边新内容，右边撞上的既有记忆。
        </p>
      </div>
      <button class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="tabs">
      <button :class="{ active: status === 'pending' }" @click="status = 'pending'; page = 1">待处理</button>
      <button :class="{ active: status === 'discarded' }" @click="status = 'discarded'; page = 1">已丢弃</button>
      <button :class="{ active: status === 'restored' }" @click="status = 'restored'; page = 1">已放行</button>
    </div>

    <div class="toolbar">
      <div class="seg">
        <button :class="{ on: band === 'all' }" @click="band = 'all'">全部</button>
        <button :class="{ on: band === 'high' }" @click="band = 'high'">≥95%</button>
        <button :class="{ on: band === 'mid' }" @click="band = 'mid'">90–95%</button>
        <button :class="{ on: band === 'low' }" @click="band = 'low'">&lt;90%</button>
      </div>
      <select v-model="sortBy" class="sort">
        <option value="score_desc">相似度 ↓</option>
        <option value="score_asc">相似度 ↑</option>
        <option value="time_desc">时间 ↓</option>
        <option value="time_asc">时间 ↑</option>
      </select>
      <span class="count">共 {{ total }} 条 · 本页 {{ shown.length }}</span>
      <div v-if="status === 'pending'" class="bulk">
        <span v-if="selected.length" class="picked">已选 {{ selected.length }}</span>
        <button class="btn btn-sm btn-danger" :disabled="!selected.length || saving" @click="bulkDiscard">
          批量丢弃
        </button>
      </div>
    </div>

    <div v-if="error" class="alert err">{{ error }}</div>
    <div v-if="msg" class="alert ok">{{ msg }}</div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!shown.length" class="empty">
      {{ status === 'pending' ? '没有待处理的条目' : '该状态下没有条目' }}
    </div>

    <div v-else class="rows">
      <div class="row head">
        <span class="c-check">
          <input v-if="status === 'pending'" type="checkbox" :checked="allChecked" @change="toggleAll" />
        </span>
        <span class="c-id">编号</span>
        <span class="c-score">相似度</span>
        <span class="c-sum">摘要</span>
        <span class="c-time">时间</span>
        <span class="c-src">来源</span>
        <span class="c-act"></span>
      </div>

      <template v-for="it in shown" :key="it.id">
        <div class="row" :class="{ open: expanded.includes(it.id) }">
          <span class="c-check">
            <input v-if="status === 'pending'" v-model="selected" type="checkbox" :value="it.id" />
          </span>
          <span class="c-id">#{{ it.id }}</span>
          <span class="c-score"><b :class="bandClass(it.score)">{{ pct(it.score) }}</b></span>
          <span class="c-sum" @click="toggleExpand(it.id)">{{ oneLine(it.content) }}</span>
          <span class="c-time">{{ it.created_at.slice(5, 16).replace('T', ' ') }}</span>
          <span class="c-src">{{ it.source_ref || '—' }}</span>
          <span class="c-act">
            <button class="btn btn-sm btn-ghost" @click="toggleExpand(it.id)">
              {{ expanded.includes(it.id) ? '收起' : '对照' }}
            </button>
            <template v-if="status === 'pending'">
              <button class="btn btn-sm btn-ghost" @click="openAct(it, 'discard')">丢弃</button>
              <button class="btn btn-sm btn-primary" @click="openAct(it, 'restore')">放行</button>
            </template>
          </span>
        </div>

        <div v-if="expanded.includes(it.id)" class="detail">
          <div class="col">
            <div class="col-title">新内容（被拦下的）</div>
            <p>{{ it.content }}</p>
          </div>
          <div class="col existing">
            <div class="col-title">撞上的既有记忆 · {{ shortId(it.matched_memory_id) }}</div>
            <p v-if="it.matched_content">{{ it.matched_content }}</p>
            <p v-else class="faint">（该记忆已归档或删除，无法对照）</p>
          </div>
          <div v-if="it.discard_reason || it.restore_reason" class="col-reason">
            {{ it.discard_reason ? '丢弃理由：' + it.discard_reason : '' }}
            {{ it.restore_reason ? '放行理由：' + it.restore_reason : '' }}
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
        <h3>{{ actKind === 'discard' ? '丢弃' : '放行' }} #{{ acting.id }}</h3>
        <p class="modal-hint">
          {{ actKind === 'discard'
            ? '确认与右侧既有记忆是同一件事，不写进记忆池。'
            : '确认是独立事实，补录进记忆池并即时回填向量。' }}
        </p>
        <div v-if="actKind === 'restore'" class="mode">
          <label>
            <input v-model="actSupersede" type="radio" :value="true" />
            <span>替代旧记忆<em>（推荐）</em>：新记忆用人工确认档优先级，旧的归档但可溯源</span>
          </label>
          <label>
            <input v-model="actSupersede" type="radio" :value="false" />
            <span>两条并存：都留（适用于「都是独立事实、只是措辞相似」）</span>
          </label>
        </div>
        <input v-model="actReason" placeholder="理由（可选，建议写）" />
        <p class="modal-note">理由非必填；留痕的价值在于以后调阈值时知道砍掉的是哪一类。</p>
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
/* 全部走设计令牌，深浅主题自动跟随；不自定义色值 */
.near-dup { padding: var(--space-5) var(--space-6) 60px; max-width: var(--content-max); }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-4); margin-bottom: var(--space-3); }
.page-head h2 { margin: 0 0 var(--space-1); font-size: var(--fs-xl); color: var(--text); }
.sub { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; max-width: 720px; }

.toolbar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; margin-bottom: var(--space-3); }
.seg { display: flex; border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
.seg button { border: none; background: var(--surface); color: var(--text-muted); padding: 5px 12px; font-size: var(--fs-xs); cursor: pointer; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button.on { background: var(--brand-soft); color: var(--brand-text); font-weight: 600; }
.sort { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); padding: 5px 8px; font-size: var(--fs-xs); }
.count { color: var(--text-faint); font-size: var(--fs-xs); }
.bulk { margin-left: auto; display: flex; align-items: center; gap: var(--space-2); }
.picked { font-size: var(--fs-xs); color: var(--brand-text); }

.rows { border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; background: var(--surface); }
.row { display: grid; grid-template-columns: 28px 56px 64px 1fr 92px 150px 190px; align-items: center; gap: var(--space-2); padding: 6px 12px; font-size: var(--fs-sm); color: var(--text); border-top: 1px solid var(--divider); }
.row.head { border-top: none; background: var(--surface-muted); color: var(--text-muted); font-size: var(--fs-xs); font-weight: 600; padding: 7px 12px; }
.row.open { background: var(--surface-hover); }
.c-check { display: flex; align-items: center; }
.c-id { color: var(--text-muted); font-variant-numeric: tabular-nums; }
.c-score b { font-variant-numeric: tabular-nums; }
.s-high { color: var(--danger); }
.s-mid { color: var(--warn); }
.s-low { color: var(--text-muted); }
.c-sum { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.c-sum:hover { color: var(--brand-text); }
.c-time, .c-src { color: var(--text-faint); font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.c-act { display: flex; gap: 4px; justify-content: flex-end; }

.detail { display: grid; grid-template-columns: 1fr 1fr; gap: 0; border-top: 1px solid var(--divider); background: var(--surface-muted); }
.col { padding: 10px 14px; }
.col + .col { border-left: 1px dashed var(--border); }
.col-title { font-size: var(--fs-xs); color: var(--text-faint); margin-bottom: var(--space-1); }
.col p { margin: 0; font-size: var(--fs-sm); line-height: 1.7; color: var(--text); white-space: pre-wrap; word-break: break-word; }
.col .faint { color: var(--text-faint); font-style: italic; }
.col-reason { grid-column: 1 / -1; padding: 6px 14px 10px; font-size: var(--fs-xs); color: var(--text-muted); border-top: 1px dashed var(--border); }

.alert { padding: 8px 12px; border-radius: var(--radius-sm); font-size: var(--fs-sm); margin-bottom: var(--space-3); }
.alert.err { background: var(--danger-soft); color: var(--danger); }
.alert.ok { background: var(--success-soft); color: var(--success); }
.modal-hint { margin: 0 0 var(--space-3); color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; }
.mode { display: flex; flex-direction: column; gap: var(--space-2); margin: 0 0 var(--space-3); padding: var(--space-2) var(--space-3); background: var(--surface-muted); border-radius: var(--radius-sm); }
.mode label { display: flex; align-items: flex-start; gap: var(--space-2); font-size: var(--fs-xs); color: var(--text); cursor: pointer; line-height: 1.6; }
.mode label em { font-style: normal; color: var(--brand-text); }
.mode input[type='radio'] { margin-top: 2px; }
.modal-note { margin: var(--space-2) 0 var(--space-4); color: var(--text-faint); font-size: var(--fs-xs); line-height: 1.6; }
</style>
