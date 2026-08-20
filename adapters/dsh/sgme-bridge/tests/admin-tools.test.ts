/**
 * T-86 新工具测试 — 三池登记（idea_add/demand_create/project_register）+
 * 角色模板（role_list/role_assemble/role_active_get/role_active_set）+
 * 记忆纠错（memory_get/memory_reject）+ registerTools 注册 + dimensions 描述修正。
 *
 * mock SgmeClient 对应方法，验证参数传递、结果格式化与降级路径。
 */
import { describe, it, expect, vi } from 'vitest'
import {
  createIdeaAddTool,
  createDemandCreateTool,
  createProjectRegisterTool,
  createRoleListTool,
  createRoleAssembleTool,
  createRoleActiveGetTool,
  createRoleActiveSetTool,
  createMemoryGetTool,
  createMemoryRejectTool,
  createMemorySearchTool,
  registerTools,
} from '../src/tools.js'
import type { SgmeClient } from '../src/sgme-client.js'

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

// ---------- 三池登记 ----------

describe('idea_add tool', () => {
  it('execute 调 ideaAdd 并返回登记确认（含 memory_id）', async () => {
    const client = makeMockClient({
      ideaAdd: vi.fn(async () => ({ idea: { memory_id: 'mem-001' }, created: true })),
    })
    const tool = asToolLike(createIdeaAddTool(client))
    const result = (await tool.execute({ content: '做一个记忆复盘工具' })) as string
    expect(client.ideaAdd).toHaveBeenCalledWith({
      content: '做一个记忆复盘工具',
      priority: null,
      source_ref: null,
    })
    expect(result).toContain('mem-001')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ ideaAdd: vi.fn(async () => null) })
    const tool = asToolLike(createIdeaAddTool(client))
    const result = (await tool.execute({ content: 'x' })) as string
    expect(result).toContain('不可达')
  })
})

describe('demand_create tool', () => {
  it('execute 调 demandCreate 并返回 demand_id（含 warnings 展示）', async () => {
    const client = makeMockClient({
      demandCreate: vi.fn(async () => ({
        demand_id: 'dem-001', title: '适配 T-86', status: 'pending',
        warnings: ['project_id 未登记'],
      })),
    })
    const tool = asToolLike(createDemandCreateTool(client))
    const result = (await tool.execute({ title: '适配 T-86', project_id: 'sgme' })) as string
    expect(client.demandCreate).toHaveBeenCalledWith({
      title: '适配 T-86',
      content: null,
      priority: null,
      project_id: 'sgme',
      source_ref: null,
    })
    expect(result).toContain('dem-001')
    expect(result).toContain('project_id 未登记')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ demandCreate: vi.fn(async () => null) })
    const tool = asToolLike(createDemandCreateTool(client))
    const result = (await tool.execute({ title: 'x' })) as string
    expect(result).toContain('不可达')
  })
})

describe('project_register tool', () => {
  it('execute 调 projectRegister 并返回 project_id', async () => {
    const client = makeMockClient({
      projectRegister: vi.fn(async () => ({ project_id: 'sgme' })),
    })
    const tool = asToolLike(createProjectRegisterTool(client))
    const result = (await tool.execute({ project_id: 'sgme', path: 'D:/Projects/SGME' })) as string
    expect(client.projectRegister).toHaveBeenCalledWith({
      project_id: 'sgme',
      path: 'D:/Projects/SGME',
      name: null,
      git_repo: null,
      milestone: null,
    })
    expect(result).toContain('sgme')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ projectRegister: vi.fn(async () => null) })
    const tool = asToolLike(createProjectRegisterTool(client))
    const result = (await tool.execute({ project_id: 'x' })) as string
    expect(result).toContain('不可达')
  })
})

// ---------- 角色模板 ----------

describe('role_list tool', () => {
  it('execute 列出角色并标注当前角色', async () => {
    const client = makeMockClient({
      roleList: vi.fn(async () => ({
        roles: [
          { role_id: 'butler', name: '管家', description: '生活事务管家' },
          { role_id: 'mentor', name: '导师', description: null },
        ],
        total: 2,
      })),
      roleActiveGet: vi.fn(async () => ({ role_id: 'butler' })),
    })
    const tool = asToolLike(createRoleListTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('管家')
    expect(result).toContain('当前：butler')
    expect(result).toContain('←当前')
  })

  it('execute 未设置角色时展示未设置', async () => {
    const client = makeMockClient({
      roleList: vi.fn(async () => ({ roles: [{ role_id: 'butler', name: '管家' }], total: 1 })),
      roleActiveGet: vi.fn(async () => ({ role_id: null })),
    })
    const tool = asToolLike(createRoleListTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('未设置')
  })

  it('execute Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ roleList: vi.fn(async () => null) })
    const tool = asToolLike(createRoleListTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('不可达')
  })
})

describe('role_assemble tool', () => {
  it('execute 调 roleAssemble（inject_mode 透传）并返回 JSON 产物', async () => {
    const client = makeMockClient({
      roleAssemble: vi.fn(async () => ({
        role_id: 'butler', role_name: '管家',
        system_prompt: '你是{{char}}', care_policy: null, persona: null, profile_blocks: [],
      })),
    })
    const tool = asToolLike(createRoleAssembleTool(client))
    const result = (await tool.execute({ role_id: 'butler', inject_mode: 'daily' })) as string
    expect(client.roleAssemble).toHaveBeenCalledWith('butler', 'daily')
    expect(result).toContain('system_prompt')
    expect(result).toContain('你是{{char}}')
  })

  it('execute 角色不存在时返回失败提示', async () => {
    const client = makeMockClient({ roleAssemble: vi.fn(async () => null) })
    const tool = asToolLike(createRoleAssembleTool(client))
    const result = (await tool.execute({ role_id: 'nope' })) as string
    expect(result).toContain('失败')
  })
})

describe('role_active_get / role_active_set tool', () => {
  it('active_get 返回当前角色', async () => {
    const client = makeMockClient({ roleActiveGet: vi.fn(async () => ({ role_id: 'butler', status: 'active' })) })
    const tool = asToolLike(createRoleActiveGetTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('butler')
  })

  it('active_get 未设置时返回未设置', async () => {
    const client = makeMockClient({ roleActiveGet: vi.fn(async () => ({ role_id: null })) })
    const tool = asToolLike(createRoleActiveGetTool(client))
    const result = (await tool.execute({})) as string
    expect(result).toContain('未设置')
  })

  it('active_set 调 PUT 语义方法并返回切换确认', async () => {
    const client = makeMockClient({ roleActiveSet: vi.fn(async () => ({ role_id: 'mentor', status: 'active' })) })
    const tool = asToolLike(createRoleActiveSetTool(client))
    const result = (await tool.execute({ role_id: 'mentor' })) as string
    expect(client.roleActiveSet).toHaveBeenCalledWith('mentor')
    expect(result).toContain('mentor')
  })

  it('active_set 失败时返回失败提示', async () => {
    const client = makeMockClient({ roleActiveSet: vi.fn(async () => null) })
    const tool = asToolLike(createRoleActiveSetTool(client))
    const result = (await tool.execute({ role_id: 'x' })) as string
    expect(result).toContain('失败')
  })
})

// ---------- 记忆纠错 ----------

describe('memory_get / memory_reject tool', () => {
  it('memory_get 调 client 并返回详情 JSON', async () => {
    const client = makeMockClient({
      memoryGet: vi.fn(async () => ({
        memory: { memory_id: 'mem-001', content: '用户偏好深色主题', status: 'active' },
        sources: [],
        archive_chain: [],
      })),
    })
    const tool = asToolLike(createMemoryGetTool(client))
    const result = (await tool.execute({ memory_id: 'mem-001' })) as string
    expect(client.memoryGet).toHaveBeenCalledWith('mem-001')
    expect(result).toContain('用户偏好深色主题')
  })

  it('memory_reject 调 client（reason 透传）并返回确认', async () => {
    const client = makeMockClient({
      memoryReject: vi.fn(async () => ({ memory_id: 'mem-001', status: 'rejected', reject_reason: '记错了' })),
    })
    const tool = asToolLike(createMemoryRejectTool(client))
    const result = (await tool.execute({ memory_id: 'mem-001', reason: '记错了' })) as string
    expect(client.memoryReject).toHaveBeenCalledWith('mem-001', '记错了')
    expect(result).toContain('不采用')
    expect(result).toContain('记错了')
  })

  it('memory_reject Gateway 不可达时返回降级提示', async () => {
    const client = makeMockClient({ memoryReject: vi.fn(async () => null) })
    const tool = asToolLike(createMemoryRejectTool(client))
    const result = (await tool.execute({ memory_id: 'x' })) as string
    expect(result).toContain('失败')
  })
})

// ---------- 注册 + dimensions 描述修正 ----------

describe('registerTools 注册（T-86 扩充）', () => {
  it('注册全部 19 个工具（9 旧 + 9 新 + inject）', () => {
    const registered: string[] = []
    const ctx = {
      tools: { register: (tool: unknown) => { registered.push((tool as ToolLike).name); return () => {} } },
    }
    registerTools(ctx as unknown as Parameters<typeof registerTools>[0], makeMockClient({}), 5)
    expect(registered).toHaveLength(19)
    for (const name of [
      'idea_add', 'demand_create', 'project_register',
      'role_list', 'role_assemble', 'role_active_get', 'role_active_set',
      'memory_get', 'memory_reject',
    ]) {
      expect(registered).toContain(name)
    }
  })

  it('memory_search dimensions 描述不含已裁剪的 projects/tasks', () => {
    const tool = createMemorySearchTool(makeMockClient({}), 5)
    // defineTool 会转换 parameters 形态，用 JSON 序列化断言描述文案
    const serialized = JSON.stringify(tool)
    expect(serialized).not.toContain('identity/projects')
    expect(serialized).toContain('identity/status/focus/goals/ideas')
    expect(serialized).toContain('已裁剪')
  })
})
