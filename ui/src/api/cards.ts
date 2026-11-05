// core 记忆卡 API（/v1/admin/cards/*，D-79 2026-11-05）
//
// 卡片 = 关于用户本人的稳定事实（身份/家庭/作息/偏好/协作准则/目标）。
// 每次注入必带——这是「AI 知道你」的那部分。
//
// 写入规则：一槽一值覆盖式、旧值进历史、单卡 ≤400 字 / 单槽 ≤120 字。
import { api } from './client'

export interface CardSpec {
  card_id: string
  title: string
  slots: string[]
  multi: string[]
}

export interface CardSchema {
  cards: CardSpec[]
  limits: { card_max_chars: number; slot_max_chars: number }
}

export interface CardItem {
  card_id: string
  title: string
  slots: Record<string, string | string[]>
  filled: boolean
  updated_at?: string | null
  updated_by?: string | null
  version?: number
}

export interface CardHistoryItem {
  id: number
  card_id: string
  slot_key: string
  old_value: string | null
  new_value: string | null
  changed_at: string
  changed_by: string | null
  reason: string | null
}

export function cardSchema() {
  return api.get<CardSchema>('/v1/admin/cards/schema')
}

export function listCards() {
  return api.get<{ cards: CardItem[]; total: number }>('/v1/admin/cards')
}

export function updateSlot(
  cardId: string,
  slotKey: string,
  value: string | string[] | null,
  reason?: string,
) {
  const body: Record<string, unknown> = { value, changed_by: 'webui' }
  if (reason) body.reason = reason
  return api.post<{ card_id: string; slot_key: string; version: number }>(
    '/v1/admin/cards/' + encodeURIComponent(cardId) + '/slots/' + encodeURIComponent(slotKey),
    body,
  )
}

export function cardHistory(limit = 50) {
  return api.get<{ items: CardHistoryItem[]; total: number }>('/v1/admin/cards/history?limit=' + String(limit))
}

/** 单卡修改历史（GET /v1/admin/cards/{card_id}/history?limit=；limit 服务端默认 50，
 * 最新在前）。未知 card_id 返回 ERR_NOT_FOUND，调用方按空态处理。 */
export function cardHistoryOf(cardId: string, limit = 50) {
  return api.get<{ items: CardHistoryItem[]; total: number }>(
    '/v1/admin/cards/' + encodeURIComponent(cardId) + '/history?limit=' + String(limit),
  )
}
