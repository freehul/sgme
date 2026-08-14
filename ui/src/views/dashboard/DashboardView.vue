<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getHealth,
  getStats,
  getRefineRuns,
  getDreamReports,
  getEvents,
  type HealthStatus,
  type Stats,
  type RefineRuns,
  type DreamReports,
  type Events,
  type SignalEvent,
} from '../../api/dashboard'
import { ApiError } from '../../api/client'

const tab = ref<'overview' | 'anomaly'>('overview')

const health = ref<HealthStatus | null>(null)
const stats = ref<Stats | null>(null)
const refineRuns = ref<RefineRuns | null>(null)
const dreamReports = ref<DreamReports | null>(null)
const events = ref<Events | null>(null)
const loading = ref(true)
const error = ref('')

// 异常日志筛选
const logLevel = ref('')
const logSource = ref('')

const STAGE_LABEL: Record<string, string> = {
  l1_extraction: 'L1 抽取',
  l1_conflict: 'L1.5 冲突提炼',
  l2_scene: 'L2 场景',
  tier0_summary: 'Tier0 摘要',
}
const RUN_STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  ok: '成功',
  error: '失败',
}

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}

function watermarkAge(sec: number | null | undefined): string {
  if (sec == null || sec < 0) return '暂无提炼'
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${(sec / 60).toFixed(0)}m`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`
  return `${(sec / 86400).toFixed(1)}d`
}

// token 汇总（基于最近 refine_runs）
const tokenSummary = computed({
  get: () => {
    const items = refineRuns.value?.items || []
    return items.reduce(
      (acc, it) => ({
        prompt: acc.prompt + (it.prompt_tokens || 0),
        completion: acc.completion + (it.completion_tokens || 0),
        total: acc.total + (it.total_tokens || 0),
      }),
      { prompt: 0, completion: 0, total: 0 },
    )
  },
  set: () => {},
})

// ---------- 管线概览（对齐参考：进度条 + token + 模型链 + 健康） ----------
const pipelineStages = computed(() => {
  const s = stats.value
  if (!s) return []
  const max = Math.max(s.raw_files.total, 1)
  return [
    { label: 'L0 原始待提炼', value: `${s.raw_files.new} 条`, pct: (s.raw_files.new / max) * 100, color: '#3B82F6' },
    { label: 'L1 提炼成功', value: `${s.raw_files.refined} 条`, pct: (s.raw_files.refined / max) * 100, color: '#6366F1' },
    { label: 'L1.5 冲突/失败', value: `${s.raw_files.error} 条`, pct: (s.raw_files.error / max) * 100, color: '#F59E0B' },
    { label: 'L2 场景', value: `${s.dimension_distribution.length} 维度`, pct: 0, color: '#10B981' },
  ]
})

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [h, s, r, d, ev] = await Promise.all([
      getHealth(),
      getStats(),
      getRefineRuns({ page: 1, limit: 100 }),
      getDreamReports({ page: 1, limit: 10 }),
      getEvents({ limit: 200 }),
    ])
    health.value = h
    stats.value = s
    refineRuns.value = r
    dreamReports.value = d
    events.value = ev
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// ---------- 异常日志（对齐参考：来自失败 refine run + anomaly_warn 事件） ----------
interface AnomalyRow {
  id: string
  time: string
  level: string
  source: string
  summary: string
  state: string
}

const anomalyRows = computed<AnomalyRow[]>(() => {
  const rows: AnomalyRow[] = []
  ;(refineRuns.value?.items || [])
    .filter((r) => r.status === 'error' || !!r.error)
    .forEach((r) => {
      rows.push({
        id: r.run_id,
        time: r.started_at,
        level: 'ERROR',
        source: r.stage,
        summary: r.error || `提炼失败（${r.file_id}）`,
        state: '未处理',
      })
    })
  ;(events.value?.events || [])
    .filter((e) => e.type === 'anomaly_warn')
    .forEach((e) => {
      const p = (e.payload || {}) as Record<string, unknown>
      rows.push({
        id: e.event_id,
        time: e.ts,
        level: 'WARN',
        source: e.source,
        summary: String(p.message || e.source || '异常告警'),
        state: '未处理',
      })
    })
  return rows.sort((a, b) => (a.time < b.time ? 1 : -1))
})

const filteredAnomalies = computed(() => {
  return anomalyRows.value.filter((r) => {
    if (logLevel.value && r.level !== logLevel.value) return false
    if (logSource.value && r.source !== logSource.value) return false
    return true
  })
})

const anomalySources = computed(() => [...new Set(anomalyRows.value.map((r) => r.source))])

onMounted(loadAll)
</script>

<template>
  <div class="dashboard">
    <div class="head">
      <h2>提炼监控</h2>
      <button class="btn" :disabled="loading" @click="loadAll">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <!-- 标签页导航（对齐参考 refineMonitorPage） -->
    <div class="tabs-line">
      <button :class="{ active: tab === 'overview' }" @click="tab = 'overview'">管线概览</button>
      <button :class="{ active: tab === 'anomaly' }" @click="tab = 'anomaly'">
        异常日志
        <span v-if="anomalyRows.length" class="badge-count">{{ anomalyRows.length }}</span>
      </button>
    </div>

    <!-- ==================== 管线概览 ==================== -->
    <template v-if="tab === 'overview'">
      <!-- 1. 提炼管线进度 → 4 信息卡横向排列（与系统健康等高） -->
      <section v-if="stats" class="panel">
        <h3>提炼管线进度</h3>
        <div class="stage-cards">
          <div v-for="s in pipelineStages" :key="s.label" class="stage-card">
            <div class="stage-card-val" :style="{ color: s.color }">{{ s.value }}</div>
            <div class="stage-card-label">{{ s.label }}</div>
            <div class="bar"><i :style="{ background: s.color, width: s.pct + '%' }" /></div>
          </div>
        </div>
      </section>

      <!-- 2+5. 系统健康 + Dream 日报 同一排 -->
      <div class="monitor-grid">
        <section v-if="health" class="panel">
          <h3>系统健康 <span class="ver">v{{ health.version }}</span></h3>
          <div class="badge-row">
            <div class="badge" :class="health.llm.available ? 'ok' : 'err'">
              <span class="dot" /> LLM
              <span class="sub">{{ health.llm.provider || health.llm.model || health.llm.error || '未知' }}</span>
            </div>
            <div class="badge" :class="health.vector.available ? 'ok' : 'err'">
              <span class="dot" /> 向量
              <span class="sub">{{ health.vector.engine }}（{{ health.vector.memory_vectors + health.vector.scene_vectors }} 条）</span>
            </div>
            <div class="badge" :class="health.refinement.heartbeat_ok ? 'ok' : 'err'">
              <span class="dot" /> 提炼心跳
              <span class="sub">{{ health.refinement.heartbeat_ok ? '正常' : '停摆' }}</span>
            </div>
            <div class="badge" :class="health.refinement.stalled ? 'warn' : 'ok'">
              提炼水位 <span class="sub">{{ watermarkAge(health.refinement.watermark_age_sec) }}</span>
            </div>
            <div class="badge">
              队列 <span class="sub">{{ health.refinement.queue_depth }} 待提炼</span>
            </div>
          </div>
          <p v-if="health.vector.reason" class="hint">{{ health.vector.reason }}</p>
        </section>

        <section v-if="dreamReports" class="panel">
          <h3>Dream 日报 <span class="sub">共 {{ dreamReports.total }} 篇</span></h3>
          <div v-if="!dreamReports.reports.length" class="empty">暂无日报</div>
          <div v-else class="dream-list">
            <div v-for="d in dreamReports.reports" :key="d.date" class="dream-card">
              <div class="dream-date">{{ d.date }}</div>
              <div class="dream-meta">记忆 {{ d.memory_count }} / 场景 {{ d.scene_count }} / 提炼 {{ d.refined_count }} / 失败 {{ d.error_count }}</div>
              <p v-if="d.summary" class="dream-summary">{{ d.summary }}</p>
            </div>
          </div>
        </section>
      </div>

      <!-- 3. 数据概览 -->
      <section v-if="stats" class="panel">
        <h3>数据概览</h3>
        <div class="stat-grid">
          <div class="stat-card"><span class="v">{{ stats.memories.total }}</span><span class="l">记忆总数</span></div>
          <div class="stat-card"><span class="v">{{ stats.raw_files.total }}</span><span class="l">原始文件</span></div>
          <div class="stat-card"><span class="v">{{ stats.raw_files.refined }}</span><span class="l">已提炼</span></div>
          <div class="stat-card"><span class="v">{{ stats.raw_files.new }}</span><span class="l">待提炼</span></div>
          <div class="stat-card"><span class="v">{{ stats.raw_files.error }}</span><span class="l">失败</span></div>
          <div class="stat-card"><span class="v">{{ stats.agents.length }}</span><span class="l">注册 Agent</span></div>
        </div>
        <div v-if="stats.dimension_distribution.length" class="dims">
          <div v-for="d in stats.dimension_distribution" :key="d.id" class="dim">
            <span class="dim-name">{{ d.display_name }}（{{ d.id }}）</span>
            <div class="bar"><i :style="{ width: (d.count / Math.max(...stats.dimension_distribution.map(x => x.count)) * 100) + '%' }" /></div>
            <span class="dim-count">{{ d.count }}</span>
          </div>
        </div>
      </section>

      <!-- 4. Token 用量 + 提炼记录 -->
      <section v-if="refineRuns" class="panel">
        <h3>
          Token 用量统计
          <span class="sub">prompt {{ tokenSummary.prompt }} / completion {{ tokenSummary.completion }} / total {{ tokenSummary.total }} tokens</span>
        </h3>
        <table class="tbl">
          <thead>
            <tr><th>时间</th><th>文件</th><th>阶段</th><th>状态</th><th>记忆数</th><th>Tokens</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in refineRuns.items.slice(0, 20)" :key="r.run_id">
              <td>{{ fmtTs(r.started_at) }}</td>
              <td class="mono">{{ r.file_id }}</td>
              <td>{{ STAGE_LABEL[r.stage] || r.stage }}</td>
              <td><span class="st" :class="r.status">{{ RUN_STATUS_LABEL[r.status] || r.status }}</span></td>
              <td>{{ r.memories_count }}</td>
              <td>{{ r.total_tokens }}</td>
            </tr>
            <tr v-if="!refineRuns.items.length"><td colspan="6" class="empty">暂无提炼记录</td></tr>
          </tbody>
        </table>
      </section>
    </template>

    <!-- ==================== 异常日志 ==================== -->
    <template v-else>
      <section class="panel">
        <div class="filter-bar">
          <label>日志级别
            <select v-model="logLevel">
              <option value="">全部级别</option>
              <option value="ERROR">ERROR</option>
              <option value="WARN">WARN</option>
            </select>
          </label>
          <label>来源
            <select v-model="logSource">
              <option value="">全部来源</option>
              <option v-for="s in anomalySources" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
        </div>

        <table class="tbl">
          <thead>
            <tr><th>时间</th><th>级别</th><th>来源</th><th>内容摘要</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in filteredAnomalies" :key="r.id">
              <td>{{ fmtTs(r.time) }}</td>
              <td><span class="tag" :class="r.level === 'ERROR' ? 'dimtag-red' : 'dimtag-yellow'">{{ r.level }}</span></td>
              <td class="mono">{{ r.source }}</td>
              <td>{{ r.summary }}</td>
              <td><span class="tag" :class="r.state === '未处理' ? 'dimtag-yellow' : 'dimtag-green'">{{ r.state }}</span></td>
            </tr>
            <tr v-if="!filteredAnomalies.length"><td colspan="5" class="empty">暂无异常日志</td></tr>
          </tbody>
        </table>
        <p class="desc">异常日志由失败提炼记录与 anomaly_warn 事件实时汇总。</p>
      </section>
    </template>

    <!-- 事件流（两侧共用） -->
    <section v-if="events" class="panel">
      <h3>事件流</h3>
      <ul class="ev-list">
        <li v-for="e in events.events.slice(0, 30)" :key="e.event_id" class="ev">
          <span class="ev-type" :class="e.type">{{ e.type }}</span>
          <span class="ev-src">{{ e.source }}</span>
          <span class="ev-ts">{{ fmtTs(e.ts) }}</span>
        </li>
        <li v-if="!events.events.length" class="empty">暂无事件</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.monitor-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
  align-items: start;
}
@media (max-width: 900px) {
  .monitor-grid {
    grid-template-columns: 1fr;
  }
}
.tabs-line {
  display: flex;
  gap: 24px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 20px;
}
.tabs-line button {
  padding: 10px 2px;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 15px;
  font-weight: 500;
  color: var(--text-muted);
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tabs-line button.active {
  color: #3B82F6;
  border-bottom-color: #3B82F6;
}
.badge-count {
  background: rgba(239,68,68,.12);
  color: #ef4444;
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 12px;
  font-weight: 600;
}
/* 提炼管线进度：4 信息卡横向排列（与系统健康等高） */
.stage-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.stage-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  background: var(--surface);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.stage-card-val {
  font-size: 26px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.stage-card-label { font-size: 13px; color: var(--text-muted); }
.stage-card .bar { height: 6px; background: var(--surface-muted); border-radius: 4px; overflow: hidden; margin-top: auto; }
.stage-card .bar i { display: block; height: 100%; border-radius: 4px; transition: width .4s ease; }
@media (max-width: 900px) {
  .stage-cards { grid-template-columns: repeat(2, 1fr); }
}
.filter-bar { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
.filter-bar label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-muted); }
.filter-bar select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); }
</style>