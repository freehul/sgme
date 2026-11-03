/**
 * D-58（2026-11-04）：适配器启动能力面自检测试。
 *
 * 口径（设计 §9.7）：读 GET /v1/capabilities.api_compat 与适配器声明比较；
 * 不匹配/端点缺失只返回结构化结果供日志提示，**绝不抛异常阻塞插件**。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  SgmeClient, SUPPORTED_API_COMPAT,
} from '../src/sgme-client.js'

interface MockResp {
  ok: boolean
  status: number
  body: unknown
}

function makeFetchMock(resp: MockResp) {
  return vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => ({
    ok: resp.ok,
    status: resp.status,
    json: async () => resp.body,
    text: async () => JSON.stringify(resp.body),
  } as Response))
}

function makeClient(): SgmeClient {
  return new SgmeClient({
    baseUrl: 'http://127.0.0.1:9910',
    agentKey: 'agt_test',
    adminKey: 'adm_test',
    agentId: 'dsh',
    timeoutMs: 500,
  })
}

describe('capabilities（D-58 能力面自检）', () => {
  let originalFetch: typeof globalThis.fetch

  beforeEach(() => { originalFetch = globalThis.fetch })
  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('capabilities 正常返回三字段', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true, status: 200,
      body: {
        server: { name: 'sgme', version: '2.0.4' },
        schema_version: 6,
        api_compat: '1.x',
        catalog_version: 'abc123',
      },
    })
    const caps = await makeClient().capabilities()
    expect(caps?.api_compat).toBe('1.x')
    expect(caps?.schema_version).toBe(6)
  })

  it('兼容族一致 → ok=true', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true, status: 200,
      body: { schema_version: 6, api_compat: '1.x' },
    })
    const c = await makeClient().checkApiCompat()
    expect(c.ok).toBe(true)
    expect(c.reason).toBeNull()
    expect(c.supported).toBe(SUPPORTED_API_COMPAT)
  })

  it('兼容族不一致 → mismatch（不抛异常）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true, status: 200,
      body: { schema_version: 7, api_compat: '2.x' },
    })
    const c = await makeClient().checkApiCompat()
    expect(c.ok).toBe(false)
    expect(c.reason).toBe('mismatch')
    expect(c.apiCompat).toBe('2.x')
  })

  it('端点缺失（404）→ unreachable（旧版服务端按 1.x 继续）', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 404, body: {} })
    const c = await makeClient().checkApiCompat()
    expect(c.ok).toBe(false)
    expect(c.reason).toBe('unreachable')
    expect(c.apiCompat).toBeNull()
  })
})
