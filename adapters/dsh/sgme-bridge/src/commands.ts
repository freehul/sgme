/**
 * commands.ts — /sgme 综合检索命令
 *
 * 用户在 dsh 会话内输入 `/sgme <关键词>` 时触发，检索 SGME 记忆 + 知识库并返回结果。
 * 与 reasonix 的 /sgme 命令语义对齐。
 *
 * dsh 命令规范（2026-08-14 T-53 本地加载确认，对齐 @deepseek-ai/dsh-commands 官方文档）：
 * - ctx.commands.register(definition: CommandDefinition) — 单参数对象
 * - definition = { name, description, handler(invocation): CommandResult }
 * - CommandResult = { kind: 'success', text } | { kind: 'error', text }
 * - invocation = { commandId, agent, rawInput, signal }
 *
 * 契约对齐：POST /v1/search（Agent Key，scopes=["memory","wiki","wiki_pages"]）
 */
import type { SgmeClient } from './sgme-client.js'

/** /sgme 命令配置。 */
export interface CommandConfig {
  searchLimit: number
}

/** dsh 命令结果（对齐 dsh-commands CommandResult）。 */
export type CommandResult =
  | { kind: 'success'; text: string }
  | { kind: 'error'; text: string }

/** dsh 命令 invocation（执行上下文）。 */
export interface CommandInvocation {
  readonly rawInput: string
  readonly signal: AbortSignal
}

/**
 * 执行 /sgme 检索，返回 dsh CommandResult。
 *
 * @param query 用户输入的检索关键词（/sgme 后的参数）
 */
export async function executeSgmeCommand(
  client: SgmeClient,
  config: CommandConfig,
  query: string,
): Promise<CommandResult> {
  const trimmed = query.trim()
  if (!trimmed) {
    return {
      kind: 'error',
      text: [
        '用法：/sgme <关键词>',
        '',
        '检索 SGME 记忆池 + 知识库，查询用户/项目的历史事实与场景知识。',
        '示例：/sgme 之前提过的项目',
      ].join('\n'),
    }
  }

  const resp = await client.search({
    query: trimmed,
    scopes: ['memory', 'wiki', 'wiki_pages'],
    limit: config.searchLimit,
  })

  if (!resp) {
    return { kind: 'error', text: '[/sgme 失败：SGME Gateway 不可达，请检查服务是否运行]' }
  }

  if (resp.results.length === 0) {
    return { kind: 'success', text: `[/sgme 无结果：query="${trimmed}"]\n\n记忆库中未找到相关内容。` }
  }

  const lines: string[] = [`[/sgme 检索结果：query="${trimmed}"]`, '']
  for (const r of resp.results) {
    const titlePrefix = r.title ? `「${r.title}」` : ''
    const routes = r.routes && r.routes.length > 0 ? ` [${r.routes.join(',')}]` : ''
    const content = r.content.length > 500 ? r.content.slice(0, 500) + '…' : r.content
    lines.push(`## ${r.rank}. [${r.source}]${titlePrefix}${routes}`)
    lines.push(content)
    lines.push('')
  }
  return { kind: 'success', text: lines.join('\n') }
}

/**
 * 向 dsh ctx 注册 /sgme 命令（对齐 dsh-commands 官方 register 签名）。
 *
 * ctx.commands.register(definition) 单参数，返回 disposer。
 */
export function registerSgmeCommand(
  ctx: {
    commands: {
      register: (definition: {
        name: string
        description: string
        handler: (invocation: CommandInvocation) => Promise<CommandResult>
      }) => () => void
    }
  },
  client: SgmeClient,
  config: CommandConfig,
): void {
  ctx.commands.register({
    name: 'sgme',
    description: '检索 SGME 记忆 + 知识库（query 为关键词）',
    async handler(invocation) {
      return executeSgmeCommand(client, config, invocation.rawInput)
    },
  })
}
