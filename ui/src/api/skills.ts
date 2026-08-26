// 技能仓库 API 封装（T-106：读侧四级披露端点 sgme/server/routes_skills.py；agent key 鉴权）
import { api } from './client'

// L0 索引条目（GET /v1/skills?limit=&offset=）
export interface SkillIndexItem {
  name: string
  description: string
  category: string
  tags: string[]
  pattern?: string
  version?: string
  source?: string
}

export interface SkillsIndex {
  skills: SkillIndexItem[]
  total: number
  returned: number
  offset?: number
  budget?: number
}

// L1 摘要（GET /v1/skills/{name}/digest）
export interface SkillDigest {
  name: string
  description: string
  version: string
  pattern: string
  category: string
  tags: string[]
  uses: string[]
  sections: unknown[]
  sha256: string
  source: string
  origin_path?: string
}

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

/** L0 全量索引（分页拉取；limit=500 覆盖当前 400+ 规模） */
export function listSkillsIndex(limit = 500, offset = 0) {
  return api.get<SkillsIndex>(
    `/v1/skills?limit=${limit}&offset=${offset}`
  )
}

/** L1 摘要（审核媒介层） */
export function getSkillDigest(name: string) {
  return api.get<SkillDigest>(`/v1/skills/${encodeURIComponent(name)}/digest`)
}

/** 冷启动包（T-106 M5）：索引全量 + 热集全文 + SGME 操作手册 */
export interface ColdstartPack {
  index: { items: SkillIndexItem[]; total: number }
  hotset: { name: string; content: string; sha256: string }[]
  manual: { page_id: string; title: string; content: string } | null
}

export function getColdstart() {
  return api.get<ColdstartPack>('/v1/skills/coldstart')
}

// ---------- 旧 admin 直写接口（写侧仍走治理版 PUT/DELETE，见 routes_skills_admin） ----------

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
