/**
 * SGME 1.10.x 对齐测试 —— stats 维度分布数组形态 + refine_runs 批次指标透出。
 *
 * 背景（2026-10-19 实测服务端 1.10.4）：
 * - GET /v1/admin/stats 的 dimension_distribution 现为 [{id, display_name, count}] 数组
 *   （sgme/data/stats_dao.py:dimension_distribution → list[dict]）；适配器旧实现按
 *   Record<string, number> 用 Object.entries 渲染 → "0=[object Object]"，维度分布不可读。
 * - GET /v1/admin/refine_runs 条目新增 finished_at / total_tokens / memories_count / error
 *   （1.10.2 P4 起）；旧实现只渲染 status/stage/file_id/started_at，运维看不到耗时与用量。
 */
import { describe, it, expect, vi } from 'vitest'
import { createRefineStatusTool, createStatsTool } from '../src/tools.js'
import type { SgmeClient as SgmeClientType } from '../src/sgme-client.js'

type ToolLike = {
  name: string
  execute: (args: unknown, exec?: unknown) => Promise<unknown>
}

function asToolLike(tool: unknown): ToolLike {
  return tool as unknown as ToolLike
}

function makeMockClient(overrides: Partial<SgmeClientType>): SgmeClientType {
  return overrides as unknown as SgmeClientType
}

describe('stats 工具：1.10.x 数组形态维度分布', () => {
  it('按 display_name + count 渲染并降序，不出现 [object Object]', async () => {
    const client = makeMockClient({
      stats: vi.fn(async () => ({
        memories: { total: 100, archived: 2 },
        raw_files: { total: 50, new: 0, refined: 48, error: 1, archived: 1 },
        dimension_distribution: [
          { id: 'tech_stack', display_name: '技术栈', count: 17157 },
          { id: 'identity', display_name: '身份', count: 927 },
          { id: 'style', display_name: '风格', count: 0 },
        ],
        refinement: { watermark_age_sec: 60, last_refined_at: '2026-01-01T00:00:00Z', queue_depth: 1 },
        agents: [{ agent_id: 'dsh', role: 'agent' }],
      })),
    })
    const result = (await asToolLike(createStatsTool(client)).execute({})) as string
    expect(result).toContain('技术栈=17157')
    expect(result).toContain('身份=927')
    expect(result).not.toContain('[object Object]')
    // 降序：技术栈（17157）排在身份（927）之前
    expect(result.indexOf('技术栈')).toBeLessThan(result.indexOf('身份'))
  })

  it('兼容旧 dict 形态（历史服务端），不回归', async () => {
    const client = makeMockClient({
      stats: vi.fn(async () => ({
        memories: { total: 100, archived: 2 },
        raw_files: { total: 50, new: 0, refined: 48, error: 1, archived: 1 },
        dimension_distribution: { identity: 5, status: 3 },
        refinement: { watermark_age_sec: 60, last_refined_at: '2026-01-01T00:00:00Z', queue_depth: 1 },
        agents: [],
      })),
    })
    const result = (await asToolLike(createStatsTool(client)).execute({})) as string
    expect(result).toContain('identity=5')
    expect(result).not.toContain('[object Object]')
  })
})

describe('refine_status 工具：1.10.x 批次指标', () => {
  it('渲染耗时 / total_tokens / memories_count', async () => {
    const client = makeMockClient({
      refineRuns: vi.fn(async () => ({
        items: [
          {
            file_id: 'f1',
            status: 'ok',
            stage: 'l2_scene',
            started_at: '2026-10-19T13:26:17Z',
            finished_at: '2026-10-19T13:26:31Z',
            total_tokens: 10735,
            memories_count: 2,
          },
        ],
        count: 1,
        total: 9,
        page: 1,
        limit: 10,
      })),
    })
    const result = (await asToolLike(createRefineStatusTool(client)).execute({})) as string
    expect(result).toContain('共 9 条')
    expect(result).toContain('14.0s')
    expect(result).toContain('tokens 10735')
    expect(result).toContain('记忆 2 条')
  })

  it('error 批次带错误摘要', async () => {
    const client = makeMockClient({
      refineRuns: vi.fn(async () => ({
        items: [
          { file_id: 'f2', status: 'error', stage: 'l1', started_at: '2026-10-19T13:00:00Z', error: 'llm timeout' },
        ],
        count: 1,
        total: 1,
        page: 1,
        limit: 10,
      })),
    })
    const result = (await asToolLike(createRefineStatusTool(client)).execute({})) as string
    expect(result).toContain('[error]')
    expect(result).toContain('llm timeout')
  })
})
