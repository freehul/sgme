/**
 * sgme-client.ts 测试 — mock fetch 验证 4 端点 + toL0 格式化。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { SgmeClient, toL0 } from '../src/sgme-client.js'

// ---------- fetch mock 工具 ----------

interface MockResp {
  ok: boolean
  status: number
  body: unknown
}

function makeFetchMock(resp: MockResp | (() => MockResp)) {
  return vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => {
    const r = typeof resp === 'function' ? resp() : resp
    return {
      ok: r.ok,
      status: r.status,
      json: async () => r.body,
      text: async () => JSON.stringify(r.body),
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

describe('SgmeClient', () => {
  let originalFetch: typeof globalThis.fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  // ---------- search ----------

  it('search 成功返回 SearchResponse', async () => {
    const mockResp: MockResp = {
      ok: true,
      status: 200,
      body: {
        results: [
          { rank: 1, source: 'memory', content: '测试记忆', routes: ['bm25'] },
        ],
        meta: { routes: ['bm25'], rrf_k: 60 },
      },
    }
    globalThis.fetch = makeFetchMock(mockResp) as unknown as typeof globalThis.fetch

    const client = makeClient()
    const resp = await client.search({ query: '测试', scopes: ['memory'] })

    expect(resp).not.toBeNull()
    expect(resp!.results).toHaveLength(1)
    expect(resp!.results[0]!.content).toBe('测试记忆')
    expect(resp!.meta.rrf_k).toBe(60)
  })

  it('search 传正确的 Agent Key header', async () => {
    let capturedInit: RequestInit | undefined
    globalThis.fetch = vi.fn(async (_url, init) => {
      capturedInit = init
      return {
        ok: true,
        status: 200,
        json: async () => ({ results: [], meta: { routes: ['bm25'], rrf_k: 60 } }),
        text: async () => '',
      } as Response
    }) as unknown as typeof globalThis.fetch

    const client = makeClient()
    await client.search({ query: 'x' })

    const headers = capturedInit!.headers as Record<string, string>
    expect(headers['X-API-Key']).toBe('agt_test')
    expect(headers['Content-Type']).toBe('application/json')
  })

  it('search HTTP 错误返回 null', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 500, body: { error: 'err' } }) as unknown as typeof globalThis.fetch
    const client = makeClient()
    expect(await client.search({ query: 'x' })).toBeNull()
  })

  it('search 网络异常返回 null', async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error('network error')
    }) as unknown as typeof globalThis.fetch
    const client = makeClient()
    expect(await client.search({ query: 'x' })).toBeNull()
  })

  // ---------- inject ----------

  it('inject 成功返回 InjectResponse', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: {
        blocks: [{ title: 'identity', items: [{ content: '用户偏好' }] }],
        stats: { mode: 'daily', queries: 1, tokens_est: 100, tier0_present: false },
        tier0: { present: false, content: null },
      },
    }) as unknown as typeof globalThis.fetch

    const client = makeClient()
    const resp = await client.inject({ mode: 'daily' })

    expect(resp).not.toBeNull()
    expect(resp!.blocks).toHaveLength(1)
    expect(resp!.blocks[0]!.title).toBe('identity')
  })

  it('inject 失败返回 null', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 400, body: {} }) as unknown as typeof globalThis.fetch
    const client = makeClient()
    expect(await client.inject({ mode: 'invalid' })).toBeNull()
  })

  // ---------- append ----------

  it('append 成功返回 AppendResponse（新建）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { file_id: 'f1', path: 'raw/sessions/f1.md', status: 'new' },
    }) as unknown as typeof globalThis.fetch

    const client = makeClient()
    const resp = await client.append({
      session_key: 'dsh-test',
      started_at: '2026-08-14T10:00:00Z',
      content: '# test',
      agent_id: 'dsh',
    })

    expect(resp).not.toBeNull()
    expect(resp!.file_id).toBe('f1')
    expect(resp!.status).toBe('new')
    expect(resp!.idempotent).toBeUndefined()
  })

  it('append 幂等命中返回 idempotent=true', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { file_id: 'f1', path: 'raw/sessions/f1.md', status: 'exists', idempotent: true },
    }) as unknown as typeof globalThis.fetch

    const client = makeClient()
    const resp = await client.append({
      session_key: 'dsh-test',
      started_at: '2026-08-14T10:00:00Z',
      content: '# test',
    })

    expect(resp!.idempotent).toBe(true)
  })

  it('append 失败返回 null', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 500, body: {} }) as unknown as typeof globalThis.fetch
    const client = makeClient()
    expect(await client.append({
      session_key: 'x', started_at: 'x', content: 'x',
    })).toBeNull()
  })

  // ---------- triggerRefine ----------

  it('triggerRefine 成功返回（状态码 200）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: {
        triggered: 'async',
        file_id: 'batch',
        status: 'queued',
        note: '后台执行',
      },
    }) as unknown as typeof globalThis.fetch

    const client = makeClient()
    const resp = await client.triggerRefine({ limit: 50 })

    expect(resp).not.toBeNull()
    expect(resp!.triggered).toBe('async')
    expect(resp!.status).toBe('queued')
  })

  it('triggerRefine 用 Admin Key（不是 Agent Key）', async () => {
    let capturedInit: RequestInit | undefined
    globalThis.fetch = vi.fn(async (_url, init) => {
      capturedInit = init
      return {
        ok: true, status: 200,
        json: async () => ({ triggered: 'async', file_id: 'batch', status: 'queued', note: '' }),
        text: async () => '',
      } as Response
    }) as unknown as typeof globalThis.fetch

    const client = makeClient()
    await client.triggerRefine({ limit: 50 })

    const headers = capturedInit!.headers as Record<string, string>
    expect(headers['X-API-Key']).toBe('adm_test')
  })

  it('triggerRefine 失败返回 null', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 400, body: {} }) as unknown as typeof globalThis.fetch
    const client = makeClient()
    expect(await client.triggerRefine({ limit: 0 })).toBeNull()
  })

  // ---------- baseUrl 处理 ----------

  it('baseUrl 尾部斜杠被去除', async () => {
    let capturedUrl: string | undefined
    globalThis.fetch = vi.fn(async (url) => {
      capturedUrl = url as string
      return {
        ok: true, status: 200,
        json: async () => ({ results: [], meta: { routes: [], rrf_k: 60 } }),
        text: async () => '',
      } as Response
    }) as unknown as typeof globalThis.fetch

    const client = new SgmeClient({
      baseUrl: 'http://127.0.0.1:9910/',
      agentKey: 'k', adminKey: 'k', agentId: 'dsh',
    })
    await client.search({ query: 'x' })

    expect(capturedUrl).toBe('http://127.0.0.1:9910/v1/search')
  })
})

// ---------- toL0 格式化测试 ----------

describe('toL0', () => {
  it('user 消息格式正确', () => {
    const l0 = toL0([{ role: 'user', content: '你好', ts: '2026-08-14T10:00:00Z' }])
    expect(l0).toContain('# 2026-08-14T10:00:00Z user\n你好')
    expect(l0.endsWith('\n')).toBe(true)
  })

  it('assistant 消息格式正确', () => {
    const l0 = toL0([{ role: 'assistant', content: '回答', ts: '2026-08-14T10:00:01Z' }])
    expect(l0).toContain('## 2026-08-14T10:00:01Z assistant\n回答')
  })

  it('tool 消息包含工具名', () => {
    const l0 = toL0([{
      role: 'tool', content: '{"ok": true}', ts: '2026-08-14T10:00:02Z', toolName: 'memory_search',
    }])
    expect(l0).toContain('## 2026-08-14T10:00:02Z tool\n**tool**: memory_search\n{"ok": true}')
  })

  it('tool 消息无 toolName 时默认 "tool"', () => {
    const l0 = toL0([{ role: 'tool', content: '结果', ts: '2026-08-14T10:00:02Z' }])
    expect(l0).toContain('**tool**: tool')
  })

  it('多消息用空行分隔', () => {
    const l0 = toL0([
      { role: 'user', content: '问', ts: '2026-08-14T10:00:00Z' },
      { role: 'assistant', content: '答', ts: '2026-08-14T10:00:01Z' },
    ])
    expect(l0).toContain('\n\n## ')
    expect(l0.split('\n\n')).toHaveLength(2)
  })

  it('空数组返回单个换行', () => {
    const l0 = toL0([])
    expect(l0).toBe('\n')
  })
})
