// Wiki 知识库 API 封装（数据源见 sgme/wiki/routes.py；走 agent key 鉴权）
import { api } from './client'

export interface WikiPage {
  page_id: string
  title: string
  content: string
  category?: string | null
  tags?: string[]
  created_at: string
  updated_at: string
}

export interface WikiPages {
  pages: WikiPage[]
  total: number
  limit: number
  offset: number
}

export function listWikiPages(params: { category?: string; limit?: number; offset?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.category) qs.set('category', params.category)
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.offset) qs.set('offset', String(params.offset))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<WikiPages>(`/v1/wiki/pages${suffix}`)
}

export function getWikiPage(pageId: string) {
  return api.get<WikiPage>(`/v1/wiki/pages/${pageId}`)
}

export function searchWiki(q: string, limit = 10) {
  const qs = new URLSearchParams({ q, limit: String(limit) })
  return api.get<{ results: Array<{ page_id: string; title: string; snippet?: string }> }>(`/v1/wiki/search?${qs.toString()}`)
}

export function exportWikiPage(pageId: string) {
  return api.get<string>(`/v1/wiki/pages/${pageId}/export`)
}

// ---------- POST /v1/wiki/pages（T-55 wiki 直接写入，不走提炼） ----------
export function createWikiPage(body: {
  title: string
  content: string
  category?: string | null
  tags?: string[]
}) {
  return api.post<{ page_id: string; status: string }>('/v1/wiki/pages', body)
}