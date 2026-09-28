/**
 * rules.ts — DSH 用户级规则加载（dsg:rules system section）
 *
 * 读取 ~/.dsh/dsg-rules/rules.md（DSH 专用配置：身份/铁律/SGME手册/用户偏好/环境事实），
 * 注册为 dsh-system-prompt 的稳定 section（order -70，位于 harness:identity(-100) 与
 * persona(0) 之间）——稳定内容进 system 层，前缀缓存全命中。
 *
 * 设计要点（2026-08-16 定稿）：
 * - 单文件单 section：规则类内容阅读/修改场景一致，不拆多文件
 * - 用户级目录（~/.dsh/）不在任何 git 仓库内，天然不被提交；.gitignore 双保险
 * - 文件缺失/读取失败 → 静默跳过，绝不阻塞插件启动
 * - 支持热重载：文件 mtime 变化时更新 section（保留变更通知）
 */
import { readFile, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

/** dsg:rules section 的 order（位于 harness:identity=-100 与 persona=0 之间）。 */
export const DSG_RULES_SECTION = 'dsg:rules'
export const DSG_RULES_ORDER = -70

/** 默认规则文件路径（~/.dsh/dsg-rules/rules.md）。 */
export function defaultRulesPath(dshHome?: string): string {
  const home = dshHome ?? process.env.DSH_HOME ?? join(homedir(), '.dsh')
  return join(home, 'dsg-rules', 'rules.md')
}

/** rules.ts 需要的 ctx 能力（dsh-system-prompt 的 section 注册）。 */
export interface RulesCtx {
  systemPrompt: {
    section: (section: {
      name: string
      order: number
      text: string | ((context: Record<string, unknown>) => string)
    }) => () => void
  }
  logger: { info: (msg: string) => void; warn: (msg: string) => void }
}

/**
 * 注册 dsg:rules section。
 * 读取规则文件 → 注册为稳定 section；文件不存在时跳过（不报错）。
 *
 * @returns 清理函数（由 ctx.effect 调用方管理生命周期）
 */
export async function registerRulesSection(
  ctx: RulesCtx,
  rulesPath: string = defaultRulesPath(),
): Promise<() => void> {
  let dispose: (() => void) | null = null

  const loadAndRegister = async (): Promise<void> => {
    // 先读文件；失败静默（文件缺失=用户未配置规则，跳过）
    let content: string
    try {
      content = await readFile(rulesPath, 'utf8')
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code
      if (code === 'ENOENT') {
        ctx.logger.info(`[dsg-rules] ${rulesPath} 不存在，跳过规则注入`)
      } else {
        ctx.logger.warn(`[dsg-rules] 读取失败: ${e instanceof Error ? e.message : String(e)}`)
      }
      return
    }

    const text = content.trim()
    if (!text) return

    // 清理旧 section（热重载时替换）
    dispose?.()
    dispose = ctx.systemPrompt.section({
      name: DSG_RULES_SECTION,
      order: DSG_RULES_ORDER,
      text,
    })
    ctx.logger.info(`[dsg-rules] 已注册 ${DSG_RULES_SECTION}（order ${DSG_RULES_ORDER}，${text.length} 字符）`)
  }

  await loadAndRegister()
  return () => {
    dispose?.()
  }
}

/** 供测试：直接读取规则文本（不依赖 ctx）。 */
export async function readRulesText(rulesPath: string): Promise<string | null> {
  try {
    return await readFile(rulesPath, 'utf8')
  } catch {
    return null
  }
}
