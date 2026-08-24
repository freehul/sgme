<script setup lang="ts">
// 关怀信号面板（T-41）：手动触发扫描 + 未消费信号列表 + 消费
// 数据源：/v1/admin/care/*（SGME-CareEngine设计-v0.1 §关怀信号）
import { onMounted, ref } from 'vue'
import {
  ackCareSignal, claimCareSignal, consumeAllCareSignals, listCareSignals, scanCareSignals,
  type CareSignal,
} from '../../api/roles'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

const signals = ref<CareSignal[]>([])
const loading = ref(false)
const error = ref('')
const msg = ref('')
const scanStats = ref<Record<string, number> | null>(null)
const typeFilter = ref('')
const unconsumedOnly = ref(false)
const busy = ref(false)

const TYPE_LABEL: Record<string, { label: string; color: string }> = {
  care_todo_due: { label: '待办提醒', color: 'var(--warn)' },
  care_mood: { label: '情绪关怀', color: '#e91e63' }, // 保留品牌外情感色，无对应语义 token
  care_overwork: { label: '过劳预警', color: 'var(--danger)' },
  care_daily: { label: '每日问候', color: 'var(--success)' },
  memory_updated: { label: '记忆更新', color: 'var(--brand)' },
}

function typeInfo(t: string) {
  return TYPE_LABEL[t] || { label: t, color: 'var(--text-faint)' }
}

function parsePayload(p: string): Record<string, unknown> {
  try {
    return JSON.parse(p) as Record<string, unknown>
  } catch {
    return { raw: p }
  }
}

function payloadSummary(p: string): string {
  const d = parsePayload(p)
  if (d.content) return String(d.content)
  if (d.date) return `日期: ${d.date}${d.focus_count ? `，专注 ${d.focus_count} 条` : ''}`
  return JSON.stringify(d)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listCareSignals({
      unconsumedOnly: unconsumedOnly.value,
      signalType: typeFilter.value || undefined,
      limit: 50,
    })
    signals.value = data.signals
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function scan() {
  busy.value = true
  msg.value = ''
  try {
    const r = await scanCareSignals()
    scanStats.value = r.scan
    const total = Object.values(r.scan).reduce((a, b) => a + b, 0)
    msg.value = total > 0 ? `扫描完成：新增 ${total} 条关怀信号` : '扫描完成：无新增（幂等去重）'
    await load()
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function claim(s: CareSignal) {
  busy.value = true
  msg.value = ''
  try {
    await claimCareSignal(s.event_id)
    msg.value = '认领成功——处理关怀后点「回执已处理」'
    await load()
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      msg.value = '已被其他 agent 消费，跳过'
      await load()
    } else {
      msg.value = e instanceof ApiError ? e.message : String(e)
    }
  } finally {
    busy.value = false
  }
}

// T-87：全部消费（清空未消费信号，幂等；带确认防误操作）
async function clearAll() {
  if (!signals.value.length) return
  const scope = typeFilter.value ? typeInfo(typeFilter.value).label : '全部类型'
  if (!confirm(`确认全部消费当前列表 ${signals.value.length} 条未消费信号（${scope}）？
此操作仅标记已消费（consumed_at），数据保留可溯源。`)) return
  busy.value = true
  msg.value = ''
  try {
    const r = await consumeAllCareSignals({ signalType: typeFilter.value || undefined })
    msg.value = `已全部消费：${r.consumed} 条信号`
    await load()
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function ack(s: CareSignal, status: 'acked' | 'failed') {
  busy.value = true
  msg.value = ''
  try {
    await ackCareSignal(s.event_id, status, status === 'acked' ? '已处理' : '处理失败')
    msg.value = status === 'acked' ? '已回执' : '已记录失败回执'
    await load()
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="signals">
    <div class="head">
      <h2>关怀信号</h2>
      <div class="filters">
        <button class="btn btn-primary" :disabled="busy" @click="scan">
          {{ busy ? '扫描中…' : '触发扫描' }}
        </button>
        <button class="btn btn-danger" :disabled="busy || !signals.length" @click="clearAll">
          {{ busy ? '处理中…' : '全部消费' }}
        </button>
        <select v-model="typeFilter" @change="load">
          <option value="">全部类型</option>
          <option v-for="(v, k) in TYPE_LABEL" :key="k" :value="k">{{ v.label }}</option>
        </select>
        <label class="chk">
          <input v-model="unconsumedOnly" type="checkbox" @change="load" /> 仅未消费
        </label>
        <button @click="load">刷新</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="msg" class="note-text">{{ msg }}</p>

    <div v-if="scanStats" class="scan-stats">
      <span v-for="(v, k) in scanStats" :key="k" class="stat-chip">
        {{ typeInfo(k).label }}: <b>{{ v }}</b>
      </span>
    </div>

    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!signals.length" class="empty">暂无信号。点击「触发扫描」从记忆池推导关怀信号（待办老化/情绪/过劳/每日）。</p>
    <table v-else class="tbl">
      <thead>
        <tr>
          <th>类型</th>
          <th>内容</th>
          <th>时间</th>
          <th>状态</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in signals" :key="s.event_id">
          <td>
            <span class="type-tag" :style="{ color: typeInfo(s.type).color, borderColor: typeInfo(s.type).color }">
              {{ typeInfo(s.type).label }}
            </span>
          </td>
          <td class="content-cell">
            <div class="sig-content">{{ payloadSummary(s.payload) }}</div>
            <code class="mono sig-id">{{ s.event_id.slice(0, 8) }}…</code>
          </td>
          <td class="ts-cell">{{ fmtTs(s.ts) }}</td>
          <td>
            <span v-if="s.consumed_at" class="consumed" :title="`消费方：${s.consumed_by || '未知'}`">
              已消费{{ s.consumed_by ? `（${s.consumed_by}）` : '' }}
            </span>
            <span v-else class="pending">未消费</span>
          </td>
          <td class="act-cell">
            <button v-if="!s.consumed_at" class="btn btn-sm btn-primary" :disabled="busy" @click="claim(s)">
              认领
            </button>
            <button v-else class="btn btn-sm" :disabled="busy" @click="ack(s, 'acked')">
              回执已处理
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <p class="hint">说明：信号消费 = 主动关怀，谁消费谁标记——「认领」后处理关怀并「回执」，防多 agent 重复打扰；已被其他 agent 消费的信号会显示消费方。</p>
  </div>
</template>

<style scoped>
.scan-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.stat-chip {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  background: color-mix(in srgb, var(--brand) 12%, transparent);
}
.type-tag {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  border: 1px solid;
  white-space: nowrap;
}
.content-cell { max-width: 420px; }
.sig-content { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sig-id { font-size: 11px; opacity: 0.5; }
.ts-cell { font-size: 12px; opacity: 0.7; white-space: nowrap; }
.consumed { color: var(--success); font-size: 12px; }
.pending { color: var(--warn); font-size: 12px; }
.hint { margin-top: 12px; font-size: 12px; opacity: 0.55; }
</style>
