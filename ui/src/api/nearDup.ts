// 近似重复隔离区 API（/v1/admin/near-dup/*，2026-11-05 B313）
//
// 灰区 = 提炼时判定「像重复」的新记忆：不进记忆池。
// 2026-11-08 起 ≥threshold 自动 discarded（行保留可翻案）；pending 仅剩遗留/关闭 auto_discard 的条目。
//
// 端点形状以服务端 sgme/server/routes_review.py 为准：
//   GET  /v1/admin/near-dup/quarantine?status=&limit=&offset=
//   POST /v1/admin/near-dup/{qid}/discard  body {reason?}
//   POST /v1/admin/near-dup/{qid}/restore  body {reason?}
// 理由【非必填】（用户 2026-11-05 拍板）：批量清理要快；但非空时留痕，
// 供后续调阈值时回算「被丢掉的是哪一类」。
import { api } from './client'

export interface QuarantineItem {
  id: number
  content: string
  content_hash: string
  matched_memory_id: string
  /** 被撞上的既有记忆原文——对照视图的右侧；缺失说明该记忆已归档/删除 */
  matched_content: string | null
  score: number
  source_ref: string | null
  dimension_ids: string[]
  created_at: string
  status: 'pending' | 'discarded' | 'restored'
  restored_memory_id: string | null
  restore_reason: string | null
  discard_reason: string | null
}

export interface QuarantinePage {
  items: QuarantineItem[]
  total: number
}

export type QuarantineStatus = 'pending' | 'discarded' | 'restored'

export function listQuarantine(status: QuarantineStatus = 'pending', limit = 20, offset = 0) {
  const qs = new URLSearchParams({ status, limit: String(limit), offset: String(offset) })
  return api.get<QuarantinePage>('/v1/admin/near-dup/quarantine?' + qs.toString())
}

export interface DiscardResult {
  id: number
  ok: boolean
  status: string
  reason: string | null
}

export function discardQuarantine(qid: number, reason?: string) {
  return api.post<DiscardResult>(
    '/v1/admin/near-dup/' + String(qid) + '/discard',
    reason ? { reason } : {},
  )
}

export interface RestoreResult {
  id: number
  memory_id: string
  status: string
  reason: string
}

// supersede=true（默认）：人工放行 = 判定新内容更准 → 新记忆 priority=80，
// 被撞上的旧记忆 mark_superseded（退出注入/召回，不删可溯源）。
// supersede=false：两条并存——适用于「都是独立事实、只是措辞相似」。
export function restoreQuarantine(qid: number, reason?: string, supersede = true) {
  const body: Record<string, unknown> = { supersede }
  if (reason) body.reason = reason
  return api.post<RestoreResult>('/v1/admin/near-dup/' + String(qid) + '/restore', body)
}

// 相似度分档（与梦境日报、人工复核习惯同口径）：
// >=0.95 基本可整批丢 / 0.90-0.95 逐条看 / <0.90 倾向是独立事实
export type ScoreBand = 'all' | 'high' | 'mid' | 'low'

export function inBand(score: number, band: ScoreBand): boolean {
  if (band === 'all') return true
  if (band === 'high') return score >= 0.95
  if (band === 'mid') return score >= 0.90 && score < 0.95
  return score < 0.90
}
