// 场景与会话原文 API 封装（② 记忆与知识剩余视图）
import { api } from './client'

// ---------- GET /v1/admin/scenes ----------
export interface Scene {
  scene_id: string
  title: string
  content: string
  heat: number
  status: string // active | rejected | expired | archived
  created_at: string
  updated_at: string
  memories_count: number
}
export interface ScenesPage {
  items: Scene[]
  count: number
  total: number
  page: number
  limit: number
  generated_at: string
}
export function listScenes(params: {
  page?: number
  limit?: number
  status?: string
  sort?: string
  order?: string
  since?: string
  until?: string
} = {}) {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.status) qs.set('status', params.status)
  if (params.sort) qs.set('sort', params.sort)
  if (params.order) qs.set('order', params.order)
  if (params.since) qs.set('since', params.since)
  if (params.until) qs.set('until', params.until)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<ScenesPage>(`/v1/admin/scenes${suffix}`)
}

// ---------- POST /v1/admin/scenes/{id}/status ----------
export function setSceneStatus(id: string, status: string, reason?: string) {
  return api.post<Record<string, unknown>>(`/v1/admin/scenes/${id}/status`, reason ? { status, reason } : { status })
}

// ---------- GET /v1/admin/sessions ----------
export interface SessionItem {
  file_id: string
  session_key: string
  agent_id: string | null
  status: string // new | refined | archived
  size: number
  started_at: string
  ended_at: string | null
  refined_at: string | null
}
export interface SessionsPage {
  items: SessionItem[]
  count: number
  total: number
  page: number
  limit: number
  generated_at: string
}
export function listSessions(params: {
  page?: number
  limit?: number
  session_key?: string
  agent_id?: string
  status?: string
} = {}) {
  const qs = new URLSearchParams()
  if (params.page) qs.set('page', String(params.page))
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.session_key) qs.set('session_key', params.session_key)
  if (params.agent_id) qs.set('agent_id', params.agent_id)
  if (params.status) qs.set('status', params.status)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<SessionsPage>(`/v1/admin/sessions${suffix}`)
}

// ---------- GET /v1/admin/sessions/{file_id} ----------
export interface SessionRaw {
  file_id: string
  session_key: string
  agent_id: string | null
  content: string
}
export function getSessionRaw(fileId: string) {
  return api.get<SessionRaw>(`/v1/admin/sessions/${fileId}`)
}