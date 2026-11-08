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
// 2.0 关联页索引（memory_pages 表：后端详情接口暂未返回，有值才渲染该区块）
export interface MemoryPageRef {
  kind: string // wiki（ref_id=page_id）| skill（ref_id=技能名）
  ref_id: string
  relation: string // details（详情在该页）| derived_from（该页由本条记忆生成）
}

// ---------- 事件上下文（W06 / W01《上下文归属与因果边》§5） ----------
// 与 operations/memory.py 的 empty_context()/build_context() 逐字段对齐：
// - 主干/侧事实：main_event（父记忆 id + 正文）与 parent_memory_id；
// - 单元事实：facts（侧事实自身无 l2_facts_json 时后端回流主干的单元事实）；
// - 因果：causes（原因 → 结论，纯文本对）；
// - 关联边：edges 只含 belongs_to（source=l2_extract）与 causes，双向，五键。
export interface MemoryContextMainEvent {
  memory_id: string
  content: string
}
export interface MemoryContextFact {
  type: string | null
  content: string
  subject: string | null
}
export interface MemoryContextCause {
  from: string
  to: string
}
export interface MemoryContextEdge {
  from_id: string
  to_id: string
  relation: 'belongs_to' | 'causes' | string
  weight: number
  source: string
}
export interface MemoryContext {
  main_event: MemoryContextMainEvent | null
  facts: MemoryContextFact[]
  causes: MemoryContextCause[]
  parent_memory_id: string | null
  edges: MemoryContextEdge[]
}

export interface MemoryDetail {
  memory: MemoryItem & { sources: MemorySource[] } & Partial<Record<string, unknown>>
  sources: MemorySource[]
  archive_chain: Array<Record<string, unknown>>
  pages?: MemoryPageRef[]
  // W06 新增（纯增量键）：老后端不返回该字段时按空形态处理，详情页不报错
  context?: MemoryContext
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

// ---------- POST /v1/inject（画像注入，ProfileView 用） ----------
export interface InjectBlock {
  title: string
  items: { content: string; relative_time?: string; memory_id?: string }[]
  present: boolean
}
export interface InjectResponse {
  blocks: InjectBlock[]
  // D-79（2026-11-05）：Tier0 摘要字段已砍，画像由 core 记忆卡承担
  stats: { mode: string; queries: number; tokens_est: number; core_present?: boolean }
}
export function injectProfile(mode: string) {
  return api.post<InjectResponse>('/v1/inject', { mode })
}