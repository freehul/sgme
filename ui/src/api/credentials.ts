// 凭据管理 API 封装（EP-11：/v1/credentials/*）
// 安全红线：值（value）只在请求体与导出下载链路中瞬时存在，
// 禁止写入 console.log、localStorage 或任何持久化位置
import { api, getAdminKey, getAgentKey } from './client'

// 列表元数据（后端不含值）
export interface CredentialMeta {
  key_name: string
  description: string | null
  created_at: string
  updated_at: string
  updated_by: string
}

export interface CredentialWriteResult {
  ok: boolean
  key_name: string
  created: boolean
  updated_at: string
}

// 读端点（list/export）后端 require_agent_key：agent key 优先，回退 admin key
// （后端 AgentKeyStore.is_agent 接受 admin key）
const readKey = (): string => getAgentKey() || getAdminKey()
// 写端点（write/delete/restore）后端 require_admin_key
const writeKey = (): string => getAdminKey()

export function listCredentials() {
  return api.get<{ credentials: CredentialMeta[]; count: number }>('/v1/credentials', readKey())
}

export function writeCredential(keyName: string, value: string, description?: string) {
  return api.post<CredentialWriteResult>(
    '/v1/credentials',
    { key_name: keyName, value, description: description || null },
    writeKey(),
  )
}

export function deleteCredential(keyName: string) {
  return api.delete<{ ok: boolean; key_name: string; changed: boolean }>(
    `/v1/credentials/${keyName}`,
    undefined,
    writeKey(),
  )
}

export function restoreCredential(keyName: string) {
  return api.post<{ ok: boolean; key_name: string; changed: boolean }>(
    `/v1/credentials/${keyName}/restore`,
    undefined,
    writeKey(),
  )
}

// ⚠️ 危险操作：返回全部明文值映射。调用方（CredentialsTab）必须：
// 二次确认 → 拿到即组装下载 → 引用即弃，不进 console / 不落任何存储
export async function exportCredentials(): Promise<Record<string, string>> {
  const data = await api.get<{ credentials: Record<string, string>; count: number }>(
    '/v1/credentials/export',
    readKey(),
  )
  return data.credentials
}
