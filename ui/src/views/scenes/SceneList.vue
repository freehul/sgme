<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listScenes, setSceneStatus, type Scene } from '../../api/knowledge'
import { ApiError } from '../../api/client'

const rows = ref<Scene[]>([])
const total = ref(0)
const page = ref(1)
const limit = 20
const statusFilter = ref('')
const sort = ref('heat')
const loading = ref(false)
const error = ref('')

// 右侧详情（master-detail，列表已含完整 content，直接取选中项）
const selected = ref<Scene | null>(null)
const busy = ref(false)
const actionError = ref('')

const STATUS_LABEL: Record<string, string> = {
  active: '有效',
  rejected: '已拒绝',
  expired: '已过期',
  archived: '已归档',
}
const SORT_OPTIONS = [
  { value: 'heat', label: '热度' },
  { value: 'updated_at', label: '更新时间' },
  { value: 'created_at', label: '创建时间' },
]

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}

// 场景标题提取：content 首行/首段才有可读性，title 是 merged_xxx 无意义
function sceneTitle(sc: Scene): string {
  const first = sc.content.split('\n')[0]?.trim() || ''
  return first.length > 80 ? first.slice(0, 80) + '…' : first || sc.title
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listScenes({
      page: page.value,
      limit,
      status: statusFilter.value || undefined,
      sort: sort.value,
    })
    rows.value = data.items
    total.value = data.total
    // 列表刷新后若选中的场景已不在当前页，清空右侧详情
    if (selected.value && !data.items.some((s) => s.scene_id === selected.value!.scene_id)) {
      selected.value = null
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function apply() {
  page.value = 1
  load()
}

function select(sc: Scene) {
  selected.value = sc
  actionError.value = ''
}

async function changeStatus(status: string) {
  if (!selected.value) return
  const sc = selected.value
  busy.value = true
  actionError.value = ''
  try {
    await setSceneStatus(sc.scene_id, status)
    await load()
    // 重新选中（load 后 rows 已更新，按 id 找回最新状态）
    selected.value = rows.value.find((r) => r.scene_id === sc.scene_id) || null
  } catch (e) {
    actionError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="scenes">
    <div class="head">
      <h2>场景列表</h2>
      <div class="filters">
        <select v-model="statusFilter" @change="apply">
          <option value="">全部状态</option>
          <option v-for="(label, key) in STATUS_LABEL" :key="key" :value="key">{{ label }}</option>
        </select>
        <select v-model="sort" @change="apply">
          <option v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div class="split">
      <!-- 左侧：数据列表（序号 / 内容） -->
      <div class="list-pane">
        <p v-if="loading" class="empty">加载中…</p>
        <p v-else-if="!rows.length" class="empty">暂无场景。</p>
        <template v-else>
          <table class="tbl scene-tbl">
            <thead>
              <tr>
                <th class="col-idx">#</th>
                <th class="col-dot">状态</th>
                <th>标题</th>
                <th>关联</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(sc, i) in rows"
                :key="sc.scene_id"
                :class="{ active: selected && selected.scene_id === sc.scene_id }"
                @click="select(sc)"
              >
                <td class="col-idx idx">{{ (page - 1) * limit + i + 1 }}</td>
                <td class="col-dot">
                  <span
                    class="prio-dot"
                    :class="sc.status === 'active' ? 'ok' : 'err'"
                    :title="STATUS_LABEL[sc.status] || sc.status"
                  />
                </td>
                <td class="content-cell">{{ sceneTitle(sc) }}</td>
                <td class="rel-cell">
                  <span v-if="sc.memories_count" class="rel-tag">{{ sc.memories_count }} 记忆</span>
                  <span v-else class="dim-muted">—</span>
                </td>
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
        <div v-if="!selected" class="empty">从左侧选择一个场景查看详情</div>
        <template v-else>
          <div class="detail-head">
            <h3>场景详情</h3>
            <span class="status" :class="selected.status">
              <span class="status-dotx" :class="selected.status" />{{ STATUS_LABEL[selected.status] || selected.status }}
            </span>
          </div>

          <p v-if="actionError" class="error">{{ actionError }}</p>

          <section class="detail-block">
            <pre class="content">{{ selected.content }}</pre>
            <div class="meta">
              <span class="heat">🔥 热度 {{ selected.heat }}</span>
              <span>{{ selected.memories_count }} 记忆</span>
              <span>创建: {{ fmtTs(selected.created_at) }}</span>
              <span>更新: {{ fmtTs(selected.updated_at) }}</span>
            </div>
          </section>

          <section v-if="selected.related_memories?.length" class="detail-block">
            <h4>关联记忆（{{ selected.memories_count }}）</h4>
            <ul class="rel-list">
              <li v-for="rm in selected.related_memories" :key="rm.memory_id" class="rel-item">
                <p class="rel-content">{{ rm.content }}</p>
                <div class="meta">
                  <span v-for="d in rm.dimensions" :key="d" class="dim-tag">{{ d }}</span>
                  <span v-if="!rm.dimensions.length" class="dim-muted">—</span>
                </div>
              </li>
            </ul>
          </section>

          <section class="detail-block">
            <h4>操作</h4>
            <div class="status-op">
              <label>切换状态</label>
              <select :value="selected.status" :disabled="busy" @change="changeStatus(($event.target as HTMLSelectElement).value)">
                <option v-for="(label, key) in STATUS_LABEL" :key="key" :value="key">{{ label }}</option>
              </select>
            </div>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
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

.scene-tbl {
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
/* 状态圆点：有效绿 / 其余红（对齐记忆浏览） */
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
.scene-tbl tbody tr {
  cursor: pointer;
}
.scene-tbl tbody tr.active {
  background: var(--brand-soft);
}
.scene-tbl tbody tr.active td:first-child {
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
.status-op {
  display: flex;
  align-items: center;
  gap: 10px;
}
.status-op label {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
</style>