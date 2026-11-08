<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getMemory, rejectMemory, unrejectMemory, type MemoryDetail, type MemorySource, type MemoryContext, type MemoryContextEdge } from '../../api/memory'
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

// ---------- 2.0 字段展示辅助 ----------
// 字段一律直显后端原值；缺失/空值统一回落「—」，不强转不报错。
// 后端 SELECT * 裸返回 memories 全列，2.0 列可能为 NULL（1.x 存量记忆），
// 所以逐字段带 || '' 兜底而不是断言存在。
function rawField(key: string): string {
  const v = detail.value?.memory?.[key]
  return v === null || v === undefined ? '' : String(v)
}

// 七值 type 已是中文（约束/习惯/偏好/状态/事实/经验/想法），直显；
// 其余枚举英文原值旁附中文释义，便于阅读。
const ENUM_LABELS: Record<string, Record<string, string>> = {
  certainty: { confirmed: '已确认', tentative: '暂定', discussing: '讨论中' },
  derivation: { fact: '事实陈述', reconstructed: '情景重构', inferred: '推理所得' },
  modality: { done: '已发生', planned: '计划中', wished: '期望', quoted: '转述', unknown: '未知' },
  route: { memory: '记忆池', skill_candidate: '技能候选', wiki_candidate: '知识页候选', discard: '丢弃' },
}

function enumText(key: string): string {
  const v = rawField(key)
  if (!v) return ''
  const label = ENUM_LABELS[key]?.[v]
  return label ? `${label}（${v}）` : v
}

// 主体 self / others:xxx / unknown：others:名字 翻译为「他人：名字」
function subjectText(): string {
  const v = rawField('subject')
  if (!v) return ''
  if (v === 'self') return '自己（self）'
  if (v === 'unknown') return '未知（unknown）'
  if (v.startsWith('others:')) return `他人：${v.slice('others:'.length)}`
  return v
}

// 待审闸门（D-23）：1 = 不进主注入，只进待审候选
const needsReview = computed(() => rawField('needs_review') === '1')

// 感受/特质：人类可读场景才有意义，1.x 存量为空
const feelingText = computed(() => rawField('feeling'))
const traitText = computed(() => rawField('trait'))

// ---------- 事件上下文（W06 / W01 §5） ----------
// 后端 build_context() 保证五键齐备（无数据也是空形态）；老后端没有 context 键 →
// 回落空形态，详情页不报错、不制造空面板（整块无数据只显示一行「—」）。
const EMPTY_CONTEXT: MemoryContext = {
  main_event: null,
  facts: [],
  causes: [],
  parent_memory_id: null,
  edges: [],
}
const context = computed<MemoryContext>(() => detail.value?.context ?? EMPTY_CONTEXT)
const hasContext = computed(() =>
  Boolean(
    context.value.main_event ||
      context.value.parent_memory_id ||
      context.value.facts.length ||
      context.value.causes.length ||
      context.value.edges.length,
  ),
)

// 事实主体与记忆主体同规则：self / unknown / others:<名>；缺失显示 —
function factSubjectText(v: string | null): string {
  if (!v) return '—'
  if (v === 'self') return '自己（self）'
  if (v === 'unknown') return '未知（unknown）'
  if (v.startsWith('others:')) return `他人：${v.slice('others:'.length)}`
  return v
}

// 关联边：后端只暴露 belongs_to（source=l2_extract）与 causes，双向；
// relation 用中文标签 + 原值，对端 id 可点进对应记忆详情。
const RELATION_LABEL: Record<string, string> = {
  belongs_to: '归属（belongs_to）',
  causes: '因果（causes）',
}
function relationLabel(v: string): string {
  return RELATION_LABEL[v] || v
}
function edgePeer(e: MemoryContextEdge): string {
  return e.from_id === id.value ? e.to_id : e.from_id
}
function edgeDirection(e: MemoryContextEdge): string {
  return e.from_id === id.value ? '出边 →' : '← 入边'
}
function openMemory(memoryId: string) {
  if (memoryId && memoryId !== id.value) router.push(`/memories/${memoryId}`)
}

// 关联页（memory_pages）：后端详情接口暂未返回该表（截至 2026-11 无 HTTP 端点），
// 契约按 api/memory.ts MemoryPageRef 预留，后端补上后此区块自动生效。
const pages = computed(() => (Array.isArray(detail.value?.pages) ? detail.value!.pages! : []))

const qualityRows = computed(() => {
  const rows: Array<{ label: string; value: string; mono?: boolean }> = [
    { label: '类型（type）', value: rawField('type') || '—' },
    { label: '主体（subject）', value: subjectText() || '—' },
    { label: '确定性（certainty）', value: enumText('certainty') || '—' },
    { label: '推导方式（derivation）', value: enumText('derivation') || '—' },
    { label: '断言模态（modality）', value: enumText('modality') || '—' },
    { label: '分流（route）', value: enumText('route') || '—' },
    { label: '待审（needs_review）', value: needsReview.value ? '是（1）' : '否（0）' },
    { label: '场景键（scene_key）', value: rawField('scene_key') || '—', mono: true },
  ]
  return rows
})

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
        <span v-if="needsReview" class="status pending">待审 · 不进主注入</span>
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

      <!-- 拒绝详情（status=rejected）：reject_reason/rejected_at 随 GET /v1/memory/{id} 原样返回（DAO SELECT *） -->
      <div v-if="detail.memory.status === 'rejected'" class="panel">
        <h3>拒绝原因</h3>
        <p class="note-text">{{ rawField('reject_reason') || '未填写' }}</p>
        <div class="meta">
          <span>拒绝时间: {{ rawField('rejected_at') ? fmtTs(rawField('rejected_at')) : '未填写' }}</span>
        </div>
      </div>

      <!-- 事件上下文（W06 / W01 §5）：主事件 / 父记忆 / 事实 / 因果 / 关联边 -->
      <div class="panel">
        <h3>事件上下文 <span class="sub">主事件 / 事实 / 因果 / 关联边</span></h3>
        <template v-if="hasContext">
          <div class="ctx-section">
            <b class="q-label">主事件</b>
            <template v-if="context.main_event">
              <p class="ctx-main-text">{{ context.main_event.content }}</p>
              <p class="meta">
                主事件 ID:
                <a class="ctx-link" href="#" @click.prevent="openMemory(context.main_event.memory_id)">{{ context.main_event.memory_id }}</a>
              </p>
            </template>
            <p v-else class="ctx-dash">—</p>
            <p class="meta">
              父记忆（parent_memory_id）:
              <template v-if="context.parent_memory_id">
                <a class="ctx-link" href="#" @click.prevent="openMemory(context.parent_memory_id)">{{ context.parent_memory_id }}</a>
              </template>
              <template v-else>—</template>
            </p>
          </div>

          <div class="ctx-section">
            <b class="q-label">事实（facts）</b>
            <ul v-if="context.facts.length" class="ctx-list">
              <li v-for="(f, i) in context.facts" :key="i" class="ctx-item">
                <span class="ctx-tag">{{ f.type || '—' }}</span>
                <span class="ctx-text">{{ f.content }}</span>
                <span class="ctx-sub">主体 {{ factSubjectText(f.subject) }}</span>
              </li>
            </ul>
            <p v-else class="ctx-dash">—</p>
          </div>

          <div class="ctx-section">
            <b class="q-label">因果（causes · 原因 → 结论）</b>
            <ul v-if="context.causes.length" class="ctx-list">
              <li v-for="(c, i) in context.causes" :key="i" class="ctx-item">
                <span class="ctx-text">{{ c.from }}</span>
                <span class="ctx-arrow">→</span>
                <span class="ctx-text">{{ c.to }}</span>
              </li>
            </ul>
            <p v-else class="ctx-dash">—</p>
          </div>

          <div class="ctx-section">
            <b class="q-label">关联边（edges · belongs_to / causes）</b>
            <ul v-if="context.edges.length" class="ctx-list">
              <li v-for="(e, i) in context.edges" :key="i" class="ctx-item">
                <span class="ctx-tag">{{ relationLabel(e.relation) }}</span>
                <span class="ctx-text">
                  {{ edgeDirection(e) }}
                  <a class="ctx-link" href="#" @click.prevent="openMemory(edgePeer(e))">{{ edgePeer(e) }}</a>
                </span>
                <span class="ctx-sub">weight {{ e.weight }} · source {{ e.source || '—' }}</span>
              </li>
            </ul>
            <p v-else class="ctx-dash">—</p>
          </div>
        </template>
        <p v-else class="ctx-dash">—（无主事件 / 事实 / 因果 / 关联边数据）</p>
      </div>

      <div class="panel">
        <h3>记忆质量 <span class="sub">SGME 2.0 元数据</span></h3>
        <div class="meta q-grid">
          <span v-for="row in qualityRows" :key="row.label" class="q-item">
            <b class="q-label">{{ row.label }}：</b><span :class="{ mono: row.mono }">{{ row.value }}</span>
          </span>
          <span class="q-item">
            <b class="q-label">感受（feeling）：</b><span>{{ feelingText || '—' }}</span>
          </span>
          <span class="q-item">
            <b class="q-label">特质（trait）：</b><span>{{ traitText || '—' }}</span>
          </span>
          <span class="q-item">
            <b class="q-label">备注（notes）：</b><span>{{ detail.memory.notes || '—' }}</span>
          </span>
        </div>
      </div>

      <div v-if="pages.length" class="panel">
        <h3>关联页 <span class="sub">memory_pages</span></h3>
        <ul class="src-list">
          <li v-for="(p, i) in pages" :key="i" class="src">
            <span class="src-type">{{ p.kind }}</span>
            <code class="inline">{{ p.ref_id }}</code>
            <span class="chain-ts">{{ p.relation }}</span>
          </li>
        </ul>
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

<style scoped>
/* 记忆质量字段网格：两列自适应，窄屏自动并一列；全部复用现有令牌，不引入新色 */
.q-grid {
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-2);
}

@media (min-width: 720px) {
  .q-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-2) var(--space-4);
  }
}

.q-item {
  font-size: var(--fs-sm);
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.q-label {
  font-weight: 600;
  color: var(--text-muted);
}

/* 事件上下文（W06）：纯文本列表，复用现有令牌；分节用间距而不是新色 */
.ctx-section {
  margin-top: var(--space-3);
}

.ctx-section:first-of-type {
  margin-top: 0;
}

.ctx-main-text {
  margin: var(--space-1) 0;
  font-size: var(--fs-sm);
  color: var(--text);
  white-space: pre-wrap;
}

.ctx-list {
  list-style: none;
  margin: var(--space-1) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.ctx-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--text);
}

.ctx-tag {
  flex: none;
  padding: 0 var(--space-1);
  border-radius: var(--radius-sm);
  background: var(--surface-muted);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.ctx-text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.ctx-sub {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.ctx-arrow {
  color: var(--text-muted);
}

.ctx-dash {
  margin: var(--space-1) 0 0;
  color: var(--text-muted);
}

.ctx-link {
  color: var(--brand);
  text-decoration: none;
}

.ctx-link:hover {
  text-decoration: underline;
}
</style>
