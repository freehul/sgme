/**
 * session-sync.ts — turn/end 会话入库（v1.1：累积事件流）
 *
 * v1.1 策略（T-53 实测后修正）：
 * - dsh 的 turn/end 事件本身不含 messages（消息分布在 user/message / assistant/message / tool/result 事件中）
 * - 监听 session/event 统一事件流，累积 user/assistant/tool 消息到当前 turn buffer
 * - turn/end 触发时打包 buffer → toL0 → POST /v1/append → 触发提炼
 *
 * 幂等设计：session_key 用 dsh 会话首条消息时间戳，started_at 用 turn 起始时间戳——
 * 同一 turn 重复触发时 SGME append 幂等命中（status=idempotent）。
 *
 * 事件结构（T-53 解压 session.jsonl.zstd 确认）：
 * - user/message:      { type, seq, time, data: { content: [{type:'text', text}], role:'user', ... } }
 * - assistant/message:  { type, seq, time, data: { turn, step, message: { role:'assistant', content: [{type:'text'|'reasoning'|'tool-call', text?}], ... } } }
 * - tool/result:        { type, seq, time, data: { turn, step, message: { source:{callId}, content: [{type:'tool-result', content:[{type:'text', text}]}] } } }
 * - turn/start:         { type, seq, time, data: { turn } }
 * - turn/end:           { type, seq, time, data: { turn, reason:{kind} } }
 *
 * 契约对齐：
 * - POST /v1/append（Agent Key，session_key/started_at/content/agent_id/ended_at）
 * - POST /v1/admin/refine/trigger_async（Admin Key，limit=50，兼容 200/202）
 */
import type { SgmeClient, SessionMessage } from './sgme-client.js'
import { toL0 } from './sgme-client.js'

/** 会话同步配置。 */
export interface SessionSyncConfig {
  agentId: string               // SGME agent_id（默认 "dsh"）
  syncOnTurnEnd: boolean        // 是否启用 turn/end 同步
  turnBatchSize: number         // v1=1 即每 turn 即 append
}

/** session/event 事件需要的 ctx 能力。 */
export interface SessionSyncCtx {
  on: (event: string, handler: (...args: unknown[]) => void) => () => void
  logger: { info: (msg: string) => void; warn: (msg: string) => void }
}

/**
 * dsh turn/end 事件 payload（v1.1 累积式）。
 *
 * turn/end 本身不含 messages，messages 由 user/assistant/tool 事件累积填充。
 */
export interface TurnEndPayload {
  sessionId?: string
  turnId?: number
  messages: Array<{
    role: string
    content: string
    ts?: string
    toolName?: string
  }>
  startedAt?: string
  endedAt?: string
}

/**
 * 注册 turn/end 会话同步（v1.1 累积式）。
 *
 * @returns 清理函数（由调用方通过 ctx.effect 管理生命周期）
 */
export function registerSessionSync(
  ctx: SessionSyncCtx,
  client: SgmeClient,
  config: SessionSyncConfig,
): () => void {
  if (!config.syncOnTurnEnd) {
    ctx.logger.info('[SGME session-sync] 已禁用（syncOnTurnEnd=false）')
    return () => {}
  }

  // 当前 turn 的消息缓冲区
  let currentTurnMessages: SessionMessage[] = []
  // 当前 turn 起始时间（turn/start 的 time，毫秒戳）
  let currentTurnStartMs: number | undefined
  let currentTurnId: number | undefined
  // 会话级 sessionId（首次 user/message 时用其 time 毫秒戳生成，保证同进程内稳定）
  let sessionKey: string | undefined

  /** 从事件 args 中提取 event 对象（兼容 (event) / (session, event) 形态）。 */
  function pickEvent(args: unknown[]): ({ type?: string } & Record<string, unknown>) | undefined {
    for (const a of args) {
      if (typeof a === 'object' && a !== null && 'type' in a) {
        return a as ({ type?: string } & Record<string, unknown>)
      }
    }
    return undefined
  }

  /** 毫秒时间戳 → ISO 8601 字符串。 */
  function msToIso(ms: unknown): string | undefined {
    if (typeof ms === 'number' && Number.isFinite(ms)) {
      return new Date(ms).toISOString()
    }
    return undefined
  }

  /** 从 user/message 事件提取消息并推入 buffer。 */
  function handleUserMessage(event: Record<string, unknown>): void {
    const data = (event.data ?? {}) as Record<string, unknown>
    const contentArr = data.content
    if (!Array.isArray(contentArr)) return
    // data.content: [{type:'text', text:'...'}, ...]
    const text = contentArr
      .map((c) => (typeof c === 'object' && c !== null ? (c as Record<string, unknown>).text : null))
      .filter((t): t is string => typeof t === 'string')
      .join('\n')
    if (!text.trim()) return

    const ts = msToIso(event.time)
    // 首次 user/message 时生成 sessionId（用 user 消息时间戳，同进程内稳定）
    // syncTurnToSgme 会拼前缀 dsh-{sessionId}
    if (!sessionKey && ts) {
      sessionKey = String(event.time as number)
    }
    currentTurnMessages.push({
      role: 'user',
      content: text,
      ts: ts ?? new Date().toISOString(),
    })
  }

  /** 从 assistant/message 事件提取文本消息并推入 buffer（忽略 reasoning / tool-call 块）。 */
  function handleAssistantMessage(event: Record<string, unknown>): void {
    const data = (event.data ?? {}) as Record<string, unknown>
    const message = data.message as Record<string, unknown> | undefined
    if (!message) return
    const contentArr = message.content
    if (!Array.isArray(contentArr)) return
    // message.content: [{type:'text'|'reasoning'|'tool-call', text?}, ...]
    const text = contentArr
      .filter((c) => {
        if (typeof c !== 'object' || c === null) return false
        const t = (c as Record<string, unknown>).type
        return t === 'text'
      })
      .map((c) => (c as Record<string, unknown>).text)
      .filter((t): t is string => typeof t === 'string')
      .join('\n')
    if (!text.trim()) return

    currentTurnMessages.push({
      role: 'assistant',
      content: text,
      ts: msToIso(event.time) ?? new Date().toISOString(),
    })
  }

  /** 从 tool/result 事件提取工具结果文本并推入 buffer。 */
  function handleToolResult(event: Record<string, unknown>): void {
    const data = (event.data ?? {}) as Record<string, unknown>
    const message = data.message as Record<string, unknown> | undefined
    if (!message) return
    const contentArr = message.content
    if (!Array.isArray(contentArr)) return
    // message.content: [{type:'tool-result', toolCallId, content:[{type:'text', text}]}]
    for (const c of contentArr) {
      if (typeof c !== 'object' || c === null) continue
      const item = c as Record<string, unknown>
      if (item.type !== 'tool-result') continue
      const inner = item.content
      if (!Array.isArray(inner)) continue
      const text = inner
        .map((t) => (typeof t === 'object' && t !== null ? (t as Record<string, unknown>).text : null))
        .filter((t): t is string => typeof t === 'string')
        .join('\n')
      if (!text.trim()) continue
      currentTurnMessages.push({
        role: 'tool',
        content: text,
        toolName: 'tool',
        ts: msToIso(event.time) ?? new Date().toISOString(),
      })
    }
  }

  /** turn/start：记录 turn 起始时间，清空 buffer 准备新 turn。 */
  function handleTurnStart(event: Record<string, unknown>): void {
    const data = (event.data ?? {}) as Record<string, unknown>
    currentTurnId = typeof data.turn === 'number' ? data.turn : undefined
    currentTurnStartMs = typeof event.time === 'number' ? event.time : undefined
    currentTurnMessages = []
  }

  /** turn/end：打包 buffer → append → 触发提炼。 */
  function handleTurnEnd(event: Record<string, unknown>): void {
    const data = (event.data ?? {}) as Record<string, unknown>
    const turnId = typeof data.turn === 'number' ? data.turn : currentTurnId
    const endedAt = msToIso(event.time)
    const startedAt = msToIso(currentTurnStartMs) ?? (currentTurnMessages[0]?.ts ?? new Date().toISOString())

    // 构造 payload 并触发同步
    const payload: TurnEndPayload = {
      messages: currentTurnMessages.map((m) => ({
        role: m.role,
        content: m.content,
        ts: m.ts,
        ...(m.toolName !== undefined ? { toolName: m.toolName } : {}),
      })),
    }
    if (sessionKey) payload.sessionId = sessionKey
    if (turnId !== undefined) payload.turnId = turnId
    if (startedAt) payload.startedAt = startedAt
    if (endedAt) payload.endedAt = endedAt

    if (currentTurnMessages.length === 0) {
      ctx.logger.info(`[SGME session-sync] turn ${turnId ?? '?'} 无有效消息，跳过`)
      return
    }

    // 异步处理（不阻塞 dsh 主循环）
    void syncTurnToSgme(ctx, client, config, payload)

    // 清空 buffer，等下一个 turn/start
    currentTurnMessages = []
  }

  // 统一事件处理器
  const handler = (...args: unknown[]): void => {
    const event = pickEvent(args)
    if (!event?.type) return

    switch (event.type) {
      case 'turn/start':
        handleTurnStart(event)
        return
      case 'user/message':
        handleUserMessage(event)
        return
      case 'assistant/message':
        handleAssistantMessage(event)
        return
      case 'tool/result':
        handleToolResult(event)
        return
      case 'turn/end':
        handleTurnEnd(event)
        return
      default:
        return
    }
  }

  // 监听 session/event（dsh 统一事件流）
  const dispose = ctx.on('session/event', handler)
  ctx.logger.info('[SGME session-sync] 已注册 v1.1 累积式同步监听（user/assistant/tool/turn）')
  return dispose
}

/**
 * 同步单个 turn 到 SGME。
 *
 * 1. 收集本 turn 消息 → 转 L0 格式
 * 2. POST /v1/append（session_key=dsh-{sessionId}，started_at=turn 起始时间）
 * 3. POST /v1/admin/refine/trigger_async（fire-and-forget，失败只 log）
 */
async function syncTurnToSgme(
  ctx: SessionSyncCtx,
  client: SgmeClient,
  config: SessionSyncConfig,
  payload: TurnEndPayload,
): Promise<void> {
  try {
    const messages = extractMessages(payload)
    if (messages.length === 0) {
      ctx.logger.info('[SGME session-sync] turn 无有效消息，跳过')
      return
    }

    const l0Text = toL0(messages)
    const sessionKey = `dsh-${payload.sessionId ?? 'unknown'}`
    const startedAt = payload.startedAt ?? messages[0]!.ts

    // 1. L0 写入
    const appendResp = await client.append({
      session_key: sessionKey,
      started_at: startedAt,
      content: l0Text,
      agent_id: config.agentId,
      ...(payload.endedAt ? { ended_at: payload.endedAt } : {}),
    })

    if (!appendResp) {
      ctx.logger.warn(`[SGME session-sync] append 失败：session=${sessionKey}`)
      return
    }

    ctx.logger.info(
      `[SGME session-sync] append 成功：session=${sessionKey} status=${appendResp.status}` +
      (appendResp.idempotent ? ' (幂等命中)' : '') +
      (appendResp.appended ? ' (追加段)' : ''),
    )

    // 2. 触发提炼（fire-and-forget，失败不阻塞）
    const refineResp = await client.triggerRefine({ limit: 50 })
    if (!refineResp) {
      ctx.logger.warn('[SGME session-sync] 提炼触发失败（数据已在 L0 等待，可稍后手动触发）')
    } else {
      ctx.logger.info(`[SGME session-sync] 提炼已触发：${refineResp.file_id} ${refineResp.status}`)
    }
  } catch (e) {
    ctx.logger.warn(`[SGME session-sync] 同步异常：${e instanceof Error ? e.message : String(e)}`)
  }
}

/**
 * 从 payload 提取消息列表，过滤 system 消息 + 空内容。
 */
function extractMessages(payload: TurnEndPayload): SessionMessage[] {
  if (!payload.messages) return []

  const messages: SessionMessage[] = []
  for (const m of payload.messages) {
    // 过滤 system 消息（与 reasonix to_l0 一致）
    if (m.role === 'system') continue
    // 过滤空内容
    if (!m.content || !m.content.trim()) continue

    const msg: SessionMessage = {
      role: normalizeRole(m.role),
      content: m.content,
      ts: m.ts ?? new Date().toISOString(),
    }
    if (m.toolName !== undefined) {
      msg.toolName = m.toolName
    }
    messages.push(msg)
  }
  return messages
}

/** 角色归一化（dsh 可能用 'tool_result' 等变体，统一到 L0 格式）。 */
function normalizeRole(role: string): SessionMessage['role'] {
  if (role === 'user') return 'user'
  if (role === 'assistant') return 'assistant'
  // tool_result / tool_call_result / function 等变体统一为 tool
  return 'tool'
}
