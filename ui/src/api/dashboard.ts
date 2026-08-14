// 总览 Dashboard API 封装（数据源见 SGME-WebUI设计-v0.1 §4 ①）
import { api } from './client'

// ---------- GET /v1/health ----------
export interface HealthLLM {
  provider?: string
  model?: string
  available?: boolean
  error?: string
}
export interface HealthRefinement {
  watermark_age_sec: number | null
  queue_depth: number
  last_refined_at: string | null
  stalled: boolean
  stalled_hours: number | null
  heartbeat_ok: boolean
}
export interface HealthVector {
  available: boolean
  engine: string
  memory_vectors: number
  scene_vectors: number
  reason: string | null
}
export interface HealthStatus {
  status: string
  version: string
  llm: HealthLLM
  refinement: HealthRefinement
  vector: HealthVector
}
export function getHealth() {
  return api.get<HealthStatus>('/v1/health')
}

// ---------- GET /v1/admin/stats ----------
export interface Stats {
  memories: { total: number; archived: number }
  raw_files: { total: number; new: number; refined: number; error: number }
  dimension_distribution: Array<{ id: string; display_name: string; count: number }>
  refinement: { watermark_age_sec: number | null; last_refined_at: string | null; queue_depth: number }
  agents: Array<{ agent_id: string; role: string }>
}
export function getStats() {
  return api.get<Stats>('/v1/admin/stats')
}

// ---------- GET /v1/admin/refine_runs ----------
export interface RefineRun {
  run_id: string
  file_id: string
  stage: string
  version: string
  provider: string
  status: string
  error: string | null
  started_at: string
  finished_at: string | null
  memories_count: number
  action_counts: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}
export interface RefineRuns {
  items: RefineRun[]
  count: number
  total: number
  page: number
  limit: number
  generated_at: string
}
export function getRefineRuns(params: { page?: number; limit?: number; stage?: string; status?: string } = {}) {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.stage) qs.set('stage', params.stage)
  if (params.status) qs.set('status', params.status)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<RefineRuns>(`/v1/admin/refine_runs${suffix}`)
}

// ---------- GET /v1/admin/dream/reports ----------
export interface DreamReport {
  date: string
  path: string
  refined_count: number
  memory_count: number
  scene_count: number
  error_count: number
  expired_count: number
  archived_count: number
  summary: string | null
  created_at: string
}
export interface DreamReports {
  reports: DreamReport[]
  total: number
  page: number
  limit: number
}
export function getDreamReports(params: { page?: number; limit?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.limit) qs.set('limit', String(params.limit))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<DreamReports>(`/v1/admin/dream/reports${suffix}`)
}

// ---------- GET /v1/events ----------
export interface SignalEvent {
  event_id: string
  type: string
  source: string
  payload: unknown
  ts: string
}
export interface Events {
  events: SignalEvent[]
  next_cursor: string | null
}
export function getEvents(params: { after?: string; limit?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.after) qs.set('after', params.after)
  if (params.limit) qs.set('limit', String(params.limit))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<Events>(`/v1/events${suffix}`)
}