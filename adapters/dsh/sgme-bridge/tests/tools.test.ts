/**
 * tools.ts 测试 — memory_search + wiki_search 工具的 execute 方法。
 *
 * mock SgmeClient.search，验证工具调用的参数传递 + 结果格式化 + 降级路径。
 * 断言对齐 defineTool() 返回的 ToolDefinition（2026-08-14 T-53 修复后）。
 */
import { describe, it, expect, vi } from 'vitest'
import { createMemorySearchTool, createWikiSearchTool } from '../src/tools.js'
import type { SgmeClient, SearchResponse } from '../src/sgme-client.js'

// ---------- mock SgmeClient ----------

function makeMockClient(searchImpl: (req: unknown) => SearchResponse | null): SgmeClient {
  return {
    search: vi.fn(searchImpl) as unknown as SgmeClient['search'],
  } as unknown as SgmeClient
}

// ---------- defineTool 产物的字段访问 helper ----------
// defineTool 返回的 ToolDefinition 字段是私有的，但通过公共属性可读取 name/description。
// execute 签名是 (args, exec) => Promise<unknown>，测试用 unknown 中转再断言。

type ToolLike = {
  name: string
  description: string
  execute: (args: unknown, exec?: unknown) => Promise<unknown>
}

function asToolLike(tool: unknown): ToolLike {
  return tool as unknown as ToolLike
}

describe('memory_search tool', () => {
  it('工具名和描述正确', () => {
    const client = makeMockClient(() => null)
    const tool = asToolLike(createMemorySearchTool(client, 5))
    expect(tool.name).toBe('memory_search')
    expect(tool.description).toContain('SGME 长期记忆')
  })

  it('execute 传 scopes=["memory"]', async () => {
    const client = makeMockClient(() => ({
      results: [{ rank: 1, source: 'memory', content: '记忆1', routes: ['bm25'] }],
      meta: { routes: ['bm25'], rrf_k: 60 },
    }))
    const tool = asToolLike(createMemorySearchTool(client, 5))
    const result = (await tool.execute({ query: '测试' })) as string

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      scopes: ['memory'],
      query: '测试',
    }))
    expect(result).toContain('记忆1')
  })

  it('execute 用 defaultLimit 兜底', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    const tool = asToolLike(createMemorySearchTool(client, 7))
    await tool.execute({ query: 'x' })

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      limit: 7,
    }))
  })

  it('execute 传 limit 参数覆盖默认', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    const tool = asToolLike(createMemorySearchTool(client, 5))
    await tool.execute({ query: 'x', limit: 20 })

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      limit: 20,
    }))
  })

  it('execute 传 dimensions 和 match', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    const tool = asToolLike(createMemorySearchTool(client, 5))
    await tool.execute({ query: 'x', dimensions: ['identity'], match: 'all' })

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      dimensions: ['identity'],
      match: 'all',
    }))
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient(() => null)
    const tool = asToolLike(createMemorySearchTool(client, 5))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('失败')
    expect(result).toContain('不可达')
  })

  it('execute 无结果时返回空提示', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    const tool = asToolLike(createMemorySearchTool(client, 5))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('无结果')
  })

  it('execute 超长内容被截断', async () => {
    const longContent = 'A'.repeat(600)
    const client = makeMockClient(() => ({
      results: [{ rank: 1, source: 'memory', content: longContent, routes: [] }],
      meta: { routes: [], rrf_k: 60 },
    }))
    const tool = asToolLike(createMemorySearchTool(client, 5))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('…')
    expect(result.length).toBeLessThan(700)
  })
})

// ---------- wiki_search ----------

describe('wiki_search tool', () => {
  it('工具名和描述正确', () => {
    const client = makeMockClient(() => null)
    const tool = asToolLike(createWikiSearchTool(client, 5))
    expect(tool.name).toBe('wiki_search')
    expect(tool.description).toContain('知识库')
  })

  it('execute 传 scopes=["wiki","wiki_pages"]', async () => {
    const client = makeMockClient(() => ({
      results: [{ rank: 1, source: 'wiki', content: '场景1', title: '场景标题', routes: ['bm25'] }],
      meta: { routes: ['bm25'], rrf_k: 60 },
    }))
    const tool = asToolLike(createWikiSearchTool(client, 5))
    const result = (await tool.execute({ query: '测试' })) as string

    expect(client.search).toHaveBeenCalledWith(expect.objectContaining({
      scopes: ['wiki', 'wiki_pages'],
    }))
    expect(result).toContain('场景1')
    expect(result).toContain('场景标题')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient(() => null)
    const tool = asToolLike(createWikiSearchTool(client, 5))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('失败')
  })

  it('execute 无结果时返回空提示', async () => {
    const client = makeMockClient(() => ({
      results: [], meta: { routes: [], rrf_k: 60 },
    }))
    const tool = asToolLike(createWikiSearchTool(client, 5))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('无结果')
  })
})
