// 项目注册表 API 封装（/v1/admin/projects/*）
import { api } from './client'
import type { Page } from './ideas'

export interface Project {
  project_id: string
  name: string
  path: string
  git_repo: string | null
  last_active_at: string | null
  milestone: string | null
  created_at: string
  updated_at: string
}

export interface ProjectQuery {
  page?: number
  limit?: number
  q?: string
  milestone?: string
  sort?: string
  order?: string
}

export function listProjects(params: ProjectQuery = {}) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  })
  return api.get<Page<Project>>(`/v1/admin/projects?${qs.toString()}`)
}

export function getProject(id: string) {
  return api.get<{ project: Project }>(`/v1/admin/projects/${id}`)
}

// 登记/创建项目（用户主动立项，2026-08-13 用户定；upsert，二次登记=更新）
export function createProject(body: {
  project_id: string
  path: string
  name?: string
  git_repo?: string
  milestone?: string
}) {
  return api.post<{ project: Project; created: boolean }>('/v1/admin/projects', body)
}

export function updateProject(id: string, body: Record<string, unknown>) {
  return api.patch<{ project: Project; updated_fields: string[] }>(`/v1/admin/projects/${id}`, body)
}