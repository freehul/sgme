/**
 * F2（2026-11-05）：事件提醒类型白名单测试。
 *
 * 生产现状：memory_updated 每轮都产生（已堆数千条），全部拼进「【SGME 事件提醒】」
 * 会把每轮系统提示噪声化。本文件覆盖：
 * - filterEventsByNotifyTypes：精确 type / 前缀_* / 单独 * / 空数组 / 缺省白名单
 * - buildEventNoticeText：白名单内事件照旧摘要，过滤后为空返回空串
 * - pre-step 中间件：memory_updated 不注入；anomaly_warn、care_* 仍注入；自定义白名单生效
 */
import { describe, it, expect, vi } from 'vitest'
import {
  DEFAULT_EVENT_NOTIFY_TYPES,
  buildEventNoticeText,
  filterEventsByNotifyTypes,
  registerContextInjection,
} from '../src/context.js'
import type { SgmeEvent } from '../src/events.js'
import type { SgmeClient, InjectResponse } from '../src/sgme-client.js'

/** 构造一条事件（字段对齐 /v1/events/stream 的信封）。 */
function ev(event_id: string, type: string): SgmeEvent {
  return { event_id, type, source: 'sgme', payload: { msg: '测试' }, ts: '2026-11-05T00:00:00Z' }
}

// ---------- filterEventsByNotifyTypes ----------

describe('filterEventsByNotifyTypes（F2 类型白名单）', () => {
  const events = [ev('e1', 'care_daily'), ev('e2', 'anomaly_warn'), ev('e3', 'memory_updated')]

  it('缺省白名单：memory_updated 被过滤，anomaly_warn 与 care_* 保留', () => {
    expect(filterEventsByNotifyTypes(events).map((e) => e.type)).toEqual(['care_daily', 'anomaly_warn'])
    expect(filterEventsByNotifyTypes(events, undefined).map((e) => e.type)).toEqual(['care_daily', 'anomaly_warn'])
    expect(DEFAULT_EVENT_NOTIFY_TYPES).toEqual(['anomaly_warn', 'care_*'])
  })

  it('精确 type 白名单：只放行写死的类型', () => {
    expect(filterEventsByNotifyTypes(events, ['anomaly_warn']).map((e) => e.event_id)).toEqual(['e2'])
  })

  it('前缀_* 匹配：care_* 命中 care_ 开头，不命中 carefree', () => {
    const mixed = [ev('e1', 'care_mood'), ev('e2', 'carefree'), ev('e3', 'memory_updated')]
    expect(filterEventsByNotifyTypes(mixed, ['care_*']).map((e) => e.event_id)).toEqual(['e1'])
  })

  it('单独 * = 全放行；空数组 = 一律不提醒', () => {
    expect(filterEventsByNotifyTypes(events, ['*'])).toHaveLength(3)
    expect(filterEventsByNotifyTypes(events, [])).toHaveLength(0)
  })
})

// ---------- buildEventNoticeText ----------

describe('buildEventNoticeText（F2 入口过滤）', () => {
  it('全是 memory_updated 时返回空串（不产生空提醒）', () => {
    expect(buildEventNoticeText([ev('e1', 'memory_updated')])).toBe('')
  })

  it('anomaly_warn 与 care_* 仍按原摘要格式注入', () => {
    const text = buildEventNoticeText([ev('e1', 'care_daily'), ev('e2', 'anomaly_warn'), ev('e3', 'memory_updated')])
    expect(text).toContain('关怀信号 1 条')
    expect(text).toContain('异常告警 1 条')
    // 被过滤掉的事件不计入「其他事件」，也不出现在事件列表里
    expect(text).not.toContain('其他事件')
    expect(text).not.toContain('memory_updated')
  })

  it('自定义白名单生效：只提醒 memory_updated', () => {
    const text = buildEventNoticeText([ev('e1', 'care_daily'), ev('e2', 'memory_updated')], ['memory_updated'])
    expect(text).toContain('其他事件 1 条')
    expect(text).not.toContain('关怀信号')
  })
})

// ---------- pre-step 中间件注入行为 ----------

function makeClient(): SgmeClient {
  const profile: InjectResponse = {
    blocks: [{ title: 'identity', items: [{ content: '用户名：张三' }] }],
    stats: { mode: 'daily', queries: 1, tokens_est: 10, tier0_present: false },
    tier0: { present: false, content: null },
  }
  return {
    inject: vi.fn(async () => profile),
    search: vi.fn(async () => null),
    activeCareLevel: vi.fn(async () => 'session'),
  } as unknown as SgmeClient
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

function makePayload(step = 1, messages: unknown[] = []) {
  return {
    agent: { inbox: { nextStep: [], remove: vi.fn(() => true) } },
    messages,
    step,
    signal: new AbortController().signal,
  }
}

function makeDecision(messages: unknown[] = [{ id: 'user-1', role: 'user' }]) {
  return { kind: 'enter' as const, messages }
}

/** 事件订阅器桩：unnotifiedEvents 每次调用取当前队列（便于分步切换）。 */
function makeSubscriber(current: () => SgmeEvent[]) {
  return {
    unnotifiedEvents: vi.fn(() => current()),
    pendingEvents: vi.fn(() => current()),
    markNotified: vi.fn(),
    markConsumed: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
  }
}

describe('pre-step 事件提醒（F2 白名单）', () => {
  it('首步注入：只有 memory_updated 时不拼事件提醒，但仍 markNotified 防重复', async () => {
    const { ctx, listeners } = makeCtx()
    const subscriber = makeSubscriber(() => [ev('m1', 'memory_updated')])
    registerContextInjection(ctx, makeClient(), {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
    })
    const handler = listeners.get('agent/pre-step')!
    const res = await handler(makePayload(1), vi.fn(async () => makeDecision())) as { messages: any[] }
    const injected = res.messages.find((m) => m.source?.kind === 'dsh-sgme')
    expect(injected).toBeTruthy()
    expect(injected.content[0].text).toContain('SGME 用户画像')
    expect(injected.content[0].text).not.toContain('SGME 事件提醒')
    expect(subscriber.markNotified).toHaveBeenCalledWith(['m1'])
  })

  it('首步注入：anomaly_warn 仍进提醒摘要（memory_updated 不计入其他事件）', async () => {
    const { ctx, listeners } = makeCtx()
    const subscriber = makeSubscriber(() => [ev('m1', 'memory_updated'), ev('w1', 'anomaly_warn')])
    registerContextInjection(ctx, makeClient(), {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
    })
    const handler = listeners.get('agent/pre-step')!
    const res = await handler(makePayload(1), vi.fn(async () => makeDecision())) as { messages: any[] }
    const text = res.messages.find((m) => m.source?.kind === 'dsh-sgme').content[0].text
    expect(text).toContain('异常告警 1 条')
    expect(text).not.toContain('其他事件')
    expect(text).not.toContain('memory_updated')
    expect(subscriber.markNotified).toHaveBeenCalledWith(['m1', 'w1'])
  })

  it('已注入后再来 memory_updated：不注入提醒消息（只 markNotified）', async () => {
    const { ctx, listeners } = makeCtx()
    let current: SgmeEvent[] = []
    const subscriber = makeSubscriber(() => current)
    registerContextInjection(ctx, makeClient(), {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
    })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    const r1 = await handler(makePayload(1), next) as { messages: any[] }
    expect(r1.messages.length).toBe(2)  // 画像 + 基线

    current = [ev('m1', 'memory_updated')]
    const r2 = await handler(makePayload(2), next) as { messages: any[] }
    expect(r2.messages.length).toBe(1)  // 未注入事件提醒
    expect(subscriber.markNotified).toHaveBeenCalledWith(['m1'])
  })

  it('care_* 事件照旧注入提醒', async () => {
    const { ctx, listeners } = makeCtx()
    let current: SgmeEvent[] = []
    const subscriber = makeSubscriber(() => current)
    registerContextInjection(ctx, makeClient(), {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
    })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    await handler(makePayload(1), next)

    current = [ev('c1', 'care_daily')]
    const r2 = await handler(makePayload(2), next) as { messages: any[] }
    expect(r2.messages.length).toBe(2)  // 基线 + 事件提醒
    const notice = r2.messages.find((m) => m.source?.kind === 'dsh-sgme')
    expect(notice.content[0].text).toContain('关怀信号 1 条')
  })

  it('自定义白名单生效：eventNotifyTypes 放行 memory_updated', async () => {
    const { ctx, listeners } = makeCtx()
    let current: SgmeEvent[] = []
    const subscriber = makeSubscriber(() => current)
    registerContextInjection(ctx, makeClient(), {
      injectMode: 'daily', injectMaxTokens: 800, searchLimit: 5,
      eventSubscriber: subscriber as any,
      eventNotifyTypes: ['memory_updated'],
    })
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision())
    await handler(makePayload(1), next)

    current = [ev('m1', 'memory_updated')]
    const r2 = await handler(makePayload(2), next) as { messages: any[] }
    expect(r2.messages.length).toBe(2)
    const notice = r2.messages.find((m) => m.source?.kind === 'dsh-sgme')
    expect(notice.content[0].text).toContain('其他事件 1 条')
  })
})
