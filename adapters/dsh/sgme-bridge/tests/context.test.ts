/**
 * context.ts 测试 — buildInjectionText 画像注入文本拼接。
 *
 * 测试纯函数 buildInjectionText（不需要 mock HTTP）。
 */
import { describe, it, expect } from 'vitest'
import { buildInjectionText } from '../src/context.js'
import type { InjectResponse } from '../src/sgme-client.js'

// ---------- 工具函数 ----------

function makeProfile(overrides: Partial<InjectResponse> = {}): InjectResponse {
  return {
    blocks: [],
    stats: { mode: 'daily', queries: 0, tokens_est: 0, tier0_present: false },
    tier0: { present: false, content: null },
    ...overrides,
  }
}

// ---------- buildInjectionText ----------

describe('buildInjectionText', () => {
  it('profile 为 null 且 related 为 null 时返回空串', () => {
    expect(buildInjectionText(null, null)).toBe('')
  })

  it('profile 为空 blocks 但有 related 时只返回相关记忆', () => {
    const result = buildInjectionText(
      makeProfile({ blocks: [] }),
      { results: [{ rank: 1, content: '相关记忆1' }] },
    )
    expect(result).toContain('相关记忆')
    expect(result).toContain('1. 相关记忆1')
    expect(result).not.toContain('SGME 用户画像')
  })

  it('profile 有 Tier0 摘要时包含摘要内容', () => {
    const profile = makeProfile({
      tier0: { present: true, content: '这是 Tier0 摘要' },
    })
    const result = buildInjectionText(profile, null)
    expect(result).toContain('Tier0 摘要')
    expect(result).toContain('这是 Tier0 摘要')
  })

  it('profile 有 blocks 时包含各维度区块', () => {
    const profile = makeProfile({
      blocks: [
        { title: 'identity', items: [{ content: '用户名：张三' }] },
        { title: 'projects', items: [{ content: '项目A' }, { content: '项目B' }] },
      ],
    })
    const result = buildInjectionText(profile, null)
    expect(result).toContain('[identity]')
    expect(result).toContain('用户名：张三')
    expect(result).toContain('[projects]')
    expect(result).toContain('项目A')
    expect(result).toContain('项目B')
  })

  it('空 items 的 block 被跳过', () => {
    const profile = makeProfile({
      blocks: [
        { title: 'empty', items: [] },
        { title: 'identity', items: [{ content: '有内容' }] },
      ],
    })
    const result = buildInjectionText(profile, null)
    expect(result).not.toContain('[empty]')
    expect(result).toContain('[identity]')
  })

  it('超长记忆内容被截断', () => {
    const longContent = 'X'.repeat(300)
    const profile = makeProfile({
      blocks: [{ title: 'test', items: [{ content: longContent }] }],
    })
    const result = buildInjectionText(profile, null)
    expect(result).toContain('…')
    // 200 字符截断 + … 前缀
    expect(result).not.toContain('X'.repeat(300))
  })

  it('同时有 profile 和 related 时两者都包含', () => {
    const profile = makeProfile({
      blocks: [{ title: 'identity', items: [{ content: '画像内容' }] }],
    })
    const result = buildInjectionText(
      profile,
      { results: [{ rank: 1, content: '相关记忆' }] },
    )
    expect(result).toContain('SGME 用户画像')
    expect(result).toContain('画像内容')
    expect(result).toContain('相关记忆')
  })

  it('包含注入引导语', () => {
    const profile = makeProfile({
      blocks: [{ title: 'identity', items: [{ content: '内容' }] }],
    })
    const result = buildInjectionText(profile, null)
    expect(result).toContain('可直接引用')
  })

  it('related 记忆内容被截断', () => {
    const longContent = 'Y'.repeat(300)
    const result = buildInjectionText(
      null,
      { results: [{ rank: 1, content: longContent }] },
    )
    expect(result).toContain('…')
    expect(result).not.toContain('Y'.repeat(300))
  })
})
