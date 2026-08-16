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
import type { SgmeClient, InjectResponse } from '../src/sgme-client.js'

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
