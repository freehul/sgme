/**
 * 占位测试（确保 vitest 有测试文件可跑，T-52 补全真实测试）。
 */
import { describe, it, expect } from 'vitest'
import { name, inject, Config, apply } from '../src/index.js'

describe('plugin skeleton', () => {
  it('exports correct name', () => {
    expect(name).toBe('dsh-sgme')
  })

  it('declares dependencies', () => {
    expect(inject).toContain('tools')
    expect(inject).toContain('commands')
  })

  it('Config has required fields', () => {
    expect(Config).toBeDefined()
  })

  it('apply is a function', () => {
    expect(typeof apply).toBe('function')
  })

  it('apply does not throw with mock context', () => {
    const mockCtx = {
      logger: () => ({ info: () => {}, warn: () => {} }),
      effect: () => {},
      on: () => () => {},
      tools: { register: () => {} },
      commands: { register: () => ({ action: () => {} }) },
    }
    const config = {
      baseUrl: 'http://127.0.0.1:9910',
      agentKey: 'test',
      adminKey: '',
      agentId: 'dsh',
      injectMode: 'daily' as const,
      injectMaxTokens: 800,
      searchLimit: 5,
      syncOnTurnEnd: true,
      turnBatchSize: 1,
    }
    expect(() => apply(mockCtx as any, config)).not.toThrow()
  })
})
