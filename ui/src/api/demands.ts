// 需求池 API 封装（/v1/admin/demands/*）
import { api } from './client'
import type { Page } from './ideas'

export interface Demand {
  demand_id: string
  title: string
  content: string
  status: 'pending' | 'planned' | 'partial' | 'done'
  priority: number
  project_id: string | null
  origin_idea_id: string | null
  source_ref: string | null
  created_at: string
  updated_at: string
  resolved_at: string | null
}

export const DEMAND_STATUS: Record<string, string> = {
  pending: '未立项',
  planned: '已立项',
  partial: '部分解决',
  done: '已解决',
}

export interface DemandQuery {
  page?: number
  limit?: number
  status?: string
  project_id?: string
  q?: string
  sort?: string
  order?: string
}

export function listDemands(params: DemandQuery = {}) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  })
  return api.get<Page<Demand>>(`/v1/admin/demands?${qs.toString()}`)
}

// 新建待办/需求（跨项目统一待办池，2026-08-13 用户定）
export function createDemand(body: {
  title: string
  content?: string
  priority?: number
  project_id?: string
  source_ref?: string
}) {
  return api.post<{ demand: Demand } & Record<string, unknown>>('/v1/admin/demands', body)
}

export function getDemand(id: string) {
  return api.get<{ demand: Demand }>(`/v1/admin/demands/${id}`)
}

export function setDemandStatus(id: string, status: string) {
  return api.put<{ demand: Demand }>(`/v1/admin/demands/${id}/status`, { status })
}

export function updateDemand(id: string, body: Record<string, unknown>) {
  return api.patch<{ demand: Demand }>(`/v1/admin/demands/${id}`, body)
}