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
  /** 状态报告用：SGME 地址（缺省显示未知）。 */
  baseUrl?: string
  /** 状态报告用：agent key 是否已配置。 */
  agentKeySet?: boolean
  /** 状态报告用：admin key 是否已配置。 */
  adminKeySet?: boolean
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
 * 构建 /sgme status 状态报告（连接自检：health + key 配置 + 记忆水位）。
 *
 * 不可达时给出桥接插件定位与本体安装指引（防止「装了插件没记忆功能」困惑）。
 */
async function buildStatusReport(
  client: SgmeClient,
  config: CommandConfig,
): Promise<CommandResult> {
  const health = await client.health()
  if (!health) {
    return {
      kind: 'error',
      text: [
        '[/sgme status] SGME Gateway 不可达',
        '',
        'baseUrl: ' + (config.baseUrl ?? '(未知)'),
        'agent key: ' + (config.agentKeySet ? '已配置' : '未配置'),
        'admin key: ' + (config.adminKeySet ? '已配置' : '未配置'),
        '',
        '本插件是桥接插件，依赖 SGME 本体（Python 服务 :9910）才能工作，没有本体是空壳。',
        '安装指引见插件 README「前置条件」：https://github.com/freehul/sgme',
      ].join('\n'),
    }
  }
  const lines = [
    '[/sgme status]',
    '- 连接: 正常' + (health.version ? '（v' + health.version + '）' : ''),
    '- baseUrl: ' + (config.baseUrl ?? '?'),
    '- agent key: ' + (config.agentKeySet ? '已配置' : '未配置'),
    '- LLM: ' + (health.llm?.model ?? '未知') + '（' + (health.llm?.available ? '可用' : '不可用') + '）',
    '- 提炼: 水位 ' + (health.refinement?.watermark_age_sec ?? '?') + 's / 队列 ' + (health.refinement?.queue_depth ?? '?') + (health.refinement?.stalled ? '（停摆!）' : ''),
    '- 记忆向量: ' + (health.vector?.memory_vectors ?? '?'),
  ]
  return { kind: 'success', text: lines.join('\n') }
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
  // 无参数或 status → 连接状态报告（安装后自检：一条命令确认插件是否就绪）
  if (!trimmed || trimmed === 'status') {
    return buildStatusReport(client, config)
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
    // content 兜底：skills 层结果无 content（只有 name/description），取 .length 前必须兜底
    const raw =
      r.content ??
      (r.name ? (r.description ? `${r.name} — ${r.description}` : r.name) : (r.description ?? ''))
    const content = raw.length > 500 ? raw.slice(0, 500) + '…' : raw
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
    description: 'SGME 状态/检索（无参数或 status = 连接自检；<关键词> = 记忆+知识库检索）',
    async handler(invocation) {
      return executeSgmeCommand(client, config, invocation.rawInput)
    },
  })
}
