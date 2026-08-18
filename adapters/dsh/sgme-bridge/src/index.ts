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
import { registerRulesSection, defaultRulesPath } from './rules.js'
import { SgmeEventSubscriber } from './events.js'

export const name = 'dsh-sgme'

// 依赖声明：dsh 的工具/命令/事件能力
export const inject = ['tools', 'commands', 'systemPrompt']

// Cordis Context 类型（对齐 dsh-commands / dsh-tools 官方 register 签名，2026-08-14 T-53 确认）
interface CordisContext {
  logger: (name: string) => { info: (msg: string) => void; warn: (msg: string) => void }
  effect: (fn: () => () => void, label?: string) => void
  on: (event: string, handler: (...args: any[]) => any) => () => void
  systemPrompt: {
    section: (section: {
      name: string
      order: number
      text: string | ((context: Record<string, unknown>) => string)
    }) => () => void
  }
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
  projectHint?: string             // 项目名提示（用于相关记忆检索，可空）
  rulesPath?: string               // DSH 用户级规则文件（缺省 ~/.dsh/dsg-rules/rules.md）
  syncOnTurnEnd: boolean
  turnBatchSize: number
  evolveEnabled?: boolean          // W4 自进化自动触发（默认 true）
  evolveMinRounds?: number         // 费用门禁：会话消息块下限（默认 5）
  eventSubscribe?: boolean          // 2026-08-18：SGME 事件流订阅（SSE 长连，默认 true）
}

export const Config: Schema<Config> = Schema.object({
  baseUrl: Schema.string().default('http://localhost:9910').description('SGME Gateway 地址（本机部署默认 localhost；SGME 在其他机器请改成对应 IP）'),
  agentKey: Schema.string().default('').description('SGME agent key（/v1/admin/agents/register 签发）'),
  adminKey: Schema.string().default('').description('SGME admin key（触发提炼用）'),
  agentId: Schema.string().default('dsh').description('SGME agent id'),
  injectMode: Schema.union(['daily', 'full', 'coding', 'work']).default('daily').description('画像注入模式'),
  injectMaxTokens: Schema.number().default(800).description('画像注入 token 上限'),
  searchLimit: Schema.number().default(5).description('检索返回条数上限'),
  projectHint: Schema.string().default('').description('项目名提示（用于相关记忆检索，可空；缺省按会话 cwd 目录名推断）'),
  rulesPath: Schema.string().default('').description('DSH 用户级规则文件（缺省 ~/.dsh/dsg-rules/rules.md，注册为 dsg:rules system section）'),
  syncOnTurnEnd: Schema.boolean().default(true).description('是否在 turn/end 时同步入库'),
  turnBatchSize: Schema.number().default(1).description('入库攒批大小（v1=1 即每 turn 即 append）'),
  evolveEnabled: Schema.boolean().default(true).description('自进化自动触发（W4：turn/end 后调 /v1/wiki/evolve/trigger，evolve 侧幂等+费用门禁兜底）'),
  evolveMinRounds: Schema.number().default(5).description('自进化费用门禁：会话消息块下限'),
  eventSubscribe: Schema.boolean().default(true).description('SGME 事件流订阅（SSE 长连，实时接收 care_*/anomaly_warn，注入提醒）'),
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

  // 2.5. SGME 事件流订阅（2026-08-18 SSE 长连）：实时接收 care_*/anomaly_warn，
  // 缓存到本地队列，context 注入时提醒 agent 调 signal_pull 消费
  // 2026-08-18 修复：订阅器创建提前到工具注册之前——signal_claim/signal_ack 需
  // 同步 markConsumed 本地队列（兜底铁律：防「提醒反复注入但 pull 为空」死循环）
  const eventSubscriber = config.eventSubscribe !== false
    ? new SgmeEventSubscriber({
        baseUrl: config.baseUrl,
        agentKey: config.agentKey,
        agentId: config.agentId,
      })
    : null
  if (eventSubscriber) {
    eventSubscriber.start()
    ctx.effect(() => () => {
      eventSubscriber.stop()
    }, 'sgme-event-subscribe')
    logger.info(`SGME 事件订阅已启动（SSE: ${config.baseUrl}/v1/events/stream）`)
  }

  // 1. 注册工具（检索 + 信号 + 三池 + 角色 + 记忆纠错）
  const toolsCtx = { tools: ctx.tools }
  registerTools(toolsCtx, client, config.searchLimit, eventSubscriber)
  logger.info('工具已注册：memory_search, wiki_search, wiki_pages, wiki_page, wiki_page_update, wiki_page_add, signal_*, idea_add, demand_create, project_register, role_*, memory_get/reject')

  // 2. 画像首步注入（turn/start 拦截，对齐 dsh-agent-instructions 的 session/event 用法）
  const contextCtx = {
    on: ctx.on,
    logger: { info: logger.info, warn: logger.warn },
  }
  const projectHint = config.projectHint
    || (process.env.SGME_PROJECT_HINT ?? '')

  const disposeContext = registerContextInjection(contextCtx, client, {
    injectMode: config.injectMode,
    injectMaxTokens: config.injectMaxTokens,
    searchLimit: config.searchLimit,
    eventSubscriber,
    ...(projectHint ? { projectHint } : {}),
  })
  ctx.effect(() => disposeContext, 'sgme-context-injection')

  // 3. /sgme 命令（status 自检 + 检索）
  const commandsCtx = { commands: ctx.commands }
  registerSgmeCommand(commandsCtx, client, {
    searchLimit: config.searchLimit,
    baseUrl: config.baseUrl,
    agentKeySet: !!config.agentKey,
    adminKeySet: !!config.adminKey,
  })
  logger.info('命令已注册：/sgme（status 自检 + 检索）')

  // 4. turn/end 会话入库
  const syncCtx = {
    on: ctx.on,
    logger: { info: logger.info, warn: logger.warn },
  }
  const disposeSync = registerSessionSync(syncCtx, client, {
    agentId: config.agentId,
    syncOnTurnEnd: config.syncOnTurnEnd,
    turnBatchSize: config.turnBatchSize,
    ...(config.evolveEnabled !== undefined ? { evolveEnabled: config.evolveEnabled } : {}),
    ...(config.evolveMinRounds !== undefined ? { evolveMinRounds: config.evolveMinRounds } : {}),
  })
  ctx.effect(() => disposeSync, 'sgme-session-sync')

  // 5. DSH 用户级规则（dsg:rules system section，order -70）
  // 读取 ~/.dsh/dsg-rules/rules.md → 注册为稳定 section（前缀缓存命中）
  // apply 是同步的：async 注册 fire-and-forget，dispose 经闭包在完成时接回
  const rulesPath = config.rulesPath || defaultRulesPath()
  void registerRulesSection(
    { systemPrompt: ctx.systemPrompt, logger: { info: logger.info, warn: logger.warn } },
    rulesPath,
  ).then((disposeRules) => {
    ctx.effect(() => disposeRules, 'sgme-rules-section')
  }).catch((e) => {
    const msg = e instanceof Error ? e.message : String(e)
    logger.warn(`[dsg-rules] 注册失败: ${msg}`)
  })

  // 6. 启动连接探测（fire-and-forget，不阻塞插件加载；不可达给出本体安装指引）
  // 解决「只装插件没装本体 = 空壳」的困惑：启动即明确提示，而非调用时才报错
  void client.health().then((h) => {
    if (h) {
      logger.info(
        'SGME 连接正常: v' + (h.version ?? '?') + ' llm=' + (h.llm?.model ?? '?') + ' 记忆向量=' + (h.vector?.memory_vectors ?? '?'),
      )
    } else {
      logger.warn(
        '[dsh-sgme] SGME Gateway 不可达（baseUrl=' + config.baseUrl + '）——'
        + '本插件是桥接插件，依赖 SGME 本体（Python 服务 :9910），没有本体是空壳。'
        + '安装指引见 README 前置条件：https://github.com/freehul/sgme',
      )
    }
  }).catch((e) => {
    const msg = e instanceof Error ? e.message : String(e)
    logger.warn('[dsh-sgme] 启动连接探测异常: ' + msg)
  })

  logger.info('SGME bridge 全部能力已注册（画像注入 + 工具 + 命令 + 会话同步 + dsg-rules + 连接探测）')
}
