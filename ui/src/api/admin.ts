// 配置与管理 API 封装（数据源见 SGME-WebUI设计-v0.1 §4 ④）
import { api } from './client'

// ---------- 模板 ----------
export interface TemplateItem {
  name: string
  display_name: string | null
  memory_types: string[]
  token_budget: number | null
  sections: unknown[]
  content: string
  builtin: boolean
  valid: boolean
  error: string | null
}
export interface TemplatesPage {
  items: TemplateItem[]
  count: number
  total: number
  generated_at: string
}
export function listTemplates() {
  return api.get<TemplatesPage>('/v1/admin/templates')
}
export function createTemplate(body: Record<string, unknown>) {
  return api.post<Record<string, unknown>>('/v1/admin/templates', body)
}
export function updateTemplate(name: string, body: Record<string, unknown>) {
  return api.put<Record<string, unknown>>(`/v1/admin/templates/${name}`, body)
}
export function deleteTemplate(name: string) {
  return api.delete<Record<string, unknown>>(`/v1/admin/templates/${name}`)
}

// ---------- 维度注册表 ----------
export interface Dimension {
  id: string
  display_name: string
  category: string
  time_velocity: string
  ttl_days: number | null
  description: string | null
  active: boolean
  created_at: string
  aliases: string[]
}
export function listRegistry() {
  return api.get<{ total: number; dimensions: Dimension[] }>('/v1/admin/registry')
}
export function createDimension(body: Partial<Dimension>) {
  return api.post<Record<string, unknown>>('/v1/admin/registry/dimensions', body)
}
export function updateDimension(id: string, updates: Record<string, unknown>) {
  return api.put<Record<string, unknown>>(`/v1/admin/registry/dimensions/${id}`, updates)
}
export function createAlias(alias: string, dimension_id: string) {
  return api.post<Record<string, unknown>>('/v1/admin/registry/aliases', { alias, dimension_id })
}
export function deleteAlias(alias: string) {
  return api.delete<Record<string, unknown>>(`/v1/admin/registry/aliases/${alias}`)
}

// ---------- Agent 管理 ----------
export interface Agent {
  agent_id: string
  role: string
  scope: string[]
  endpoint: string | null
  status: string
  registered_at: string
  last_seen_at: string | null
  last_seen_source: string | null
  key_count: number
  key_ref: string
  agent_model: string
}
export function listAgents() {
  return api.get<{ agents: Agent[]; count: number }>('/v1/admin/agents')
}
export function registerAgent(agent_id: string, scope: string[], agent_model?: string) {
  return api.post<{ agent_id: string; api_key: string; role: string; scope: string[]; note: string }>(
    '/v1/admin/agents/register',
    { agent_id, scope, agent_model: agent_model || '' },
  )
}
export function revokeAgent(agent_id: string) {
  return api.delete<{ status: string; agent_id: string; revoked: number }>(`/v1/admin/agents/${agent_id}`)
}

// ---------- 提示词 ----------
export interface PromptVersion {
  version: string
  file: string
  sha256: string
  created_at: string
  note?: string
}
export interface PromptStage {
  stage: string
  active: string
  ab: { enabled: boolean }
  versions: PromptVersion[]
}
export function listPrompts() {
  return api.get<{ stages: PromptStage[] }>('/v1/admin/prompts')
}
export function publishPrompt(body: Record<string, unknown>) {
  return api.post<Record<string, unknown>>('/v1/admin/prompts/publish', body)
}
export function activatePrompt(body: Record<string, unknown>) {
  return api.post<Record<string, unknown>>('/v1/admin/prompts/activate', body)
}
export function getPromptMetrics() {
  return api.get<{ stage?: string; since?: string; groups: unknown[] }>('/v1/admin/prompts/metrics')
}

// ---------- 系统配置 ----------
export function getConfig() {
  return api.get<{ config: Record<string, unknown>; writable_sections: string[] }>('/v1/admin/config')
}
export function updateConfig(section: string, config: Record<string, unknown>) {
  return api.put<Record<string, unknown>>(`/v1/admin/config/${section}`, config)
}

// ---------- 备份与技能 ----------
export interface Snapshot {
  snapshot_id: string
  level: string
  path: string
}
export function createBackup(level = 'incremental') {
  return api.post<Record<string, unknown>>('/v1/admin/backup/create', { level })
}
export function listBackups() {
  return api.get<{ snapshots: Snapshot[]; total: number }>('/v1/admin/backup/list')
}
export function restoreBackup(snapshot_id: string) {
  return api.post<Record<string, unknown>>('/v1/admin/backup/restore', { snapshot_id })
}
export function syncSkills(direction = 'both') {
  return api.post<Record<string, unknown>>('/v1/admin/skills/sync', { direction })
}