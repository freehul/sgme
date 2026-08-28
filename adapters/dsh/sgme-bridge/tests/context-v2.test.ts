/**
 * context.ts v2 测试 — agent/pre-step middleware 画像注入。
 *
 * 覆盖新逻辑：
 * - 首步（step===1）触发注入，后续 step 不重复
 * - inject/search 返回 null 时降级返回基线 decision
 * - decision.kind==='reject' 时不注入
 * - 注入消息插入 claimed messages 之后
 * - buildInjectionText 已有独立测试（context.test.ts），此处专注 middleware 行为
 */
import { describe, it, expect, vi } from 'vitest'
import { registerContextInjection } from '../src/context.js'
import type { SgmeClient, InjectResponse, SearchResponse, SearchResult } from '../src/sgme-client.js'

// ---------- 最小 mock ----------

function makeClient(overrides: Partial<SgmeClient> = {}): SgmeClient {
  const base = {
    inject: vi.fn(async () => makeProfile()),
    search: vi.fn(async () => null),
  }
  return { ...base, ...overrides } as unknown as SgmeClient
}

function makeProfile(): InjectResponse {
  return {
    blocks: [{ title: 'identity', items: [{ content: '用户名：张三' }] }],
    stats: { mode: 'daily', queries: 1, tokens_est: 10, tier0_present: false },
    tier0: { present: false, content: null },
  }
}

function makeCtx() {
  const listeners = new Map<string, (...args: any[]) => any>()
  return {
    ctx: {
      on: (event: string, handler: (...args: any[]) => any) => {
        listeners.set(event, handler)
        return () => listeners.delete(event)
      },
      logger: { info: vi.fn(), warn: vi.fn() },
    },
    listeners,
  }
}

/** 构造 pre-step payload。 */
function makePayload(step = 1, messages: unknown[] = []) {
  return {
    agent: { inbox: { nextStep: [], remove: vi.fn(() => true) } },
    messages,
    step,
    signal: new AbortController().signal,
  }
}

/** 基线 decision（enter + 若干消息）。 */
function makeDecision(messages: unknown[] = [{ id: 'user-1', role: 'user' }]) {
  return { kind: 'enter' as const, messages }
}

// ---------- registerContextInjection ----------

describe('registerContextInjection (v2 pre-step middleware)', () => {
  it('注册 agent/pre-step 监听器并返回清理函数', () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const dispose = registerContextInjection(ctx, client, {
      injectMode: 'daily',
      injectMaxTokens: 800,
      searchLimit: 5,
    })
    expect(listeners.has('agent/pre-step')).toBe(true)
    expect(typeof dispose).toBe('function')
    dispose()
    expect(listeners.has('agent/pre-step')).toBe(false)
  })

  it('首步（step===1）调用 inject 并注入画像消息', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    // next() 返回基线 decision
    const next = vi.fn(async () => makeDecision())
    const result = await handler(makePayload(1), next)

    expect(client.inject).toHaveBeenCalledWith({ mode: 'daily' })
    expect(result.kind).toBe('enter')
    const messages = (result as { messages: any[] }).messages
    // 注入后比基线多 1 条（画像消息）
    expect(messages.length).toBe(2)
    // 画像消息是 user 角色、plugin source（claimed 为空 => 插在最前）
    const injected = messages[0]
    expect(injected.source.kind).toBe('plugin')
    expect(injected.source.plugin).toBe('dsh-sgme')
    expect(injected.content[0].text).toContain('SGME 用户画像')
  })

  it('非首步（step>1）不注入，返回基线', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const result = await handler(makePayload(3), next)

    expect(client.inject).not.toHaveBeenCalled()
    expect(result).toEqual(await next())
  })

  it("decision.kind 为 reject 时不注入", async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => ({ kind: 'reject' as const }))
    const result = await handler(makePayload(1), next)

    expect(client.inject).not.toHaveBeenCalled()
    expect(result).toEqual({ kind: 'reject' })
  })

  it('inject 返回 null（网关不可达）时降级返回基线 decision', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({ inject: vi.fn(async () => null) })
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const result = await handler(makePayload(1), next)

    expect(result).toEqual(await next())
    expect(ctx.logger.warn).not.toHaveBeenCalled() // 降级不报警
  })

  it('inject 抛异常时捕获并返回基线 decision', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({
      inject: vi.fn(async () => { throw new Error('boom') }),
    })
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const result = await handler(makePayload(1), next)

    expect(result).toEqual(await next())
    expect(ctx.logger.warn).toHaveBeenCalled()
  })

  it('注入消息插入首条消息之前（前缀缓存优化）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const claimed = [
      { id: 'a', role: 'user' },
      { id: 'b', role: 'user' },
    ]
    const next = vi.fn(async () => makeDecision(claimed))
    const result = await handler(makePayload(1, claimed), next) as { messages: any[] }

    expect(result.messages.length).toBe(3)
    // 注入消息在首位（前缀优化：稳定画像靠前）
    expect(result.messages[0].source.kind).toBe('plugin')
    expect(result.messages[1].id).toBe('a')
    expect(result.messages[2].id).toBe('b')
  })

  it('重复调用（同 step 已注入）不重复注入', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const first = await handler(makePayload(1), next) as { messages: any[] }
    const second = await handler(makePayload(1), next) as { messages: any[] }

    expect(client.inject).toHaveBeenCalledTimes(1)
    // 第二次调用已注入过：直接返回基线（1 条，不再注入）
    expect(second.messages.length).toBe(1)
    expect(second.messages.length).not.toBe(first.messages.length)
  })

  it('已有相同注入消息时跳过（防重复）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    // 基线里已有一条 plugin/dsh-sgme 消息（内容与 buildInjectionText 输出一致）
    const baselineMsg = {
      id: 'sgme-1',
      role: 'user',
      content: [{
        type: 'text',
        text: '--- SGME 用户画像 ---\n[identity]\n- 用户名：张三\n（以上为 SGME 注入的画像与记忆，可直接引用，不必重复询问用户）',
      }],
      source: { kind: 'plugin', plugin: 'dsh-sgme' },
    }
    const next = vi.fn(async () => makeDecision([baselineMsg]))
    const result = await handler(makePayload(1), next) as { messages: any[] }

    // 不重复注入：消息数不变
    expect(result.messages.length).toBe(1)
  })

  it('预拉取失败后下次 step 重试成功（失败不置位 injected）', async () => {
    const { ctx, listeners } = makeCtx()
    // 第一次 inject 失败，第二次成功
    let call = 0
    const client = makeClient({
      inject: vi.fn(async () => {
        call++
        if (call === 1) return null  // 首次失败（网关不可达）
        return makeProfile()          // 重试成功
      }),
    })
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())

    // 第一次：失败 → 返回基线（不注入）
    const r1 = await handler(makePayload(1), next) as { messages: any[] }
    expect(r1.messages.length).toBe(1)

    // 第二次（模拟下一 turn）：重试成功 → 注入
    const r2 = await handler(makePayload(1), next) as { messages: any[] }
    expect(client.inject).toHaveBeenCalledTimes(2)
    expect(r2.messages.length).toBe(2)
    expect(r2.messages[0].source.kind).toBe('plugin')
  })

  it('画像为空时跳过且不置位（可后续重试）', async () => {
    const { ctx, listeners } = makeCtx()
    const emptyProfile = {
      blocks: [], stats: { mode: 'daily', queries: 0, tokens_est: 0, tier0_present: false },
      tier0: { present: false, content: null },
    }
    const client = makeClient({ inject: vi.fn(async () => emptyProfile) })
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())

    const r1 = await handler(makePayload(1), next) as { messages: any[] }
    expect(r1.messages.length).toBe(1)  // 未注入
    // 再次触发，inject 应再次被调用（未置位）
    await handler(makePayload(1), next)
    expect(client.inject).toHaveBeenCalledTimes(2)
  })
})

// ---------- 事件提醒注入行为（2026-08-20 修复回归测试） ----------

function makeEventSubscriber(events: Array<{ event_id: string; type: string }>) {
  const sseEvents = events.map((e) => ({
    event_id: e.event_id,
    type: e.type,
    source: 'care',
    payload: { msg: '测试' },
    ts: '2026-08-20T00:00:00Z',
  }))
  return {
    unnotifiedEvents: vi.fn(() => sseEvents),
    pendingEvents: vi.fn(() => sseEvents),
    markNotified: vi.fn(),
    markConsumed: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
  }
}

describe('事件提醒注入（2026-08-20 修复）', () => {
  it('有未提醒事件时注入一次摘要提醒并 markNotified', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    const subscriber = makeEventSubscriber([
      { event_id: 'e1', type: 'care_daily' },
      { event_id: 'e2', type: 'anomaly_warn' },
    ])
    registerContextInjection(ctx, client, {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
    })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const result = await handler(makePayload(1), next) as { messages: any[] }

    // 注入一条摘要提醒（类型+数量，无全文 JSON）
    const injected = result.messages.find((m) => m.source?.plugin === 'dsh-sgme')
    expect(injected).toBeTruthy()
    const text = injected.content[0].text
    expect(text).toContain('关怀信号 1 条')
    expect(text).toContain('异常告警 1 条')
    // 摘要化：不含 payload 内容 msg
    expect(text).not.toContain('msg')
    // 已 markNotified（防下轮重复）
    expect(subscriber.markNotified).toHaveBeenCalledWith(['e1', 'e2'])
  })

  it('同一事件不重复注入（markNotified 后 unnotified 为空）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    // 第一轮内 161 行（injected=false 跳过）和 215 行（首轮注入）各调用一次 unnotifiedEvents；
    // 第二轮后 markNotified 生效 → 返回空。前 2 次返回 ev1，之后返回空。
    let calls = 0
    const subscriber = makeEventSubscriber([{ event_id: 'e1', type: 'care_daily' }])
    const ev1 = { event_id: 'e1', type: 'care_daily', source: 'care', payload: { msg: '测试' }, ts: 'x' }
    subscriber.unnotifiedEvents.mockImplementation(() => {
      calls++
      return calls <= 2 ? [ev1] : []
    })
    registerContextInjection(ctx, client, {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
    })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())

    // 第一轮：注入
    const r1 = await handler(makePayload(1), next) as { messages: any[] }
    expect(r1.messages.length).toBe(2)  // 画像 + 事件提醒
    // 第二轮：unnotified 已空 → 不再注入（只有画像）
    const r2 = await handler(makePayload(2), next) as { messages: any[] }
    expect(r2.messages.length).toBe(1)
    expect(subscriber.markNotified).toHaveBeenCalledTimes(1)
  })
})

// ---------- T-88 对话内容驱动（首句选场景，2026-08-20 用户定） ----------

function makeSceneSearchResponse(scenes: number, memories: number): SearchResponse {
  // 显式标注 SearchResult[]：否则 source 被推断为 string，无法赋给联合字面量类型
  const results: SearchResult[] = []
  for (let i = 1; i <= scenes; i++) {
    results.push({ rank: i, source: 'wiki', title: `场景${i}`, content: `L2 场景内容${i}：SGME 架构相关` })
  }
  for (let i = 1; i <= memories; i++) {
    results.push({ rank: scenes + i, source: 'memory', content: `相关记忆${i}` })
  }
  return { results, meta: { routes: ['bm25'], rrf_k: 60 } }
}

describe('registerContextInjection (T-88 对话内容驱动)', () => {
  it('首句命中 L2 场景 → 用首句调 search(memory+wiki) 并注入场景块', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({
      search: vi.fn(async () => makeSceneSearchResponse(1, 1)),
    })
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const firstMsg = { id: 'u1', role: 'user', content: [{ type: 'text', text: '帮我看看 SGME 架构设计' }] }
    const result = await handler(makePayload(1, [firstMsg]), next)

    // 首句作为 query，双 scope
    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      query: '帮我看看 SGME 架构设计',
      scopes: ['memory', 'wiki'],
    }))
    // 注入文本含场景块
    const messages = (result as { messages: any[] }).messages
    const injected = messages.find((m) => m.source?.plugin === 'dsh-sgme')
    expect(injected).toBeTruthy()
    expect(injected.content[0].text).toContain('SGME 相关场景')
    expect(injected.content[0].text).toContain('场景1')
    // 命中场景时不再调 inject 模板
    expect(client.inject).not.toHaveBeenCalled()
  })

  it('首句无场景命中 → 回退模板注入（inject 被调用）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({
      search: vi.fn(async (): Promise<SearchResponse> => ({
        results: [{ rank: 1, source: 'memory', content: '普通记忆' }],
        meta: { routes: ['bm25'], rrf_k: 60 },
      })),
    })
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const firstMsg = { id: 'u1', role: 'user', content: [{ type: 'text', text: '随便聊聊' }] }
    await handler(makePayload(1, [firstMsg]), next)

    // 回退：inject 模板被调用（mode 来自配置）
    expect(client.inject).toHaveBeenCalledWith({ mode: 'daily' })
  })

  it('无首句（消息为空）→ 维持原逻辑（inject + projectHint 检索）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, { injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5 })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    await handler(makePayload(1, []), next)

    expect(client.inject).toHaveBeenCalledWith({ mode: 'daily' })
  })
})

