/**
 * commands.ts 测试 — executeSgmeCommand 检索命令执行。
 *
 * mock SgmeClient.search，验证空查询/有结果/无结果/Gateway 不可达 4 种场景。
 * 断言对齐 dsh-commands CommandResult { kind, text }（2026-08-14 T-53 修复后）。
 */
import { describe, it, expect, vi } from 'vitest'
import { executeSgmeCommand } from '../src/commands.js'
import type { SgmeClient, SearchResponse } from '../src/sgme-client.js'

function makeMockClient(searchImpl: (req: unknown) => SearchResponse | null): SgmeClient {
  return {
    search: vi.fn(searchImpl) as unknown as SgmeClient['search'],
  } as unknown as SgmeClient
}

describe('executeSgmeCommand', () => {
  it('空查询返回 error + 用法说明', async () => {
    const client = makeMockClient(() => null)
    const result = await executeSgmeCommand(client, { searchLimit: 5 }, '')
    expect(result.kind).toBe('error')
    expect(result.text).toContain('用法')
    expect(result.text).toContain('/sgme <关键词>')
    // 不应调用 search
    expect(client.search).not.toHaveBeenCalled()
  })

  it('纯空格查询返回 error + 用法说明', async () => {
    const client = makeMockClient(() => null)
    const result = await executeSgmeCommand(client, { searchLimit: 5 }, '   ')
    expect(result.kind).toBe('error')
    expect(result.text).toContain('用法')
  })

  it('有结果时返回 success + 格式化结果', async () => {
    const client = makeMockClient(() => ({
      results: [
        { rank: 1, source: 'memory', content: '记忆1', routes: ['bm25'] },
        { rank: 2, source: 'wiki', content: '场景1', title: '场景标题', routes: ['vector'] },
      ],
      meta: { routes: ['bm25', 'vector'], rrf_k: 60 },
    }))
    const result = await executeSgmeCommand(client, { searchLimit: 5 }, '测试')

    expect(result.kind).toBe('success')
    expect(result.text).toContain('测试')
    expect(result.text).toContain('[memory]')
    expect(result.text).toContain('记忆1')
    expect(result.text).toContain('[wiki]')
    expect(result.text).toContain('场景标题')
  })

  it('传 scopes=["memory","wiki","wiki_pages"]', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    await executeSgmeCommand(client, { searchLimit: 5 }, 'x')

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      scopes: ['memory', 'wiki', 'wiki_pages'],
    }))
  })

  it('用 searchLimit 配置', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    await executeSgmeCommand(client, { searchLimit: 10 }, 'x')

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      limit: 10,
    }))
  })

  it('无结果时返回 success + 未找到提示', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    const result = await executeSgmeCommand(client, { searchLimit: 5 }, '不存在的')
    expect(result.kind).toBe('success')
    expect(result.text).toContain('无结果')
    expect(result.text).toContain('未找到')
  })

  it('Gateway 不可达时返回 error + 失败提示', async () => {
    const client = makeMockClient(() => null)
    const result = await executeSgmeCommand(client, { searchLimit: 5 }, 'x')
    expect(result.kind).toBe('error')
    expect(result.text).toContain('失败')
    expect(result.text).toContain('不可达')
  })

  it('超长结果内容被截断', async () => {
    const longContent = 'Z'.repeat(600)
    const client = makeMockClient(() => ({
      results: [{ rank: 1, source: 'memory', content: longContent, routes: [] }],
      meta: { routes: [], rrf_k: 60 },
    }))
    const result = await executeSgmeCommand(client, { searchLimit: 5 }, 'x')
    expect(result.kind).toBe('success')
    expect(result.text).toContain('…')
    expect(result.text.length).toBeLessThan(800)
  })
})
