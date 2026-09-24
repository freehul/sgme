/**
 * SGME 1.2.2 对齐测试 — 新增 15 工具 + 客户端 12 方法。
 *
 * 分两层：
 * - 客户端层：mock fetch，断言请求 method / URL / 鉴权 key / body，
 *   以及失败（非 2xx）时归一到 null 的故障隔离语义。
 * - 工具层：mock SgmeClient，断言参数传递、结果格式化与降级提示。
 *
 * 契约来源：sgme/server/routes_memory.py（answer/unreject）、routes_admin.py
 * （stats/refine 三兄弟/consume_all）、routes_config.py、routes_skills.py（materialize）、
 * routes_skills_admin.py（put/delete/rename）。
 *
 * 注：本注释段内不得出现「星号+斜杠」连写，否则会提前闭合块注释（esbuild 解析报错）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { SgmeClient } from '../src/sgme-client.js'
import {
  createAnswerTool,
  createHealthTool,
  createStatsTool,
  createMemoryUnrejectTool,
  createSignalClearTool,
  createWikiEvolveTriggerTool,
  createConfigGetTool,
  createConfigUpdateTool,
  createRefineStatusTool,
  createRefineTriggerTool,
  createRefineBatchTool,
  createSkillMaterializeTool,
  createSkillPutTool,
  createSkillDeleteTool,
  createSkillRenameTool,
  registerTools,
} from '../src/tools.js'
import type { SgmeClient as SgmeClientType } from '../src/sgme-client.js'

// ---------- 公共 mock 工具 ----------

interface MockResp {
  ok: boolean
  status: number
  body: unknown
}

/** 记录每次 fetch 的入参，供断言请求契约。 */
let calls: Array<{ url: string; init?: RequestInit }> = []

function makeFetchMock(resp: MockResp | (() => MockResp)) {
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), ...(init ? { init } : {}) })
    const r = typeof resp === 'function' ? resp() : resp
    return {
      ok: r.ok,
      status: r.status,
      json: async () => r.body,
      text: async () => JSON.stringify(r.body),
    } as Response
  })
}

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

function makeClient(): SgmeClient {
  return new SgmeClient({
    baseUrl: 'http://127.0.0.1:9910',
    agentKey: 'agt_test',
    adminKey: 'adm_test',
    agentId: 'dsh',
    timeoutMs: 1000,
  })
}

/** 取最近一次请求的 header 中某个 key。 */
function lastHeader(name: string): string | undefined {
  const init = calls[calls.length - 1]?.init
  const headers = (init?.headers ?? {}) as Record<string, string>
  return headers[name]
}

type ToolLike = {
  name: string
  description: string
  parameters: Record<string, { description?: string }>
  execute: (args: unknown, exec?: unknown) => Promise<unknown>
}

function asToolLike(tool: unknown): ToolLike {
  return tool as unknown as ToolLike
}

function makeMockClient(overrides: Partial<SgmeClientType>): SgmeClientType {
  return overrides as unknown as SgmeClientType
}

describe('sgme-client 1.2.2 新增方法', () => {
  let originalFetch: typeof globalThis.fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
    calls = []
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  // ---------- answer（T-149） ----------

  it('answer 走 POST /v1/answer（Agent Key），body 带 query/question_type', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { answer: '3 次', question_type: 'aggregate', candidates_used: 3 },
    })
    const r = await makeClient().answer('我一共提过几次 X', 'aggregate')
    expect(r?.answer).toBe('3 次')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/answer')
    expect(callAt(0).init?.method).toBe('POST')
    expect(lastHeader('X-API-Key')).toBe('agt_test')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({
      query: '我一共提过几次 X',
      question_type: 'aggregate',
    })
  })

  it('answer limit 缺省时不进 body；显式传入才带', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { answer: 'x' } })
    await makeClient().answer('q')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({ query: 'q', question_type: null })

    calls = []
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { answer: 'x' } })
    await makeClient().answer('q', null, 12)
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({
      query: 'q',
      question_type: null,
      limit: 12,
    })
  })

  it('answer LLM 全链不可用（503）→ null', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 503, body: { error: 'llm' } })
    expect(await makeClient().answer('q')).toBeNull()
  })

  // ---------- memory_unreject（T-163） ----------

  it('memoryUnreject 走 POST /v1/memory/{id}/unreject（Agent Key）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { memory_id: 'mem-1', status: 'active' },
    })
    const r = await makeClient().memoryUnreject('mem-1')
    expect(r?.status).toBe('active')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/memory/mem-1/unreject')
    expect(lastHeader('X-API-Key')).toBe('agt_test')
  })

  it('memoryUnreject memory_id 做 URL 编码', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { memory_id: 'a/b' } })
    await makeClient().memoryUnreject('a/b')
    expect(callAt(0).url).toContain('/v1/memory/a%2Fb/unreject')
  })

  // ---------- stats ----------

  it('stats 走 GET /v1/admin/stats 且用 Admin Key', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { memories: { total: 100, archived: 1 }, dimension_distribution: { identity: 3 } },
    })
    const r = await makeClient().stats()
    expect(r?.memories.total).toBe(100)
    expect(callAt(0).init?.method).toBe('GET')
    expect(lastHeader('X-API-Key')).toBe('adm_test')
  })

  // ---------- config ----------

  it('configGet 整体读走 /v1/admin/config', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { config: { l1: {} } } })
    const r = await makeClient().configGet()
    expect(r?.config).toBeDefined()
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/config')
  })

  it('configGet 单段读走 /v1/admin/config/{section}', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { section: 'refine', config: {} } })
    await makeClient().configGet('refine')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/config/refine')
  })

  it('configUpdate 走 POST /v1/admin/config（服务端 POST 等价 PUT），带 Admin Key', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { status: 'ok', config: {} } })
    const r = await makeClient().configUpdate('search', { limit: 8 })
    expect(r?.status).toBe('ok')
    expect(callAt(0).init?.method).toBe('POST')
    expect(lastHeader('X-API-Key')).toBe('adm_test')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({
      section: 'search',
      values: { limit: 8 },
    })
  })

  // ---------- signal_clear（T-87） ----------

  it('signalClear 走 POST consume_all，type 与 subscriber_id 进 query', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { consumed: 5, type: 'anomaly_warn', subscriber_id: 'dsh' },
    })
    const r = await makeClient().signalClear('anomaly_warn', 'dsh')
    expect(r?.consumed).toBe(5)
    expect(callAt(0).url).toContain('/v1/admin/events/consume_all?')
    expect(callAt(0).url).toContain('type=anomaly_warn')
    expect(callAt(0).url).toContain('subscriber_id=dsh')
    expect(lastHeader('X-API-Key')).toBe('adm_test')
  })

  it('signalClear 无参时 URL 不带 query', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { consumed: 0 } })
    await makeClient().signalClear()
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/events/consume_all')
  })

  // ---------- refine ----------

  it('refineTriggerSync 走 POST /v1/admin/refine/trigger', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { triggered: 'file', file_id: 'f1', status: 'ok' },
    })
    const r = await makeClient().refineTriggerSync('f1')
    expect(r?.triggered).toBe('file')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/refine/trigger')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({ file_id: 'f1' })
  })

  it('refineRuns 走 GET /v1/admin/refine_runs，过滤参数进 query', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { items: [], count: 0, total: 0, page: 1, limit: 10 },
    })
    const r = await makeClient().refineRuns({ limit: 10, status: 'error' })
    expect(r?.total).toBe(0)
    expect(callAt(0).url).toContain('/v1/admin/refine_runs?')
    expect(callAt(0).url).toContain('limit=10')
    expect(callAt(0).url).toContain('status=error')
    expect(lastHeader('X-API-Key')).toBe('adm_test')
  })

  // ---------- 技能物化 + 写侧 ----------

  it('skillMaterialize 走 POST /v1/skills/{name}/materialize（Agent Key）', async () => {
    globalThis.fetch = makeFetchMock({
      ok: true,
      status: 200,
      body: { name: 'pdf', path: '/tmp/pdf/SKILL.md', sha256: 'abc' },
    })
    const r = await makeClient().skillMaterialize('pdf', '/tmp')
    expect(r?.sha256).toBe('abc')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/skills/pdf/materialize')
    expect(lastHeader('X-API-Key')).toBe('agt_test')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({ dest_dir: '/tmp' })
  })

  it('skillPut 走 PUT /v1/admin/skills/{name}（Admin Key）且带 content', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { ok: true, name: 'x' } })
    const r = await makeClient().skillPut('x', '# x\n正文')
    expect(r?.ok).toBe(true)
    expect(callAt(0).init?.method).toBe('PUT')
    expect(lastHeader('X-API-Key')).toBe('adm_test')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({
      content: '# x\n正文',
      skip_limits: false,
    })
  })

  it('skillDelete 走 DELETE /v1/admin/skills/{name}，hard/force 进 query', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { ok: true, deleted: true } })
    const r = await makeClient().skillDelete('x', true, true)
    expect(r?.ok).toBe(true)
    expect(callAt(0).init?.method).toBe('DELETE')
    expect(callAt(0).url).toContain('/v1/admin/skills/x?')
    expect(callAt(0).url).toContain('hard=true')
    expect(callAt(0).url).toContain('force=true')
    expect(lastHeader('X-API-Key')).toBe('adm_test')
  })

  it('skillRename 走 POST /v1/admin/skills/{name}/rename，body 带 new_name', async () => {
    globalThis.fetch = makeFetchMock({ ok: true, status: 200, body: { ok: true } })
    await makeClient().skillRename('old', 'new')
    expect(callAt(0).url).toBe('http://127.0.0.1:9910/v1/admin/skills/old/rename')
    expect(JSON.parse(String(callAt(0).init?.body))).toEqual({ new_name: 'new' })
  })

  it('写侧被服务端拒绝（409）→ null（故障隔离，不抛异常）', async () => {
    globalThis.fetch = makeFetchMock({ ok: false, status: 409, body: { error: 'conflict' } })
    expect(await makeClient().skillPut('x', 'y')).toBeNull()
    expect(await makeClient().skillDelete('x')).toBeNull()
    expect(await makeClient().skillRename('a', 'b')).toBeNull()
  })
})

// ---------- 工具层：15 个新工具 ----------

describe('answer tool', () => {
  it('调 answer 并渲染答案 + 题型/候选 meta', async () => {
    const client = makeMockClient({
      answer: vi.fn(async () => ({
        answer: '你提过 3 次',
        question_type: 'aggregate',
        candidates_used: 3,
        provider: 'agnes',
      })),
    })
    const tool = asToolLike(createAnswerTool(client))
    const result = (await tool.execute({ query: '我提过几次' })) as string
    expect(client.answer).toHaveBeenCalledWith('我提过几次', null, undefined)
    expect(result).toContain('你提过 3 次')
    expect(result).toContain('aggregate')
  })

  it('结果带 evidence 时渲染依据清单', async () => {
    const client = makeMockClient({
      answer: vi.fn(async () => ({
        answer: 'A',
        question_type: 'generic',
        evidence: [{ content: '证据一' }, { content: '证据二' }],
      })),
    })
    const tool = asToolLike(createAnswerTool(client))
    const result = (await tool.execute({ query: 'q' })) as string
    expect(result).toContain('证据一')
    expect(result).toContain('依据（2 条）')
  })

  it('不可达/LLM 不可用时降级提示', async () => {
    const client = makeMockClient({ answer: vi.fn(async () => null) })
    const tool = asToolLike(createAnswerTool(client))
    const result = (await tool.execute({ query: 'q' })) as string
    expect(result).toContain('answer 失败')
  })
})

describe('health tool', () => {
  it('渲染状态/版本/LLM/提炼/向量四行', async () => {
    const client = makeMockClient({
      health: vi.fn(async () => ({
        status: 'ok',
        version: '1.2.2',
        llm: { available: true, provider: 'agnes', model: 'agnes-2.5-flash' },
        refinement: { watermark_age_sec: 100, queue_depth: 0, stalled: false },
        vector: { available: true, memory_vectors: 100, scene_vectors: 5 },
      })),
    })
    const tool = asToolLike(createHealthTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('1.2.2')
    expect(result).toContain('正常')
    expect(result).toContain('记忆 100')
  })

  it('提炼停摆时给 ⚠️ 标记', async () => {
    const client = makeMockClient({
      health: vi.fn(async () => ({
        status: 'ok',
        refinement: { watermark_age_sec: 99999, queue_depth: 3, stalled: true },
      })),
    })
    const tool = asToolLike(createHealthTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('停摆')
  })

  it('Gateway 不可达时给出桥接插件定位提示', async () => {
    const client = makeMockClient({ health: vi.fn(async () => null) })
    const tool = asToolLike(createHealthTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('桥接插件')
  })
})

describe('stats tool', () => {
  it('渲染记忆/原始文件/水位/维度分布/agent', async () => {
    const client = makeMockClient({
      stats: vi.fn(async () => ({
        memories: { total: 100, archived: 2 },
        raw_files: { total: 50, new: 1, refined: 48, error: 1, archived: 0 },
        dimension_distribution: { identity: 5, status: 3 },
        refinement: { watermark_age_sec: 60, last_refined_at: '2026-01-01T00:00:00Z', queue_depth: 1 },
        agents: [{ agent_id: 'dsh', role: 'agent' }],
      })),
    })
    const tool = asToolLike(createStatsTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('100 条')
    expect(result).toContain('identity=5')
    expect(result).toContain('dsh(agent)')
  })

  it('不可达/无 Admin Key 时降级提示', async () => {
    const client = makeMockClient({ stats: vi.fn(async () => null) })
    const tool = asToolLike(createStatsTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('Admin Key')
  })
})

describe('memory_unreject tool', () => {
  it('调 memoryUnreject 并返回恢复确认', async () => {
    const client = makeMockClient({
      memoryUnreject: vi.fn(async () => ({ memory_id: 'm1', status: 'active' })),
    })
    const tool = asToolLike(createMemoryUnrejectTool(client))
    const result = (await tool.execute({ memory_id: 'm1' })) as string
    expect(client.memoryUnreject).toHaveBeenCalledWith('m1')
    expect(result).toContain('已恢复')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ memoryUnreject: vi.fn(async () => null) })
    const tool = asToolLike(createMemoryUnrejectTool(client))
    const result = (await tool.execute({ memory_id: 'm1' })) as string
    expect(result).toContain('memory_unreject 失败')
  })
})

describe('signal_clear tool', () => {
  it('调 signalClear 并报告消费条数', async () => {
    const client = makeMockClient({
      signalClear: vi.fn(async () => ({ consumed: 7, type: null, subscriber_id: 'dsh' })),
    })
    const tool = asToolLike(createSignalClearTool(client))
    const result = (await tool.execute({ subscriber_id: 'dsh' })) as string
    expect(client.signalClear).toHaveBeenCalledWith(null, 'dsh')
    expect(result).toContain('消费 7 条')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ signalClear: vi.fn(async () => null) })
    const tool = asToolLike(createSignalClearTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('signal_clear 失败')
  })
})

describe('wiki_evolve_trigger tool', () => {
  it('调 evolveTrigger 默认 min_rounds=5', async () => {
    const client = makeMockClient({ evolveTrigger: vi.fn(async () => ({ status: 'triggered' })) })
    const tool = asToolLike(createWikiEvolveTriggerTool(client))
    const result = (await tool.execute({})) as string
    expect(client.evolveTrigger).toHaveBeenCalledWith(null, 5)
    expect(result).toContain('triggered')
  })

  it('显式传 session_key / min_rounds 时透传', async () => {
    const client = makeMockClient({ evolveTrigger: vi.fn(async () => ({ status: 'skipped' })) })
    const tool = asToolLike(createWikiEvolveTriggerTool(client))
    await tool.execute({ session_key: 'dsh-abc', min_rounds: 8 })
    expect(client.evolveTrigger).toHaveBeenCalledWith('dsh-abc', 8)
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ evolveTrigger: vi.fn(async () => null) })
    const tool = asToolLike(createWikiEvolveTriggerTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('wiki_evolve_trigger 失败')
  })
})

describe('config_get tool', () => {
  it('整体读返回 JSON + 可写段清单', async () => {
    const client = makeMockClient({
      configGet: vi.fn(async () => ({ config: { l1: { x: 1 } }, writable_sections: ['l1', 'refine'] })),
    })
    const tool = asToolLike(createConfigGetTool(client))
    const result = (await tool.execute({})) as string
    expect(client.configGet).toHaveBeenCalledWith(null)
    expect(result).toContain('可写段：l1, refine')
  })

  it('单段读透传 section', async () => {
    const client = makeMockClient({ configGet: vi.fn(async () => ({ section: 'refine', config: {} })) })
    const tool = asToolLike(createConfigGetTool(client))
    await tool.execute({ section: 'refine' })
    expect(client.configGet).toHaveBeenCalledWith('refine')
  })

  it('段名不存在时降级提示带 section', async () => {
    const client = makeMockClient({ configGet: vi.fn(async () => null) })
    const tool = asToolLike(createConfigGetTool(client))
    const result = (await tool.execute({ section: 'nope' })) as string
    expect(result).toContain('nope')
  })
})

describe('config_update tool', () => {
  it('调 configUpdate 并返回生效确认', async () => {
    const client = makeMockClient({
      configUpdate: vi.fn(async () => ({ status: 'ok', config: {}, section: 'search' })),
    })
    const tool = asToolLike(createConfigUpdateTool(client))
    const result = (await tool.execute({ section: 'search', values: { limit: 9 } })) as string
    expect(client.configUpdate).toHaveBeenCalledWith('search', { limit: 9 })
    expect(result).toContain('已生效')
  })

  it('描述含「仅在用户明确要求」护栏', () => {
    const tool = asToolLike(createConfigUpdateTool(makeMockClient({})))
    expect(tool.description).toContain('仅在用户明确要求')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ configUpdate: vi.fn(async () => null) })
    const tool = asToolLike(createConfigUpdateTool(client))
    const result = (await tool.execute({ section: 'x', values: {} })) as string
    expect(result).toContain('config_update 失败')
  })
})

describe('refine_status tool', () => {
  it('渲染最近批次记录', async () => {
    const client = makeMockClient({
      refineRuns: vi.fn(async () => ({
        items: [{ file_id: 'f1', status: 'error', stage: 'l1', started_at: '2026-01-01' }],
        count: 1,
        total: 9,
        page: 1,
        limit: 10,
      })),
    })
    const tool = asToolLike(createRefineStatusTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('共 9 条')
    expect(result).toContain('[error]')
  })

  it('无记录时给出提示', async () => {
    const client = makeMockClient({
      refineRuns: vi.fn(async () => ({ items: [], count: 0, total: 0, page: 1, limit: 10 })),
    })
    const tool = asToolLike(createRefineStatusTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('无记录')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ refineRuns: vi.fn(async () => null) })
    const tool = asToolLike(createRefineStatusTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('refine_status 失败')
  })
})

describe('refine_trigger tool', () => {
  it('单文件形态渲染 memories_count', async () => {
    const client = makeMockClient({
      refineTriggerSync: vi.fn(async () => ({
        triggered: 'file',
        file_id: 'f1',
        status: 'ok',
        memories_count: 4,
      })),
    })
    const tool = asToolLike(createRefineTriggerTool(client))
    const result = (await tool.execute({ file_id: 'f1' })) as string
    expect(result).toContain('单文件完成')
    expect(result).toContain('记忆 4 条')
  })

  it('批量形态渲染 processed/total_memories', async () => {
    const client = makeMockClient({
      refineTriggerSync: vi.fn(async () => ({ triggered: 'batch', processed: 3, total_memories: 9 })),
    })
    const tool = asToolLike(createRefineTriggerTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('批量完成')
    expect(result).toContain('3 个文件')
  })

  it('描述含成本护栏', () => {
    const tool = asToolLike(createRefineTriggerTool(makeMockClient({})))
    expect(tool.description).toContain('消耗 LLM 额度')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ refineTriggerSync: vi.fn(async () => null) })
    const tool = asToolLike(createRefineTriggerTool(client))
    const result = (await tool.execute({ file_id: 'f1' })) as string
    expect(result).toContain('refine_trigger 失败')
  })
})

describe('refine_batch tool', () => {
  it('调 triggerRefine（异步）并返回排队确认', async () => {
    const client = makeMockClient({
      triggerRefine: vi.fn(async () => ({
        triggered: 'async' as const,
        file_id: '',
        status: 'queued' as const,
        note: 'ok',
      })),
    })
    const tool = asToolLike(createRefineBatchTool(client))
    const result = (await tool.execute({ limit: 20 })) as string
    expect(client.triggerRefine).toHaveBeenCalledWith({ file_id: null, limit: 20 })
    expect(result).toContain('已排队')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ triggerRefine: vi.fn(async () => null) })
    const tool = asToolLike(createRefineBatchTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('refine_batch 失败')
  })
})

describe('skill_materialize tool', () => {
  it('返回落盘路径与 sha256', async () => {
    const client = makeMockClient({
      skillMaterialize: vi.fn(async () => ({ name: 'pdf', path: '/w/pdf/SKILL.md', sha256: 'deadbeef' })),
    })
    const tool = asToolLike(createSkillMaterializeTool(client))
    const result = (await tool.execute({ name: 'pdf', dest_dir: '/w' })) as string
    expect(client.skillMaterialize).toHaveBeenCalledWith('pdf', '/w')
    expect(result).toContain('/w/pdf/SKILL.md')
    expect(result).toContain('deadbeef')
  })

  it('失败时降级提示', async () => {
    const client = makeMockClient({ skillMaterialize: vi.fn(async () => null) })
    const tool = asToolLike(createSkillMaterializeTool(client))
    const result = (await tool.execute({ name: 'x', dest_dir: '/w' })) as string
    expect(result).toContain('skill_materialize 失败')
  })
})

describe('skill_put / skill_delete / skill_rename tool', () => {
  it('skill_put 调 skillPut 默认 skip_limits=false', async () => {
    const client = makeMockClient({ skillPut: vi.fn(async () => ({ ok: true })) })
    const tool = asToolLike(createSkillPutTool(client))
    const result = (await tool.execute({ name: 'x', content: '# x' })) as string
    expect(client.skillPut).toHaveBeenCalledWith('x', '# x', false)
    expect(result).toContain('已写入')
  })

  it('skill_put 描述含查重/护栏提示', () => {
    const tool = asToolLike(createSkillPutTool(makeMockClient({})))
    expect(tool.description).toContain('仅在用户明确要求')
    expect(tool.description).toContain('查重')
  })

  it('skill_put 失败（lint/查重）时降级提示', async () => {
    const client = makeMockClient({ skillPut: vi.fn(async () => null) })
    const tool = asToolLike(createSkillPutTool(client))
    const result = (await tool.execute({ name: 'x', content: 'y' })) as string
    expect(result).toContain('lint 门禁')
  })

  it('skill_delete 透传 hard/force', async () => {
    const client = makeMockClient({ skillDelete: vi.fn(async () => ({ ok: true })) })
    const tool = asToolLike(createSkillDeleteTool(client))
    const result = (await tool.execute({ name: 'x', hard: true, force: true })) as string
    expect(client.skillDelete).toHaveBeenCalledWith('x', true, true)
    expect(result).toContain('物理删除')
  })

  it('skill_delete 默认软删文案', async () => {
    const client = makeMockClient({ skillDelete: vi.fn(async () => ({ ok: true })) })
    const tool = asToolLike(createSkillDeleteTool(client))
    const result = (await tool.execute({ name: 'x' })) as string
    expect(client.skillDelete).toHaveBeenCalledWith('x', false, false)
    expect(result).toContain('软删')
  })

  it('skill_rename 调 skillRename 并提示墓碑', async () => {
    const client = makeMockClient({ skillRename: vi.fn(async () => ({ ok: true })) })
    const tool = asToolLike(createSkillRenameTool(client))
    const result = (await tool.execute({ name: 'old', new_name: 'new' })) as string
    expect(client.skillRename).toHaveBeenCalledWith('old', 'new')
    expect(result).toContain('墓碑')
  })

  it('三个写侧工具失败时均降级提示', async () => {
    const put = asToolLike(createSkillPutTool(makeMockClient({ skillPut: vi.fn(async () => null) })))
    const del = asToolLike(createSkillDeleteTool(makeMockClient({ skillDelete: vi.fn(async () => null) })))
    const ren = asToolLike(createSkillRenameTool(makeMockClient({ skillRename: vi.fn(async () => null) })))
    expect((await put.execute({ name: 'x', content: 'y' })) as string).toContain('skill_put 失败')
    expect((await del.execute({ name: 'x' })) as string).toContain('skill_delete 失败')
    expect((await ren.execute({ name: 'a', new_name: 'b' })) as string).toContain('skill_rename 失败')
  })
})

describe('registerTools 1.2.2 对齐注册', () => {
  it('新增 15 工具全部挂载且总数 40（T-207 ③ 加 conversation_search）', () => {
    const registered: string[] = []
    const ctx = {
      tools: { register: (tool: unknown) => { registered.push((tool as ToolLike).name); return () => {} } },
    }
    registerTools(ctx as unknown as Parameters<typeof registerTools>[0], makeMockClient({}), 5)
    expect(registered).toHaveLength(40)
    expect(registered).toContain('conversation_search')
    for (const name of [
      'answer', 'health', 'stats', 'memory_unreject', 'signal_clear',
      'wiki_evolve_trigger', 'config_get', 'config_update',
      'refine_status', 'refine_trigger', 'refine_batch',
      'skill_materialize', 'skill_put', 'skill_delete', 'skill_rename',
    ]) {
      expect(registered).toContain(name)
    }
  })

  it('append 不在注册清单内（dsh 侧由 session-sync 自动入库）', () => {
    const registered: string[] = []
    const ctx = {
      tools: { register: (tool: unknown) => { registered.push((tool as ToolLike).name); return () => {} } },
    }
    registerTools(ctx as unknown as Parameters<typeof registerTools>[0], makeMockClient({}), 5)
    expect(registered).not.toContain('append')
  })
})
