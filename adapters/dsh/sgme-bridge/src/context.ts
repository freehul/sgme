/**
 * context.ts — 画像 + 相关记忆首步注入
 *
 * v1 策略：在会话首个 agent step 之前，拉取 SGME 画像（/v1/inject）+ 项目相关记忆
 * （/v1/search），通过 agent.inject(message) 注入 dsh inbox。
 *
 * v1 只做首步注入（已注入标志位防重），每轮注入留 v2（需研究 agent/pre-step 去重/预算机制，
 * 避免与 dsh 内置 agent-instructions 冲突）。
 *
 * 契约对齐：POST /v1/inject（Agent Key，mode + custom_filter 二选一）
 */
import type { SgmeClient, InjectResponse } from './sgme-client.js'

/** 注入模式（对应 templates/{mode}.yaml）。 */
export type InjectMode = 'daily' | 'full' | 'coding' | 'work'

/** 画像注入配置。 */
export interface ContextConfig {
  injectMode: InjectMode
  injectMaxTokens: number          // 协议接受但不消费，留作 v2 预算控制参考
  searchLimit: number              // 相关记忆检索条数
  projectHint?: string             // 项目名提示（用于相关记忆检索，可空）
}

/** agent/pre-step 事件需要的 ctx 能力（内联类型，对齐 dsh Cordis）。 */
export interface ContextInjectionCtx {
  on: (event: string, handler: (...args: unknown[]) => void) => () => void
  /** dsh agent 注入消息到 inbox 的能力（v1 用 console 占位，T-53 本地加载时确认实际 API）。 */
  logger: { info: (msg: string) => void; warn: (msg: string) => void }
}

/**
 * 注册画像首步注入。
 *
 * 实现方式：监听 agent 事件（首步触发），拉取 SGME 画像 → 拼接为指令文本 → 返回给 dsh。
 *
 * 注意：dsh 的 agent.inject(message) API 在 v0.1 不稳定，本实现先用返回值方式
 * （通过 agent/pre-step 事件返回注入内容），T-53 本地加载时确认实际注入路径。
 *
 * @returns 清理函数（由 ctx.effect 调用方管理生命周期）
 */
export function registerContextInjection(
  ctx: ContextInjectionCtx,
  client: SgmeClient,
  config: ContextConfig,
): () => void {
  let injected = false  // 首步注入标志位（v1 只注入一次）

  // handler 接收 dsh session/event 的可变参数（...args）。
  // 兼容 (session, event) 与 (event) 两种调用形态：找第一个含 type 字段的对象作为 event。
  const handler = (...args: unknown[]): void => {
    let event: ({ type?: string } & Record<string, unknown>) | undefined
    for (const a of args) {
      if (typeof a === 'object' && a !== null && 'type' in a) {
        event = a as ({ type?: string } & Record<string, unknown>)
        break
      }
    }
    // 只在 turn/start 时触发首步注入（对齐 dsh-agent-instructions 的 session/event 用法）
    if (event?.type !== 'turn/start') return
    if (injected) return  // 已注入，跳过
    injected = true

    // 异步拉取画像 + 相关记忆（不阻塞 dsh 主循环，失败只 log）
    void (async () => {
      try {
        const [profile, related] = await Promise.all([
          client.inject({ mode: config.injectMode }),
          config.projectHint
            ? client.search({
                query: config.projectHint,
                scopes: ['memory'],
                limit: config.searchLimit,
              })
            : Promise.resolve(null),
        ])

        const injectionText = buildInjectionText(profile, related)
        if (injectionText) {
          // v1：通过 logger 输出注入文本（dsh agent.inbox.splice API 待 v2 接入）
          ctx.logger.info(`[SGME 画像注入]\n${injectionText}`)
        }
      } catch (e) {
        ctx.logger.warn(`[SGME 画像注入失败] ${e instanceof Error ? e.message : String(e)}`)
      }
    })()
  }

  // 监听 session/event（dsh 统一事件流），过滤 turn/start 触发首步注入
  const dispose = ctx.on('session/event', handler)
  return dispose
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
  related: { results: Array<{ rank: number; content: string }> } | null,
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

/** 格式化相关记忆列表。 */
function formatRelatedMemories(
  results: Array<{ rank: number; content: string }>,
): string {
  return results
    .map((r) => {
      const truncated = r.content.length > 200 ? r.content.slice(0, 200) + '…' : r.content
      return `${r.rank}. ${truncated}`
    })
    .join('\n')
}
