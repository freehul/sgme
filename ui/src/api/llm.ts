// LLM 供应商与降级链 API 封装（数据源见后端 routes_llm.py）
import { api } from './client'

// ---------- GET /v1/admin/llm ----------
export interface LlmNode {
  provider: string
  model?: string
  base_url?: string
  api_key_env?: string
  context_window?: number
  timeout_s?: number
  health_endpoint?: string
  max_tokens?: number
  sampling?: Record<string, unknown>
  extra_body?: Record<string, unknown>
  rule?: string
}

export interface LlmChainNode extends LlmNode {
  provider: string
  model?: string
  rule?: string
}

export interface LlmRules {
  timeout_s?: number
  max_retries?: number
  fallback_on?: string[]
  backoff?: { base_s?: number; max_s?: number; jitter_s?: number }
  throttle?: { enabled?: boolean; rps?: number; burst?: number }
  context?: { reserved_output?: number; prompt_overhead?: number }
  allowed_models?: { deny_prefixes?: string[]; deny_exact?: string[] }
}

export interface LlmProviderInfo extends LlmNode {
  provider: string
  display_name?: string
  models?: string[]
  // T-44 统一供应商模型：该供应商是否同时用作向量（embedding）
  vector_capable?: boolean
}

// 向量提供商（providers.yaml 顶层 embedding 段，T-43）
export interface EmbeddingProvider {
  provider: string
  display_name?: string
  base_url?: string
  api_key_env?: string
  default_model?: string
  models?: string[]
  timeout_s?: number
  max_retries?: number
}

export interface LlmStatus {
  chains: Record<string, LlmChainNode[]>
  rules: LlmRules
  providers: Record<string, LlmProviderInfo>
  embedding: Record<string, EmbeddingProvider>
  vector_current: string
}
export function getLlm() {
  return api.get<LlmStatus>('/v1/admin/llm')
}

// ---------- GET /v1/admin/llm/health ----------
export interface ProviderHealth {
  available: boolean
  error?: string
  latency_ms?: number
}
export interface LlmHealth {
  health: Record<string, ProviderHealth>
}
export function getLlmHealth() {
  return api.get<LlmHealth>('/v1/admin/llm/health')
}

// ---------- POST/DELETE /v1/admin/llm/providers（供应商管理） ----------
export interface ProviderMutationResult {
  provider: string
  deleted?: boolean
  providers_file?: string
  providers?: Record<string, LlmProviderInfo>
}
// 新增/更新供应商（api_key_env 只存环境变量名，禁止明文 key）
export function upsertProvider(provider: string, payload: Partial<LlmProviderInfo>) {
  return api.post<ProviderMutationResult>('/v1/admin/llm/providers', { provider, payload })
}
// 删除供应商（被降级链引用时后端拒绝）
export function deleteProvider(provider: string) {
  return api.delete<ProviderMutationResult>(`/v1/admin/llm/providers/${encodeURIComponent(provider)}`)
}

// ---------- PUT /v1/admin/llm/embedding/active（T-43 切换向量提供商） ----------
export interface EmbeddingSetActiveResult {
  provider: string
  vector: { provider?: string; base_url?: string; api_key_env?: string; model?: string; enabled?: boolean }
}
export function setActiveEmbedding(provider: string) {
  return api.put<EmbeddingSetActiveResult>('/v1/admin/llm/embedding/active', { provider })
}

// ---------- PUT /v1/admin/llm/chains（T-44 降级链编辑） ----------
export interface ChainUpdateResult {
  chains: Record<string, LlmChainNode[]>
  providers_file?: string
}
// 整体更新降级链（增删节点 + 排序，写回 llm.yaml 并刷新运行时）
export function updateChains(chains: Record<string, LlmChainNode[]>) {
  return api.put<ChainUpdateResult>('/v1/admin/llm/chains', { chains })
}