/**
 * session-sync.ts 测试 — turn/end 会话入库（v1.1 累积式）。
 *
 * 通过 registerSessionSync 公共 API 测试：
 * - syncOnTurnEnd=false 时不注册
 * - 事件触发时调用 client.append + triggerRefine
 * - 消息累积（user/message + assistant/message + tool/result）
 * - L0 格式化
 * - 容错处理（空消息 / system 消息 / append 失败）
 * - turn 隔离（前 turn 消息不污染后 turn）
 */
import { describe, it, expect, vi } from 'vitest'
import { registerSessionSync } from '../src/session-sync.js'
import type { SgmeClient, AppendResponse, RefineTriggerResponse } from '../src/sgme-client.js'

// ---------- mock 工具 ----------

interface MockCtx {
  on: ReturnType<typeof vi.fn>
  logger: { info: ReturnType<typeof vi.fn>; warn: ReturnType<typeof vi.fn> }
}

function makeMockCtx(): MockCtx & { handlerRef: { current: ((...args: unknown[]) => void) | null } } {
  const handlerRef: { current: ((...args: unknown[]) => void) | null } = { current: null }
  return {
    handlerRef,
    on: vi.fn((_event: string, handler: (...args: unknown[]) => void) => {
      handlerRef.current = handler
      return () => { handlerRef.current = null }
    }),
    logger: {
      info: vi.fn(),
      warn: vi.fn(),
    },
  }
}

function makeMockClient(
  appendImpl?: () => AppendResponse | null,
  refineImpl?: () => RefineTriggerResponse | null,
): SgmeClient {
  return {
    append: vi.fn(appendImpl ?? (() => ({
      file_id: 'f1', path: 'raw/sessions/f1.md', status: 'new',
    }))) as unknown as SgmeClient['append'],
    triggerRefine: vi.fn(refineImpl ?? (() => ({
      triggered: 'async', file_id: 'batch', status: 'queued', note: '',
    }))) as unknown as SgmeClient['triggerRefine'],
  } as unknown as SgmeClient
}

// 等待微任务（让 async handler 完成）
function flushMicrotasks(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

// 事件工厂（对齐 dsh session.jsonl 真实结构）
function ev(type: string, time: number, data: Record<string, unknown> = {}): Record<string, unknown> {
  return { type, seq: time, time, data }
}

function userMessageEvent(time: number, text: string): Record<string, unknown> {
  return ev('user/message', time, {
    content: [{ type: 'text', text }],
    source: { kind: 'user' },
    role: 'user',
    id: `u-${time}`,
  })
}

function assistantMessageEvent(time: number, text: string, reasoning?: string): Record<string, unknown> {
  const content: Array<Record<string, unknown>> = []
  if (reasoning) content.push({ type: 'reasoning', text: reasoning })
  content.push({ type: 'text', text })
  return ev('assistant/message', time, {
    turn: 1,
    step: 1,
    message: {
      role: 'assistant',
      content,
      source: { kind: 'model', provider: 'deepseek-official', model: 'deepseek-v4-flash' },
      id: `a-${time}`,
    },
    usage: { inputTokens: 100, outputTokens: 20 },
  })
}

function assistantWithToolCallEvent(time: number, callId: string, toolName: string, args: string): Record<string, unknown> {
  return ev('assistant/message', time, {
    turn: 1,
    step: 1,
    message: {
      role: 'assistant',
      content: [
        { type: 'reasoning', text: 'deciding to call tool' },
        { type: 'tool-call', id: callId, name: toolName, arguments: args },
      ],
      source: { kind: 'model' },
      id: `a-${time}`,
    },
  })
}

function toolResultEvent(time: number, callId: string, text: string): Record<string, unknown> {
  return ev('tool/result', time, {
    turn: 1,
    step: 1,
    message: {
      source: { kind: 'tool', callId },
      content: [{
        type: 'tool-result',
        toolCallId: callId,
        content: [{ type: 'text', text }],
      }],
    },
  })
}

function turnStartEvent(time: number, turn: number = 1): Record<string, unknown> {
  return ev('turn/start', time, { turn })
}

function turnEndEvent(time: number, turn: number = 1): Record<string, unknown> {
  return ev('turn/end', time, { turn, reason: { kind: 'completed' } })
}

// ---------- registerSessionSync ----------

describe('registerSessionSync', () => {
  it('syncOnTurnEnd=false 时不注册监听', () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()
    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: false, turnBatchSize: 1,
    })
    expect(ctx.on).not.toHaveBeenCalled()
    expect(ctx.logger.info).toHaveBeenCalledWith(expect.stringContaining('已禁用'))
  })

  it('syncOnTurnEnd=true 时注册 session/event 监听', () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()
    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })
    expect(ctx.on).toHaveBeenCalledWith('session/event', expect.any(Function))
    expect(ctx.logger.info).toHaveBeenCalledWith(expect.stringContaining('已注册'))
  })

  it('返回 dispose 清理函数', () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()
    const dispose = registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })
    expect(typeof dispose).toBe('function')
  })

  it('完整 turn 流程（user + assistant → turn/end）触发 append + triggerRefine', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    // 模拟完整 turn 事件流
    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(userMessageEvent(1100, '你好'))
    ctx.handlerRef.current!(assistantMessageEvent(1200, '回答', '思考中'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    expect(client.append).toHaveBeenCalledTimes(1)
    expect(client.append).toHaveBeenCalledWith(expect.objectContaining({
      session_key: expect.stringMatching(/^dsh-\d+$/),  // sessionKey 用首条 user 消息 time
      started_at: '1970-01-01T00:00:01.000Z',  // turnStart time=1000ms
      agent_id: 'dsh',
      ended_at: '1970-01-01T00:00:02.000Z',    // turnEnd time=2000ms
    }))
    expect(client.triggerRefine).toHaveBeenCalledWith({ limit: 50 })
  })

  it('累积 user/message + assistant/message + tool/result', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(userMessageEvent(1100, '查询 SGME'))
    ctx.handlerRef.current!(assistantWithToolCallEvent(1200, 'call_00', 'memory_search', '{"query":"SGME"}'))
    ctx.handlerRef.current!(toolResultEvent(1300, 'call_00', '## 1. [memory] SGME 是记忆引擎'))
    ctx.handlerRef.current!(assistantMessageEvent(1400, '已找到 SGME 相关记忆'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    const callArgs = (client.append as ReturnType<typeof vi.fn>).mock.calls[0]![0] as { content: string }
    // user 消息
    expect(callArgs.content).toContain('查询 SGME')
    // assistant 文本消息（应包含两条）
    expect(callArgs.content).toContain('已找到 SGME 相关记忆')
    // tool 结果
    expect(callArgs.content).toContain('## 1. [memory] SGME 是记忆引擎')
    // 不应包含 reasoning 文本
    expect(callArgs.content).not.toContain('deciding to call tool')
  })

  it('append 传 L0 格式 content（user 用 # 前缀，assistant 用 ## 前缀）', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(userMessageEvent(1100, '测试内容'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    const callArgs = (client.append as ReturnType<typeof vi.fn>).mock.calls[0]![0] as { content: string }
    // user 用 # 前缀
    expect(callArgs.content).toMatch(/^# 1970-01-01T00:00:01\.100Z user\n测试内容/m)
  })

  it('turn/start 重置 buffer（前 turn 消息不污染后 turn）', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    // 第一个 turn
    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(userMessageEvent(1100, 'turn1 消息'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    // 第二个 turn
    ctx.handlerRef.current!(turnStartEvent(3000, 2))
    ctx.handlerRef.current!(userMessageEvent(3100, 'turn2 消息'))
    ctx.handlerRef.current!(turnEndEvent(4000, 2))

    await flushMicrotasks()

    expect(client.append).toHaveBeenCalledTimes(2)
    const secondCallArgs = (client.append as ReturnType<typeof vi.fn>).mock.calls[1]![0] as { content: string }
    expect(secondCallArgs.content).toContain('turn2 消息')
    expect(secondCallArgs.content).not.toContain('turn1 消息')
  })

  it('空内容消息被过滤', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(userMessageEvent(1100, ''))           // 空 user
    ctx.handlerRef.current!(userMessageEvent(1150, '   '))        // 空白 user
    ctx.handlerRef.current!(assistantMessageEvent(1200, '有效内容'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    const callArgs = (client.append as ReturnType<typeof vi.fn>).mock.calls[0]![0] as { content: string }
    expect(callArgs.content).toContain('有效内容')
    // 不应包含空消息块（避免出现 # ... user\n$ 空行）
    expect(callArgs.content.match(/^# .* user\n$/m)).toBeNull()
  })

  it('无有效消息时不调用 append', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    // turn/start 后没有任何消息事件，直接 turn/end
    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    expect(client.append).not.toHaveBeenCalled()
  })

  it('append 失败时不调用 triggerRefine', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient(() => null)

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    ctx.handlerRef.current!(turnStartEvent(1000))
    ctx.handlerRef.current!(userMessageEvent(1100, '你好'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    expect(client.append).toHaveBeenCalled()
    expect(client.triggerRefine).not.toHaveBeenCalled()
    expect(ctx.logger.warn).toHaveBeenCalledWith(expect.stringContaining('append 失败'))
  })

  it('未触发 turn/start 时仍能用 turn/end 兜底（用首条消息 ts 作为 startedAt）', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    // 跳过 turn/start，直接 user/message + turn/end
    ctx.handlerRef.current!(userMessageEvent(1100, '你好'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    expect(client.append).toHaveBeenCalledTimes(1)
    // startedAt 兜底为首条消息 ts
    expect(client.append).toHaveBeenCalledWith(expect.objectContaining({
      started_at: '1970-01-01T00:00:01.100Z',
    }))
  })

  it('忽略未识别的事件类型', async () => {
    const ctx = makeMockCtx()
    const client = makeMockClient()

    registerSessionSync(ctx, client, {
      agentId: 'dsh', syncOnTurnEnd: true, turnBatchSize: 1,
    })

    // 发送未识别的事件
    ctx.handlerRef.current!(ev('session/title', 1000, { title: '测试' }))
    ctx.handlerRef.current!(ev('approval/policy', 1100, { kind: 'auto' }))
    ctx.handlerRef.current!(turnStartEvent(1200))
    ctx.handlerRef.current!(userMessageEvent(1300, '有效消息'))
    ctx.handlerRef.current!(turnEndEvent(2000))

    await flushMicrotasks()

    // 未识别事件不影响 turn 流程
    expect(client.append).toHaveBeenCalledTimes(1)
    const callArgs = (client.append as ReturnType<typeof vi.fn>).mock.calls[0]![0] as { content: string }
    expect(callArgs.content).toContain('有效消息')
  })
})
