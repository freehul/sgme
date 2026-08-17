/**
 * wiki 工具测试 — wiki_pages + wiki_page + wiki_page_update（W5，方案 v0.3 §5.5）。
 *
 * mock SgmeClient 的 wikiListPages / wikiGetPage，验证工具参数传递、
 * 结果格式化与降级路径。
 */
import { describe, it, expect, vi } from 'vitest'
import { createWikiSearchTool, createWikiPagesTool, createWikiPageTool, createWikiPageUpdateTool, createWikiPageAddTool } from '../src/tools.js'
import type { SgmeClient, WikiPagesResponse, WikiPage } from '../src/sgme-client.js'

type ToolLike = {
  name: string
  description: string
  execute: (args: unknown, exec?: unknown) => Promise<unknown>
}

function asToolLike(tool: unknown): ToolLike {
  return tool as unknown as ToolLike
}

function makeMockClient(overrides: Partial<SgmeClient>): SgmeClient {
  return overrides as unknown as SgmeClient
}

const pagesResp: WikiPagesResponse = {
  pages: [
    {
      page_id: 'sgme操作手册-58ca9939', title: 'SGME操作手册',
      category: 'skill/sgme', tags: ['skill', 'sgme'],
      source_type: 'text', source_url: null, source_file: null,
      ingested_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T00:00:00Z',
      description: 'SGME 操作手册：记忆/知识库/运维全流程',
    },
  ],
  total: 1, limit: 20, offset: 0,
}

const pageDetail: WikiPage = {
  page_id: 'sgme操作手册-58ca9939',
  title: 'SGME操作手册',
  category: 'skill/sgme',
  tags: ['skill', 'sgme'],
  source_type: 'text',
  source_url: null,
  source_file: null,
  ingested_at: '2026-08-16T00:00:00Z',
  updated_at: '2026-08-16T00:00:00Z',
  description: 'SGME 操作手册：记忆/知识库/运维全流程',
  content: '# SGME 操作手册\n\n正文内容',
  content_seg: 'SGME 操作手册 正文内容',
}

describe('wiki_search tool', () => {
  it('工具名和描述正确', () => {
    const tool = asToolLike(createWikiSearchTool(makeMockClient({}), 20))
    expect(tool.name).toBe('wiki_search')
    expect(tool.description).toContain('skill')
  })

  it('execute 走执行通道调 wikiSearch 并格式化结果（含 skill 页）', async () => {
    const client = makeMockClient({
      wikiSearch: vi.fn(async () => ({
        results: [
          { page_id: 'sgme操作手册-58ca9939', title: 'SGME操作手册', snippet: '手册正文摘要', tags: '["skill","sgme"]' },
        ],
      })),
    })
    const tool = asToolLike(createWikiSearchTool(client, 20))
    const result = (await tool.execute({ query: '操作手册' })) as string

    expect(client.wikiSearch).toHaveBeenCalledWith('操作手册', 20)
    expect(result).toContain('SGME操作手册')
    expect(result).toContain('手册正文摘要')
    expect(result).toContain('skill')   // tags JSON 字符串解析后展示
  })

  it('execute 无结果时返回提示', async () => {
    const client = makeMockClient({ wikiSearch: vi.fn(async () => ({ results: [] })) })
    const tool = asToolLike(createWikiSearchTool(client, 20))
    const result = (await tool.execute({ query: 'nope' })) as string
    expect(result).toContain('无结果')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ wikiSearch: vi.fn(async () => null) })
    const tool = asToolLike(createWikiSearchTool(client, 20))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('不可达')
  })
})

describe('wiki_pages tool', () => {
  it('工具名和描述正确', () => {
    const tool = asToolLike(createWikiPagesTool(makeMockClient({}), 20))
    expect(tool.name).toBe('wiki_pages')
    expect(tool.description).toContain('category')
  })

  it('execute 按 category 过滤并格式化轻量列表', async () => {
    const client = makeMockClient({ wikiListPages: vi.fn(async () => pagesResp) })
    const tool = asToolLike(createWikiPagesTool(client, 20))
    const result = (await tool.execute({ category: 'skill/sgme' })) as string

    expect(client.wikiListPages).toHaveBeenCalledWith('skill/sgme', 20, 0)
    expect(result).toContain('SGME操作手册')
    expect(result).toContain('skill/sgme')
  })

  it('execute 无结果时返回提示', async () => {
    const client = makeMockClient({ wikiListPages: vi.fn(async () => ({ pages: [], total: 0, limit: 20, offset: 0 })) })
    const tool = asToolLike(createWikiPagesTool(client, 20))
    const result = (await tool.execute({ category: 'nope' })) as string
    expect(result).toContain('无结果')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ wikiListPages: vi.fn(async () => null) })
    const tool = asToolLike(createWikiPagesTool(client, 20))
    const result = (await tool.execute({})) as string
    expect(result).toContain('不可达')
  })
})

describe('wiki_page tool', () => {
  it('工具名和描述正确', () => {
    const tool = asToolLike(createWikiPageTool(makeMockClient({})))
    expect(tool.name).toBe('wiki_page')
    expect(tool.description).toContain('page_id')
  })

  it('execute 拉取全文并带元数据头', async () => {
    const client = makeMockClient({ wikiGetPage: vi.fn(async () => pageDetail) })
    const tool = asToolLike(createWikiPageTool(client))
    const result = (await tool.execute({ page_id: 'sgme操作手册-58ca9939' })) as string

    expect(client.wikiGetPage).toHaveBeenCalledWith('sgme操作手册-58ca9939')
    expect(result).toContain('# SGME 操作手册')
    expect(result).toContain('正文内容')
    expect(result).toContain('category: skill/sgme')
  })

  it('execute 页面不存在时返回提示', async () => {
    const client = makeMockClient({ wikiGetPage: vi.fn(async () => null) })
    const tool = asToolLike(createWikiPageTool(client))
    const result = (await tool.execute({ page_id: 'nope' })) as string
    expect(result).toContain('失败')
  })
})

describe('wiki_page_update tool', () => {
  it('工具名和描述正确', () => {
    const tool = asToolLike(createWikiPageUpdateTool(makeMockClient({})))
    expect(tool.name).toBe('wiki_page_update')
    expect(tool.description).toContain('append')
  })

  it('execute 调 wikiUpdatePage 并返回含 status 的结果', async () => {
    const client = makeMockClient({
      wikiUpdatePage: vi.fn(async () => ({ page_id: 'p1', status: 'appended' })),
    })
    const tool = asToolLike(createWikiPageUpdateTool(client))
    const result = (await tool.execute({ page_id: 'p1', content: '追加内容' })) as string

    expect(client.wikiUpdatePage).toHaveBeenCalledWith('p1', {
      content: '追加内容',
      append: true,
      author: null,
    })
    expect(result).toContain('已更新')
    expect(result).toContain('status=appended')
  })

  it('execute 失败（返回 null）时返回失败提示', async () => {
    const client = makeMockClient({ wikiUpdatePage: vi.fn(async () => null) })
    const tool = asToolLike(createWikiPageUpdateTool(client))
    const result = (await tool.execute({ page_id: 'nope', content: 'x' })) as string
    expect(result).toContain('失败')
  })
})

describe('wiki_page_add tool', () => {
  it('工具名和描述正确', () => {
    const tool = asToolLike(createWikiPageAddTool(makeMockClient({})))
    expect(tool.name).toBe('wiki_page_add')
    expect(tool.description).toContain('幂等 upsert')
  })

  it('execute 调 wikiCreatePage 并返回含 status 的结果', async () => {
    const client = makeMockClient({
      wikiCreatePage: vi.fn(async () => ({ page_id: 'p-new-123', status: 'created' })),
    })
    const tool = asToolLike(createWikiPageAddTool(client))
    const result = (await tool.execute({
      title: '测试手册', content: '正文', category: 'skill/test',
      tags: 'sgme,踩坑', author: 'test-agent',
    })) as string

    expect(client.wikiCreatePage).toHaveBeenCalledWith({
      title: '测试手册',
      content: '正文',
      category: 'skill/test',
      tags: ['sgme', '踩坑'],
      description: null,
      author: 'test-agent',
    })
    expect(result).toContain('已写入')
    expect(result).toContain('page_id=p-new-123')
    expect(result).toContain('status=created')
  })

  it('tags 未传时传 null', async () => {
    const client = makeMockClient({
      wikiCreatePage: vi.fn(async () => ({ page_id: 'p2', status: 'updated' })),
    })
    const tool = asToolLike(createWikiPageAddTool(client))
    await tool.execute({ title: 'T', content: 'C' })

    expect(client.wikiCreatePage).toHaveBeenCalledWith({
      title: 'T',
      content: 'C',
      category: null,
      tags: null,
      description: null,
      author: null,
    })
  })

  it('execute 失败（返回 null）时返回失败提示', async () => {
    const client = makeMockClient({ wikiCreatePage: vi.fn(async () => null) })
    const tool = asToolLike(createWikiPageAddTool(client))
    const result = (await tool.execute({ title: 'T', content: 'C' })) as string
    expect(result).toContain('失败')
  })
})
