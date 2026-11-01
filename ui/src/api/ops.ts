// 运维看板 API 封装（/v1/admin/ops/*）—— TTL 处置审计 + LLM 成本账本
// 后端可能尚未实现：调用方需捕获错误并显示空态（不得白屏）
import { api } from './client'

// ---------- TTL 处置（D-52 五档处置审计，ttl_disposition 表） ----------

// 五档处置：valid_expired 过期降档 / renewed 续期 / anchor 锚点 / sleeping 休眠 / archived 归档
export interface TtlDispositionItem {
  day: string
  valid_expired: number
  renewed: number
  anchor: number
  sleeping: number
  archived: number
}

export interface TtlDispositionTotal {
  valid_expired: number
  renewed: number
  anchor: number
  sleeping: number
  archived: number
}

export interface TtlDispositionResponse {
  items: TtlDispositionItem[]
  total: TtlDispositionTotal
}

export function getTtlDisposition(days: number) {
  return api.get<TtlDispositionResponse>(`/v1/admin/ops/ttl-disposition?days=${days}`)
}

// ---------- LLM 成本（llm_usage_daily 账本） ----------

export interface LlmCostItem {
  day: string
  provider: string
  model: string
  stage: string
  prompt_tokens: number
  completion_tokens: number
  calls: number
}

export interface LlmCostTotals {
  prompt_tokens: number
  completion_tokens: number
  calls: number
}

export interface LlmCostResponse {
  items: LlmCostItem[]
  totals: LlmCostTotals
}

export function getLlmCost(days: number) {
  return api.get<LlmCostResponse>(`/v1/admin/ops/llm-cost?days=${days}`)
}
