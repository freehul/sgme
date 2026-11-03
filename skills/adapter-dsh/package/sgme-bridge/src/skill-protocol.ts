/**
 * SGME 技能检索协议注入（渐进式披露入口）。
 *
 * ## 为什么需要
 *
 * SGME 的冷启动包只索引 **1 个**《技能检索协议》，全量技能不预载——协议告诉 agent
 * 「记忆 / 知识 / 专业技能分别走 inject / wiki_search / skill_search→skill_get」。
 * 但 `skill_search` 只是个工具，**工具不会自己触发**：Agent 只有在上下文里看见这张
 * 路由表，才可能在接到开发任务时想起去检索。
 *
 * 2026-11-03 实测：Hermes 适配器的 system prompt 当时只有角色与画像，结果整轮开发
 * 改了 10+ 文件，`coding-discipline` / `git-pr-workflow` 一次都没加载——不是拿不到，
 * 是压根不知道有。本模块把同样的接线补到 dsh 侧。
 *
 * ## 行为
 *
 * - 拉 `GET /v1/skills/coldstart`，取 `index.items[].content` 拼接（≈3.5KB）
 * - 只注入协议，**不注入 manual**（7.4KB，属按需查阅）；`hotset` 设计上恒空
 * - 失败 / 空 / 开关关闭 → 静默跳过，不影响插件加载
 * - 每会话注册**一次**（section 由 dsh 常驻，不在每轮重拉）
 */
import type { SgmeClient } from './sgme-client.js'

/** section 名（dsh systemPrompt.section 的稳定标识，用于热重载时替换）。 */
export const SKILL_PROTOCOL_SECTION = 'sgme-skill-protocol'

/**
 * 排序值。规则段是 -70（用户项目规则最靠前），协议紧随其后——
 * 先有项目规则约束，再有「遇到专业任务去哪取方法」的路由。
 */
export const SKILL_PROTOCOL_ORDER = -65

interface ProtocolCtx {
  systemPrompt: {
    section: (section: { name: string; order: number; text: string }) => () => void
  }
  logger: { info: (msg: string) => void; warn: (msg: string) => void }
}

/**
 * 注册技能检索协议 section。
 *
 * @returns dispose 函数（注销该 section）；失败时返回空操作。
 */
export async function registerSkillProtocolSection(
  ctx: ProtocolCtx,
  client: Pick<SgmeClient, 'skillColdstart'>,
): Promise<() => void> {
  let dispose: (() => void) | null = null

  let data: Awaited<ReturnType<SgmeClient['skillColdstart']>> = null
  try {
    data = await client.skillColdstart()
  } catch (e) {
    ctx.logger.warn(`[dsg-skill-protocol] 拉取冷启动包失败: ${e instanceof Error ? e.message : String(e)}`)
    return () => {}
  }

  const items = data?.index?.items ?? []
  const body = items
    .map((it) => (it.content ?? '').trim())
    .filter((t) => t.length > 0)
    .join('\n\n')

  if (!body) {
    // 服务端未内联正文（老版本）或索引为空——降级为不注入，不影响其余能力
    ctx.logger.info('[dsg-skill-protocol] 协议正文为空，跳过注入')
    return () => {}
  }

  dispose = ctx.systemPrompt.section({
    name: SKILL_PROTOCOL_SECTION,
    order: SKILL_PROTOCOL_ORDER,
    text: '# SGME 技能检索协议（常驻 · 按需取全文）\n\n' + body,
  })
  ctx.logger.info(`[dsg-skill-protocol] 已注入协议 ${body.length} 字`)

  return () => {
    dispose?.()
    dispose = null
  }
}
