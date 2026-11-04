/**
 * context.ts — 画像 + 相关记忆首步注入（v2：agent/pre-step middleware 真注入）
 *
 * v2 策略（2026-08-16 对齐 dsh-agent-instructions 官方做法）：
 * - 挂接 agent/pre-step waterfall middleware（与 dsh 内置 agent-instructions 同通道），
 *   在首次 step 时拉取 SGME 画像（/v1/inject）+ 项目相关记忆（/v1/search），
 *   通过返回 {kind:'enter', messages} 把注入消息真正插入模型决策流。
 * - v1 的缺陷：只 ctx.logger.info 打日志，消息从未进入模型上下文（实测会话日志
 *   agent/inbox/spliced 中只有用户消息，无 SGME 画像）→ 本次修复。
 * - 注入时机：首个 step（step === 1）注入一次，之后不再重复（避免每轮污染上下文）。
 *
 * D-72 冷场修复（2026-11-03 接线）：首步注入完成后，仅当本地廉价启发式判定可能冷场时，
 * 额外探测一次 /v1/inject（带 history + already_repaired），响应含
 * ``kind="coldstart_repair"`` 块则把修补提示作为一条 user 消息注入；同一会话最多修补 1 次，
 * 普通长对话**零额外请求**（保持前缀缓存纪律）。
 *
 * 契约对齐：POST /v1/inject（Agent Key，mode + custom_filter 二选一；可选 history/already_repaired）
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { SgmeClient, InjectResponse, SearchResult, CareLevel } from './sgme-client.js'
import type { SgmeEvent, SgmeEventSubscriber } from './events.js'

/**
 * 0.2.0 起 dsh-llm 的 MessageSourceMap 为 merge-extensible sum type
 * （每个生产者声明自己的 kind，不再有共享的 catch-all "plugin" 值；声明手法
 * 对齐官方 dsh-tools/dsh-user-approval 等内部包的 module augmentation）。
 * 本插件在此声明自己的消息来源 kind：dsh-sgme。
 */
declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'dsh-sgme': { kind: 'dsh-sgme' }
  }
}

/** 注入模式（对应 templates/{mode}.yaml）。 */
export type InjectMode = 'daily' | 'full' | 'coding' | 'work'

/** 画像注入配置。 */
/** D-61：push 档只推「关键信号」——就医/安全类 + 已到期待办/情绪低落；其余留待信号池。 */
const KEY_CARE_TYPES = new Set(['care_todo_due', 'care_mood'])
const CARE_LEVEL_TTL_MS = 5 * 60 * 1000

/**
 * D-61（2026-11-04）：按角色关怀档位过滤事件提醒（导出供测试）。
 * - silent：关怀信号一律不注入提醒（仍可 signal_pull 查看/消费；anomaly_warn 照常）
 * - session：全部提醒（旧行为）
 * - push：只保留关键关怀信号（KEY_CARE_TYPES）+ anomaly_warn
 */
export function filterEventsByCareLevel(
  events: SgmeEvent[],
  level: CareLevel,
): SgmeEvent[] {
  if (level === 'session') return events
  if (level === 'silent') {
    return events.filter((e) => !e.type.startsWith('care_'))
  }
  return events.filter(
    (e) => !e.type.startsWith('care_') || KEY_CARE_TYPES.has(e.type),
  )
}

export interface ContextConfig {
  injectMode: InjectMode
  injectMaxTokens: number          // 协议接受但不消费，留作 v2 预算控制参考
  searchLimit: number              // 相关记忆检索条数
  projectHint?: string             // 项目名提示（用于相关记忆检索，可空）
  eventSubscriber?: SgmeEventSubscriber | null  // 2026-08-18：事件订阅器（SSE），有未消费事件时注入提醒
}

/**
 * agent/pre-step 事件 payload（对齐 dsh-agent-instructions 用法）。
 * waterfall middleware：先 await next() 拿基线 decision，再决定注入。
 */
export interface PreStepPayload {
  agent: {
    inbox: {
      nextStep: unknown[]
      remove: (id: unknown) => boolean
    }
    session?: {
      header?: {
        cwd?: string
      }
    }
  }
  messages: unknown[]
  step: number
  signal: AbortSignal
}

/** PreStepDecision：reject 或 enter（携带注入后的 messages）。 */
export type PreStepDecision =
  | { kind: 'reject' }
  | { kind: 'enter'; messages: unknown[] }

/** agent/pre-step 事件需要的 ctx 能力（对齐 dsh Cordis waterfall）。 */
export interface ContextInjectionCtx {
  on: (event: string, handler: (...args: any[]) => any) => () => void
  logger: { info: (msg: string) => void; warn: (msg: string) => void }
}



/** 拼接事件提醒文本（摘要化，2026-08-20 修复）。
 *
 * 此前（44a7b85）把每条 care 信号全文 JSON 附在提醒里 → 每次注入都携带完整
 * payload，上下文重复膨胀。现改为摘要：只给类型+数量+事件 id，
 * agent 需要详情时调 signal_pull（服务端仍是权威源）。
 */
function buildEventNoticeText(events: SgmeEvent[]): string {
  const care = events.filter((e) => e.type.startsWith('care_'))
  const warn = events.filter((e) => e.type === 'anomaly_warn')
  const other = events.filter((e) => !e.type.startsWith('care_') && e.type !== 'anomaly_warn')
  const parts: string[] = []
  if (care.length) parts.push(`关怀信号 ${care.length} 条`)
  if (warn.length) parts.push(`异常告警 ${warn.length} 条`)
  if (other.length) parts.push(`其他事件 ${other.length} 条`)
  const head = [
    '【SGME 事件提醒】',
    `有未处理事件（${parts.join('、')}）。`,
    '如需处理请调 signal_pull 拉取详情，按信号消费纪律处理：signal_claim 原子认领 → 主动关怀/处理 → signal_ack 回执。',
    '不阻塞当前任务，处理完即可。',
  ].join('\n')
  // 摘要：只给类型 + event_id（不含全文 payload，避免上下文膨胀）
  const eventIds = events.slice(0, 5).map((e) => `${e.type}#${e.event_id}`)
  if (!eventIds.length) return head
  return head + '\n【事件列表（最多5条，详情请 signal_pull）】\n' + eventIds.join('\n')
}

/** 相同内容判定（对齐 agent-instructions sameContextPayload：content + source 全等）。 */
function sameContextPayload(left: unknown, right: unknown): boolean {
  if (typeof left !== 'object' || left === null || typeof right !== 'object' || right === null) {
    return left === right
  }
  const l = left as Record<string, unknown>
  const r = right as Record<string, unknown>
  return JSON.stringify(l.content) === JSON.stringify(r.content)
    && JSON.stringify(l.source) === JSON.stringify(r.source)
}

// ---------- D-72 对话冷场修复（2026-11-03 接线） ----------

/** 判据 1 阈值：最近 2 条用户消息都 ≤6 字符且不含问号（镜像服务端 conversation_state）。 */
const COLD_SHORT_USER_LEN = 6
/** 发给服务端的 history 窗口上限（服务端只用最近 4 条算判据 2；8 条足够且成本低）。 */
const COLD_HISTORY_LIMIT = 8
/** 寒暄词表：完全匹配或只差标点（镜像服务端 GREETING_FILLERS）。 */
const COLD_GREETING_FILLERS = new Set([
  '嗯', '哦', '好的', '好', '随便', '都行', '是吗', '哈哈', '在吗',
])

/** 去首尾标点与空白（对应 Python _strip_punct：只差标点视为同一词）。 */
function stripPunct(text: string): string {
  return text.replace(/^[\s\p{P}]+|[\s\p{P}]+$/gu, '')
}

/**
 * 本地廉价冷场启发式：只决定「值不值得向服务端探测」，**最终判据在服务端**。
 *
 * 镜像服务端可离线判定的两条判据（第三条 assistant_monologue 需统计均值且易误判，
 * 交给服务端）：① 最近 2 条 user 消息都 ≤6 字符且不含问号；② 最近 user 消息命中寒暄词表。
 */
function looksLikeColdStart(history: Array<{ role: string; content: string }>): boolean {
  const users = history.filter((m) => m.role === 'user').map((m) => m.content)
  const lastTwo = users.slice(-2)
  if (
    lastTwo.length === 2
    && lastTwo.every((c) => c.length <= COLD_SHORT_USER_LEN && !c.includes('?') && !c.includes('？'))
  ) {
    return true
  }
  const lastUser = users.at(-1) ?? ''
  return COLD_GREETING_FILLERS.has(lastUser) || COLD_GREETING_FILLERS.has(stripPunct(lastUser))
}

/** 从 payload.messages 构造发给服务端的 history（最近 ≤8 条，只留 user/assistant 文本）。 */
function buildColdRepairHistory(messages: unknown[]): Array<{ role: string; content: string }> {
  const out: Array<{ role: string; content: string }> = []
  for (const m of messages) {
    if (!m || typeof m !== 'object') continue
    const msg = m as Record<string, unknown>
    const source = msg.source as Record<string, unknown> | undefined
    // 跳过本插件自身注入（画像 / 事件提醒 / 修复提示）——它们不是用户真实发言，
    // 混进去会污染「最近 2 条 user 消息」判据（也避免把画像当冷场证据）。
    if (source?.kind === 'dsh-sgme') continue
    const role = msg.role
    if (role !== 'user' && role !== 'assistant') continue
    const text = extractMessageText(msg.content)
    if (!text) continue
    out.push({ role, content: text })
  }
  return out.slice(-COLD_HISTORY_LIMIT)
}

/** 从 inject 响应里取冷场修复提示文本（kind=coldstart_repair 块的 items[].content 拼接）。 */
function extractColdstartRepairHint(profile: InjectResponse | null): string | undefined {
  const block = profile?.blocks?.find((b) => b.kind === 'coldstart_repair')
  if (!block) return undefined
  const text = (block.items ?? [])
    .map((item) => (typeof item?.content === 'string' ? item.content : ''))
    .filter((t) => t.trim().length > 0)
    .join('\n')
    .trim()
  return text || undefined
}

/**
 * 注册画像首步注入（agent/pre-step middleware）。
 *
 * 实现方式：监听 agent/pre-step（waterfall），首次 step 时拉取 SGME 画像 + 相关记忆，
 * 拼接为 user 角色消息，返回 {kind:'enter', messages: ...} 注入模型决策流。
 *
 * 与 agent-instructions 共存：同通道多 middleware 串行叠加，SGME 消息插在
 * claimed messages 之后（lastClaimedIndex+1），不影响 agent-instructions 的注入。
 *
 * @returns 清理函数（由 ctx.effect 调用方管理生命周期）
 */
export function registerContextInjection(
  ctx: ContextInjectionCtx,
  client: SgmeClient,
  config: ContextConfig,
): () => void {
  // 预拉取缓存：turn/start 时异步拉取画像，pre-step 直接用缓存（不阻塞主循环）
  let profileCache: { text: string; ts: number } | null = null
  // D-61：当前角色关怀档位缓存（5 分钟 TTL；拉取失败保持旧行为 session，避免突然吞提醒）
  let careLevel: CareLevel = 'session'
  let careLevelTs = 0
  async function ensureCareLevel(): Promise<CareLevel> {
    if (careLevelTs && Date.now() - careLevelTs < CARE_LEVEL_TTL_MS) {
      return careLevel
    }
    try {
      const level = await client.activeCareLevel()
      if (level) {
        careLevel = level
        careLevelTs = Date.now()
      }
    } catch {
      // 失败保持旧值（首次失败 = session，宁滥勿缺；silent 只有在服务端明确返回时才生效）
    }
    return careLevel
  }
  // 正在拉取中的 promise（防并发重复拉取）
  let fetching: Promise<void> | null = null
  // 注入状态：仅成功注入后置位
  let injected = false
  // D-72 冷场修复状态：本会话已修补过（最多 1 次）；记录上次探测时的最后一条用户
  // 输入，避免同一 turn 的多个 step 对相同输入重复请求。
  let coldRepaired = false
  let coldProbeUserText = ''

  /** 预拉取画像 + 相关记忆（turn/start 触发，失败不置位，下轮重试）。 */
  const prefetch = (projectHint: string | undefined): void => {
    if (fetching) return
    fetching = (async () => {
      try {
        const [profile, related] = await Promise.all([
          client.inject({ mode: config.injectMode }),
          projectHint
            ? client.search({
                query: projectHint,
                scopes: ['memory'],
                limit: config.searchLimit,
              })
            : Promise.resolve(null),
        ])
        const text = buildInjectionText(profile, related)
        if (text) {
          profileCache = { text, ts: Date.now() }
        }
      } catch (e) {
        ctx.logger.warn(`[SGME 画像预拉取失败] ${e instanceof Error ? e.message : String(e)}`)
      } finally {
        fetching = null
      }
    })()
  }

  const handler = async (payload: PreStepPayload, next: () => Promise<PreStepDecision>): Promise<PreStepDecision> => {
    const decision = await next()
    if (decision.kind === 'reject') return decision

    // 事件提醒（2026-08-20 修复）：只对「未消费且未提醒过」的事件注入一次。
    // 此前用 pendingEvents() 判断 → 未消费事件每轮重复注入 → 上下文爆增。
    // 现用 unnotifiedEvents()：同一事件只提醒一次（markNotified 后不再注入），
    // 避免死循环；agent 处理后再 markConsumed 移除。
    const unnotified = config.eventSubscriber?.unnotifiedEvents() ?? []
    if (unnotified.length && injected) {
      // D-61：按当前角色关怀档位过滤（silent 不注入 care_*；push 只推关键信号）
      const level = await ensureCareLevel()
      const filtered = filterEventsByCareLevel(unnotified, level)
      if (filtered.length) {
        const evText = buildEventNoticeText(filtered)
        const evMsg = createUserMessage({
          content: [{ type: 'text', text: evText }],
          source: { kind: 'dsh-sgme' },
        })
        if (!decision.messages.some((m) => sameContextPayload(m, evMsg))) {
          ctx.logger.info(
            `[SGME 事件提醒] 注入 ${filtered.length} 条（care_level=${level}，step ${payload.step}）`,
          )
          // 标记已提醒，防止下轮重复注入（含被档位过滤掉的事件：它们仍可在信号池 signal_pull 查看）
          config.eventSubscriber?.markNotified(unnotified.map((e) => e.event_id))
          return { kind: 'enter', messages: [evMsg, ...decision.messages] }
        }
      } else {
        config.eventSubscriber?.markNotified(unnotified.map((e) => e.event_id))
      }
    }

    // ── D-72 对话冷场修复（2026-11-03 接线）────────────────────────────────
    // 缓存红线：首步注入仍是唯一常态 inject 调用。只有「首步已完成 + 本地启发式
    // 认为可能冷场 + 本会话未修补过 + 该用户输入未探测过」才额外探测一次——
    // 普通长对话零额外请求。响应含修复块才注入 1 条 user 消息并置已修补标记。
    if (injected && !coldRepaired) {
      const history = buildColdRepairHistory(payload.messages ?? [])
      const lastUserText = [...history].reverse().find((m) => m.role === 'user')?.content ?? ''
      if (lastUserText && lastUserText !== coldProbeUserText && looksLikeColdStart(history)) {
        coldProbeUserText = lastUserText
        try {
          const profile = await client.inject({
            mode: config.injectMode,
            history,
            already_repaired: coldRepaired,
          })
          const hint = extractColdstartRepairHint(profile)
          if (hint) {
            coldRepaired = true // 修补闸：同一会话最多修补 1 次
            const repairMsg = createUserMessage({
              content: [{ type: 'text', text: hint }],
              source: { kind: 'dsh-sgme' },
            })
            if (!decision.messages.some((m) => sameContextPayload(m, repairMsg))) {
              ctx.logger.info(`[SGME 冷场修复] 已注入修补提示（step ${payload.step}）`)
              // 追加到**末尾**：只在尾部新增 token，不破坏既有前缀缓存
              return { kind: 'enter', messages: [...decision.messages, repairMsg] }
            }
          }
        } catch (e) {
          // 修复是加分项，任何异常都不得影响主流程
          ctx.logger.warn(`[SGME 冷场修复失败] ${e instanceof Error ? e.message : String(e)}`)
        }
      }
    }

    if (injected) return decision
    if (payload.step !== 1) return decision

    // 项目提示：显式配置 > 环境变量 > 会话 cwd 目录名推断
    const projectHint = config.projectHint
      || process.env.SGME_PROJECT_HINT
      || (payload.agent?.session?.header?.cwd
        ? payload.agent.session.header.cwd.split(/[\\/]/).filter(Boolean).pop()
        : undefined)

    // ── T-88 对话内容驱动（2026-08-20 用户定，对齐 Hermes T-42）──
    // 首句 → /v1/search(memory+wiki) 命中 L2 场景 → 注入「场景 + 相关记忆」；
    // 无场景命中 / 无首句 → 回退模板画像注入（原逻辑）。
    // 缓存红线：仍只首步注入一次、会话内固定——绝不做每轮动态注入
    // （DeepSeek 前缀缓存命中 ¥0.02/M vs 未命中 ¥1/M，每轮变化 = 输入成本 ×14）。
    let sceneInjected = false
    const firstUserText = extractFirstUserText(payload.messages ?? [])
    if (firstUserText) {
      try {
        const sceneSearch = await client.search({
          query: firstUserText,
          scopes: ['memory', 'wiki'],
          limit: config.searchLimit,
        })
        const scenes = (sceneSearch?.results ?? []).filter(
          (r): r is SearchResult & { source: 'wiki' | 'scenes' | 'wiki_scene' } =>
            // ⚠️ 实测（SGME 1.1.0，2026-08-29）：wiki 场景层 source 实际返回 `wiki_scene`，
            // 旧判断只认 'wiki'/'scenes' → 过滤恒空、scenes.length 永远为 0 →
            // T-88「首句命中 L2 场景注入」从未生效，一直静默回退模板注入。此处补上实际值。
            r.source === 'wiki' || r.source === 'scenes' || r.source === 'wiki_scene',
        )
        const memories = (sceneSearch?.results ?? []).filter(
          (r): r is SearchResult & { source: 'memory' } => r.source === 'memory',
        )
        if (scenes.length > 0) {
          const text = buildSceneInjectionText(scenes, memories)
          if (text) {
            profileCache = { text, ts: Date.now() }
            sceneInjected = true
          }
        }
      } catch (e) {
        ctx.logger.warn(`[SGME 场景检索失败] ${e instanceof Error ? e.message : String(e)}`)
        // 回退模板注入（不置位，下轮可重试）
      }
    }

    // 模板画像注入（原逻辑）：场景未命中时兜底
    if (!sceneInjected) {
      // 预拉取未完成则等它（首轮画像应尽快注入）
      if (fetching) {
        try { await fetching } catch { /* 已 log */ }
      }
      // 仍无缓存（预拉取失败且未重试）→ 现场拉一次
      if (!profileCache) {
        try {
          const [profile, related] = await Promise.all([
            client.inject({ mode: config.injectMode }),
            projectHint
              ? client.search({
                  query: projectHint,
                  scopes: ['memory'],
                  limit: config.searchLimit,
                })
              : Promise.resolve(null),
          ])
          const text = buildInjectionText(profile, related)
          if (text) profileCache = { text, ts: Date.now() }
        } catch (e) {
          ctx.logger.warn(`[SGME 画像注入失败] ${e instanceof Error ? e.message : String(e)}`)
          return decision  // 不置位 injected，下轮重试
        }
      }
    }

    if (!profileCache) return decision  // 画像为空，跳过（不置位，下轮可重试）

    injected = true  // 成功注入后才置位

    // 事件提醒（2026-08-20 修复）：只对「未消费且未提醒过」的事件附加到画像注入文本
    const unnotifiedFirstTurn = config.eventSubscriber?.unnotifiedEvents() ?? []
    // D-61：首轮提醒同样按关怀档位过滤
    const firstTurnLevel = unnotifiedFirstTurn.length
      ? await ensureCareLevel()
      : 'session'
    const filteredFirstTurn = filterEventsByCareLevel(
      unnotifiedFirstTurn,
      firstTurnLevel,
    )
    const injectText = filteredFirstTurn.length
      ? profileCache.text + '\n\n' + buildEventNoticeText(filteredFirstTurn)
      : profileCache.text
    // 首轮画像注入时一并标记已提醒（防下轮重复；被档位过滤的也标记，信号池仍可手动查看）
    if (unnotifiedFirstTurn.length) {
      config.eventSubscriber?.markNotified(unnotifiedFirstTurn.map((e) => e.event_id))
    }

    const desired = createUserMessage({
      content: [{ type: 'text', text: injectText }],
      source: { kind: 'dsh-sgme' },
    })

    // 已存在相同注入则跳过
    if (decision.messages.some((message) => sameContextPayload(message, desired))) {
      return decision
    }

    // 注入位置：首条消息之前（优化前缀缓存——稳定画像靠前）
    const firstClaimedIndex = decision.messages.findIndex((message) =>
      (payload.messages ?? []).includes(message),
    )
    const insertAt = firstClaimedIndex === -1 ? 0 : firstClaimedIndex
    ctx.logger.info(`[SGME 画像注入] 已注入 ${profileCache.text.length} 字符（step ${payload.step}）`)
    return {
      kind: 'enter',
      messages: decision.messages.toSpliced(insertAt, 0, desired),
    }
  }

  // turn/start 时预拉取画像（异步，不阻塞）
  const disposePrefetch = ctx.on('turn/start', (payload: unknown) => {
    const agent = (payload as { agent?: { session?: { header?: { cwd?: string } } } })?.agent
    const projectHint = config.projectHint
      || process.env.SGME_PROJECT_HINT
      || (agent?.session?.header?.cwd
        ? agent.session.header.cwd.split(/[\\/]/).filter(Boolean).pop()
        : undefined)
    prefetch(projectHint)
  })

  const disposePreStep = ctx.on('agent/pre-step', handler)
  return () => {
    disposePrefetch()
    disposePreStep()
  }
}

/**
 * 拼接画像注入文本（模型可读格式）。
 *
 * 格式（对齐 reasonix cmd_start 注入）：
 * ```
 * --- SGME 用户画像 ---
 * [Tier0 摘要]
 * ...
 * [记忆区块 1: identity]
 * - 记忆内容...
 * --- 相关记忆 ---
 * 1. 内容...
 * ```
 */
export function buildInjectionText(
  profile: InjectResponse | null,
  related: { results: Array<{ rank: number; content?: string }> } | null,
): string {
  const hasTier0 = profile?.tier0.present && profile.tier0.content
  if (!profile || (profile.blocks.length === 0 && !hasTier0)) {
    // 画像为空（无 blocks 且无 Tier0）时只注入相关记忆（若有）
    if (related && related.results.length > 0) {
      return formatRelatedMemories(related.results)
    }
    return ''
  }

  const parts: string[] = ['--- SGME 用户画像 ---']

  // Tier0 摘要（若有）
  if (profile.tier0.present && profile.tier0.content) {
    parts.push('[Tier0 摘要]', profile.tier0.content)
  }

  // 各维度区块
  for (const block of profile.blocks) {
    if (block.items.length === 0) continue
    parts.push(`[${block.title}]`)
    for (const item of block.items) {
      const content = (item.content as string) ?? JSON.stringify(item)
      // 单条记忆截断（避免超长）
      const truncated = content.length > 200 ? content.slice(0, 200) + '…' : content
      parts.push(`- ${truncated}`)
    }
  }

  // 相关记忆（若有）
  if (related && related.results.length > 0) {
    parts.push('--- 相关记忆 ---')
    parts.push(formatRelatedMemories(related.results))
  }

  // 注入引导语（对齐 reasonix）
  parts.push('（以上为 SGME 注入的画像与记忆，可直接引用，不必重复询问用户）')

  return parts.join('\n')
}

/** 提取会话首条用户消息文本（T-88 对话内容驱动 query）。
 *
 * 兼容 dsh 消息结构：content 为字符串或 [{type:'text',text}] 数组；
 * 跳过插件注入消息（source.kind==='dsh-sgme'，避免把 SGME 画像当首句）；
 * role 存在时仅接受 user。
 */
function extractFirstUserText(messages: unknown[]): string | undefined {
  for (const m of messages) {
    if (!m || typeof m !== 'object') continue
    const msg = m as Record<string, unknown>
    const source = msg.source as Record<string, unknown> | undefined
    if (source?.kind === 'dsh-sgme') continue
    const role = msg.role
    if (role !== undefined && role !== 'user') continue
    const text = extractMessageText(msg.content)
    if (text) return text.slice(0, 500)
  }
  return undefined
}

/** 从消息 content 提取文本（字符串或 [{type:'text',text}] 数组）。 */
function extractMessageText(content: unknown): string | undefined {
  if (typeof content === 'string') return content.trim() || undefined
  if (Array.isArray(content)) {
    const parts: string[] = []
    for (const c of content) {
      if (c && typeof c === 'object') {
        const cc = c as Record<string, unknown>
        if (typeof cc.text === 'string') parts.push(cc.text)
      }
    }
    const t = parts.join(' ').trim()
    return t || undefined
  }
  return undefined
}

/** 拼接场景注入文本（T-88：首句命中 L2 场景时优先注入场景 + 相关记忆）。 */
function buildSceneInjectionText(
  // content 可选：统一搜索各层投影不保证都带 content（skills 层就没有），取 .length 前必须兜底。
  scenes: Array<{ rank: number; content?: string; title?: string }>,
  memories: Array<{ rank: number; content?: string }>,
): string {
  const parts: string[] = []
  if (scenes.length > 0) {
    parts.push('--- SGME 相关场景 ---')
    for (const s of scenes.slice(0, 3)) {
      const title = s.title ? `[${s.title}] ` : ''
      const raw = s.content ?? ''
      const truncated = raw.length > 300 ? raw.slice(0, 300) + '…' : raw
      parts.push(`- ${title}${truncated}`)
    }
  }
  if (memories.length > 0) {
    parts.push('--- 相关记忆 ---')
    parts.push(formatRelatedMemories(memories))
  }
  if (parts.length === 0) return ''
  parts.push('（以上为 SGME 注入的场景与记忆，可直接引用，不必重复询问用户）')
  return parts.join('\n')
}

/** 格式化相关记忆列表。 */
function formatRelatedMemories(
  // content 可选：skills 层结果无 content（只有 name/description），取 .length 前必须兜底。
  results: Array<{ rank: number; content?: string }>,
): string {
  return results
    .map((r) => {
      const raw = r.content ?? ''
      const truncated = raw.length > 200 ? raw.slice(0, 200) + '…' : raw
      return `${r.rank}. ${truncated}`
    })
    .join('\n')
}
