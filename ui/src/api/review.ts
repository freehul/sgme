// 待审候选 API 封装（/v1/admin/review/*，设计决策 D-30 / Backlog T-318）
import { api } from './client'

export interface ReviewItem {
  memory_id: string
  content: string
  memory_type: string | null
  type: string | null
  subject: string | null
  route: string | null
  dimension: string | null
  created_at: string
}

export interface ReviewPendingPage {
  items: ReviewItem[]
  total: number
}

export interface ReviewPendingQuery {
  limit?: number
  offset?: number
  dimension?: string
}

export function listPendingReview(params: ReviewPendingQuery = {}) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  })
  return api.get<ReviewPendingPage>(`/v1/admin/review/pending?${qs.toString()}`)
}

export function approveMemory(memoryId: string, note?: string) {
  return api.post<{ ok: boolean }>(`/v1/admin/review/${memoryId}/approve`, { note })
}

export function rejectMemory(memoryId: string, reason: string) {
  return api.post<{ ok: boolean }>(`/v1/admin/review/${memoryId}/reject`, { reason })
}
