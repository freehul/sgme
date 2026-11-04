// 待审队列展示辅助（列表卡片与详情弹层共用）：ID 截断 / 维度标签 / 置信度徽标
// 数据来源：后端 list_pending 返回 memories 表整行（含 certainty/type/route 等），
// 维度标签经 GET /v1/memory/{id} 按需补全（memories 行本身不含 memory_tags 关联）。
import type { ReviewItem } from '../../api/review'

// 记忆 ID 截断展示：保留头 8 位 + 尾 4 位，完整值由调用方放进 title 提示
export function shortMemoryId(id: string | null | undefined, head = 8, tail = 4): string {
  if (!id) return '—'
  if (id.length <= head + tail + 1) return id
  return `${id.slice(0, head)}…${id.slice(-tail)}`
}

// 维度标签：优先 dimensions 数组，回退单 dimension 字段（兼容未来接口变化）
export function dimTagsOf(item: ReviewItem): string[] {
  if (item.dimensions?.length) return item.dimensions
  return item.dimension ? [item.dimension] : []
}

// 维度 → 标签配色（复用全局 style.css 的 dimtag-* 令牌类）
const DIM_CLASS: Record<string, string> = {
  identity: 'dimtag-yellow',
  goals: 'dimtag-green',
  status: 'dimtag-blue',
  focus: 'dimtag-purple',
  preferences: 'dimtag-blue',
  ideas: 'dimtag-purple',
  care: 'dimtag-red',
}

export function dimClass(d: string): string {
  return DIM_CLASS[d] || 'dimtag-neutral'
}

// 置信度徽标：memories 表当前没有 confidence 列，故用 2.0 的 certainty 映射；
// 两个字段都缺时显示「—」并说明原因——不编造分数（对齐 D-50 存量不编造纪律）。
export interface ConfidenceBadge {
  label: string
  tone: string
  hint: string
}

const CERTAINTY_BADGE: Record<string, { label: string; tone: string }> = {
  confirmed: { label: '高', tone: 'dimtag-green' },   // LLM 已确认
  tentative: { label: '中', tone: 'dimtag-yellow' },  // 存疑
  discussing: { label: '低', tone: 'dimtag-red' },    // 讨论中
}

export function confidenceBadge(item: ReviewItem): ConfidenceBadge {
  const conf = item.confidence
  if (typeof conf === 'number' && Number.isFinite(conf)) {
    const pct = Math.round(Math.max(0, Math.min(1, conf)) * 100)
    const tone = pct >= 75 ? 'dimtag-green' : pct >= 50 ? 'dimtag-yellow' : 'dimtag-red'
    return { label: `${pct}%`, tone, hint: `后端 confidence=${conf}` }
  }
  const key = (item.certainty || '').trim().toLowerCase()
  const mapped = CERTAINTY_BADGE[key]
  if (mapped) {
    return {
      label: mapped.label,
      tone: mapped.tone,
      hint: `后端 certainty=${key}（memories 表无 confidence 列，用确定度近似置信度）`,
    }
  }
  return { label: '—', tone: 'dimtag-neutral', hint: '后端未返回 confidence/certainty 字段' }
}
