// 凭据管理 API 封装（EP-11：/v1/credentials/*）
// 安全红线：值（value）只在请求体与导出下载链路中瞬时存在，
// 禁止写入 console.log、localStorage 或任何持久化位置
import { ApiError, api, getAdminKey, getAgentKey } from './client'

// 列表元数据（后端不含值）。D-37 v1.1 增补 scope / cred_type / expires_at /
// last_used_at / expired 五项（后端 `expired` 为服务端判定的过期状态）
export interface CredentialMeta {
  key_name: string
  description: string | null
  /** 可见范围：all（全部 agent）/ admin（仅管理员）/ agents:<英文逗号分隔的 agent 名单>；null = 未设置 */
  scope: string | null
  /** 凭据类型：api_key / token / password / other / file（重要数据）；null = 未设置 */
  cred_type: string | null
  /** 过期时间（ISO 8601，toISOString 口径）；null = 不过期 */
  expires_at: string | null
  /** 最近一次被读端触达的时间（ISO 8601）；null = 从未使用 */
  last_used_at: string | null
  /** 已过期（服务端按 expires_at 判定；agent 列表里看不到已过期/仅管理员项） */
  expired: boolean
  /** D-37 v1.2 文件类凭据（cred_type='file'）的原始文件名；其它类型为 null */
  filename: string | null
  /** D-37 v1.2 文件字节数（cred_type='file' 时有值） */
  file_size: number | null
  /** D-37 v1.2 文件 SHA-256（cred_type='file' 时有值） */
  file_sha256: string | null
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

// ---------- 文件 / 二进制「重要数据」通道（D-37 v1.2） ----------
// 安全红线与值通道一致：文件字节只在 fetch ↔ Blob 之间瞬时存在，
// 不写 console、不进 localStorage / sessionStorage、不落任何持久化位置。

/** 上传结果（POST /v1/credentials/{key}/file） */
export interface CredentialFileUploadResult {
  ok: boolean
  key_name: string
  /** 已落盘的文件名（后端按 query 的 filename 归一后返回） */
  filename: string
  /** 字节数 */
  size: number
  /** 文件内容的 SHA-256（小写十六进制） */
  sha256: string
  updated_at: string
  scope: string | null
  cred_type: string | null
  expires_at: string | null
}

/** 下载结果：二进制 Blob + 服务端 Content-Disposition 给出的文件名（缺失为 null） */
export interface CredentialFileDownload {
  blob: Blob
  filename: string | null
}

// client.ts 的 request / requestBlob 只走 JSON 通道（body 会被 JSON.stringify），
// 承载不了原始二进制；上传按同一 ApiError 口径自建非 2xx 解析，
// 让调用方仍能拿到 status / code（413 = 超过后端 max_file_bytes 上限）。
async function throwRawApiError(resp: Response): Promise<never> {
  let code = 'ERR_INTERNAL'
  let message = `请求失败: ${resp.status}`
  let details: unknown
  try {
    const data = await resp.json()
    if (data?.error) {
      code = data.error.code || code
      message = data.error.message || message
      details = data.error.details
    }
  } catch {
    // 非 JSON 响应（如网关层 413 文本）：保留默认错误文案
  }
  throw new ApiError(resp.status, code, message, details)
}

/**
 * 上传 / 替换文件（POST /v1/credentials/{key}/file?filename=<name>；admin key）。
 * - 键不存在 = 创建「文件」类型凭据；键已存在 = 替换文件内容
 * - body 为原始字节（octet-stream），不走 JSON 通道
 * - filename 省略时用 file.name
 */
export async function uploadCredentialFile(
  keyName: string,
  file: File,
  filename?: string,
): Promise<CredentialFileUploadResult> {
  const name = (filename ?? file.name ?? '').trim()
  const query = name ? '?filename=' + encodeURIComponent(name) : ''
  const headers: Record<string, string> = { 'Content-Type': 'application/octet-stream' }
  const adminKey = writeKey()
  if (adminKey) headers['X-API-Key'] = adminKey
  const resp = await fetch(`/v1/credentials/${encodeURIComponent(keyName)}/file${query}`, {
    method: 'POST',
    headers,
    body: file,
  })
  if (!resp.ok) await throwRawApiError(resp)
  return (await resp.json()) as CredentialFileUploadResult
}

// Content-Disposition 文件名解析：优先 RFC 5987 filename*=UTF-8''…（非 ASCII 名），
// 回退普通 filename="…"；都解析不出返回 null（调用方用列表里的 filename 兜底）。
function filenameFromDisposition(disposition: string | null): string | null {
  if (!disposition) return null
  const star = /filename\*\s*=\s*([^;]+)/i.exec(disposition)
  if (star) {
    const raw = star[1].trim().replace(/^["']+|["']+$/g, '')
    const ext = /^[^']*'[^']*'(.*)$/.exec(raw) // charset'lang'<pct-encoded>
    const pct = ext ? ext[1] : raw
    try {
      return decodeURIComponent(pct)
    } catch {
      return pct
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(disposition)
  if (!plain) return null
  const value = (plain[1] ?? plain[2] ?? '').trim()
  if (!value) return null
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

/**
 * 下载文件（GET /v1/credentials/{key}/file；read key = 管理员优先，缺失回退 agent key）。
 * 返回 Blob 与服务端文件名——调用方负责 a[download] 触发，字节不落 console / 存储。
 * 注意：文件键的 GET /v1/credentials/{key} 返回 400，取内容一律走本函数。
 */
export async function downloadCredentialFile(keyName: string): Promise<CredentialFileDownload> {
  const { blob, headers } = await api.blob(
    `/v1/credentials/${encodeURIComponent(keyName)}/file`,
    readKey(),
  )
  return { blob, filename: filenameFromDisposition(headers.get('Content-Disposition')) }
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
