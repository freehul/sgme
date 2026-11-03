/**
 * skill-protocol.ts 测试 — SGME 技能检索协议注入。
 *
 * 背景：协议是渐进式披露的**入口**。不注入时 system prompt 里只有画像，
 * agent 不知道 SGME 有技能库可搜，「开发前加载编码纪律」这类规则从未被触发
 * （2026-11-03 实测）。本文件守住四条：注入 / 不注 manual / 空正文降级 / 失败不炸。
 */
import { describe, it, expect } from 'vitest'
import {
  registerSkillProtocolSection,
  SKILL_PROTOCOL_SECTION,
  SKILL_PROTOCOL_ORDER,
} from '../src/skill-protocol.js'

interface Section {
  name: string
  order: number
  text: string
}

function makeCtx() {
  const sections: Section[] = []
  const ctx = {
    systemPrompt: {
      section: (s: Section) => {
        sections.push(s)
        return () => {
          const i = sections.indexOf(s)
          if (i > -1) sections.splice(i, 1)
        }
      },
    },
    logger: { info: () => {}, warn: () => {} },
  }
  return { ctx, sections }
}

/** 造一个只实现 skillColdstart 的假 client。 */
function makeClient(result: unknown) {
  return {
    skillColdstart: async () => result as never,
  }
}

const PROTOCOL_TEXT = '## 核心原则：SGME 是能力平面\n\n| 任务信号 | 目标模块 |\n|---|---|\n| 框架、工具链 | skills |'

function coldstart(protocolContent: string) {
  return {
    index: {
      items: [{ name: 'skill-registry-protocol', title: '技能检索协议', content: protocolContent }],
      total: 1,
    },
    hotset: [],
    manual: { page_id: 'p1', title: 'SGME操作手册', content: 'X'.repeat(7400) },
  }
}

describe('registerSkillProtocolSection', () => {
  it('把协议正文注入 systemPrompt section（含标题与 order）', async () => {
    const { ctx, sections } = makeCtx()
    await registerSkillProtocolSection(ctx, makeClient(coldstart(PROTOCOL_TEXT)) as never)

    expect(sections).toHaveLength(1)
    expect(sections[0].name).toBe(SKILL_PROTOCOL_SECTION)
    expect(sections[0].order).toBe(SKILL_PROTOCOL_ORDER)
    expect(sections[0].text).toContain('技能检索协议')
    expect(sections[0].text).toContain('框架、工具链')   // 路由表被拼进来
  })

  it('不注入 manual（7.4KB 属按需查阅，不能进常驻前缀）', async () => {
    const { ctx, sections } = makeCtx()
    await registerSkillProtocolSection(ctx, makeClient(coldstart(PROTOCOL_TEXT)) as never)

    expect(sections[0].text).not.toContain('SGME操作手册')
    expect(sections[0].text).not.toContain('XXXX')
  })

  it('协议正文为空时降级为不注入（老服务端未内联正文）', async () => {
    const { ctx, sections } = makeCtx()
    await registerSkillProtocolSection(ctx, makeClient(coldstart('')) as never)
    expect(sections).toHaveLength(0)
  })

  it('冷启动包为 null 时不注入、不抛异常', async () => {
    const { ctx, sections } = makeCtx()
    await expect(
      registerSkillProtocolSection(ctx, makeClient(null) as never),
    ).resolves.toBeTypeOf('function')
    expect(sections).toHaveLength(0)
  })

  it('client 抛异常时吞掉（协议失败不得影响插件加载）', async () => {
    const { ctx, sections } = makeCtx()
    const bad = {
      skillColdstart: async () => {
        throw new Error('ECONNREFUSED')
      },
    }
    await expect(registerSkillProtocolSection(ctx, bad as never)).resolves.toBeTypeOf('function')
    expect(sections).toHaveLength(0)
  })

  it('dispose 能注销 section（热重载不留重复段）', async () => {
    const { ctx, sections } = makeCtx()
    const dispose = await registerSkillProtocolSection(ctx, makeClient(coldstart(PROTOCOL_TEXT)) as never)
    expect(sections).toHaveLength(1)
    dispose()
    expect(sections).toHaveLength(0)
  })

  it('order 排在用户规则段（-70）之后，不抢占项目规则的位置', () => {
    expect(SKILL_PROTOCOL_ORDER).toBeGreaterThan(-70)
  })
})
