<script setup lang="ts">
// LLM 成本看板：llm_usage_daily 账本按 (day, provider, model) 聚合展示
// 数据源 GET /v1/admin/ops/llm-cost?days=N（后端未就绪时显示空态，不白屏）
import { computed, onMounted, ref } from 'vue'
import { getLlmCost, type LlmBudget, type LlmCostItem } from '../../api/ops'
import { ApiError } from '../../api/client'
import {
  baselineSourceLabel,
  budgetLevelMeta,
  budgetProgressPct,
  formatBudgetRatio,
  isBudgetUsable,
} from './costBudget'

const days = ref<7 | 30 | 90>(30)
const items = ref<LlmCostItem[]>([])
const totals = ref<{ prompt_tokens: number; completion_tokens: number; calls: number } | null>(null)
// 成本告警档位（P1-9）：budget 为 null 时页面显示空态（账本未创建/旧后端不返回）
const budget = ref<LlmBudget | null>(null)
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

// 预算卡片视图模型：null = 走空态（available=false / 旧后端无 budget / 基线非正数）
const budgetView = computed(() => {
  const b = budget.value
  if (!b) return null
  const level = budgetLevelMeta(b.level)
  return {
    level: level.key,
    levelLabel: level.label,
    levelDesc: level.desc,
    month: b.month || '—',
    monthTokens: fmtNum(b.month_tokens ?? 0),
    baselineTokens: fmtNum(b.baseline_tokens),
    baselineSource: baselineSourceLabel(b.baseline_source),
    ratio: formatBudgetRatio(b.ratio),
    progress: budgetProgressPct(b.ratio),
    warnThreshold: `${(b.warn_ratio ?? 0).toFixed(1)}×`,
    alertThreshold: `${(b.alert_ratio ?? 0).toFixed(1)}×`,
    // enabled=false 时仍展示本月用量，但提示告警已关闭
    disabled: b.enabled === false,
  }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await getLlmCost(days.value)
    items.value = r.items || []
    totals.value = r.totals || null
    // budget 是 P1-9（2026-11-04）新增字段：旧后端不返回，账本未创建时 available=false，
    // 两种情况都按空态展示，不显示 0 基线造成的误导性百分比
    budget.value = isBudgetUsable(r.budget) ? r.budget : null
  } catch (e) {
    // 端点 404 / 服务不可达等：清空数据并显示友好提示
    items.value = []
    totals.value = null
    budget.value = null
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

    <!-- 成本预算 / 告警档位（P1-9：GET /v1/admin/ops/llm-cost 的 budget 字段） -->
    <section class="panel">
      <h3>成本预算 <span class="sub">本月 tokens 对基线 · 只告警不停服</span></h3>
      <div v-if="loading && !budgetView" class="empty">加载中…</div>
      <div v-else-if="!budgetView" class="empty">
        暂无预算数据——LLM 用量账本尚未创建（available=false）或后端未返回 budget 字段；开始记账后此处显示本月用量与告警档位。
      </div>
      <template v-else>
        <div class="stat-grid budget-grid">
          <div class="stat-card">
            <span class="v">{{ budgetView.monthTokens }}</span>
            <span class="l">本月 tokens</span>
          </div>
          <div class="stat-card">
            <span class="v">{{ budgetView.baselineTokens }}</span>
            <span class="l">基线 tokens</span>
          </div>
          <div class="stat-card">
            <span class="v">{{ budgetView.ratio }}</span>
            <span class="l">本月 / 基线</span>
          </div>
          <div class="stat-card">
            <span class="budget-level" :class="budgetView.level"><span class="dot" />{{ budgetView.levelLabel }}</span>
            <span class="l">告警档位</span>
          </div>
        </div>
        <div
          class="budget-bar"
          :class="budgetView.level"
          :title="`比值 ${budgetView.ratio}（提示线 ${budgetView.warnThreshold} / 告警线 ${budgetView.alertThreshold}）`"
        >
          <i :style="{ width: budgetView.progress + '%' }" />
        </div>
        <p class="budget-note">
          档位阈值：提示线 {{ budgetView.warnThreshold }} · 告警线 {{ budgetView.alertThreshold }}（相对基线；只告警不停服、不自动切链）
        </p>
        <p class="budget-note">
          基线来源：{{ budgetView.baselineSource }} · 统计口径：{{ budgetView.month }} 自然月（UTC）prompt + completion 合计
          <span v-if="budgetView.disabled" class="budget-off">· 成本告警已在配置中关闭（cost_alert.enabled=false）</span>
          <span v-else-if="budgetView.level !== 'none'" class="budget-off">· {{ budgetView.levelDesc }}</span>
        </p>
      </template>
    </section>

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
/* 成本预算 / 告警档位（P1-9）：配色沿用设计令牌 —— none 绿 / warn 橙 / alert 红 */
.budget-grid {
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: var(--space-3);
}
.budget-level {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--fs-sm);
  font-weight: 600;
  white-space: nowrap;
}
.budget-level .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}
.budget-level.none {
  background: var(--success-soft);
  color: var(--success);
}
.budget-level.warn {
  background: var(--warn-soft);
  color: var(--warn);
}
.budget-level.alert {
  background: var(--danger-soft);
  color: var(--danger);
}
.budget-bar {
  height: 8px;
  border-radius: 999px;
  background: var(--surface-muted);
  overflow: hidden;
  margin-bottom: var(--space-2);
}
.budget-bar > i {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 0.2s ease;
}
.budget-bar.none > i {
  background: var(--success);
}
.budget-bar.warn > i {
  background: var(--warn);
}
.budget-bar.alert > i {
  background: var(--danger);
}
.budget-note {
  margin: 0 0 var(--space-1);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.budget-off {
  color: var(--warn);
}
@media (max-width: 900px) {
  .budget-grid {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
