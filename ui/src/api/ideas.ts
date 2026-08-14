// 创意池 API 封装（/v1/admin/ideas/*）
import { api } from './client'

export interface Idea {
  idea_id: string
  content: string
  priority: number
  status: string
  created_at: string
  updated_at: string
  notes: { ts: string; text: string }[]
  custom_flag: string | null
  reject_reason: string | null
  rejected_at: string | null
  source_ref: string | null
  origin_memory_id: string | null
}

export interface Page<T> {
  items: T[]
  count: number
  total: number
  page: number
  limit: number
  generated_at: string
}

export interface IdeaQuery {
  page?: number
  limit?: number
  status?: string
  custom_flag?: string
  has_flag?: boolean | string
  q?: string
  sort?: string
  order?: string
}

export function listIdeas(params: IdeaQuery = {}) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  })
  return api.get<Page<Idea>>(`/v1/admin/ideas?${qs.toString()}`)
}

// 人工添加创意（2026-08-13 用户定：创意由用户主动提出，不再 LLM 自动打标）
export function createIdea(body: { content: string; priority?: number; source_ref?: string }) {
  return api.post<{ idea: Idea; created: boolean }>('/v1/admin/ideas', body)
}

export function getIdea(id: string) {
  return api.get<{ idea: Idea }>(`/v1/admin/ideas/${id}`)
}

export function updateIdea(id: string, body: { content?: string; priority?: number }) {
  return api.patch<{ idea: Idea; updated_fields: string[] }>(`/v1/admin/ideas/${id}`, body)
}

export function appendNote(id: string, text: string) {
  return api.post<{ idea_id: string; notes: { ts: string; text: string }[]; count: number }>(
    `/v1/admin/ideas/${id}/notes`,
    { text },
  )
}

export function setFlag(id: string, custom_flag: string | null) {
  return api.put<{ idea_id: string; custom_flag: string | null }>(
    `/v1/admin/ideas/${id}/flag`,
    { custom_flag },
  )
}

export function softDeleteIdea(id: string, reason?: string) {
  return api.delete<{ idea_id: string; status: string }>(`/v1/admin/ideas/${id}`, { reason })
}

export function restoreIdea(id: string) {
  return api.post<{ idea_id: string; status: string }>(`/v1/admin/ideas/${id}/restore`)
}

export function promoteIdea(
  id: string,
  body: { title: string; content?: string; priority?: number; project_id?: string },
) {
  return api.post<{ idea_id: string; promoted: boolean }>(`/v1/admin/ideas/${id}/promote`, body)
}