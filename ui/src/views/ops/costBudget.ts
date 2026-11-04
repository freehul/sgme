// 成本告警档位展示辅助（纯函数，列表/卡片共用，便于独立校验）
// 数据契约：GET /v1/admin/ops/llm-cost 的 budget 字段
// （后端 sgme/engine/cost_alert.py::budget_status；只读评估，只告警不停服）。
import type { LlmBudget } from '../../api/ops'

export type BudgetLevelKey = 'none' | 'warn' | 'alert'

export interface BudgetLevelMeta {
  key: BudgetLevelKey
  label: string
  desc: string
}

// 档位 → 中文文案（配色由页面 CSS 按 key 决定：none 绿 / warn 橙 / alert 红）
const LEVEL_META: Record<BudgetLevelKey, BudgetLevelMeta> = {
  none: { key: 'none', label: '正常', desc: '未达提示线' },
  warn: { key: 'warn', label: '提示', desc: '已达到提示线' },
  alert: { key: 'alert', label: '告警', desc: '已达到告警线' },
}

// 未知档位按 none 兜底（后端未来新增档位时不白屏、不误标红）
export function budgetLevelMeta(level: string | null | undefined): BudgetLevelMeta {
  const key = String(level || '').trim().toLowerCase()
  return LEVEL_META[key as BudgetLevelKey] || LEVEL_META.none
}

// 基线来源标注：config=首月配置估算；rolling_median=近 3 个完整月中位数 ×1.1
export function baselineSourceLabel(source: string | null | undefined): string {
  const key = String(source || '').trim().toLowerCase()
  if (key === 'rolling_median') return '滚动中位数 ×1.1'
  if (key === 'config') return '配置基线'
  return key || '—'
}

// 比值 → 百分比文案：ratio 为「本月 tokens / 基线」，1.0 = 100%
export function formatBudgetRatio(ratio: number | null | undefined): string {
  if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio < 0) return '—'
  return `${(ratio * 100).toFixed(1)}%`
}

// 进度条宽度（0-100）：超基线时条封顶满格，具体倍数仍以百分比文案为准
export function budgetProgressPct(ratio: number | null | undefined): number {
  if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio <= 0) return 0
  return Math.min(100, Math.round(ratio * 1000) / 10)
}

// 预算是否可用于展示：available 非 true（账本未创建，后端显式回 available=false）
// 或基线非正数 → 空态，避免 0 基线算出误导性百分比
export function isBudgetUsable(b: LlmBudget | null | undefined): b is LlmBudget {
  if (!b || b.available !== true) return false
  return (
    typeof b.baseline_tokens === 'number' &&
    Number.isFinite(b.baseline_tokens) &&
    b.baseline_tokens > 0
  )
}
