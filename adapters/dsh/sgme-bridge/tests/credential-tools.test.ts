/**
 * T-277 凭据读侧工具测试 — credential_list / credential_get（EP-11 平级对齐）。
 *
 * 分两层：
 * - 客户端层：mock fetch，断言请求 method / URL / Agent Key / 失败归一为 null。
 * - 工具层：mock SgmeClient，断言参数传递、结果格式化、值纪律提示与降级路径。
 *
 * 契约来源：sgme/server/routes_credentials.py（GET /v1/credentials 列表不含值；
 * GET /v1/credentials/{key} 取单值记 read 审计）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { SgmeClient } from '../src/sgme-client.js'
import { createCredentialListTool, createCredentialGetTool } from '../src/tools.js'
import type { SgmeClient as SgmeClientType } from '../src/sgme-client.js'

type ToolLike = {
  name: string
  description: string
  // defineTool 把扁平参数规格归一成 JSON schema 形态（实测 shape，见 tests/credential-tools.test.ts）
  parameters: {
    type?: string
    properties?: Record<string, { type?: string; description?: string }>
    required?: string[]
  }
  execute: (args: unknown, exec?: unknown) => Promise<unknown>
}

function asToolLike(tool: unknown): ToolLike {
  return tool as unknown as ToolLike
}

function makeMockClient(overrides: Partial<SgmeClientType>): SgmeClientType {
  return overrides as unknown as SgmeClientType
}

// ---------- 客户端层：请求契约（mock fetch） ----------

interface MockResp {
  ok: boolean
  status: number
  body: unknown
}

let calls: Array<{ url: string; init?: RequestInit }> = []

/**
 * 取第 n 次 fetch 调用记录。
 *
 * tsconfig 开了 noUncheckedIndexedAccess，`calls[n]` 类型含 undefined，
 * 直接 `.url` 过不了 typecheck（vitest 只转译不检查，故只在 verify 时暴露）。
 */
function callAt(n: number) {
  const c = calls[n]
  if (!c) throw new Error(`期望第 ${n} 次 fetch 调用，实际只有 ${calls.length} 次`)
  return c
}

function makeFetchMock(resp: MockResp) {
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), ...(init ? { init } : {}) })
    return {
      ok: resp.ok,
      status: resp.status,
      json: async () => resp.body,
      text: async () => JSON.stringify(resp.body),
    } as Response
  })
}

function makeClient(): SgmeClient {
  return new SgmeClient({
    baseUrl: 'http://127.0.0.1:9910',
    agentKey: 'agt_test',
    adminKey: 'adm_test',
    agentId: 'dsh',
    timeoutMs: 1000,
  })
}

describe('SgmeClient 凭据读侧（请求契约）', () => {
  let originalFetch: typeof globalThis.fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
    calls = []
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('credentialList 走 GET /v1/credentials + Agent Key', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true, status: 200,
      body: { credentials: [{ key_name: 'db/password', description: '示例' }], count: 1 },
    }) as unknown as typeof globalThis.fetch
    const resp = await makeClient().credentialList()
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/credentials')
    expect(callAt(0).init?.method).toBe('GET')
    const headers = (callAt(0).init?.headers ?? {}) as Record<string, string>
    expect(headers['X-API-Key']).toBe('agt_test')
    expect(resp?.count).toBe(1)
  })

  it('credentialGet 对 name 做路径段编码', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true, status: 200, body: { key_name: 'db/password', value: 'v' },
    }) as unknown as typeof globalThis.fetch
    await makeClient().credentialGet('db/password')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/credentials/db%2Fpassword')
  })

  it('credentialGet 失败（非 2xx）归一为 null（故障隔离）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: false, status: 404, body: { detail: '凭据不存在' },
    }) as unknown as typeof globalThis.fetch
    const missing = await makeClient().credentialGet('nope')
    expect(missing).toBeNull()
  })
})

// ---------- 工具层：格式化与降级 ----------

describe('credential_list tool', () => {
  it('列出键名与元数据（不含值），execute 调 credentialList', async () => {
    const client = makeMockClient({
      credentialList: vi.fn(async () => ({
        credentials: [
          { key_name: 'db/password', description: '数据库口令', created_at: 'a', updated_at: 'b', updated_by: 'default(admin,http)' },
        ],
        count: 1,
      })),
    })
    const tool = asToolLike(createCredentialListTool(client))
    const result = (await tool.execute({})) as string
    expect(client.credentialList).toHaveBeenCalled()
    expect(tool.name).toBe('credential_list')
    expect(tool.description).toContain('不含值')
    expect(result).toContain('db/password')
    expect(result).toContain('数据库口令')
    expect(result).toContain('1 条')
  })

  it('凭据库为空时给出明确提示', async () => {
    const client = makeMockClient({
      credentialList: vi.fn(async () => ({ credentials: [], count: 0 })),
    })
    const result = (await asToolLike(createCredentialListTool(client)).execute({})) as string
    expect(result).toContain('为空')
  })

  it('凭据模块未启用 / Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ credentialList: vi.fn(async () => null) })
    const result = (await asToolLike(createCredentialListTool(client)).execute({})) as string
    expect(result).toContain('失败')
    expect(result).toContain('credentials.enabled')
  })
})

describe('credential_get tool', () => {
  it('execute 调 credentialGet 并回传值 + 值纪律提示', async () => {
    const client = makeMockClient({
      credentialGet: vi.fn(async () => ({
        key_name: 'db/password', value: 's3cret-value',
        description: null, updated_at: 'b', updated_by: 'default(admin,http)',
      })),
    })
    const tool = asToolLike(createCredentialGetTool(client))
    const result = (await tool.execute({ name: 'db/password' })) as string
    expect(client.credentialGet).toHaveBeenCalledWith('db/password')
    expect(tool.name).toBe('credential_get')
    expect(tool.parameters.required).toContain('name')
    expect(tool.parameters.properties?.name?.description).toContain('凭据键名')
    expect(tool.description).toContain('值不得进入对话正文')
    expect(tool.description).toContain('sgme-cred run')
    expect(result).toContain('s3cret-value')
    expect(result).toContain('不得复述')
  })

  it('凭据不存在 / Gateway 不可达时返回降级提示（不回显值）', async () => {
    const client = makeMockClient({ credentialGet: vi.fn(async () => null) })
    const result = (await asToolLike(createCredentialGetTool(client)).execute({ name: 'nope' })) as string
    expect(result).toContain('失败')
    expect(result).toContain('nope')
  })
})
