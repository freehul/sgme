/**
 * rules.ts 测试 — dsg:rules system section 注册。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { registerRulesSection, defaultRulesPath, readRulesText } from '../src/rules.js'
import { writeFile, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

let tmpDir: string

beforeEach(async () => {
  tmpDir = join(tmpdir(), 'dsg-rules-test-' + Date.now())
  await mkdir(tmpDir, { recursive: true })
})

async function cleanup() {
  await rm(tmpDir, { recursive: true, force: true })
}

function makeCtx() {
  const sections: Array<{ name: string; order: number; text: unknown }> = []
  const disposers: Array<() => void> = []
  const ctx = {
    systemPrompt: {
      section: (s: { name: string; order: number; text: unknown }) => {
        sections.push(s)
        const d = () => {
          const i = sections.indexOf(s)
          if (i > -1) sections.splice(i, 1)
        }
        disposers.push(d)
        return d
      },
    },
    logger: { info: vi.fn(), warn: vi.fn() },
  }
  return { ctx, sections, disposers }
}

describe('registerRulesSection', () => {
  it('规则文件存在时注册 dsg:rules section（order -70）', async () => {
    const { ctx, sections } = makeCtx()
    const rulesPath = join(tmpDir, 'rules.md')
    await writeFile(rulesPath, '# 测试规则\n\n## 铁律\n- 原件不删', 'utf8')
    const dispose = await registerRulesSection(ctx, rulesPath)
    expect(sections.length).toBe(1)
    const s = sections[0]!
    expect(s.name).toBe('dsg:rules')
    expect(s.order).toBe(-70)
    expect(s.text).toContain('测试规则')
    dispose()
    expect(sections.length).toBe(0)
    await cleanup()
  })

  it('文件不存在时静默跳过（不抛错、不注册）', async () => {
    const { ctx, sections, disposers } = makeCtx()
    const rulesPath = join(tmpDir, 'missing.md')
    const dispose = await registerRulesSection(ctx, rulesPath)
    expect(sections.length).toBe(0)
    expect(disposers.length).toBe(0)
    dispose()
    await cleanup()
  })

  it('空文件不注册', async () => {
    const { ctx, sections } = makeCtx()
    const rulesPath = join(tmpDir, 'rules.md')
    await writeFile(rulesPath, '   \n\n  ', 'utf8')
    await registerRulesSection(ctx, rulesPath)
    expect(sections.length).toBe(0)
    await cleanup()
  })

  it('读取失败（权限等）时 warn 且不注册', async () => {
    const { ctx, sections } = makeCtx()
    const rulesPath = join(tmpDir, 'rules.md')
    // 用目录路径模拟读取失败（EISDIR）
    await mkdir(rulesPath, { recursive: true })
    await registerRulesSection(ctx, rulesPath)
    expect(sections.length).toBe(0)
    await cleanup()
  })
})

describe('defaultRulesPath / readRulesText', () => {
  it('默认路径为 ~/.dsh/dsg-rules/rules.md', () => {
    const p = defaultRulesPath('C:/fake/dsh')
    expect(p).toContain('dsg-rules')
    expect(p).toContain('rules.md')
  })

  it('readRulesText 读取文件内容', async () => {
    const rulesPath = join(tmpDir, 'rules.md')
    await writeFile(rulesPath, '内容X', 'utf8')
    expect(await readRulesText(rulesPath)).toBe('内容X')
    await cleanup()
  })

  it('readRulesText 文件缺失返回 null', async () => {
    expect(await readRulesText(join(tmpDir, 'nope.md'))).toBeNull()
    await cleanup()
  })
})
