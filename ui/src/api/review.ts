// 待审候选 API 封装（/v1/admin/review/*，设计决策 D-30 / Backlog T-318）
//
// 端点形状以服务端 sgme/server/routes_review.py 为准（放行/拒绝是「路径带 memory_id」形态）：
//   GET  /v1/admin/review/pending?limit=&offset=&dimension=
//   POST /v1/admin/review/{memory_id}/approve   body {note?}
//   POST /v1/admin/review/{memory_id}/reject    body {reason?}（2026-11-05 起非必填，空则后端记默认串）
//   POST /v1/admin/review/auto-resolve          body {days}（默认 7）
// ⚠️ 任务书里的 /v1/admin/review/approve {memory_id} 与 /reject {memory_id} 形态
//   后端并未提供，此处保持与真实契约一致（改成 body 形态会直接 404）。
import { api } from './client'

// 超期消化的后端默认阈值（D-30 七天规则）
export const AUTO_RESOLVE_DEFAULT_DAYS = 7

// 待审条目：后端 list_pending 返回 memories 表整行（SELECT m.*），
// 故除下列基础字段外还可能带 2.0 元数据（certainty/type/route/scene_key 等）；
// 这里只声明页面会消费的字段，其余按需透传。
export interface ReviewItem {
  memory_id: string
  content: string
  memory_type: string | null
  type: string | null
  subject: string | null
  route: string | null
  /** 单维度（pending 端点暂不返回；展示优先用 dimensions） */
  dimension?: string | null
  /** 维度标签（列表页按需经 GET /v1/memory/{id} 补全） */
  dimensions?: string[]
  /** 确定度（2.0 落库字段：confirmed / tentative / discussing） */
  certainty?: string | null
  /** 置信度 0-1（memories 表当前无该列；若后续返回则优先展示百分比） */
  confidence?: number | null
  priority?: number | null
  occurred_at?: string | null
  scene_key?: string | null
  trait?: string | null
  feeling?: string | null
  notes?: string | null
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

// 放行结果（后端 data 为 {memory_id}；ok 为兼容旧投影保留可选）
export interface ReviewApproveResult {
  memory_id?: string
  ok?: boolean
}

export function approveMemory(memoryId: string, note?: string) {
  return api.post<ReviewApproveResult>(
    `/v1/admin/review/${encodeURIComponent(memoryId)}/approve`,
    note ? { note } : {},
  )
}

// 拒绝结果（软删，可恢复；后端 data 为 {memory_id}）
export interface ReviewRejectResult {
  memory_id?: string
  ok?: boolean
}

export function rejectMemory(memoryId: string, reason?: string) {
  return api.post<ReviewRejectResult>(
    `/v1/admin/review/${encodeURIComponent(memoryId)}/reject`,
    reason ? { reason } : {},
  )
}

// ---------- POST /v1/admin/review/batch-reject（D2，2026-11-05 批量拒绝） ----------
// 后端逐条复用单条 reject 的语义（幂等软删、理由非必填），单条失败不打断整批：
// 返回逐条结果 + 汇总计数，页面据此显示「成功 n 条 / 失败 m 条」。
export interface ReviewBatchRejectItem {
  memory_id: string
  ok: boolean
  error?: string
  error_code?: string
}

export interface ReviewBatchRejectResult {
  results: ReviewBatchRejectItem[]
  rejected: number
  failed: number
}

export function batchRejectMemory(memoryIds: string[], reason?: string) {
  return api.post<ReviewBatchRejectResult>('/v1/admin/review/batch-reject', {
    memory_ids: memoryIds,
    ...(reason ? { reason } : {}),
  })
}

// ---------- POST /v1/admin/review/auto-resolve（超期待审批量消化，D-30 七天规则） ----------
export interface AutoResolveResult {
  resolved: number
  [k: string]: unknown
}

// days 默认 7（后端缺省同值）；页面传显式天数便于用户调整阈值
export function autoResolveReview(days: number = AUTO_RESOLVE_DEFAULT_DAYS) {
  return api.post<AutoResolveResult>('/v1/admin/review/auto-resolve', { days })
}
