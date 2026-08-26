// API 客户端：统一 fetch 封装 + Admin Key 管理 + ERR_* 错误处理
// 数据源：/v1 与 /v1/admin 契约（架构 v0.9 §22）

const KEY_STORAGE = 'sgme_admin_key'
const AGENT_KEY_STORAGE = 'sgme_agent_key'

export function getAdminKey(): string {
  return localStorage.getItem(KEY_STORAGE) || ''
}

export function setAdminKey(key: string): void {
  if (key) localStorage.setItem(KEY_STORAGE, key)
}

export function clearAdminKey(): void {
  localStorage.removeItem(KEY_STORAGE)
}

export function getAgentKey(): string {
  return localStorage.getItem(AGENT_KEY_STORAGE) || ''
}

export function setAgentKey(key: string): void {
  if (key) localStorage.setItem(AGENT_KEY_STORAGE, key)
}

export function clearAgentKey(): void {
  localStorage.removeItem(AGENT_KEY_STORAGE)
}

// 自动填充密钥：仅本机来源可用（后端 /v1/admin/keys 校验 localhost），
// 首次打开页面时自动填入 admin/agent key，免手动输入（2026-08-13 用户需求）。
export async function autoFillKeys(): Promise<{ filledAdmin: boolean; filledAgent: boolean }> {
  const filled = { filledAdmin: false, filledAgent: false }
  // 已保存则跳过（尊重用户手动配置，避免覆盖）
  if (getAdminKey() && getAgentKey()) return filled
  try {
    const resp = await fetch('/v1/admin/keys', { method: 'GET' })
    if (!resp.ok) return filled
    const data = await resp.json()
    if (!getAdminKey() && data.admin_key) {
      setAdminKey(data.admin_key)
      filled.filledAdmin = true
    }
    if (!getAgentKey() && data.agent_key) {
      setAgentKey(data.agent_key)
      filled.filledAgent = true
    }
  } catch {
    // 后端不可达/报错 → 静默跳过（不阻塞页面），用户可手动填
  }
  return filled
}

// 需要 Agent Key 的端点路径前缀（后端 require_agent_key 的端点走 agent key：
// /v1/search、/v1/wiki/*、/v1/admin/roles/*、/v1/admin/care/*）
const AGENT_KEY_PATHS = ['/v1/wiki/', '/v1/search', '/v1/skills', '/v1/admin/roles/', '/v1/admin/care/', '/v1/admin/persona/']

function pickKey(path: string, explicit?: string): string {
  if (explicit) return explicit
  const needsAgent = AGENT_KEY_PATHS.some((p) => path.startsWith(p))
  if (needsAgent) {
    // Agent Key 优先；未填时回退 Admin Key（2026-08-18：后端 is_agent
    // 接受 admin key，用户只填一个 key 也能用检索/wiki/角色等 agent 端点）
    return getAgentKey() || getAdminKey()
  }
  return getAdminKey()
}

// 统一错误结构 {error: {code, message, details?}}
export class ApiError extends Error {
  code: string
  status: number
  details?: unknown
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  key?: string
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, key } = opts
  const headers: Record<string, string> = {}
  const useKey = pickKey(path, key)
  if (useKey) headers['X-API-Key'] = useKey
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const resp = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (!resp.ok) {
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
      // 非 JSON 响应，保留默认错误
    }
    throw new ApiError(resp.status, code, message, details)
  }
  return (await resp.json()) as T
}

export const api = {
  get<T>(path: string, key?: string) {
    return request<T>(path, { method: 'GET', key })
  },
  post<T>(path: string, body?: unknown, key?: string) {
    return request<T>(path, { method: 'POST', body, key })
  },
  put<T>(path: string, body?: unknown, key?: string) {
    return request<T>(path, { method: 'PUT', body, key })
  },
  patch<T>(path: string, body?: unknown, key?: string) {
    return request<T>(path, { method: 'PATCH', body, key })
  },
  delete<T>(path: string, body?: unknown, key?: string) {
    return request<T>(path, { method: 'DELETE', body, key })
  },
}