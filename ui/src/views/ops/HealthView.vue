<script setup lang="ts">
// 运维健康总览：存储体检（DB/WAL/raw/备份）+ 9.6 场景层下线就绪
// 数据源 GET /v1/admin/ops/health-overview（Admin Key；后端未就绪时显示空态，不白屏）
import { computed, onMounted, ref } from 'vue'
import { getHealthOverview, getRefineEfficiency, getRefineStatus, type OpsHealthOverview, type RefineEfficiency, type RefineStatus } from '../../api/ops'
import { getRefineRuns, type RefineRun, type RefineRuns } from '../../api/dashboard'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'
import { compatPhaseLabel, formatBytes, formatCount, isOverviewUsable } from './healthFormat'

const data = ref<OpsHealthOverview | null>(null)
const loading = ref(false)
const error = ref('')

// ---------- 提炼状态（GET /v1/admin/refine/status + 最近批次 GET /v1/admin/refine_runs） ----------
const refine = ref<RefineStatus | null>(null)
const runs = ref<RefineRuns | null>(null)
// D-80（2026-11-06）：块级成本效率（同块重复率 / 缓存命中率 / 补跑积压）
const eff = ref<RefineEfficiency | null>(null)
const refineLoading = ref(false)
const refineError = ref('')

// 阶段展示名对齐 sgme/engine/stage_map.py（L1 捕获 / L2 提取 / L3 裁决 / L4 梦境）
const STAGE_LABEL: Record<string, string> = {
  l1_extraction: 'L2 提取',
  l1_conflict: 'L3 裁决',
  l2_scene: '遗留场景聚合',
  // D-79（2026-11-05）：Tier0 已砍，仅历史 refine_runs 行展示用
  tier0_summary: '用户画像（历史）',
}
const RUN_STATUS_LABEL: Record<string, string> = { running: '运行中', ok: '成功', error: '失败' }

// 存储体检卡片：三库体积 + raw（数+字节）+ 备份（数+字节）
const storageCards = computed(() => {
  const s = data.value?.storage
  if (!s) return []
  return [
    { key: 'memory_db', label: 'memory.db', value: formatBytes(s.memory_db_bytes) },
    {
      key: 'memory_wal',
      label: s.memory_wal_alert ? 'memory.db WAL ⚠ 超阈值' : 'memory.db WAL',
      value: formatBytes(s.memory_wal_bytes),
      warn: !!s.memory_wal_alert,
    },
    { key: 'wiki_db', label: 'wiki.db', value: formatBytes(s.wiki_db_bytes) },
    { key: 'raw_files', label: 'raw 文件数', value: `${formatCount(s.raw_files)} 个` },
    { key: 'raw_bytes', label: 'raw 体积', value: formatBytes(s.raw_bytes) },
    { key: 'backup_snapshots', label: '备份快照数', value: `${formatCount(s.backup_snapshots)} 份` },
    { key: 'backup_bytes', label: '备份体积', value: formatBytes(s.backup_bytes) },
  ]
})

// 9.6 就绪卡片：场景层三表行数 + 两类规则数
const sceneCards = computed(() => {
  const s = data.value?.scenes
  if (!s) return []
  return [
    { key: 'scenes', label: 'scenes 行数', value: formatCount(s.scenes) },
    { key: 'scene_vectors', label: 'scene_vectors 行数', value: formatCount(s.scene_vectors) },
    { key: 'scenes_fts', label: 'scenes_fts 行数', value: formatCount(s.scenes_fts) },
    { key: 'gone_rules', label: 'GONE 规则', value: formatCount(s.gone_rules) },
    { key: 'conflict_rules', label: '冲突规则', value: formatCount(s.conflict_rules) },
  ]
})

// 提炼队列水位（refine/status 四计数）
const refineQueue = computed(() => {
  const s = refine.value
  if (!s) return []
  return [
    { key: 'pending', label: '待提炼', value: formatCount(s.pending) },
    { key: 'completed', label: '已完成', value: formatCount(s.completed) },
    { key: 'failed', label: '失败', value: formatCount(s.failed) },
    { key: 'total', label: 'raw 总数', value: formatCount(s.total) },
  ]
})

// 秒级时长 → 人话（水位年龄 / 批次耗时共用）
function ageText(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '—'
  if (sec < 60) return Math.round(sec) + ' 秒'
  if (sec < 3600) return Math.round(sec / 60) + ' 分钟'
  if (sec < 86400) return (sec / 3600).toFixed(1) + ' 小时'
  return (sec / 86400).toFixed(1) + ' 天'
}

const watermarkText = computed(() => {
  const wm = refine.value?.watermark
  if (!wm) return '—'
  if (wm.watermark_age_sec == null || wm.watermark_age_sec < 0) return '从未提炼'
  return '距上次 ' + ageText(wm.watermark_age_sec)
})

// 阶段展示名：优先取后端行内 stage_label（stage_map 权威口径，2.0.4 起返回），
// 旧后端无该字段时回退上面本地映射
function stageLabel(r: RefineRun): string {
  const label = (r as unknown as { stage_label?: string }).stage_label
  return label || STAGE_LABEL[r.stage] || r.stage
}

// 单批次耗时：finished_at - started_at（运行中/缺字段时给状态文案）
function runDuration(r: RefineRun): string {
  const start = Date.parse(r.started_at)
  const end = r.finished_at ? Date.parse(r.finished_at) : NaN
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return r.status === 'running' ? '运行中' : '—'
  }
  const sec = Math.max(0, (end - start) / 1000)
  if (sec < 60) return sec.toFixed(1) + 's'
  if (sec < 3600) return Math.floor(sec / 60) + 'm' + Math.round(sec % 60) + 's'
  return (sec / 3600).toFixed(1) + 'h'
}

// D-80 块级成本卡片：重复率 >1.5 或补跑积压 >0 标黄（见设计稿 §3.12 判读口径）
const effCards = computed(() => {
  const e = eff.value
  if (!e || !e.available) return []
  const ratio = Number(e.repeat_ratio || 0)
  const hitRate = Number(e.cache_hit_rate || 0)
  return [
    {
      key: 'repeat_ratio',
      label: '同块重复率',
      value: ratio ? ratio.toFixed(2) + '×' : '—',
      warn: ratio > 1.5,
    },
    {
      key: 'cache_hit_rate',
      label: '缓存命中率',
      value: hitRate ? (hitRate * 100).toFixed(1) + '%' : '—',
    },
    { key: 'reused_tokens', label: '缓存省下 token', value: formatCount(e.reused_tokens) },
    {
      key: 'replay_pending',
      label: '失败块待补跑',
      value: formatCount(e.replay_pending),
      warn: Number(e.replay_pending) > 0,
    },
  ]
})

// 单批次缓存标记：命中时显示复用 token，未命中显示「实跑」
function runCacheText(r: RefineRun): string {
  if (r.cache_hit) return '命中 · 省 ' + formatCount(r.reused_tokens || 0)
  return '实跑'
}

function runChunkShort(r: RefineRun): string {
  const sha = r.chunk_sha
  return sha ? sha.slice(0, 8) : '—'
}
// 两个端点独立容错：队列水位是核心（refine/status），最近批次缺失不影响出图
async function loadRefine() {
  refineLoading.value = true
  refineError.value = ''
  try {
    refine.value = await getRefineStatus()
  } catch (e) {
    refine.value = null
    refineError.value = e instanceof ApiError ? e.message : String(e)
  }
  try {
    runs.value = await getRefineRuns({ limit: 5 })
  } catch {
    runs.value = null
  }
  // D-80 效率端点独立容错：旧后端没有该端点时页面照常显示其余信息
  try {
    eff.value = await getRefineEfficiency(7)
  } catch {
    eff.value = null
  }
  refineLoading.value = false
}

const phaseLabel = computed(() => compatPhaseLabel(data.value?.scenes.compat_phase))
const blockers = computed(() => data.value?.scenes.blockers || [])
const manualActions = computed(() => data.value?.manual_actions || [])

// 兼容期窗口：stable_since + days_left（后端可选字段；缺省不渲染）
const windowLabel = computed(() => {
  const s = data.value?.scenes
  if (!s || !s.stable_since) return ''
  const since = `起算 ${s.stable_since}`
  const left = s.days_left
  if (typeof left !== 'number' || !Number.isFinite(left)) return since
  return `${since} · ${left >= 0 ? `剩余 ${left} 天` : `已过期 ${Math.abs(left)} 天`}`
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await getHealthOverview()
    // storage/scenes 两段缺失视为不可用（不展示一堆 0 误导），走空态
    data.value = isOverviewUsable(r) ? r : null
  } catch (e) {
    // 端点未上线 / 服务不可达 / 鉴权失败：清空并显示友好提示
    data.value = null
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  void loadRefine()
})
</script>

<template>
  <div class="health-view">
    <div class="head">
      <h2>健康总览 <span class="sub">存储体检 + 9.6 场景层下线就绪（只读）</span></h2>
      <div class="filters">
        <span v-if="data" class="generated">生成于 {{ fmtTs(data.generated_at) }}</span>
        <button class="btn btn-sm" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px">
      <p class="error" style="margin: 0">{{ error }}</p>
      <button class="btn btn-sm" @click="load">重试</button>
    </div>

    <!-- 首次加载显示加载态；已加载过则保留旧数据（刷新失败只加错误横幅，不清空页面） -->
    <p v-if="loading && !data" class="empty">加载中…</p>
    <p v-else-if="!data && !error" class="empty">
      暂无健康数据——接口可能尚未上线，或返回体缺少 storage / scenes 段；后端就绪后点「刷新」即可。
    </p>

    <!-- ⓪ 提炼状态（T-169 refine/status：队列水位 + 最近失败；最近批次取自 refine_runs） -->
    <section class="panel">
      <h3>
        提炼状态
        <span class="sub">待提炼队列 / 水位 / 最近批次</span>
      </h3>
      <div class="ready-line">
        <button class="btn btn-sm" :disabled="refineLoading" @click="loadRefine">
          {{ refineLoading ? '刷新中…' : '刷新状态' }}
        </button>
        <span v-if="refine" class="kv">水位：<b>{{ watermarkText }}</b></span>
        <span v-if="refine?.watermark?.last_refined_at" class="kv">
          上次提炼：<b>{{ fmtTs(refine.watermark.last_refined_at) }}</b>
        </span>
      </div>

      <p v-if="refineError" class="error">
        提炼状态读取失败：{{ refineError }}（端点未上线、鉴权失败或服务不可达）
      </p>

      <template v-if="refine">
        <div class="stat-grid">
          <div v-for="c in refineQueue" :key="c.key" class="stat-card">
            <span class="v">{{ c.value }}</span>
            <span class="l">{{ c.label }}</span>
          </div>
        </div>

        <!-- D-80（2026-11-06）：块级成本效率 —— 重复率 / 缓存命中 / 补跑积压 -->
        <template v-if="effCards.length">
          <h4 class="blockers-title">
            块级成本 <span class="sub">近 7 天 · 同块重复率 / 缓存命中 / 补跑积压</span>
          </h4>
          <div class="stat-grid">
            <div
              v-for="c in effCards"
              :key="c.key"
              class="stat-card"
              :class="{ warn: (c as any).warn }"
            >
              <span class="v">{{ c.value }}</span>
              <span class="l">{{ c.label }}</span>
            </div>
          </div>
          <p class="desc">
            重复率 ≈1 表示每块只算一次，&gt;1.5 说明仍在重复重发；命中率 10–40% 属正常（新内容总要跑一遍）。
          </p>
        </template>

        <div v-if="refine.last_failure" class="fail-box">
          <div class="fail-head">
            <span class="status error">最近一次失败</span>
            <span class="kv">阶段 <b>{{ STAGE_LABEL[refine.last_failure.stage] || refine.last_failure.stage }}</b></span>
            <span class="kv">文件 <code class="mono">{{ refine.last_failure.file_id }}</code></span>
            <span class="kv">开始于 <b>{{ fmtTs(refine.last_failure.started_at) }}</b></span>
          </div>
          <p class="fail-msg">{{ refine.last_failure.error || '（后端未回错误详情）' }}</p>
        </div>
        <p v-else class="desc">最近没有失败批次。</p>
      </template>

      <template v-if="runs && runs.items.length">
        <h4 class="blockers-title">
          最近批次 <span class="sub">{{ runs.items.length }} 条 / 累计 {{ formatCount(runs.total) }} 条提炼记录</span>
        </h4>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>开始时间</th><th>文件</th><th>阶段</th><th>状态</th>
                <th>缓存</th><th>块指纹</th><th>耗时</th><th>错误</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in runs.items" :key="r.run_id">
                <td>{{ fmtTs(r.started_at) }}</td>
                <td class="mono">{{ r.file_id }}</td>
                <td>{{ stageLabel(r) }}</td>
                <td><span class="st" :class="r.status">{{ RUN_STATUS_LABEL[r.status] || r.status }}</span></td>
                <td class="num">{{ runCacheText(r) }}</td>
                <td class="mono">{{ runChunkShort(r) }}</td>
                <td class="num">{{ runDuration(r) }}</td>
                <td class="run-err">{{ r.error || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <p v-else-if="!refineError" class="desc">暂无提炼记录（refine_runs 为空，或端点尚未返回数据）。</p>
    </section>

    <template v-if="data">
      <!-- ① 存储体检 -->
      <section class="panel">
        <h3>存储体检 <span class="sub">磁盘占用与文件规模</span></h3>
        <div class="stat-grid">
          <div v-for="c in storageCards" :key="c.key" class="stat-card" :class="{ warn: (c as any).warn }">
            <span class="v">{{ c.value }}</span>
            <span class="l">{{ c.label }}</span>
          </div>
        </div>
        <p class="desc">
          数据目录：<span class="mono">{{ data.storage.data_dir || '—' }}</span>
        </p>
      </section>

      <!-- ② 9.6 就绪 -->
      <section class="panel">
        <h3>9.6 就绪 <span class="sub">场景层下线（D-53 / D-64）</span></h3>
        <div class="ready-line">
          <span class="ready-badge" :class="data.scenes.ready ? 'ok' : 'err'">
            <span class="dot" />{{ data.scenes.ready ? '就绪' : '未就绪' }}
          </span>
          <span class="kv">兼容期阶段：<b>{{ phaseLabel }}</b></span>
          <span v-if="windowLabel" class="kv">兼容期窗口：<b>{{ windowLabel }}</b></span>
          <span class="kv">enforce（语义变更 409）：<b>{{ data.scenes.enforce ? '开' : '关' }}</b></span>
          <span class="kv">enforce_gone（下线端点 410）：<b>{{ data.scenes.enforce_gone ? '开' : '关' }}</b></span>
        </div>
        <div class="stat-grid">
          <div v-for="c in sceneCards" :key="c.key" class="stat-card">
            <span class="v">{{ c.value }}</span>
            <span class="l">{{ c.label }}</span>
          </div>
        </div>
        <h4 class="blockers-title">阻塞项（{{ blockers.length }}）</h4>
        <ul v-if="blockers.length" class="blockers">
          <li v-for="(b, i) in blockers" :key="i">{{ b }}</li>
        </ul>
        <p v-else class="desc">无阻塞项——场景层下线前置条件已满足。</p>

        <template v-if="manualActions.length">
          <h4 class="blockers-title">人工动作清单（{{ manualActions.length }}）</h4>
          <ol class="blockers">
            <li v-for="(a, i) in manualActions" :key="i">{{ a }}</li>
          </ol>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.generated {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
/* 本页两块 panel 之间补间距（全局 .panel 无外边距，其它页面靠页面自身排列） */
.health-view > .panel + .panel {
  margin-top: var(--space-4);
}
.ready-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-3);
  margin-bottom: var(--space-3);
}
.ready-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 12px;
  border-radius: 999px;
  font-size: var(--fs-sm);
  font-weight: 600;
  white-space: nowrap;
}
.ready-badge .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}
.ready-badge.ok {
  background: var(--success-soft);
  color: var(--success);
}
.ready-badge.err {
  background: var(--danger-soft);
  color: var(--danger);
}
.kv {
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.kv b {
  color: var(--text);
  font-weight: 600;
}
/* 提炼状态：失败详情 + 批次表 */
.fail-box {
  margin-top: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-left: 3px solid var(--danger);
  border-radius: var(--radius);
  background: var(--danger-soft);
}
.fail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-3);
}
.fail-head code {
  background: var(--surface);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}
.fail-msg {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-sm);
  color: var(--danger);
  font-family: var(--font-mono);
  word-break: break-word;
}
.fail-head .status { background: var(--surface); }
.tbl-wrap { overflow-x: auto; }
.tbl-wrap .tbl { min-width: 640px; }
.tbl-wrap .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.tbl-wrap .run-err {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  max-width: 320px;
  word-break: break-word;
}

.blockers-title {
  margin: var(--space-3) 0 var(--space-2);
  font-size: var(--fs-md);
  font-weight: 600;
}
.blockers-title .sub {
  font-weight: 400;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-left: var(--space-2);
}
.blockers {
  margin: 0;
  padding-left: 1.4em;
  font-size: var(--fs-sm);
  color: var(--text);
}
.blockers li {
  margin: 2px 0;
}
/* F4：WAL 超阈值徽标（>200MB 或 > memory.db 50%） */
.stat-card.warn {
  background: var(--warn-soft);
  color: var(--warn);
}
</style>
