/**
 * T-111 技能层工具测试 — skill_search / skill_digest / skill_get / skill_list / skill_coldstart。
 *
 * 覆盖：参数传递、结果格式化、降级路径；以及 2026-08-29 修复的两个回归点：
 * ①skills 层结果无 content 字段 → formatSearchResults 不得 TypeError 崩；
 * ②wiki 场景层 source 实际为 wiki_scene → context.ts 场景过滤必须认它。
 *
 * 契约来源：SGME 1.1.0 实测（GET /v1/skills* + POST /v1/search scope=skills）。
 */
import { describe, it, expect, vi } from 'vitest'
import {
  createSkillSearchTool,
  createSkillDigestTool,
  createSkillGetTool,
  createSkillListTool,
  createSkillColdstartTool,
  createMemorySearchTool,
} from '../src/tools.js'
import type { SgmeClient, SearchResponse } from '../src/sgme-client.js'

type ToolLike = {
  name: string
  description: string
  parameters: Record<string, { description?: string }>
  execute: (args: unknown, exec?: unknown) => Promise<unknown>
}

function asToolLike(tool: unknown): ToolLike {
  return tool as unknown as ToolLike
}

function makeMockClient(overrides: Partial<SgmeClient>): SgmeClient {
  return overrides as unknown as SgmeClient
}

// ---------- skill_search ----------

describe('skill_search tool', () => {
  it('execute 调 skillSearch 并按「name + 描述」列出命中', async () => {
    const client = makeMockClient({
      skillSearch: vi.fn(async () => [
        { name: 'nas-docker-operations', description: 'NAS Docker 部署与排障', category: 'devops', tags: [], source: 'skills', version: null },
        { name: 'nas-ssh', description: 'NAS SSH 登录', category: 'devops', tags: [], source: 'skills', version: null },
      ]),
    })
    const tool = asToolLike(createSkillSearchTool(client, 5))
    const result = (await tool.execute({ query: 'docker 部署', limit: 3 })) as string
    expect(client.skillSearch).toHaveBeenCalledWith('docker 部署', 3)
    expect(result).toContain('nas-docker-operations')
    expect(result).toContain('[devops]')
    expect(result).toContain('skill_get')
  })

  it('limit 缺省时传 defaultLimit', async () => {
    const client = makeMockClient({ skillSearch: vi.fn(async () => []) })
    const tool = asToolLike(createSkillSearchTool(client, 7))
    await tool.execute({ query: 'x' })
    expect(client.skillSearch).toHaveBeenCalledWith('x', 7)
  })

  it('Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ skillSearch: vi.fn(async () => null) })
    const tool = asToolLike(createSkillSearchTool(client, 5))
    const result = (await tool.execute({ query: 'x' })) as string
    expect(result).toContain('skill_search')
    expect(result).toContain('不可达')
  })

  it('无命中时提示换关键词重试', async () => {
    const client = makeMockClient({ skillSearch: vi.fn(async () => []) })
    const tool = asToolLike(createSkillSearchTool(client, 5))
    const result = (await tool.execute({ query: 'qqq' })) as string
    expect(result).toContain('无结果')
  })
})

// ---------- skill_digest ----------

describe('skill_digest tool', () => {
  it('execute 调 skillDigest 并输出字段 + uses + 正文骨架', async () => {
    const client = makeMockClient({
      skillDigest: vi.fn(async () => ({
        name: 'nas-docker-operations',
        description: 'NAS Docker 管理',
        version: '2.0',
        pattern: 'manual',
        category: 'devops',
        tags: ['skill'],
        uses: ['nas-ssh'],
        sections: ['# NAS Docker Operations', '## 前置条件'],
      })),
    })
    const tool = asToolLike(createSkillDigestTool(client))
    const result = (await tool.execute({ name: 'nas-docker-operations' })) as string
    expect(client.skillDigest).toHaveBeenCalledWith('nas-docker-operations')
    expect(result).toContain('nas-docker-operations')
    expect(result).toContain('v2.0')
    expect(result).toContain('uses: nas-ssh')
    expect(result).toContain('## 前置条件')
  })

  it('技能不存在时返回失败提示并引导先检索', async () => {
    const client = makeMockClient({ skillDigest: vi.fn(async () => null) })
    const tool = asToolLike(createSkillDigestTool(client))
    const result = (await tool.execute({ name: 'nope' })) as string
    expect(result).toContain('skill_digest')
    expect(result).toContain('skill_search')
  })
})

// ---------- skill_get ----------

describe('skill_get tool', () => {
  it('execute 调 skillGet 并输出带标注的全文', async () => {
    const client = makeMockClient({
      skillGet: vi.fn(async () => ({
        name: 'nas-docker-operations',
        content: '# NAS Docker Operations\n正文',
        sha256: 'abc',
        section: null,
        truncated_by_section: false,
        source: 'git',
      })),
    })
    const tool = asToolLike(createSkillGetTool(client))
    const result = (await tool.execute({ name: 'nas-docker-operations' })) as string
    expect(client.skillGet).toHaveBeenCalledWith('nas-docker-operations', null)
    expect(result).toContain('<!-- skill: nas-docker-operations -->')
    expect(result).toContain('# NAS Docker Operations')
  })

  it('传 section 时透传并标注已截取', async () => {
    const client = makeMockClient({
      skillGet: vi.fn(async () => ({
        name: 'x', content: '片段', sha256: 'a', section: '## 前置条件',
        truncated_by_section: true, source: 'git',
      })),
    })
    const tool = asToolLike(createSkillGetTool(client))
    const result = (await tool.execute({ name: 'x', section: '## 前置条件' })) as string
    expect(client.skillGet).toHaveBeenCalledWith('x', '## 前置条件')
    expect(result).toContain('已按 section')
  })

  it('技能不存在时返回失败提示', async () => {
    const client = makeMockClient({ skillGet: vi.fn(async () => null) })
    const tool = asToolLike(createSkillGetTool(client))
    const result = (await tool.execute({ name: 'nope' })) as string
    expect(result).toContain('skill_get')
    expect(result).toContain('不可达')
  })
})

// ---------- skill_list ----------

describe('skill_list tool', () => {
  it('execute 调 skillList 并输出总量与条目', async () => {
    const client = makeMockClient({
      skillList: vi.fn(async () => ({
        skills: [{ name: 'a', description: '技能A描述', category: 'c1', tags: [], source: 'git', version: '1.0.0' }],
        total: 403, returned: 1, offset: 0, budget: 40,
      })),
    })
    const tool = asToolLike(createSkillListTool(client, 50))
    const result = (await tool.execute({})) as string
    expect(client.skillList).toHaveBeenCalledWith(50, 0)
    expect(result).toContain('403')
    expect(result).toContain('技能A描述')
  })

  it('空结果时返回无技能提示', async () => {
    const client = makeMockClient({
      skillList: vi.fn(async () => ({ skills: [], total: 0, returned: 0, offset: 10, budget: 40 })),
    })
    const tool = asToolLike(createSkillListTool(client, 50))
    const result = (await tool.execute({ offset: 10 })) as string
    expect(result).toContain('无技能')
  })
})

// ---------- skill_coldstart ----------

describe('skill_coldstart tool', () => {
  it('execute 输出协议 skill 全文 + SGME 操作手册', async () => {
    const client = makeMockClient({
      skillColdstart: vi.fn(async () => ({
        index: {
          items: [{
            name: 'skill-registry-protocol',
            description: '技能检索协议',
            category: 'sgme', tags: ['skill'], source: 'builtin', version: null,
            content: '# SGME 技能检索协议\n需要时先检索',
          }],
          total: 1,
        },
        hotset: [],
        manual: { page_id: 'p1', title: 'SGME操作手册', content: '手册正文' },
      })),
    })
    const tool = asToolLike(createSkillColdstartTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('<!-- skill: skill-registry-protocol -->')
    expect(result).toContain('先检索')
    expect(result).toContain('<!-- sgme-manual: SGME操作手册 -->')
    expect(result).toContain('手册正文')
  })

  it('冷启动项无 content 时退到 description（不产出空行）', async () => {
    const client = makeMockClient({
      skillColdstart: vi.fn(async () => ({
        index: { items: [{ name: 'x', description: '仅有描述', category: null, tags: [], source: null, version: null }], total: 1 },
        hotset: [],
        manual: null,
      })),
    })
    const tool = asToolLike(createSkillColdstartTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('仅有描述')
  })

  it('Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ skillColdstart: vi.fn(async () => null) })
    const tool = asToolLike(createSkillColdstartTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('skill_coldstart')
    expect(result).toContain('不可达')
  })
})

// ---------- 回归：skills 层无 content 不得崩（2026-08-29 修复） ----------

describe('skills 层结果无 content 的格式化兜底（回归）', () => {
  it('memory_search 收到 skills 形态结果（无 content）时不抛 TypeError', async () => {
    const resp: SearchResponse = {
      results: [
        // 实测形态：skills 层只有 name/description/category，无 content/title
        { rank: 1, source: 'skills', name: 'nas-docker-operations', description: 'NAS Docker 管理', category: 'devops', routes: ['skills_bm25'] },
      ],
      meta: { routes: ['skills_bm25'], rrf_k: 60 },
    }
    const client = makeMockClient({ search: vi.fn(async () => resp) })
    const tool = asToolLike(createMemorySearchTool(client, 5))
    // 修复前：r.content.length → TypeError；修复后应正常返回兜底文本
    await expect(tool.execute({ query: 'docker' })).resolves.toContain('nas-docker-operations')
  })

  it('兜底优先级：content > name+description > description', async () => {
    const mk = (r: Record<string, unknown>) => ({
      results: [{ rank: 1, source: 'skills', ...r }],
      meta: { routes: ['bm25'], rrf_k: 60 },
    })
    const run = async (r: Record<string, unknown>) => {
      const client = makeMockClient({ search: vi.fn(async () => mk(r) as SearchResponse) })
      const tool = asToolLike(createMemorySearchTool(client, 5))
      return (await tool.execute({ query: 'q' })) as string
    }
    // 三者都有 → 用 content
    expect(await run({ content: '正文内容', name: 'n1', description: 'd1' })).toContain('正文内容')
    // 无 content 有 name+description → 拼 name — description
    expect(await run({ name: 'n2', description: 'd2' })).toContain('n2 — d2')
    // 只有 description
    expect(await run({ description: 'd3' })).toContain('d3')
  })

  it('超长描述截断到 500 字符且带省略号', async () => {
    const long = 'x'.repeat(800)
    const client = makeMockClient({
      search: vi.fn(async () => ({
        results: [{ rank: 1, source: 'skills', name: 'n', description: long }],
        meta: { routes: ['bm25'], rrf_k: 60 },
      } as SearchResponse)),
    })
    const tool = asToolLike(createMemorySearchTool(client, 5))
    const result = (await tool.execute({ query: 'q' })) as string
    expect(result.length).toBeLessThan(600)
    expect(result).toContain('…')
  })
})
