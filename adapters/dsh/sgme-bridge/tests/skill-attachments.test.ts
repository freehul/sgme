/**
 * 技能缓存与附件 五工具测试（2.0 D-59 两级缓存 + T-404 附件读写）。
 *
 * 对齐 MCP 新增的 5 个工具：skill_catalog_version / skill_manifest /
 * skill_file_get / skill_file_put / skill_lint。
 *
 * 两层：客户端层断言请求契约（method/URL/鉴权/body），工具层断言格式化与降级。
 * 注：本注释段内不得出现「星号+斜杠」连写，否则会提前闭合块注释。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { SgmeClient } from '../src/sgme-client.js'
import {
  createSkillCatalogVersionTool,
  createSkillManifestTool,
  createSkillFileGetTool,
  createSkillFilePutTool,
  createSkillLintTool,
  registerTools,
} from '../src/tools.js'
import type { SgmeClient as SgmeClientType } from '../src/sgme-client.js'

/** 记录每次 fetch 的入参，供断言请求契约。 */
let calls: Array<{ url: string; init?: RequestInit }> = []

/** 响应体 + 可选原始字节/头（附件读是 octet-stream，不走 json）。 */
interface MockResp {
  ok: boolean
  status: number
  body?: unknown
  bytes?: Uint8Array
  headers?: Record<string, string>
}

function makeFetchMock(resp: MockResp | (() => MockResp)) {
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), ...(init ? { init } : {}) })
    const r = typeof resp === 'function' ? resp() : resp
    const headers = new Map(Object.entries(r.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]))
    return {
      ok: r.ok,
      status: r.status,
      json: async () => r.body,
      text: async () => JSON.stringify(r.body ?? ''),
      arrayBuffer: async () => (r.bytes ?? new Uint8Array()).buffer,
      headers: { get: (name: string) => headers.get(name.toLowerCase()) ?? null },
    } as unknown as Response
  })
}

function callAt(n: number) {
  const c = calls[n]
  if (!c) throw new Error(`期望第 ${n} 次 fetch 调用，实际只有 ${calls.length} 次`)
  return c
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

/** 从请求头里取 X-API-Key（Headers 或普通对象两种形态）。 */
function lastHeader(name: string): string | undefined {
  const init = callAt(calls.length - 1).init
  const h = init?.headers
  if (!h) return undefined
  if (h instanceof Headers) return h.get(name) ?? undefined
  return (h as Record<string, string>)[name]
}

function makeMockClient(overrides: Partial<SgmeClientType>): SgmeClientType {
  return overrides as unknown as SgmeClientType
}

describe('sgme-client 技能缓存/附件方法', () => {
  let originalFetch: typeof globalThis.fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
    calls = []
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('skillFileGet 走 GET /v1/skills/{name}/files/{relpath}（Agent Key，路径逐段编码）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      bytes: new TextEncoder().encode('指南正文'),
      headers: { 'X-File-Sha256': 'a'.repeat(64) },
    })
    const r = await makeClient().skillFileGet('demo', 'references/指南 1.md')
    expect(r.status).toBe(200)
    expect(r.data?.toString('utf8')).toBe('指南正文')
    expect(r.sha256).toBe('a'.repeat(64))
    expect(callAt(0).url).toBe(
      'http://127.0.0.1:9910/v1/skills/demo/files/references/%E6%8C%87%E5%8D%97%201.md',
    )
    expect(lastHeader('X-API-Key')).toBe('agt_test')
  })

  it('skillFileGet 非 2xx → 带 status 的错误（不抛异常）', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 404, body: { error: 'not found' } })
    const r = await makeClient().skillFileGet('demo', 'references/none.md')
    expect(r.status).toBe(404)
    expect(r.data).toBeUndefined()
    expect(r.error).toContain('404')
  })

  it('skillFilePut 走 PUT /v1/admin/skills/{name}/files/{relpath}（Admin Key）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { ok: true, path: 'references/x.md', bytes: 6 },
    })
    const r = await makeClient().skillFilePut('demo', 'references/x.md', '正文')
    expect(r?.ok).toBe(true)
    expect(callAt(0).init?.method).toBe('PUT')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/skills/demo/files/references/x.md')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({ content: '正文' })
    expect(lastHeader('X-API-Key')).toBe('adm_test')
  })

  it('skillLint 短正文走 GET + ?content=（只读语义），Admin Key', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { ok: true, errors: [] } })
    const r = await makeClient().skillLint('demo', '---\nname: demo\n---\n正文')
    expect(r?.ok).toBe(true)
    expect(callAt(0).init?.method).toBe('GET')
    expect(callAt(0).url).toContain('/v1/admin/skills/demo/lint?content=')
    expect(callAt(0).init?.body).toBeUndefined()
    expect(lastHeader('X-API-Key')).toBe('adm_test')
  })

  it('skillLint 超长正文改走 POST 兼容别名（GET 请求行会被代理/服务器拒绝）', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { ok: false, errors: [] } })
    const long = 'x'.repeat(13000)
    await makeClient().skillLint('demo', long)
    expect(callAt(0).init?.method).toBe('POST')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/skills/demo/lint')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({ content: long })
  })
})

describe('技能缓存/附件工具层', () => {
  it('skill_catalog_version 直接回版本串', async () => {
    const tool = createSkillCatalogVersionTool(
      makeMockClient({ skillCatalogVersion: async () => 'abc123' } as Partial<SgmeClientType>),
    )
    expect(await tool.execute({}, {} as never)).toBe('catalog_version=abc123')
  })

  it('skill_catalog_version 失败给可诊断提示', async () => {
    const tool = createSkillCatalogVersionTool(
      makeMockClient({ skillCatalogVersion: async () => null } as Partial<SgmeClientType>),
    )
    expect(String(await tool.execute({}, {} as never))).toContain('skill_catalog_version 失败')
  })

  it('skill_manifest 渲染附件清单（path/size/sha256 前缀）', async () => {
    const tool = createSkillManifestTool(
      makeMockClient({
        skillManifest: async () => ({
          name: 'demo',
          version: '1.2.0',
          package_sha256: 'f'.repeat(64),
          content_len: 100,
          updated_at: '2026-11-02T00:00:00Z',
          files: [{ path: 'references/a.md', sha256: 'b'.repeat(64), size: 12 }],
        }),
      } as Partial<SgmeClientType>),
    )
    const out = String(await tool.execute({ name: 'demo' }, {} as never))
    expect(out).toContain('package_sha256=')
    expect(out).toContain('references/a.md')
    expect(out).toContain('12 B')
  })

  it('skill_file_get 文本附件回原文', async () => {
    const tool = createSkillFileGetTool(
      makeMockClient({
        skillFileGet: async () => ({
          status: 200,
          data: Buffer.from('踩坑记录', 'utf8'),
          sha256: 'c'.repeat(64),
        }),
      } as Partial<SgmeClientType>),
    )
    expect(await tool.execute({ name: 'demo', relpath: 'references/a.md' }, {} as never)).toBe(
      '踩坑记录',
    )
  })

  it('skill_file_get 二进制附件只给摘要 + 指路 HTTP（字节不进对话）', async () => {
    const tool = createSkillFileGetTool(
      makeMockClient({
        skillFileGet: async () => ({
          status: 200,
          data: Buffer.from([0x00, 0x01, 0xff]),
          sha256: 'd'.repeat(64),
        }),
      } as Partial<SgmeClientType>),
    )
    const out = String(await tool.execute({ name: 'demo', relpath: 'assets/logo.bin' }, {} as never))
    expect(out).toContain('二进制附件')
    expect(out).toContain('/v1/skills/{name}/package')
    expect(out).toContain('d'.repeat(64))
  })

  it('skill_file_get 失败（404）给可诊断提示', async () => {
    const tool = createSkillFileGetTool(
      makeMockClient({
        skillFileGet: async () => ({ status: 404, error: 'HTTP 404: not found' }),
      } as Partial<SgmeClientType>),
    )
    const out = String(await tool.execute({ name: 'demo', relpath: 'references/x.md' }, {} as never))
    expect(out).toContain('skill_file_get 失败')
    expect(out).toContain('HTTP 404')
  })

  it('skill_file_put 成功回写入路径', async () => {
    const tool = createSkillFilePutTool(
      makeMockClient({
        skillFilePut: async () => ({ ok: true, path: 'references/x.md', bytes: 6 }),
      } as Partial<SgmeClientType>),
    )
    const out = String(
      await tool.execute({ name: 'demo', relpath: 'references/x.md', content: '正文' }, {} as never),
    )
    expect(out).toContain('skill_file_put 已写入')
    expect(out).toContain('references/x.md')
  })

  it('skill_lint 未通过时逐条列出 errors/warnings', async () => {
    const tool = createSkillLintTool(
      makeMockClient({
        skillLint: async () => ({
          ok: false,
          name: 'demo',
          errors: [{ level: 'error', rule: 'description_empty', message: '描述不能为空' }],
          warnings: [{ level: 'warn', rule: 'description_trigger', message: '建议触发式写法' }],
          violations: ['描述不能为空'],
        }),
      } as Partial<SgmeClientType>),
    )
    const out = String(await tool.execute({ name: 'demo', content: '---\n---\n正文' }, {} as never))
    expect(out).toContain('skill_lint 未通过')
    expect(out).toContain('description_empty')
    expect(out).toContain('建议触发式写法')
  })
})

describe('工具注册面', () => {
  it('五个新工具全部挂载（总数 53）', () => {
    const registered: string[] = []
    const ctx = {
      tools: { register: (tool: unknown) => { registered.push((tool as { name: string }).name); return () => {} } },
    }
    registerTools(ctx as unknown as Parameters<typeof registerTools>[0], makeMockClient({}), 5)
    expect(registered).toHaveLength(53)
    for (const name of [
      'skill_catalog_version', 'skill_manifest', 'skill_file_get', 'skill_file_put', 'skill_lint',
    ]) {
      expect(registered).toContain(name)
    }
  })
})
