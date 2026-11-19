// 知识图谱 API 封装（ST-13，GET /v1/admin/graph）
// T-427（2026-11-20）：场景层退役，图谱只剩 wiki 页面节点与 wiki_links。
import { api } from './client'

// ---------- GET /v1/admin/graph ----------
export interface GraphNode {
  id: string
  type: 'wiki'
  label: string
  title?: string
  status?: string
  category?: string | null
  updated_at?: string | null
}
export interface GraphLink {
  source: string
  target: string
  type: 'wiki_link'
  rel_type?: string
}
export interface GraphData {
  nodes: GraphNode[]
  links: GraphLink[]
  stats: {
    wiki: number
    wiki_links: number
  }
}
export function fetchGraph(params: {
  wiki_limit?: number
} = {}) {
  const qs = new URLSearchParams()
  if (params.wiki_limit) qs.set('wiki_limit', String(params.wiki_limit))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<GraphData>(`/v1/admin/graph${suffix}`)
}
