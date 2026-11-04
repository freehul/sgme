<script setup lang="ts">
// 运维健康总览：存储体检（DB/WAL/raw/备份）+ 9.6 场景层下线就绪
// 数据源 GET /v1/admin/ops/health-overview（Admin Key；后端未就绪时显示空态，不白屏）
import { computed, onMounted, ref } from 'vue'
import { getHealthOverview, type OpsHealthOverview } from '../../api/ops'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'
import { compatPhaseLabel, formatBytes, formatCount, isOverviewUsable } from './healthFormat'

const data = ref<OpsHealthOverview | null>(null)
const loading = ref(false)
const error = ref('')

// 存储体检卡片：三库体积 + raw（数+字节）+ 备份（数+字节）
const storageCards = computed(() => {
  const s = data.value?.storage
  if (!s) return []
  return [
    { key: 'memory_db', label: 'memory.db', value: formatBytes(s.memory_db_bytes) },
    { key: 'memory_wal', label: 'memory.db WAL', value: formatBytes(s.memory_wal_bytes) },
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

onMounted(load)
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

    <template v-if="data">
      <!-- ① 存储体检 -->
      <section class="panel">
        <h3>存储体检 <span class="sub">磁盘占用与文件规模</span></h3>
        <div class="stat-grid">
          <div v-for="c in storageCards" :key="c.key" class="stat-card">
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
.blockers-title {
  margin: var(--space-3) 0 var(--space-2);
  font-size: var(--fs-md);
  font-weight: 600;
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
</style>
