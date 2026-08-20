// 知识图谱 API 封装（ST-13，GET /v1/admin/graph）
import { api } from './client'

// ---------- GET /v1/admin/graph ----------
export interface GraphNode {
  id: string
  type: 'scene' | 'memory' | 'wiki'
  label: string
  title?: string
  content?: string
  memory_type?: string
  priority?: number
  heat?: number
  status?: string
  memories_count?: number
  dimensions?: string[]
  category?: string | null
  created_at?: string | null
  updated_at?: string | null
}
export interface GraphLink {
  source: string
  target: string
  type: 'scene_memory' | 'wiki_link'
  rel_type?: string
}
export interface GraphData {
  nodes: GraphNode[]
  links: GraphLink[]
  stats: {
    scenes: number
    memories: number
    wiki: number
    scene_links: number
    wiki_links: number
  }
}
export function fetchGraph(params: {
  scene_limit?: number
  wiki_limit?: number
  memory_limit?: number
} = {}) {
  const qs = new URLSearchParams()
  if (params.scene_limit) qs.set('scene_limit', String(params.scene_limit))
  if (params.wiki_limit) qs.set('wiki_limit', String(params.wiki_limit))
  if (params.memory_limit) qs.set('memory_limit', String(params.memory_limit))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<GraphData>(`/v1/admin/graph${suffix}`)
}
