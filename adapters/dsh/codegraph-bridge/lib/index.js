/**
 * dsh-codegraph — CodeGraph (colbymchenry/codegraph) × DeepSeek Harness 桥接插件
 *
 * 把本地代码知识图谱 CLI（codegraph，Rust 内核 + SQLite）暴露为 dsh 工具：
 *   - codegraph_explore：一次调用返回相关符号原文源码 + 调用路径 + 影响范围（主工具）
 *   - codegraph_query   ：按名称搜索符号（JSON 解析后输出精简结果）
 *   - codegraph_node    ：单个符号源码 + 调用者/被调用者轨迹，或按文件读取
 *   - codegraph_status  ：索引状态与统计
 *
 * 运行时零 Python 依赖：全部通过子进程调用 codegraph CLI（npm-shim.js）。
 * 项目目录需已执行过 `codegraph init`（自动同步已开启，改代码即增量更新）。
 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import fs from 'node:fs'
import path from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'
import Schema from 'schemastery'

const execFileAsync = promisify(execFile)

export const name = 'dsh-codegraph'

// 依赖声明：dsh 的工具注册能力
export const inject = ['tools']

/**
 * 解析 codegraph npm-shim.js 默认路径（通用，不写死单机路径）：
 *   1) 环境变量 DSH_CODEGRAPH_BIN 显式覆盖（最优先）
 *   2) Windows 用户级 npm 全局目录（%APPDATA%/npm/node_modules）
 *   3) 类 Unix 用户级 npm 全局目录（~/.npm-global/lib/node_modules）
 *   4) 兜底：PATH 中的 `codegraph` 命令名
 */
function resolveDefaultBin() {
  if (process.env.DSH_CODEGRAPH_BIN) return process.env.DSH_CODEGRAPH_BIN
  const candidates = []
  if (process.platform === 'win32' && process.env.APPDATA) {
    candidates.push(path.join(process.env.APPDATA, 'npm', 'node_modules', '@colbymchenry', 'codegraph', 'npm-shim.js'))
  }
  if (process.env.HOME) {
    candidates.push(path.join(process.env.HOME, '.npm-global', 'lib', 'node_modules', '@colbymchenry', 'codegraph', 'npm-shim.js'))
  }
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c } catch { /* ignore */ }
  }
  return 'codegraph'
}

const DEFAULT_BIN = resolveDefaultBin()

/** 插件配置（由 cordis.patch.yml 的 config 注入）。 */
export const Config = Schema.object({
  bin: Schema.string().default(DEFAULT_BIN).description('codegraph npm-shim.js 绝对路径'),
  projectPath: Schema.string().default(process.cwd()).description('默认项目目录（跟随 dsh 启动目录，需已 codegraph init；工具参数 path 可覆盖）'),
  queryLimit: Schema.number().default(10).description('codegraph_query 默认返回条数'),
})

/**
 * 插件入口（Cordis apply）。
 * 注册 4 个 codegraph 工具；所有 CLI 调用失败只返回错误文本，绝不抛异常阻塞 dsh。
 */
export function apply(ctx, config) {
  const logger = ctx.logger('codegraph-bridge')

  const runCli = async (args, opts = {}) => {
    const { path, timeoutMs = 180000 } = opts
    try {
      const { stdout } = await execFileAsync(process.execPath, [config.bin, '--no-color', ...args], {
        cwd: path || config.projectPath,
        maxBuffer: 32 * 1024 * 1024,
        windowsHide: true,
        timeout: timeoutMs,
        encoding: 'utf8',
      })
      return stdout
    } catch (err) {
      const stderr = err && err.stderr ? String(err.stderr) : ''
      const msg = err && err.message ? String(err.message) : String(err)
      return '[codegraph CLI 调用失败] ' + (stderr || msg)
    }
  }

  const textOutput = {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: String(value) }],
  }

  ctx.tools.register(defineTool({
    name: 'codegraph_explore',
    description: [
      '探索代码库某个区域：一次调用返回相关符号的原文源码（带行号）+ 调用路径 + 影响范围（等价 codegraph_explore MCP 工具）。',
      '适合"X 怎么工作的 / 架构 / 调用链 / 改这里影响谁"类问题；返回的源码视为已读，无需再用文件工具重复读取。',
    ].join(' '),
    parameters: {
      query: { type: 'string', required: true, description: '自然语言任务/问题描述，或符号名/文件名集合' },
      path: { type: 'string', description: '项目目录（默认取插件配置 projectPath）' },
      maxFiles: { type: 'number', description: '返回源码的最大文件数（默认不限）' },
    },
    output: textOutput,
    async execute(args) {
      const a = args || {}
      const cliArgs = ['explore', String(a.query)]
      if (a.maxFiles) cliArgs.push('--max-files', String(a.maxFiles))
      return runCli(cliArgs, { path: a.path })
    },
  }))

  ctx.tools.register(defineTool({
    name: 'codegraph_query',
    description: '按名称在代码库中搜索符号（函数/类/方法/接口等），返回位置、签名与 docstring。适合"X 在哪定义"类问题。',
    parameters: {
      search: { type: 'string', required: true, description: '符号名或名称片段' },
      path: { type: 'string', description: '项目目录（默认取插件配置 projectPath）' },
      kind: { type: 'string', description: '按类型过滤（function/class/method/interface/type/variable/route/component 等）' },
      limit: { type: 'number', description: '最大结果数（默认取插件配置 queryLimit）' },
    },
    output: textOutput,
    async execute(args) {
      const a = args || {}
      const cliArgs = ['query', String(a.search), '-j', '--limit', String(a.limit || config.queryLimit)]
      if (a.kind) cliArgs.push('--kind', String(a.kind))
      const raw = await runCli(cliArgs, { path: a.path })
      try {
        const rows = JSON.parse(raw)
        if (!Array.isArray(rows) || rows.length === 0) {
          return '[codegraph_query 无结果：' + a.search + ']'
        }
        return rows.map((r) => {
          const n = r.node || {}
          return (n.kind || 'symbol') + '  ' + (n.name || '?') +
            '\n  ' + (n.filePath || '?') + ':' + (n.startLine ?? '?') +
            (n.signature ? '\n  ' + n.signature : '')
        }).join('\n\n')
      } catch {
        return raw // CLI 返回的不是 JSON（错误信息等）
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'codegraph_node',
    description: '单个符号的详情：源码 + 调用者/被调用者轨迹；或按文件读取带行号源码（等价 codegraph_node MCP 工具）。',
    parameters: {
      symbol: { type: 'string', description: '符号名（不传则按文件模式）' },
      path: { type: 'string', description: '项目目录（默认取插件配置 projectPath）' },
      file: { type: 'string', description: '文件模式：读取该文件（配合或替代 symbol）' },
      offset: { type: 'number', description: '文件模式：起始行（1 基）' },
      limit: { type: 'number', description: '文件模式：最大行数' },
    },
    output: textOutput,
    async execute(args) {
      const a = args || {}
      const cliArgs = ['node']
      if (a.symbol) cliArgs.push(String(a.symbol))
      if (a.file) cliArgs.push('--file', String(a.file))
      if (a.offset) cliArgs.push('--offset', String(a.offset))
      if (a.limit) cliArgs.push('--limit', String(a.limit))
      return runCli(cliArgs, { path: a.path })
    },
  }))

  ctx.tools.register(defineTool({
    name: 'codegraph_status',
    description: '查看当前项目的 codegraph 索引状态与统计（文件数/节点数/边数/数据库大小）。',
    parameters: {
      path: { type: 'string', description: '项目目录（默认取插件配置 projectPath；也可作为位置参数）' },
    },
    output: textOutput,
    async execute(args) {
      const a = args || {}
      const raw = await runCli(['status', '-j'], { path: a.path })
      try {
        const obj = JSON.parse(raw)
        // CLI v1.5 status -j 字段：projectPath/fileCount/nodeCount/edgeCount/dbSizeBytes
        // （旧插件误用 project/files/nodes/edges/dbSize 导致全部显示 ?，2026-08-16 修复）
        const langs = obj.languages ? obj.languages.join(', ') : '?'
        const indexState = obj.index ? (obj.index.state || '?') : '?'
        const byKind = obj.nodesByKind
          ? Object.entries(obj.nodesByKind).map(([k, v]) => k + '=' + v).join(' ')
          : ''
        return '项目: ' + (obj.projectPath || '?') +
          '\n文件: ' + (obj.fileCount ?? '?') +
          '\n节点: ' + (obj.nodeCount ?? '?') +
          '\n边: ' + (obj.edgeCount ?? '?') +
          '\n数据库: ' + (obj.dbSizeBytes || '?') +
          '\n版本: ' + (obj.version || '?') +
          '\n索引状态: ' + indexState +
          '\n语言: ' + langs +
          (byKind ? '\n按类型: ' + byKind : '')
      } catch {
        return raw
      }
    },
  }))

  logger.info('codegraph bridge loaded: project=' + config.projectPath)
}