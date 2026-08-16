// 记忆与知识 API 封装（数据源见 SGME-WebUI设计-v0.1 §4 ②）
import { api } from './client'

// ---------- GET /v1/admin/memories ----------
export interface MemoryItem {
  memory_id: string
  content: string
  memory_type: string
  priority: number
  status: string // active | rejected | expired | archived
  created_at: string
  updated_at: string
  occurred_at: string | null
  notes: string | null
  custom_flag: string | null
  dimensions: string[]
  source_ref: string | null
}
export interface MemoriesPage {
  items: MemoryItem[]
  count: number
  total: number
  page: number
  limit: number
  generated_at: string
}
export function listMemories(params: {
  page?: number
  limit?: number
  dimension_id?: string
  dimensions?: string[]
  status?: string
  sort?: string
  order?: string
  since?: string
  until?: string
  ttl_filter?: boolean
} = {}) {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.dimension_id) qs.set('dimension_id', params.dimension_id)
  if (params.dimensions?.length) qs.set('dimensions', params.dimensions.join(','))
  if (params.status) qs.set('status', params.status)
  if (params.sort) qs.set('sort', params.sort)
  if (params.order) qs.set('order', params.order)
  if (params.since) qs.set('since', params.since)
  if (params.until) qs.set('until', params.until)
  if (params.ttl_filter) qs.set('ttl_filter', 'true')
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<MemoriesPage>(`/v1/admin/memories${suffix}`)
}

// ---------- GET /v1/memory/{id} ----------
export interface MemorySource {
  source_ref: string
  source_type: string
}
export interface MemoryDetail {
  memory: MemoryItem & { sources: MemorySource[] }
  sources: MemorySource[]
  archive_chain: Array<Record<string, unknown>>
}
export function getMemory(id: string) {
  return api.get<MemoryDetail>(`/v1/memory/${id}`)
}

// ---------- POST /v1/memory/{id}/reject 与 unreject ----------
export interface RejectResult {
  status?: string
  memory_id?: string
  [k: string]: unknown
}
export function rejectMemory(id: string, reason?: string) {
  return api.post<RejectResult>(`/v1/memory/${id}/reject`, reason ? { reason } : {})
}
export function unrejectMemory(id: string) {
  return api.post<RejectResult>(`/v1/memory/${id}/unreject`)
}

// ---------- POST /v1/search ----------
export interface SearchTrace {
  [k: string]: unknown
}
export interface SearchResult {
  rank: number
  score: number
  source: string // memory | wiki | wiki_pages
  memory_id?: string
  scene_id?: string
  page_id?: string
  title?: string
  category?: string | null
  tags?: string[]
  content: string
  dimensions: string[]
  priority: number
  updated_at: string
  routes: string[]
  trace?: SearchTrace[]
}
export interface SearchResponse {
  results: SearchResult[]
  meta: { routes: string[]; rrf_k: number }
}
export function search(params: {
  query: string
  scopes?: string[]
  dimensions?: string[]
  match?: string
  limit?: number
  include_sources?: boolean
}) {
  return api.post<SearchResponse>('/v1/search', {
    query: params.query,
    scopes: params.scopes ?? ['memory'],
    dimensions: params.dimensions,
    match: params.match ?? 'any',
    limit: params.limit ?? 20,
    include_sources: params.include_sources ?? true,
  })
}