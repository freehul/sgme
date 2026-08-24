// ST-35 T-102：人格洞察 API 封装（数据源 /v1/admin/persona/*，见 routes_persona.py）
import { api } from './client'

export interface PersonaTrait {
  trait_id: string
  dimension: string
  value: string
  confidence: number
  evidence_count: number
  evidence_refs: string[]
  scene_context: string
  status: string
  source: string
  created_at: string
  updated_at: string
}

export interface MbtiRecord {
  id: number
  mbti_type: string
  source: string
  note: string | null
  recorded_at: string
}

export interface PersonaReport {
  report_id: string
  period: string
  report: string
  mbti_result: string | null
  trait_changes: Array<{ dimension?: string; from?: string; to?: string }>
  created_at: string
}

const BASE = '/v1/admin/persona'

export function listTraits() {
  return api.get<{ traits: PersonaTrait[]; count: number }>(`${BASE}/traits?_t=${Date.now()}`)
}

export function getMbti() {
  return api.get<{ history: MbtiRecord[]; latest: MbtiRecord | null }>(`${BASE}/mbti?_t=${Date.now()}`)
}

export function addMbti(mbti_type: string, note?: string) {
  return api.post<{ record: MbtiRecord }>(`${BASE}/mbti`, { mbti_type, note })
}

export function listReports(limit = 12) {
  return api.get<{ reports: PersonaReport[]; count: number }>(
    `${BASE}/reports?limit=${limit}&_t=${Date.now()}`,
  )
}

export function calibrate() {
  return api.post<{ result: Record<string, unknown> }>(`${BASE}/calibrate`)
}
