<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listRegistry, type Dimension } from '../../api/admin'
import {
  listMemories,
  getMemory,
  rejectMemory,
  unrejectMemory,
  type MemoryItem,
  type MemoryDetail,
} from '../../api/memory'
import { ApiError } from '../../api/client'

const rows = ref<MemoryItem[]>([])
const total = ref(0)
const page = ref(1)
const limit = 20
// 维度复选（2026-08-13：维度数量不多，全部列出，复选参与检索；AND 语义——勾选维度全部命中）
const dimOptions = ref<Dimension[]>([])
const selectedDims = ref<string[]>([])
const statusFilter = ref('')
const sort = ref('updated_at')
const order = ref('desc')
const ttlFilter = ref(false)
const loading = ref(false)
const error = ref('')

// 右侧详情（master-detail）
const selectedId = ref<string | null>(null)
const detail = ref<MemoryDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const reason = ref('')
const busy = ref(false)

const STATUS_LABEL: Record<string, string> = {
  active: '有效',
  rejected: '已拒绝',
  expired: '已过期',
  archived: '已归档',
}
const SORT_OPTIONS = [
  { value: 'updated_at', label: '更新时间' },
  { value: 'occurred_at', label: '发生时间' },
  { value: 'priority', label: '优先级' },
]

// 维度 → 参考设计彩色胶囊映射
const DIM_COLOR: Record<string, string> = {
  tasks: 'blue',
  focus: 'blue',
  status: 'blue',
  projects: 'green',
  goals: 'green',
  identity: 'yellow',
  habits: 'yellow',
  skills: 'purple',
  interests: 'purple',
  ideas: 'purple',
  tech_stack: 'neutral',
}

function dimClass(d: string): string {
  return `dim-tag dimtag-${DIM_COLOR[d] || 'neutral'}`
}

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listMemories({
      page: page.value,
      limit,
      dimensions: selectedDims.value,
      status: statusFilter.value || undefined,
      sort: sort.value,
      order: order.value,
      ttl_filter: ttlFilter.value,
    })
    rows.value = data.items
    total.value = data.total
    // 列表刷新后若选中的项已不在当前页，清空右侧详情
    if (selectedId.value && !data.items.some((m) => m.memory_id === selectedId.value)) {
      selectedId.value = null
      detail.value = null
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// 加载维度候选（注册表 active 维度）
async function loadDims() {
  try {
    const data = await listRegistry()
    dimOptions.value = data.dimensions
  } catch (e) {
    // 维度列表加载失败不阻塞列表（仅复选不可用）
    console.error('维度列表加载失败', e)
  }
}

function apply() {
  page.value = 1
  load()
}

async function select(id: string) {
  selectedId.value = id
  detail.value = null
  detailLoading.value = true
  detailError.value = ''
  reason.value = ''
  try {
    detail.value = await getMemory(id)
  } catch (e) {
    detailError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

async function reject() {
  if (!selectedId.value) return
  busy.value = true
  detailError.value = ''
  try {
    await rejectMemory(selectedId.value, reason.value.trim() || undefined)
    await select(selectedId.value)
    await load()
  } catch (e) {
    detailError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function unreject() {
  if (!selectedId.value) return
  busy.value = true
  detailError.value = ''
  try {
    await unrejectMemory(selectedId.value)
    await select(selectedId.value)
    await load()
  } catch (e) {
    detailError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(() => { loadDims(); load() })
</script>

<template>
  <div class="memories">
    <div class="head">
      <h2>记忆列表</h2>
      <div class="filters dim-filter">
        <div class="dim-select">
          <span class="dim-label">维度</span>
          <div class="dim-checks">
            <label v-for="d in dimOptions" :key="d.id" class="chk-chip" :class="{ on: selectedDims.includes(d.id) }">
              <input v-model="selectedDims" type="checkbox" :value="d.id" @change="apply" />
              <span class="chk-chip-label">{{ d.display_name || d.id }}</span>
            </label>
            <span v-if="!dimOptions.length" class="dim-muted">（无维度）</span>
          </div>
        </div>
        <select v-model="statusFilter" @change="apply">
          <option value="">全部状态</option>
          <option v-for="(label, key) in STATUS_LABEL" :key="key" :value="key">{{ label }}</option>
        </select>
        <select v-model="sort" @change="apply">
          <option v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <select v-model="order" @change="apply">
          <option value="desc">倒序</option>
          <option value="asc">正序</option>
        </select>
        <label class="chk"><input v-model="ttlFilter" type="checkbox" @change="apply" /> TTL 过滤</label>
        <button @click="apply">检索</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div class="split">
      <!-- 左侧：数据列表（序号 / 优先级圆圈 / 内容） -->
      <div class="list-pane">
        <p v-if="loading" class="empty">加载中…</p>
        <p v-else-if="!rows.length" class="empty">暂无记忆。</p>
        <template v-else>
          <table class="tbl mem-tbl">
            <thead>
              <tr>
                <th class="col-idx">#</th>
                <th class="col-dot">状态</th>
                <th>内容</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(m, i) in rows"
                :key="m.memory_id"
                :class="{ active: selectedId === m.memory_id }"
                @click="select(m.memory_id)"
              >
                <td class="col-idx idx">{{ (page - 1) * limit + i + 1 }}</td>
                <td class="col-dot">
                  <span
                    class="prio-dot"
                    :class="m.status === 'active' ? 'ok' : 'err'"
                    :title="STATUS_LABEL[m.status] || m.status"
                  />
                </td>
                <td class="content-cell">{{ m.content }}</td>
              </tr>
            </tbody>
          </table>
          <div class="pager">
            <button :disabled="page <= 1" @click="page--; load()">上一页</button>
            <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
            <button :disabled="page * limit >= total" @click="page++; load()">下一页</button>
          </div>
        </template>
      </div>

      <!-- 右侧：详情 + 操作按钮 -->
      <div class="detail-pane">
        <div v-if="!selectedId" class="empty">从左侧选择一条记忆查看详情</div>
        <p v-else-if="detailLoading" class="empty">加载中…</p>
        <p v-else-if="detailError" class="error">{{ detailError }}</p>
        <template v-else-if="detail">
          <div class="detail-head">
            <h3>记忆详情</h3>
            <span class="status" :class="detail.memory.status">
              <span class="status-dotx" :class="detail.memory.status" />{{ STATUS_LABEL[detail.memory.status] || detail.memory.status }}
            </span>
          </div>

          <section class="detail-block">
            <pre class="content">{{ detail.memory.content }}</pre>
            <div class="meta">
              <span>ID: <code class="mono">{{ detail.memory.memory_id }}</code></span>
              <span>类型: {{ detail.memory.memory_type }}</span>
              <span>优先级: P{{ detail.memory.priority }}</span>
              <span>发生: {{ fmtTs(detail.memory.occurred_at) }}</span>
              <span>更新: {{ fmtTs(detail.memory.updated_at) }}</span>
            </div>
            <div class="meta">
              <span v-for="d in detail.memory.dimensions" :key="d" :class="dimClass(d)">{{ d }}</span>
              <span v-if="detail.memory.custom_flag" class="flag">标记: {{ detail.memory.custom_flag }}</span>
            </div>
            <p v-if="detail.memory.notes" class="note-text">备注: {{ detail.memory.notes }}</p>
          </section>

          <section v-if="detail.sources.length" class="detail-block">
            <h4>溯源引用</h4>
            <ul class="src-list">
              <li v-for="(s, i) in detail.sources" :key="i" class="src">
                <span class="src-type">{{ s.source_type }}</span>
                <code class="inline">{{ s.source_ref }}</code>
              </li>
            </ul>
          </section>

          <section v-if="detail.archive_chain.length" class="detail-block">
            <h4>归档链（supersession）</h4>
            <ul class="chain">
              <li v-for="(a, i) in detail.archive_chain" :key="i" class="chain-item">
                <code class="inline">{{ (a as Record<string, unknown>).memory_id }}</code>
                <span class="chain-ts">{{ fmtTs(String((a as Record<string, unknown>).archived_at || '')) }}</span>
              </li>
            </ul>
          </section>

          <section class="detail-block">
            <h4>操作</h4>
            <template v-if="detail.memory.status !== 'rejected'">
              <textarea v-model="reason" placeholder="拒绝原因（可选）" rows="2" />
              <button class="danger" :disabled="busy" @click="reject">标记为拒绝</button>
            </template>
            <button v-else class="btn" :disabled="busy" @click="unreject">恢复为有效</button>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 维度复选（2026-08-13） */
.dim-filter { flex-wrap: wrap; }
.dim-select { display: flex; align-items: center; gap: 8px; }
.dim-label { font-size: 12px; color: var(--text-muted); white-space: nowrap; }
.dim-checks { display: flex; flex-wrap: wrap; gap: 6px; }
.chk-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-muted);
  transition: all 0.15s;
}
.chk-chip input { margin: 0; }
.chk-chip.on { border-color: var(--brand); background: var(--brand-soft); color: var(--brand-text); }
.chk-chip-label { white-space: nowrap; }
.dim-muted { font-size: 12px; color: var(--text-faint); }

.split {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.list-pane {
  flex: 1;
  min-width: 0;
}
.detail-pane {
  flex: 1;
  min-width: 0;
  position: sticky;
  top: 0;
}

.mem-tbl {
  table-layout: fixed;
}
.col-idx {
  width: 44px;
  text-align: center;
}
.col-dot {
  width: 40px;
  text-align: center;
}
.mem-tbl tbody tr {
  cursor: pointer;
}
.mem-tbl tbody tr.active {
  background: var(--brand-soft);
}
.mem-tbl tbody tr.active td:first-child {
  border-left: 3px solid var(--brand);
}
.idx {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.content-cell {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 优先级状态圆圈：有效绿 / 其余红 */
.prio-dot {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  vertical-align: middle;
}
.prio-dot.ok {
  background: var(--success);
}
.prio-dot.err {
  background: var(--danger);
}

.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.detail-head h3 {
  margin: 0;
  font-size: var(--fs-lg);
  font-weight: 600;
}
.detail-block {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  background: var(--surface);
  margin-bottom: 16px;
  box-shadow: var(--shadow-sm);
}
.detail-block h4 {
  margin: 0 0 10px;
  font-size: var(--fs-md);
  font-weight: 600;
}
.detail-block .content {
  margin-bottom: 12px;
}
.detail-block textarea {
  width: 100%;
  margin-bottom: 10px;
}
.detail-block .danger,
.detail-block .btn {
  width: 100%;
}
</style>