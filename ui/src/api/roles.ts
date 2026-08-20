// 角色 API 封装（ST-25/T-35 Care Engine 角色层，数据源见 SGME-CareEngine设计-v0.1）
import { api } from './client'

// ---------- GET /v1/admin/roles ----------
export interface RoleItem {
  role_id: string
  name: string
  description: string
  updated_at: string | null
}
export interface RoleListResp {
  roles: RoleItem[]
  total: number
}

// ---------- GET /v1/admin/roles/{role_id} ----------
export interface RoleCardData {
  name: string
  description: string
  personality?: string | null
  scenario?: string | null
  first_mes?: string | null
  mes_example?: string | null
  system_prompt?: string | null
  post_history_instructions?: string | null
  character_book?: unknown
  extensions?: {
    sgme_care?: {
      greeting_templates?: string[]
      trigger_rules?: Record<string, string>
      frequency?: Record<string, unknown>
    }
  }
  created_at?: string
  updated_at?: string
}
export interface RoleCard {
  spec: string
  spec_version: string
  data: RoleCardData
  role_id?: string
}
export interface RoleDetailResp {
  role: RoleCard
}

// ---------- POST /v1/admin/roles/{role_id} ----------
export interface RoleSaveResp {
  role_id: string
  status: string
}

// ---------- DELETE /v1/admin/roles/{role_id} ----------

// ---------- persona ----------
export interface PersonaResp {
  role_id: string
  persona?: string
  path?: string
  provider?: string
}

export function listRoles() {
  return api.get<RoleListResp>('/v1/admin/roles')
}
export function getRole(roleId: string) {
  return api.get<RoleDetailResp>(`/v1/admin/roles/${roleId}`)
}
export function saveRole(roleId: string, data: RoleCardData) {
  return api.post<RoleSaveResp>(`/v1/admin/roles/${roleId}`, { data })
}
export function archiveRole(roleId: string) {
  return api.delete<RoleSaveResp>(`/v1/admin/roles/${roleId}`)
}
export function getPersona(roleId: string) {
  return api.get<PersonaResp>(`/v1/admin/roles/${roleId}/persona`)
}
export function generatePersona(roleId: string) {
  return api.post<PersonaResp>(`/v1/admin/roles/${roleId}/persona`)
}

// ---------- 当前角色（T-40） ----------
export interface ActiveRoleResp {
  role_id: string | null
}
export function getActiveRole() {
  return api.get<ActiveRoleResp>('/v1/admin/care/active-role')
}
export function setActiveRole(roleId: string) {
  return api.put<ActiveRoleResp>('/v1/admin/care/active-role', { role_id: roleId })
}

// ---------- 装配预览（T-40） ----------
export interface AssembleResp {
  role_id: string
  role_name: string
  system_prompt: string
  persona: string | null
  profile_blocks: unknown[]
  care_policy: Record<string, unknown> | null
}
export function assembleRole(roleId: string, injectMode?: string) {
  const qs = injectMode ? `?inject_mode=${injectMode}` : ''
  return api.get<AssembleResp>(`/v1/admin/roles/${roleId}/assemble${qs}`)
}

// ---------- 关怀信号（T-41 / ST-27 T-57） ----------
export interface CareSignal {
  event_id: string
  type: string
  source: string
  payload: string
  ts: string
  consumed_at: string | null
  consumed_by: string | null
}
export interface CareSignalsResp {
  signals: CareSignal[]
  total: number
}
export interface CareScanResp {
  scan: Record<string, number>
}
export function scanCareSignals() {
  return api.post<CareScanResp>('/v1/admin/care/scan')
}
export function listCareSignals(opts: { unconsumedOnly?: boolean; signalType?: string; limit?: number } = {}) {
  const qs = new URLSearchParams()
  if (opts.unconsumedOnly) qs.set('unconsumed_only', 'true')
  if (opts.signalType) qs.set('signal_type', opts.signalType)
  if (opts.limit) qs.set('limit', String(opts.limit))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<CareSignalsResp>(`/v1/admin/care/signals${suffix}`)
}
// 原子认领（谁消费谁标记）：认领成功 200，已被他人消费 409（前端按 ApiError.status 判断）
export function claimCareSignal(eventId: string) {
  return api.post<{ event_id: string; status: string; agent_id: string | null }>(
    `/v1/admin/care/signals/${eventId}/consume`,
  )
}
// 写消费回执（ST-27 T-57）：claimed / acked / failed
export function ackCareSignal(eventId: string, status: 'claimed' | 'acked' | 'failed', result?: string) {
  return api.post<{ event_id: string; agent_id: string; status: string }>(
    `/v1/admin/care/signals/${eventId}/ack`,
    { status, result },
  )
}
// 批量清空未消费信号（T-87）：全部标记已消费（幂等，二次调用 consumed=0）
// 可选 type 过滤（如 anomaly_warn / care_daily）；后端为管理端点（admin key）
export interface ConsumeAllResp {
  consumed: number
  type: string | null
  subscriber_id: string | null
}
export function consumeAllCareSignals(opts: { signalType?: string } = {}) {
  const qs = new URLSearchParams()
  if (opts.signalType) qs.set('type', opts.signalType)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.post<ConsumeAllResp>(`/v1/admin/events/consume_all${suffix}`)
}
