/**
 * dsh-sgme — SGME 记忆引擎 × DeepSeek Harness 桥接插件
 *
 * 把 SGME 的多 Agent 共享长期记忆能力接入 dsh：
 * - 画像 + 相关记忆首步注入（agent/pre-step 拦截）
 * - memory_search / wiki_search 工具
 * - /sgme 综合检索命令
 * - 每轮对话结束自动入库（session/event turn/end → /v1/append + 触发提炼）
 *
 * 运行时零 Python 依赖，全部通过 HTTP 调 SGME Gateway。
 *
 * 契约来源：sgme/server/routes_memory.py / routes_admin.py（2026-08-14 调研确认）
 */
import Schema from 'schemastery'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { SgmeClient } from './sgme-client.js'
import { registerTools } from './tools.js'
import { registerContextInjection } from './context.js'
import type { InjectMode } from './context.js'
import { registerSgmeCommand } from './commands.js'
import type { CommandResult, CommandInvocation } from './commands.js'
import { registerSessionSync } from './session-sync.js'

export const name = 'dsh-sgme'

// 依赖声明：dsh 的工具/命令/事件能力
export const inject = ['tools', 'commands']

// Cordis Context 类型（对齐 dsh-commands / dsh-tools 官方 register 签名，2026-08-14 T-53 确认）
interface CordisContext {
  logger: (name: string) => { info: (msg: string) => void; warn: (msg: string) => void }
  effect: (fn: () => () => void, label?: string) => void
  on: (event: string, handler: (...args: unknown[]) => void) => () => void
  tools: { register: (tool: ToolDefinition) => () => void }
  commands: {
    register: (definition: {
      name: string
      description: string
      handler: (invocation: CommandInvocation) => Promise<CommandResult>
    }) => () => void
  }
}

/** 插件配置（由 cordis.yml 的 config 注入，install.py 写入 .env 后 dsh 加载）。 */
export interface Config {
  baseUrl: string
  agentKey: string
  adminKey: string
  agentId: string
  injectMode: InjectMode
  injectMaxTokens: number
  searchLimit: number
  syncOnTurnEnd: boolean
  turnBatchSize: number
}

export const Config: Schema<Config> = Schema.object({
  baseUrl: Schema.string().default('http://192.168.10.10:9910').description('SGME Gateway 地址'),
  agentKey: Schema.string().default('').description('SGME agent key（/v1/admin/agents/register 签发）'),
  adminKey: Schema.string().default('').description('SGME admin key（触发提炼用）'),
  agentId: Schema.string().default('dsh').description('SGME agent id'),
  injectMode: Schema.union(['daily', 'full', 'coding', 'work']).default('daily').description('画像注入模式'),
  injectMaxTokens: Schema.number().default(800).description('画像注入 token 上限'),
  searchLimit: Schema.number().default(5).description('检索返回条数上限'),
  syncOnTurnEnd: Schema.boolean().default(true).description('是否在 turn/end 时同步入库'),
  turnBatchSize: Schema.number().default(1).description('入库攒批大小（v1=1 即每 turn 即 append）'),
})

/**
 * 插件入口（Cordis apply）。
 *
 * 拼装 5 类能力：
 * 1. sgmeClient — HTTP 客户端（其他能力共享）
 * 2. tools — memory_search + wiki_search 工具注册
 * 3. context — 画像首步注入（agent/pre-step 拦截）
 * 4. commands — /sgme 综合检索命令
 * 5. sessionSync — turn/end 会话入库
 *
 * 故障隔离：所有 SGME 调用失败只 log，绝不阻塞 dsh 主循环。
 */
export function apply(ctx: CordisContext, config: Config): void {
  const logger = ctx.logger('sgme-bridge')
  logger.info(
    `SGME bridge loaded: baseUrl=${config.baseUrl} agentId=${config.agentId} mode=${config.injectMode}`,
  )

  // 0. 创建共享 HTTP 客户端
  const client = new SgmeClient({
    baseUrl: config.baseUrl,
    agentKey: config.agentKey,
    adminKey: config.adminKey,
    agentId: config.agentId,
  })

  // 1. 注册工具（memory_search + wiki_search）
  const toolsCtx = { tools: ctx.tools }
  registerTools(toolsCtx, client, config.searchLimit)
  logger.info('工具已注册：memory_search, wiki_search')

  // 2. 画像首步注入（turn/start 拦截，对齐 dsh-agent-instructions 的 session/event 用法）
  const contextCtx = {
    on: ctx.on,
    logger: { info: logger.info, warn: logger.warn },
  }
  const disposeContext = registerContextInjection(contextCtx, client, {
    injectMode: config.injectMode,
    injectMaxTokens: config.injectMaxTokens,
    searchLimit: config.searchLimit,
  })
  ctx.effect(() => disposeContext, 'sgme-context-injection')

  // 3. /sgme 命令
  const commandsCtx = { commands: ctx.commands }
  registerSgmeCommand(commandsCtx, client, { searchLimit: config.searchLimit })
  logger.info('命令已注册：/sgme')

  // 4. turn/end 会话入库
  const syncCtx = {
    on: ctx.on,
    logger: { info: logger.info, warn: logger.warn },
  }
  const disposeSync = registerSessionSync(syncCtx, client, {
    agentId: config.agentId,
    syncOnTurnEnd: config.syncOnTurnEnd,
    turnBatchSize: config.turnBatchSize,
  })
  ctx.effect(() => disposeSync, 'sgme-session-sync')

  logger.info('SGME bridge 全部能力已注册（画像注入 + 工具 + 命令 + 会话同步）')
}
