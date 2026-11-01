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
</style>
