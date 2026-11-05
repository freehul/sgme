/**
 * 角色人设自动注入测试（2026-11-05）。
 *
 * 背景：服务端 active_role 只是状态位；旧适配器从未把它注入模型上下文，
 * 用户激活「伴侣」后语气无变化（实测）。本测试锁死新行为：
 * - 首步自动 active_role → role_assemble → 人设拼进首步注入消息；
 * - 无激活角色时注入不受影响；
 * - 角色热切换（turn/start 复查）在下一步末尾补一条切换消息；
 * - roleInject=false 时完全不发角色请求；角色接口失败不阻塞画像注入。
 */
import { describe, it, expect, vi } from 'vitest'
import { registerContextInjection } from '../src/context.js'
import type { SgmeClient, InjectResponse } from '../src/sgme-client.js'

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

function makeRoleClient(opts: {
  roleId?: string | null
  roleName?: string
  prompt?: string
  assembleThrows?: boolean
} = {}): SgmeClient {
  const roleId = opts.roleId === undefined ? 'companion' : opts.roleId
  const base = {
    inject: vi.fn(async () => makeProfile()),
    search: vi.fn(async () => null),
    roleActiveGet: vi.fn(async () => ({ role_id: roleId })),
    roleAssemble: vi.fn(async (id: string) => {
      if (opts.assembleThrows) throw new Error('assemble boom')
      return {
        role_id: id,
        role_name: opts.roleName ?? (id === 'companion' ? '伴侣' : '管家'),
        system_prompt: opts.prompt ?? '你是他的伴侣，温柔亲密，先接住情绪再说事。',
        care_level: 'silent' as const,
        care_policy: null,
        persona: null,
        profile_blocks: [],
      }
    }),
  }
  return base as unknown as SgmeClient
}

const CFG = { injectMode: 'daily' as const, injectMaxTokens: 800, searchLimit: 5 }

async function flush(): Promise<void> {
  await new Promise((r) => setTimeout(r, 10))
}

describe('角色人设自动注入（active_role → role_assemble → 首步注入）', () => {
  it('首步把当前角色人设拼进注入消息（角色块在前）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeRoleClient()
    registerContextInjection(ctx, client, { ...CFG, roleInject: true })
    const handler = listeners.get('agent/pre-step')!
    const result = (await handler(makePayload(1), vi.fn(async () => makeDecision()))) as {
      messages: any[]
    }
    expect(client.roleActiveGet).toHaveBeenCalled()
    expect(client.roleAssemble).toHaveBeenCalledWith('companion', 'daily')
    const text = result.messages[0].content[0].text as string
    expect(text).toContain('【当前沟通角色：伴侣】')
    expect(text).toContain('你是他的伴侣，温柔亲密')
    expect(text).toContain('SGME 用户画像') // 画像内容仍保留
  })

  it('无激活角色时只注入画像（不调用 assemble）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeRoleClient({ roleId: null })
    registerContextInjection(ctx, client, { ...CFG, roleInject: true })
    const handler = listeners.get('agent/pre-step')!
    const result = (await handler(makePayload(1), vi.fn(async () => makeDecision()))) as {
      messages: any[]
    }
    expect(client.roleActiveGet).toHaveBeenCalled()
    expect(client.roleAssemble).not.toHaveBeenCalled()
    const text = result.messages[0].content[0].text as string
    expect(text).not.toContain('【当前沟通角色')
    expect(text).toContain('SGME 用户画像')
  })

  it('角色热切换：turn/start 复查后在下一步末尾补一条切换消息', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeRoleClient()
    registerContextInjection(ctx, client, { ...CFG, roleInject: true })
    const handler = listeners.get('agent/pre-step')!
    const turnStart = listeners.get('turn/start')!
    // 首步注入 companion
    await handler(makePayload(1), vi.fn(async () => makeDecision()))
    // 用户切成管家
    ;(client.roleActiveGet as any).mockResolvedValue({ role_id: 'butler' })
    turnStart({ agent: { session: { header: {} } } })
    await flush()
    expect(client.roleAssemble).toHaveBeenCalledWith('butler', 'daily')
    // 下一 step：追加切换消息
    const result = (await handler(makePayload(2), vi.fn(async () => makeDecision()))) as {
      messages: any[]
    }
    const last = result.messages[result.messages.length - 1]
    expect(last.source.kind).toBe('dsh-sgme')
    expect(last.content[0].text).toContain('【角色切换：伴侣 → 管家】')
    expect(last.content[0].text).toContain('你是他的伴侣') // 新人设全文
  })

  it('角色接口失败不阻塞画像注入，仅告警', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeRoleClient({ assembleThrows: true })
    registerContextInjection(ctx, client, { ...CFG, roleInject: true })
    const handler = listeners.get('agent/pre-step')!
    const result = (await handler(makePayload(1), vi.fn(async () => makeDecision()))) as {
      messages: any[]
    }
    expect(result.messages.length).toBe(2)
    expect(result.messages[0].content[0].text).toContain('SGME 用户画像')
    expect(ctx.logger.warn).toHaveBeenCalled()
  })

  it('roleInject=false 时完全不发角色请求（可一键关）', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeRoleClient()
    registerContextInjection(ctx, client, { ...CFG, roleInject: false })
    const handler = listeners.get('agent/pre-step')!
    await handler(makePayload(1), vi.fn(async () => makeDecision()))
    expect(client.roleActiveGet).not.toHaveBeenCalled()
    expect(client.roleAssemble).not.toHaveBeenCalled()
  })
  it('中途首次激活角色：下一个 step 追加激活消息', async () => {
    const { ctx, listeners } = makeCtx()
    const client = makeRoleClient({ roleId: null })
    registerContextInjection(ctx, client, { ...CFG, roleInject: true })
    const handler = listeners.get('agent/pre-step')!
    const turnStart = listeners.get('turn/start')!
    // 首步：无角色，只注画像
    await handler(makePayload(1), vi.fn(async () => makeDecision()))
    // 中途激活伴侣
    ;(client.roleActiveGet as any).mockResolvedValue({ role_id: 'companion' })
    turnStart({ agent: { session: { header: {} } } })
    await flush()
    const result = (await handler(makePayload(2), vi.fn(async () => makeDecision()))) as {
      messages: any[]
    }
    const last = result.messages[result.messages.length - 1]
    expect(last.content[0].text).toContain('【角色激活：伴侣】')
  })
})
