<script setup lang="ts">
// LLM 成本看板：llm_usage_daily 账本按 (day, provider, model) 聚合展示
// 数据源 GET /v1/admin/ops/llm-cost?days=N（后端未就绪时显示空态，不白屏）
import { computed, onMounted, ref } from 'vue'
import { getLlmCost, type LlmCostItem } from '../../api/ops'
import { ApiError } from '../../api/client'

const days = ref<7 | 30 | 90>(30)
const items = ref<LlmCostItem[]>([])
const totals = ref<{ prompt_tokens: number; completion_tokens: number; calls: number } | null>(null)
const loading = ref(false)
const error = ref('')

const DAY_OPTIONS: Array<7 | 30 | 90> = [7, 30, 90]

// 按 (day, provider, model) 聚合（stage 不同行合并，stage 列表并入悬浮提示）
interface AggRow {
  key: string
  day: string
  provider: string
  model: string
  stages: string[]
  prompt_tokens: number
  completion_tokens: number
  calls: number
}

const aggRows = computed<AggRow[]>(() => {
  const map = new Map<string, AggRow>()
  for (const it of items.value) {
    const key = `${it.day}|${it.provider}|${it.model}`
    let row = map.get(key)
    if (!row) {
      row = { key, day: it.day, provider: it.provider, model: it.model, stages: [], prompt_tokens: 0, completion_tokens: 0, calls: 0 }
      map.set(key, row)
    }
    row.prompt_tokens += it.prompt_tokens || 0
    row.completion_tokens += it.completion_tokens || 0
    row.calls += it.calls || 0
    if (it.stage && !row.stages.includes(it.stage)) row.stages.push(it.stage)
  }
  return [...map.values()].sort((a, b) => (a.day < b.day ? 1 : -1))
})

// 按 model 汇总（全窗口），用于横向占比条
const modelBars = computed(() => {
  const map = new Map<string, { model: string; provider: string; tokens: number }>()
  let maxTokens = 0
  for (const it of items.value) {
    const key = `${it.provider}|${it.model}`
    let row = map.get(key)
    if (!row) {
      row = { model: it.model, provider: it.provider, tokens: 0 }
      map.set(key, row)
    }
    row.tokens += (it.prompt_tokens || 0) + (it.completion_tokens || 0)
  }
  const list = [...map.values()].sort((a, b) => b.tokens - a.tokens)
  maxTokens = list.length ? list[0].tokens : 0
  const grand = list.reduce((s, r) => s + r.tokens, 0)
  return list.map((r) => ({
    ...r,
    pct: maxTokens ? (r.tokens / maxTokens) * 100 : 0,
    share: grand ? ((r.tokens / grand) * 100).toFixed(1) : '0.0',
  }))
})

function fmtNum(n: number): string {
  return n.toLocaleString('zh-CN')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await getLlmCost(days.value)
    items.value = r.items || []
    totals.value = r.totals || null
  } catch (e) {
    // 端点 404 / 服务不可达等：清空数据并显示友好提示
    items.value = []
    totals.value = null
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function switchDays(d: 7 | 30 | 90) {
  if (days.value === d) return
  days.value = d
  load()
}

onMounted(load)
</script>

<template>
  <div class="cost-view">
    <div class="head">
      <h2>LLM 成本 <span class="sub">llm_usage_daily 账本（按天 × 模型）</span></h2>
      <div class="days-switch">
        <button
          v-for="d in DAY_OPTIONS"
          :key="d"
          :class="{ active: days === d }"
          :disabled="loading"
          @click="switchDays(d)"
        >
          近 {{ d }} 天
        </button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}（接口可能尚未上线，稍后重试或联系管理员）</p>

    <!-- 顶部汇总 -->
    <section class="panel">
      <h3>用量汇总 <span class="sub">近 {{ days }} 天累计</span></h3>
      <div class="summary-grid">
        <div class="stat-card"><span class="v">{{ fmtNum(totals?.calls ?? 0) }}</span><span class="l">总调用数</span></div>
        <div class="stat-card"><span class="v">{{ fmtNum(totals?.prompt_tokens ?? 0) }}</span><span class="l">总输入 tokens</span></div>
        <div class="stat-card"><span class="v">{{ fmtNum(totals?.completion_tokens ?? 0) }}</span><span class="l">总输出 tokens</span></div>
      </div>
    </section>

    <!-- 按模型占比条（纯 CSS） -->
    <section v-if="modelBars.length" class="panel">
      <h3>模型占比 <span class="sub">按总 tokens（输入+输出）</span></h3>
      <div class="dims">
        <div v-for="m in modelBars" :key="`${m.provider}|${m.model}`" class="dim">
          <span class="dim-name" :title="`${m.provider} / ${m.model}`">{{ m.provider }} · {{ m.model }}</span>
          <div class="bar"><i :style="{ width: m.pct + '%' }" /></div>
          <span class="dim-count" :title="`${fmtNum(m.tokens)} tokens`">{{ m.share }}%</span>
        </div>
      </div>
    </section>

    <!-- 按 (day, provider, model) 聚合表 -->
    <section class="panel">
      <h3>按天明细 <span class="sub">共 {{ aggRows.length }} 组（天 × 模型）</span></h3>
      <p v-if="loading" class="empty">加载中…</p>
      <div v-else-if="!aggRows.length" class="empty">
        {{ error ? '暂无可显示的用量数据' : '暂无 LLM 用量记录——窗口内没有触发提炼/摘要等 LLM 调用' }}
      </div>
      <table v-else class="tbl">
        <thead>
          <tr>
            <th>日期</th>
            <th>供应商</th>
            <th>模型</th>
            <th>阶段</th>
            <th>调用数</th>
            <th>输入 tokens</th>
            <th>输出 tokens</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in aggRows" :key="r.key">
            <td class="mono">{{ r.day }}</td>
            <td>{{ r.provider }}</td>
            <td class="mono">{{ r.model }}</td>
            <td>
              <span class="tag dimtag-neutral" :title="r.stages.join(', ')">
                {{ r.stages.length > 1 ? `${r.stages.length} 个阶段` : r.stages[0] }}
              </span>
            </td>
            <td>{{ fmtNum(r.calls) }}</td>
            <td>{{ fmtNum(r.prompt_tokens) }}</td>
            <td>{{ fmtNum(r.completion_tokens) }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.days-switch {
  display: inline-flex;
  gap: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.days-switch button {
  padding: 6px 14px;
  border: none;
  background: var(--surface);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.days-switch button + button {
  border-left: 1px solid var(--border);
}
.days-switch button.active {
  background: var(--brand);
  color: #fff;
}
.days-switch button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-3);
}
@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }
}
.summary-grid .stat-card .v {
  font-size: 26px;
}
</style>
