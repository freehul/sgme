/**
 * wiki 工具测试 — wiki_pages + wiki_page（W5，方案 v0.3 §5.5）。
 *
 * mock SgmeClient 的 wikiListPages / wikiGetPage，验证工具参数传递、
 * 结果格式化与降级路径。
 */
import { describe, it, expect, vi } from 'vitest'
import { createWikiPagesTool, createWikiPageTool } from '../src/tools.js'
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
