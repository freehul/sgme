/**
 * context.ts D-72 对话冷场修复接线测试（2026-11-03）。
 *
 * 背景：服务端 POST /v1/inject 支持**可选** history[{role,content}] 与 already_repaired——冷场时
 * 在 blocks 末尾追加 kind="coldstart_repair" 的修补提示块。适配器此前从不传 history，
 * 于是「2.0 冷场修复」在 DSH 侧永不生效（服务端能力空转）。
 *
 * 接线纪律（缓存红线不变）：
 * - 首步画像注入仍是唯一常态 inject 调用；普通长对话**零额外请求**；
 * - 只有「首步已完成 + 本地廉价启发式认为可能冷场 + 本会话未修补过」才额外探测一次；
 * - 响应含修复块 → 注入 1 条 user 消息（source dsh-sgme），并把已修补标记置位；
 * - 同一会话最多修补 1 次；服务端未给块 → 静默降级（探到不注入，不报错）。
 *
 * 本文件守住以上五条 + history 构造（最近 ≤8 条、过滤插件自身注入消息）。
 */
import { describe, it, expect, vi } from 'vitest'
import { registerContextInjection } from '../src/context.js'
import type { SgmeClient, InjectResponse } from '../src/sgme-client.js'

/** 服务端冷场修复提示（reason=short_replies 的确定性文案）。 */
const REPAIR_HINT = '对方回得很短，换个具体话题，别追问'

const CFG = { injectMode: 'daily' as const, injectMaxTokens: 800, searchLimit: 5 }

// ---------- 最小 mock（与 context-v2.test.ts 同款） ----------

function makeProfile(): InjectResponse {
  return {
    blocks: [{ title: 'identity', items: [{ content: '用户名：张三' }] }],
    stats: { mode: 'daily', queries: 1, tokens_est: 10, tier0_present: false },
    tier0: { present: false, content: null },
  }
}

/** 冷场响应：正常画像 + 末尾 kind="coldstart_repair" 修复块（服务端契约原样）。 */
function makeColdProfile(): InjectResponse {
  return {
    blocks: [
      { title: 'identity', items: [{ content: '用户名：张三' }] },
      {
        title: '对话冷场修复',
        items: [{ content: REPAIR_HINT }],
        present: true,
        kind: 'coldstart_repair',
        reason: 'short_replies',
        signals: { user_avg_len: 1.5, assistant_avg_len: 12, last_user_len: 1 },
      },
    ],
    stats: { mode: 'daily', queries: 1, tokens_est: 10, tier0_present: false },
    tier0: { present: false, content: null },
  }
}

function makeClient(overrides: Partial<SgmeClient> = {}): SgmeClient {
  const base = {
    inject: vi.fn(async () => makeProfile()),
    search: vi.fn(async () => null),
  }
  return { ...base, ...overrides } as unknown as SgmeClient
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

function makeDecision(messages: unknown[] = [{ id: 'u-baseline', role: 'user' }]) {
  return { kind: 'enter' as const, messages }
}

// ---------- 消息构造（对齐 dsh 消息形态：content 为 [{type:'text',text}]） ----------

function userMsg(text: string, id = '') {
  return { id: id || `u-${text}`, role: 'user', content: [{ type: 'text', text }] }
}

function assistantMsg(text: string, id = '') {
  return { id: id || `a-${text}`, role: 'assistant', content: [{ type: 'text', text }] }
}

/** 本插件注入的消息（source.kind='dsh-sgme'，不应进冷场判据 history）。 */
function sgmeMsg(text: string) {
  return {
    id: `sgme-${text.slice(0, 8)}`,
    role: 'user' as const,
    content: [{ type: 'text', text }],
    source: { kind: 'dsh-sgme' },
  }
}

function injectCallArgs(client: SgmeClient, index: number): any {
  const calls = (client.inject as unknown as { mock: { calls: any[][] } }).mock.calls
  return calls[index]?.[0]
}

/** 取出 decision 里本插件注入的消息。 */
function injectedMessages(result: unknown): any[] {
  const messages = (result as { messages?: any[] }).messages ?? []
  return messages.filter((m) => m?.source?.kind === 'dsh-sgme')
}

// ---------- 用例 ----------

describe('registerContextInjection（D-72 冷场修复）', () => {
  it('普通长对话：首步注入后零额外 inject 请求（缓存纪律不变）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient()
    registerContextInjection(ctx, client, CFG)
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision([userMsg('继续')]))

    // 首步：长问句 → 正常画像注入
    await handler(makePayload(1, [userMsg('帮我调研一下 SGME 2.0 的迁移方案，并给出证据？')]), next)
    expect(client.inject).toHaveBeenCalledTimes(1)

    // 第二步：正常长对话（有问号、内容长）→ 不探测、不额外请求
    const longMessages = [
      userMsg('帮我看看 D-72 的接线怎么做，需要哪些证据？'),
      assistantMsg('这里需要对照服务端 inject 的冷场判据与修补闸……（长文本，远超阈值）'),
      userMsg('那适配器侧应该在哪一步调用它，会不会破坏前缀缓存？'),
    ]
    const result = await handler(makePayload(2, longMessages), next)

    expect(client.inject).toHaveBeenCalledTimes(1)
    expect(injectedMessages(result)).toHaveLength(0)
  })

  it('冷场序列（最近 2 条 user 均 ≤6 字且无问号）→ 探测 1 次并注入 1 条修复消息', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({
      inject: vi.fn(async (req: any) =>
        Array.isArray(req?.history) ? makeColdProfile() : makeProfile()),
    })
    registerContextInjection(ctx, client, CFG)
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision([userMsg('好')]))

    await handler(makePayload(1, [userMsg('随便聊聊？')]), next)
    expect(client.inject).toHaveBeenCalledTimes(1)

    const cold = [
      userMsg('嗯'),
      assistantMsg('哦哦'),
      sgmeMsg('--- SGME 用户画像 ---\n[identity]\n- 用户名：张三'), // 插件注入，须被过滤
      userMsg('好'),
    ]
    const result = (await handler(makePayload(2, cold), next)) as { messages: any[] }

    // 恰好额外探测 1 次，携带 history + already_repaired 当前值
    expect(client.inject).toHaveBeenCalledTimes(2)
    const probe = injectCallArgs(client, 1)
    expect(probe.mode).toBe('daily')
    expect(probe.already_repaired).toBe(false)
    expect(probe.history).toEqual([
      { role: 'user', content: '嗯' },
      { role: 'assistant', content: '哦哦' },
      { role: 'user', content: '好' },
    ])

    // 修复提示作为 1 条 user 消息注入（source dsh-sgme，内容取自 items[].content）
    const injected = injectedMessages(result)
    expect(injected).toHaveLength(1)
    expect(injected[0]!.role).toBe('user')
    expect(injected[0]!.content[0].text).toBe(REPAIR_HINT)
  })

  it('同一会话最多修补 1 次：第二次冷场不再探测/不再注入', async () => {
    const { ctx, listeners } = makeCtx()
    let probes = 0
    const client = makeClient({
      inject: vi.fn(async (req: any) => {
        if (Array.isArray(req?.history)) {
          probes += 1
          return makeColdProfile()
        }
        return makeProfile()
      }),
    })
    registerContextInjection(ctx, client, CFG)
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision([userMsg('哦')]))

    await handler(makePayload(1, [userMsg('在吗？')]), next)

    const r2 = await handler(
      makePayload(2, [userMsg('嗯'), assistantMsg('在的'), userMsg('哦')]),
      next,
    )
    expect(injectedMessages(r2)).toHaveLength(1)

    // 第三次仍是冷场序列：已修补 → 零探测、零注入
    const r3 = await handler(
      makePayload(3, [userMsg('嗯'), assistantMsg('嗯嗯'), userMsg('好'), userMsg('哦')]),
      next,
    )
    expect(probes).toBe(1)
    expect(client.inject).toHaveBeenCalledTimes(2) // 首步 1 + 探测 1
    expect(injectedMessages(r3)).toHaveLength(0)
  })

  it('探测到冷场但服务端未返回修复块 → 静默降级（不注入、不抛错）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient() // inject 恒返回普通画像（无 coldstart_repair）
    registerContextInjection(ctx, client, CFG)
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision([userMsg('好')]))

    await handler(makePayload(1, [userMsg('在吗？')]), next)
    const result = await handler(
      makePayload(2, [userMsg('嗯'), assistantMsg('嗯'), userMsg('好')]),
      next,
    )

    expect(client.inject).toHaveBeenCalledTimes(2) // 探测发生了
    expect(injectedMessages(result)).toHaveLength(0) // 但不注入
    expect(ctx.logger.warn).not.toHaveBeenCalled() // 也不告警
  })

  it('寒暄词（含标点变体）同样触发探测', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({
      inject: vi.fn(async (req: any) =>
        Array.isArray(req?.history) ? makeColdProfile() : makeProfile()),
    })
    registerContextInjection(ctx, client, CFG)
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision([userMsg('好的。')]))

    await handler(makePayload(1, [userMsg('聊点什么？')]), next)
    const result = await handler(
      makePayload(2, [assistantMsg('想聊点什么？'), userMsg('好的。')]),
      next,
    )

    expect(client.inject).toHaveBeenCalledTimes(2)
    expect(injectCallArgs(client, 1).history).toEqual([
      { role: 'assistant', content: '想聊点什么？' },
      { role: 'user', content: '好的。' },
    ])
    expect(injectedMessages(result)).toHaveLength(1)
  })

  it('history 只带最近 ≤8 条（窗口外消息不发给服务端）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeClient({
      inject: vi.fn(async (req: any) =>
        Array.isArray(req?.history) ? makeColdProfile() : makeProfile()),
    })
    registerContextInjection(ctx, client, CFG)
    const handler = listeners.get('agent/pre-step')!
    const next = vi.fn(async () => makeDecision([userMsg('好')]))

    await handler(makePayload(1, [userMsg('随便聊聊？')]), next)

    const fillers = Array.from({ length: 10 }, (_, i) => assistantMsg(`填充消息${i}`))
    const messages = [...fillers, userMsg('嗯'), assistantMsg('嗯嗯'), userMsg('好')]
    await handler(makePayload(2, messages), next)

    const history = injectCallArgs(client, 1).history as Array<{ role: string; content: string }>
    expect(history).toHaveLength(8)
    expect(history.at(-1)).toEqual({ role: 'user', content: '好' })
    // 窗口外的最早填充消息不得出现
    expect(history.some((m) => m.content === '填充消息0')).toBe(false)
    expect(history.some((m) => m.content === '填充消息1')).toBe(false)
  })
})
