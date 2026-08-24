<script setup lang="ts">
// ST-35 T-102：用户画像页新增「性格参考」区块——MBTI 轨迹 + 特质倾向 + 月度洞察报告
// 数据源：/v1/admin/persona/*（routes_persona.py）
// 设计原则：措辞用「倾向」不用标签判决（Backlog ST-35 AC）
import { computed, onMounted, ref } from 'vue'
import {
  addMbti, calibrate, getMbti, listReports, listTraits,
  type MbtiRecord, type PersonaReport, type PersonaTrait,
} from '../../api/persona'
import { ApiError } from '../../api/client'

const traits = ref<PersonaTrait[]>([])
const mbtiHistory = ref<MbtiRecord[]>([])
const latestMbti = ref<MbtiRecord | null>(null)
const reports = ref<PersonaReport[]>([])
const loading = ref(true)
const error = ref('')
const msg = ref('')
const busy = ref(false)
const originDebug = ref(window.location.origin)

// 自报 MBTI 表单
const mbtiDraft = ref('')
const showMbtiForm = ref(false)

const SOURCE_LABEL: Record<string, string> = {
  self_reported: '自报',
  llm_monthly: '月度校准',
}

function confidenceLevel(c: number): string {
  if (c >= 0.75) return '高'
  if (c >= 0.55) return '中'
  return '初步'
}

// 置信度条宽度（百分比）
function pct(c: number): number {
  return Math.round(c * 100)
}

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [t, m, r] = await Promise.all([listTraits(), getMbti(), listReports()])
    traits.value = t.traits
    mbtiHistory.value = m.history
    latestMbti.value = m.latest
    reports.value = r.reports
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function saveMbti() {
  const t = mbtiDraft.value.trim().toUpperCase()
  busy.value = true
  msg.value = ''
  try {
    await addMbti(t, 'WebUI 自报')
    mbtiDraft.value = ''
    showMbtiForm.value = false
    msg.value = `已记录 MBTI：${t}`
    const m = await getMbti()
    mbtiHistory.value = m.history
    latestMbti.value = m.latest
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function runCalibrate() {
  busy.value = true
  msg.value = '校准中…（LLM 分析约需数十秒）'
  try {
    await calibrate()
    msg.value = '月度校准完成'
    await load()
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// 挂载即拉取数据（修复：此前 load() 未绑定 onMounted，区块永远显示空态）
onMounted(load)
</script>

<template>
  <div class="persona-block">
    <div class="pb-head">
      <h3>性格参考 <span class="pb-sub">{{ originDebug }} · 倾向而非判决</span></h3>
      <div class="pb-actions">
        <button class="btn btn-sm" :disabled="busy" @click="showMbtiForm = !showMbtiForm">
          {{ showMbtiForm ? '取消' : '自报 MBTI' }}
        </button>
        <button class="btn btn-primary btn-sm" :disabled="busy" @click="runCalibrate">
          {{ busy ? '处理中…' : '手动校准' }}
        </button>
      </div>
    </div>

    <!-- 自报 MBTI 表单 -->
    <div v-if="showMbtiForm" class="mbti-form">
      <input
        v-model="mbtiDraft"
        placeholder="如 INTJ"
        maxlength="4"
        @keyup.enter="saveMbti"
      />
      <button class="btn btn-primary btn-sm" :disabled="busy || mbtiDraft.trim().length !== 4" @click="saveMbti">
        记录
      </button>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="msg" class="pb-msg">{{ msg }}</p>

    <div class="pb-grid">
      <!-- MBTI 轨迹时间线 -->
      <section class="pb-card">
        <h4>MBTI 轨迹 <span class="pb-dim">娱乐参考</span></h4>
        <p v-if="!mbtiHistory.length" class="pb-empty">暂无记录。可自报，或等首次月度校准。</p>
        <ol v-else class="mbti-line">
          <li v-for="r in mbtiHistory" :key="r.id">
            <b class="mono">{{ r.mbti_type }}</b>
            <span class="pb-meta">{{ SOURCE_LABEL[r.source] || r.source }} · {{ fmtTs(r.recorded_at) }}</span>
          </li>
        </ol>
      </section>

      <!-- 特质倾向列表 -->
      <section class="pb-card">
        <h4>特质倾向 <span class="pb-dim">置信度随证据累积</span></h4>
        <p v-if="!traits.length" class="pb-empty">
          暂无特质。提炼记忆时自动抽取（关键词规则），月度校准会微调置信度。
        </p>
        <div v-else class="trait-list">
          <div v-for="t in traits.slice(0, 8)" :key="t.trait_id" class="trait-row">
            <div class="trait-head">
              <span class="trait-name">{{ t.dimension }}：{{ t.value }}</span>
              <span class="pb-meta">{{ confidenceLevel(t.confidence) }}置信 · {{ t.evidence_count }} 条证据</span>
            </div>
            <div class="trait-bar"><i :style="{ width: pct(t.confidence) + '%' }" /></div>
          </div>
        </div>
      </section>

      <!-- 月度洞察报告 -->
      <section class="pb-card pb-wide">
        <h4>月度洞察报告</h4>
        <p v-if="!reports.length" class="pb-empty">暂无报告。每月自动生成，也可点「手动校准」立即分析。</p>
        <div v-for="r in reports.slice(0, 3)" :key="r.report_id" class="report-item">
          <div class="report-head">
            <b>{{ r.period }}</b>
            <span v-if="r.mbti_result" class="mono mbti-tag">{{ r.mbti_result }}</span>
            <span class="pb-meta">{{ fmtTs(r.created_at) }}</span>
          </div>
          <p class="report-body">{{ r.report }}</p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.persona-block { margin-top: 20px; }
.pb-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
.pb-sub { font-size: var(--fs-xs); color: var(--text-muted); font-weight: 400; margin-left: 6px; }
.pb-actions { display: flex; gap: 8px; }
.pb-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-top: 10px; }
.pb-card { border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; background: var(--surface); }
.pb-card h4 { margin: 0 0 8px; font-size: var(--fs-md); }
.pb-wide { grid-column: 1 / -1; }
.pb-dim { font-size: var(--fs-xs); color: var(--text-muted); font-weight: 400; margin-left: 6px; }
.pb-empty { color: var(--text-muted); font-size: var(--fs-sm); }
.pb-meta { font-size: var(--fs-xs); color: var(--text-faint, var(--text-muted)); }
.pb-msg { color: var(--success); font-size: var(--fs-sm); }
.mbti-form { display: flex; gap: 8px; margin-top: 8px; }
.mbti-form input { width: 100px; }
.mbti-line { list-style: none; padding: 0; margin: 0; }
.mbti-line li { display: flex; justify-content: space-between; align-items: baseline; padding: 4px 0; border-bottom: 1px dashed var(--divider, var(--border)); }
.mbti-line li:last-child { border-bottom: none; }
.mbti-line b { font-size: var(--fs-lg); }
.trait-list { display: flex; flex-direction: column; gap: 10px; }
.trait-row { display: flex; flex-direction: column; gap: 4px; }
.trait-head { display: flex; justify-content: space-between; align-items: baseline; font-size: var(--fs-sm); }
.trait-bar { height: 5px; background: var(--surface-muted); border-radius: 3px; overflow: hidden; }
.trait-bar i { display: block; height: 100%; background: var(--brand); border-radius: 3px; transition: width 0.3s ease; }
.report-item { padding: 8px 0; border-bottom: 1px dashed var(--divider, var(--border)); }
.report-item:last-child { border-bottom: none; }
.report-head { display: flex; gap: 10px; align-items: baseline; }
.mbti-tag { padding: 1px 8px; border-radius: 10px; font-size: var(--fs-xs); background: var(--brand-soft); color: var(--brand-text); }
.report-body { margin: 4px 0 0; font-size: var(--fs-sm); line-height: 1.6; white-space: pre-wrap; }
</style>
