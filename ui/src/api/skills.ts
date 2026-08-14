// 技能仓库 API 封装（数据源 sgme/server/routes_admin.py；走 admin key 鉴权）
import { api } from './client'

export interface SkillsList {
  enabled: boolean
  mode: string
  path: string
  total: number
  skills: string[]
}

export interface SkillGet {
  name: string
  content: string
}

export interface SkillPut {
  name: string
  path: string
}

export function listSkills() {
  return api.get<SkillsList>('/v1/admin/skills')
}

export function getSkill(name: string) {
  return api.get<SkillGet>(`/v1/admin/skills/${encodeURIComponent(name)}`)
}

export function putSkill(name: string, content: string) {
  return api.put<SkillPut>(`/v1/admin/skills/${encodeURIComponent(name)}`, { content })
}

export function deleteSkill(name: string) {
  return api.delete<{ deleted: boolean }>(`/v1/admin/skills/${encodeURIComponent(name)}`)
}