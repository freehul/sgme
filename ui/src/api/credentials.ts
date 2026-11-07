// 凭据管理 API 封装（EP-11：/v1/credentials/*）
// 安全红线：值（value）只在请求体与导出下载链路中瞬时存在，
// 禁止写入 console.log、localStorage 或任何持久化位置
import { api, getAdminKey, getAgentKey } from './client'

// 列表元数据（后端不含值）。D-37 v1.1 增补 scope / cred_type / expires_at /
// last_used_at / expired 五项（后端 `expired` 为服务端判定的过期状态）
export interface CredentialMeta {
  key_name: string
  description: string | null
  /** 可见范围：all（全部 agent）/ admin（仅管理员）/ agents:<英文逗号分隔的 agent 名单>；null = 未设置 */
  scope: string | null
  /** 凭据类型：api_key / token / password / other；null = 未设置 */
  cred_type: string | null
  /** 过期时间（ISO 8601，toISOString 口径）；null = 不过期 */
  expires_at: string | null
  /** 最近一次被读端触达的时间（ISO 8601）；null = 从未使用 */
  last_used_at: string | null
  /** 已过期（服务端按 expires_at 判定；agent 列表里看不到已过期/仅管理员项） */
  expired: boolean
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

// 写入参数（D-37 v1.1）。字段语义按后端约定：
// - 缺省或 null = 不变（编辑时；新建时即不设置该字段）
// - 空串 "" = 清空该字段（编辑时的清空手段）
// - 新建时 value 必填；编辑时省略 value = 只更新元数据（不强制重填值）
// - expires_at 传 ISO 字符串（由表单 datetime-local 本地值 new Date(v).toISOString() 得到）
export interface CredentialWriteInput {
  key_name: string
  value?: string
  description?: string | null
  scope?: string | null
  cred_type?: string | null
  expires_at?: string | null
}

// 读端点（list/export）后端 require_agent_key：**管理员 Key 优先**——管理员才能
// 看到已过期与「仅管理员」项；未填时回退 agent key（后端 is_agent 接受 admin key）
const readKey = (): string => getAdminKey() || getAgentKey()
// 写端点（write/delete/restore）后端 require_admin_key
const writeKey = (): string => getAdminKey()

export function listCredentials() {
  return api.get<{ credentials: CredentialMeta[]; count: number }>('/v1/credentials', readKey())
}

// 提交请求体按调用方给的字段原样上送（未给的键由 JSON.stringify 自动省略，
// 保证「缺省 = 不变」语义；要清空请显式传空串）
export function writeCredential(input: CredentialWriteInput) {
  return api.post<CredentialWriteResult>('/v1/credentials', input, writeKey())
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
