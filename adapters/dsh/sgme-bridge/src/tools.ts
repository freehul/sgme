/**
 * tools.ts — memory_search / wiki_search 工具注册
 *
 * 把 SGME 检索能力暴露为 dsh 工具，模型可按需调用查询记忆/知识库。
 *
 * 契约对齐：POST /v1/search（Agent Key）
 * - memory_search：scopes=["memory"]
 * - wiki_search：scopes=["wiki","wiki_pages"]
 *
 * dsh 工具规范（2026-08-14 T-53 本地加载确认，对齐 @deepseek-ai/dsh-tools 官方文档）：
 * - 使用 defineTool() helper 生成 ToolDefinition（参数类型自动推导）
 * - parameters 用扁平映射 { name: { type, required?, description?, enum? } }
 * - execute(args, exec) — exec 含 signal，协作式取消
 * - output { schema, render(args, value) } — schema 是 ValueSchemaSpec，render 把 value 转 ContentBlock[]
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { SgmeClient } from './sgme-client.js'

/** 工具参数：检索查询。 */
interface SearchArgs {
  query: string
  limit?: number
  dimensions?: string[]
  match?: 'any' | 'all'
}

/**
 * 创建 memory_search 工具（检索 L1.5 记忆池）。
 *
 * 模型调用此工具查询用户/项目的长期记忆，例如"用户之前提过什么相关需求"。
 */
export function createMemorySearchTool(client: SgmeClient, defaultLimit: number) {
  return defineTool({
    name: 'memory_search',
    description: [
      '检索 SGME 长期记忆池（L1.5 标签化记忆）。',
      '用于查询用户/项目的历史事实、偏好、决策——当问题涉及"之前/以前/上次/还记得"时必用。',
      '查询不到时返回空，应如实告知"记忆库中未找到"。',
    ].join(' '),
    parameters: {
      query: {
        type: 'string',
        required: true,
        description: '检索关键词或自然语言问题',
      },
      limit: {
        type: 'number',
        description: `返回条数上限（默认 ${defaultLimit}）`,
      },
      dimensions: {
        type: 'array',
        description: '维度过滤（注册表 id，如 identity/status/focus/goals/ideas；projects/tasks 已裁剪不可用）',
      },
      match: {
        type: 'string',
        enum: ['any', 'all'],
        description: '维度匹配语义：any=任一命中，all=全部命中（默认 any）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as SearchArgs
      const resp = await client.search({
        query: a.query,
        scopes: ['memory'],
        limit: a.limit ?? defaultLimit,
        dimensions: a.dimensions ?? null,
        match: a.match ?? 'any',
      })
      if (!resp) {
        return '[memory_search 失败：SGME Gateway 不可达，稍后重试]'
      }
      if (resp.results.length === 0) {
        return `[memory_search 无结果：query="${a.query}"]`
      }
      return formatSearchResults(resp.results)
    },
  })
}

/**
 * 创建 wiki_search 工具（检索 wiki 知识库页面，执行通道）。
 *
 * 走 GET /v1/wiki/search（执行通道，exclude_skill=False），保留 skill 手册——
 * 设计 D4/D5 语义「回忆通道不见手册，执行通道专找」。与统一搜索（/v1/search
 * 的 wiki_pages 层滤 skill）区分开。
 */
export function createWikiSearchTool(client: SgmeClient, defaultLimit: number) {
  return defineTool({
    name: 'wiki_search',
    description: [
      '检索 SGME wiki 知识库页面（wiki_pages，含 skill 技能手册——执行通道，不过滤 skill）。',
      '用于查找操作手册/技能手册/经验文档等 wiki 页面正文。',
      '配合 wiki_pages（按分类列目录）/ wiki_page（按 page_id 拉全文）使用。',
      '与 memory_search 互补：memory 是原始记忆，wiki_search 是 wiki 知识库页面。',
    ].join(' '),
    parameters: {
      query: {
        type: 'string',
        required: true,
        description: '检索关键词或自然语言问题',
      },
      limit: {
        type: 'number',
        description: `返回条数上限（默认 ${defaultLimit}）`,
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as SearchArgs
      const resp = await client.wikiSearch(a.query, a.limit ?? defaultLimit)
      if (!resp) {
        return '[wiki_search 失败：SGME Gateway 不可达，稍后重试]'
      }
      if (resp.results.length === 0) {
        return `[wiki_search 无结果：query="${a.query}"]`
      }
      return formatWikiSearchResults(resp.results)
    },
  })
}

/** 格式化 /v1/wiki/search 结果（page_id/title/snippet/tags，tags 防御解析 JSON 字符串/数组）。 */
function formatWikiSearchResults(
  results: Array<{ page_id: string; title: string; snippet: string; tags?: string[] | string }>,
): string {
  const lines = results.map((r, i) => {
    let tagsText = ''
    const tags = r.tags
    if (Array.isArray(tags)) {
      tagsText = tags.length > 0 ? ` [${tags.join(', ')}]` : ''
    } else if (typeof tags === 'string' && tags) {
      try {
        const parsed = JSON.parse(tags)
        if (Array.isArray(parsed) && parsed.length > 0) tagsText = ` [${parsed.join(', ')}]`
      } catch { /* 保持空 */ }
    }
    return `## ${i + 1}. ${r.title}${tagsText}\n${r.snippet}`
  })
  return lines.join('\n\n')
}

/**
 * 创建 wiki_pages 工具（按分类列出知识库页面，轻量字段）。
 *
 * W5（方案 v0.3 §5.5）：L2 索引层——模型按 category 发现手册，
 * 正文用 wiki_page 二次拉取（渐进式披露）。
 */
export function createWikiPagesTool(client: SgmeClient, defaultLimit: number) {
  return defineTool({
    name: 'wiki_pages',
    description: [
      '列出 SGME 知识库页面（可按 category 分类过滤，如 skill/sgme 技能手册、design 设计文档）。',
      '返回轻量字段（标题/描述/分类/标签），正文用 wiki_page 按 page_id 拉取。',
      '渐进式披露：先列目录判断加载哪本，再拉全文，避免全量注入。',
    ].join(' '),
    parameters: {
      category: {
        type: 'string',
        description: '分类过滤（如 skill/sgme、design；省略列出全部）',
      },
      limit: {
        type: 'number',
        description: `返回条数上限（默认 ${defaultLimit}）`,
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { category?: string; limit?: number }
      const resp = await client.wikiListPages(a.category ?? null, a.limit ?? defaultLimit, 0)
      if (!resp) {
        return '[wiki_pages 失败：SGME Gateway 不可达，稍后重试]'
      }
      if (resp.pages.length === 0) {
        return `[wiki_pages 无结果${a.category ? `：category="${a.category}"` : ''}]`
      }
      const lines = resp.pages.map((p, i) => {
        const cat = p.category ? ` [${p.category}]` : ''
        const desc = p.description ? ` — ${p.description}` : ''
        return `${i + 1}. ${p.title}${cat}（${p.page_id}）${desc}`
      })
      return `共 ${resp.total} 页（显示 ${resp.pages.length}）：\n` + lines.join('\n')
    },
  })
}

/**
 * 创建 wiki_page 工具（按 page_id 拉取知识库页面全文）。
 *
 * W5（方案 v0.3 §5.5）：L2 加载层——索引 skill 引导模型用本工具取手册正文执行。
 */
export function createWikiPageTool(client: SgmeClient) {
  return defineTool({
    name: 'wiki_page',
    description: [
      '按 page_id 拉取 SGME 知识库页面全文（技能手册正文，含 frontmatter 与踩坑记录）。',
      'page_id 来自 wiki_pages / wiki_search 返回结果。',
    ].join(' '),
    parameters: {
      page_id: {
        type: 'string',
        required: true,
        description: '页面 id（wiki_pages 返回的 page_id）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { page_id: string }
      const page = await client.wikiGetPage(a.page_id)
      if (!page) {
        return `[wiki_page 失败：页面不存在或 Gateway 不可达（page_id="${a.page_id}"）]`
      }
      const header = [
        `# ${page.title}`,
        `page_id: ${page.page_id}`,
        `category: ${page.category ?? '-'}`,
        `tags: ${(page.tags ?? []).join(', ') || '-'}`,
      ].join('\n')
      return header + '\n\n' + (page.content ?? '')
    },
  })
}

/**
 * 创建 wiki_page_update 工具（按 page_id 更新知识库页面）。
 *
 * W5（方案 v0.3 §5.5）：L2 写回层——模型修正手册或追加踩坑记录（PATCH append，默认追加）。
 */
export function createWikiPageUpdateTool(client: SgmeClient) {
  return defineTool({
    name: 'wiki_page_update',
    description: [
      '按 page_id 更新 SGME 知识库页面（PATCH，默认 append=true 追加正文）。',
      '用于修正手册内容、追加踩坑记录或更新元数据（title/category/tags/description/author）。',
      'page_id 来自 wiki_pages / wiki_search 返回结果；append=false 时整体覆盖 content。',
    ].join(' '),
    parameters: {
      page_id: {
        type: 'string',
        required: true,
        description: '页面 id（wiki_pages 返回的 page_id）',
      },
      content: {
        type: 'string',
        required: true,
        description: '要写入的正文内容（append=true 时追加到末尾）',
      },
      append: {
        type: 'boolean',
        description: '默认 true 追加',
      },
      author: {
        type: 'string',
        description: '作者标识（可选，如 agent 名）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { page_id: string; content: string; append?: boolean; author?: string }
      const resp = await client.wikiUpdatePage(a.page_id, {
        content: a.content,
        append: a.append ?? true,
        author: a.author ?? null,
      })
      if (!resp) {
        return `[wiki_page_update 失败：页面不存在或 Gateway 不可达（page_id="${a.page_id}"）]`
      }
      return `[wiki_page_update 已更新：page_id=${resp.page_id} status=${resp.status}]`
    },
  })
}

/**
 * 创建 wiki_page_add 工具（写入新知识库页面，幂等 upsert）。
 *
 * W5（方案 v0.3 §5.5）：L2 写回层——模型直接建手册/经验页，
 * 同 title+content 重复提交命中同一 page_id 更新（不重复建页）。
 */
export function createWikiPageAddTool(client: SgmeClient) {
  return defineTool({
    name: 'wiki_page_add',
    description: [
      '创建 SGME 知识库页面（直接写入，不走 LLM 提炼；幂等 upsert）。',
      'title/content 必填；category 用 skill/<domain>（技能/手册）或 design（设计方案）。',
      '同 title+content 重复提交命中同一 page_id 更新，不重复建页；写入后立即可被 wiki_search 检索。',
    ].join(' '),
    parameters: {
      title: {
        type: 'string',
        required: true,
        description: '页面标题（如 "XXX 操作手册"）',
      },
      content: {
        type: 'string',
        required: true,
        description: '页面正文（markdown）',
      },
      category: {
        type: 'string',
        description: '分类（如 skill/sgme、design；可选）',
      },
      tags: {
        type: 'string',
        description: '标签，逗号分隔（可选，如 "sgme,运维,踩坑"）',
      },
      description: {
        type: 'string',
        description: '摘要（索引用，可选）',
      },
      author: {
        type: 'string',
        description: '作者标识（可选，如 agent 名）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as {
        title: string
        content: string
        category?: string
        tags?: string
        description?: string
        author?: string
      }
      const resp = await client.wikiCreatePage({
        title: a.title,
        content: a.content,
        category: a.category ?? null,
        tags: a.tags ? a.tags.split(',').map((t) => t.trim()).filter(Boolean) : null,
        description: a.description ?? null,
        author: a.author ?? null,
      })
      if (!resp) {
        return `[wiki_page_add 失败：Gateway 不可达或写入失败（title="${a.title}"）]`
      }
      return `[wiki_page_add 已写入：page_id=${resp.page_id} status=${resp.status}]`
    },
  })
}

/**
 * 格式化检索结果为模型可读文本。
 *
 * 格式（对齐 reasonix fetch_search 输出）：
 * ```
 * ## 1. [memory] 内容摘要...
 *    routes: bm25, vector
 * ```
 */
function formatSearchResults(
  results: Array<{
    rank: number
    source: string
    content: string
    title?: string
    routes?: string[]
  }>,
): string {
  const lines: string[] = []
  for (const r of results) {
    const titlePrefix = r.title ? `「${r.title}」` : ''
    const routes = r.routes && r.routes.length > 0 ? ` [${r.routes.join(',')}]` : ''
    // 内容截断（避免超长结果撑爆上下文）
    const content = r.content.length > 500 ? r.content.slice(0, 500) + '…' : r.content
    lines.push(`## ${r.rank}. [${r.source}]${titlePrefix}${routes}\n${content}`)
  }
  return lines.join('\n\n')
}

/**
 * 向 dsh ctx 注册全部工具（检索 + 信号消费 + 三池登记 + 角色 + 记忆纠错）。
 *
 * 调用方：index.ts apply() 内调用，传入 ctx 和 client。
 */
export function registerTools(
  ctx: { tools: { register: (tool: ReturnType<typeof defineTool>) => () => void } },
  client: SgmeClient,
  defaultLimit: number,
): void {
  ctx.tools.register(createMemorySearchTool(client, defaultLimit))
  ctx.tools.register(createWikiSearchTool(client, defaultLimit))
  ctx.tools.register(createWikiPagesTool(client, defaultLimit))
  ctx.tools.register(createWikiPageTool(client))
  ctx.tools.register(createWikiPageUpdateTool(client))
  ctx.tools.register(createWikiPageAddTool(client))
  ctx.tools.register(createSignalPullTool(client))
  ctx.tools.register(createSignalClaimTool(client))
  ctx.tools.register(createSignalAckTool(client))
  // T-86：三池登记 + 角色模板 + 记忆纠错（对齐 MCP 侧同名工具）
  ctx.tools.register(createIdeaAddTool(client))
  ctx.tools.register(createDemandCreateTool(client))
  ctx.tools.register(createProjectRegisterTool(client))
  ctx.tools.register(createRoleListTool(client))
  ctx.tools.register(createRoleAssembleTool(client))
  ctx.tools.register(createRoleActiveGetTool(client))
  ctx.tools.register(createRoleActiveSetTool(client))
  ctx.tools.register(createMemoryGetTool(client))
  ctx.tools.register(createMemoryRejectTool(client))
}

// ---------- 信号消费（ST-27 T-59：agent 成为消费者，谁消费谁标记） ----------

/** 创建 signal_pull 工具（拉取未消费关怀信号）。 */
export function createSignalPullTool(client: SgmeClient) {
  return defineTool({
    name: 'signal_pull',
    description: [
      '拉取 SGME 未消费的关怀信号（care_todo_due 待办到期 / care_mood 情绪低落 / care_overwork 过劳 / care_daily 每日问候）。',
      '会话开始主动消费：拉取后决定是否主动关怀用户。',
      '信号消费=主动关怀，谁消费谁标记：先 signal_claim 原子认领，处理完 signal_ack 写回执。',
    ].join(' '),
    parameters: {
      signal_type: {
        type: 'string',
        description: '可选过滤：care_todo_due/care_mood/care_overwork/care_daily；不传拉全部',
      },
      limit: {
        type: 'number',
        description: '返回条数上限（默认 20）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { signal_type?: string; limit?: number }
      const signals = await client.pullCareSignals(a.signal_type ?? null, a.limit ?? 20)
      if (signals === null) {
        return '[signal_pull 失败：SGME Gateway 不可达，稍后重试]'
      }
      if (signals.length === 0) {
        return '[signal_pull 无未消费关怀信号]'
      }
      const lines = signals.map((s) => {
        let payload: unknown = s.payload
        try {
          payload = JSON.parse(s.payload)
        } catch {
          /* 保持原始字符串 */
        }
        return `## ${s.type}（${s.ts}）\nevent_id=${s.event_id}\n${JSON.stringify(payload)}`
      })
      return lines.join('\n\n')
    },
  })
}

/** 创建 signal_claim 工具（原子认领信号）。 */
export function createSignalClaimTool(client: SgmeClient) {
  return defineTool({
    name: 'signal_claim',
    description: [
      '原子认领一条关怀信号（谁消费谁标记，防多 agent 重复关怀）。',
      '认领成功后应主动关怀用户，然后调 signal_ack 写回执。',
      '返回 claimed=false 说明已被其他 agent 消费，跳过即可。',
    ].join(' '),
    parameters: {
      event_id: {
        type: 'string',
        required: true,
        description: '信号 event_id（signal_pull 返回）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { event_id: string }
      const claimed = await client.claimSignal(a.event_id)
      if (claimed === null) {
        return '[signal_claim 失败：SGME Gateway 不可达，稍后重试]'
      }
      return claimed
        ? `[signal_claim 认领成功：event_id=${a.event_id}，请主动关怀用户后调 signal_ack 回执]`
        : `[signal_claim 已被消费：event_id=${a.event_id}，跳过]`
    },
  })
}

/** 创建 signal_ack 工具（写消费回执）。 */
export function createSignalAckTool(client: SgmeClient) {
  return defineTool({
    name: 'signal_ack',
    description: [
      '写信号消费回执（claimed/acked/failed）。',
      '认领（signal_claim）并处理完信号后调用，报告处理结果（如「已转告用户」「检查正常」）。',
    ].join(' '),
    parameters: {
      event_id: {
        type: 'string',
        required: true,
        description: '信号 event_id',
      },
      status: {
        type: 'string',
        required: true,
        enum: ['claimed', 'acked', 'failed'],
        description: '回执状态',
      },
      result: {
        type: 'string',
        description: '处理结果摘要',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as {
        event_id: string
        status: 'claimed' | 'acked' | 'failed'
        result?: string
      }
      const ok = await client.ackSignal(a.event_id, a.status, a.result)
      return ok
        ? `[signal_ack 已回执：event_id=${a.event_id} status=${a.status}]`
        : '[signal_ack 失败]'
    },
  })
}

// ---------- 三池登记（T-86：对齐 MCP idea_add/demand_create/project_register） ----------

/** 创建 idea_add 工具（创意池：用户主动提出才记录）。 */
export function createIdeaAddTool(client: SgmeClient) {
  return defineTool({
    name: 'idea_add',
    description: [
      '添加创意到 SGME 创意池（仅当用户主动提出创意时才记录——不要自行发散）。',
      '创意长期保存（无 TTL）；删除/升格由用户在 WebUI 操作。',
    ].join(' '),
    parameters: {
      content: {
        type: 'string',
        required: true,
        description: '创意内容（一句话概括）',
      },
      priority: {
        type: 'number',
        description: '优先级 0-100（默认 50）',
      },
      source_ref: {
        type: 'string',
        description: '溯源标识（可选，如会话主题）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { content: string; priority?: number; source_ref?: string }
      const resp = await client.ideaAdd({
        content: a.content,
        priority: a.priority ?? null,
        source_ref: a.source_ref ?? null,
      })
      if (!resp) {
        return '[idea_add 失败：SGME Gateway 不可达或无 Admin Key，稍后重试]'
      }
      const id = String((resp.idea as Record<string, unknown>)?.memory_id ?? '')
      return `[idea_add 已登记${id ? `：memory_id=${id}` : ''}（创意池，长期保存）]`
    },
  })
}

/** 创建 demand_create 工具（待办池：跨项目统一待办，agent 主动登记）。 */
export function createDemandCreateTool(client: SgmeClient) {
  return defineTool({
    name: 'demand_create',
    description: [
      '登记待办到 SGME 待办池（跨项目统一待办——不管哪个项目的事都收进来）。',
      '会话中遇到用户要办的事/项目任务/后续跟进事项，主动调用本工具登记，不要只留在对话里。',
      'project_id 是自由标记（未登记项目也允许）；完成时由用户在 WebUI 或后续操作标 done。',
    ].join(' '),
    parameters: {
      title: {
        type: 'string',
        required: true,
        description: '待办标题（一句概括）',
      },
      content: {
        type: 'string',
        description: '详情（可选）',
      },
      priority: {
        type: 'number',
        description: '优先级 0-100（默认 50）',
      },
      project_id: {
        type: 'string',
        description: '关联项目 id（自由标记，可选）',
      },
      source_ref: {
        type: 'string',
        description: '溯源标识（可选）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as {
        title: string; content?: string; priority?: number; project_id?: string; source_ref?: string
      }
      const resp = await client.demandCreate({
        title: a.title,
        content: a.content ?? null,
        priority: a.priority ?? null,
        project_id: a.project_id ?? null,
        source_ref: a.source_ref ?? null,
      })
      if (!resp) {
        return '[demand_create 失败：SGME Gateway 不可达或无 Admin Key，稍后重试]'
      }
      const warn = resp.warnings && resp.warnings.length > 0 ? `（警告：${resp.warnings.join('；')}）` : ''
      return `[demand_create 已登记：demand_id=${resp.demand_id} status=${resp.status}${warn}]`
    },
  })
}

/** 创建 project_register 工具（项目池：用户主动立项才登记）。 */
export function createProjectRegisterTool(client: SgmeClient) {
  return defineTool({
    name: 'project_register',
    description: [
      '登记/创建项目到 SGME 项目池（仅当用户主动提出立项/创建时调用；upsert，二次登记=更新）。',
      'project_id 用纯英文；新建时 path 必填。',
    ].join(' '),
    parameters: {
      project_id: {
        type: 'string',
        required: true,
        description: '项目 id（纯英文，如 sgme）',
      },
      path: {
        type: 'string',
        description: '项目本地路径（新建时必填）',
      },
      name: {
        type: 'string',
        description: '项目显示名（可选）',
      },
      git_repo: {
        type: 'string',
        description: 'git 仓库地址（可选）',
      },
      milestone: {
        type: 'string',
        description: '当前里程碑（可选）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as {
        project_id: string; path?: string; name?: string; git_repo?: string; milestone?: string
      }
      const resp = await client.projectRegister({
        project_id: a.project_id,
        path: a.path ?? null,
        name: a.name ?? null,
        git_repo: a.git_repo ?? null,
        milestone: a.milestone ?? null,
      })
      if (!resp) {
        return '[project_register 失败：SGME Gateway 不可达或无 Admin Key，稍后重试]'
      }
      return `[project_register 已登记：project_id=${resp.project_id}]`
    },
  })
}

// ---------- 角色模板（T-86：对齐 MCP role_* 四工具，换皮不换芯） ----------

/** 创建 role_list 工具（列出可用角色）。 */
export function createRoleListTool(client: SgmeClient) {
  return defineTool({
    name: 'role_list',
    description: [
      '列出 SGME 可用角色模板（管家/伴侣/朋友/导师，含人设摘要）。',
      '会话开始（或用户指定角色）时调用；选定后调 role_assemble 拿人设——换皮不换芯，记忆池不动。',
    ].join(' '),
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(_args, _exec) {
      const resp = await client.roleList()
      if (!resp) {
        return '[role_list 失败：SGME Gateway 不可达，稍后重试]'
      }
      if (resp.roles.length === 0) {
        return '[role_list 无可用角色]'
      }
      const active = await client.roleActiveGet()
      const activeId = active?.role_id ?? null
      const lines = resp.roles.map((r, i) => {
        const mark = r.role_id === activeId ? ' ←当前' : ''
        const desc = r.description ? ` — ${r.description}` : ''
        return `${i + 1}. ${r.name}（${r.role_id}）${mark}${desc}`
      })
      return `共 ${resp.total} 个角色${activeId ? `（当前：${activeId}）` : '（未设置）'}：\n` + lines.join('\n')
    },
  })
}

/** 创建 role_assemble 工具（装配角色沟通提示词）。 */
export function createRoleAssembleTool(client: SgmeClient) {
  return defineTool({
    name: 'role_assemble',
    description: [
      '装配角色沟通提示词（角色卡 system_prompt + persona + 关怀策略 + 可选画像）。',
      'role_id 来自 role_list；产物直接作为 system prompt 风格指引使用——按角色语气说话，但记忆与事实以记忆池为准。',
    ].join(' '),
    parameters: {
      role_id: {
        type: 'string',
        required: true,
        description: '角色 id（role_list 返回）',
      },
      inject_mode: {
        type: 'string',
        description: '画像注入模式（可选：daily/full/coding/work；省略不带画像）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { role_id: string; inject_mode?: string }
      const resp = await client.roleAssemble(a.role_id, a.inject_mode ?? null)
      if (!resp) {
        return `[role_assemble 失败：角色不存在或 Gateway 不可达（role_id="${a.role_id}"，先 role_list 确认）]`
      }
      return JSON.stringify(resp, null, 2)
    },
  })
}

/** 创建 role_active_get 工具（读取当前角色）。 */
export function createRoleActiveGetTool(client: SgmeClient) {
  return defineTool({
    name: 'role_active_get',
    description: '读取当前沟通角色（未设置返回 role_id=null）。',
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(_args, _exec) {
      const resp = await client.roleActiveGet()
      if (!resp) {
        return '[role_active_get 失败：SGME Gateway 不可达，稍后重试]'
      }
      return resp.role_id
        ? `[当前角色：${resp.role_id}${resp.status ? `（${resp.status}）` : ''}]`
        : '[未设置沟通角色]'
    },
  })
}

/** 创建 role_active_set 工具（设置当前角色）。 */
export function createRoleActiveSetTool(client: SgmeClient) {
  return defineTool({
    name: 'role_active_set',
    description: [
      '设置当前沟通角色（换皮不换芯：只换沟通外皮，记忆池不动）。',
      'role_id 必须存在（role_list 可见）；用户要求切换角色时调用。',
    ].join(' '),
    parameters: {
      role_id: {
        type: 'string',
        required: true,
        description: '角色 id（role_list 返回）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { role_id: string }
      const resp = await client.roleActiveSet(a.role_id)
      if (!resp) {
        return `[role_active_set 失败：角色不存在或 Gateway 不可达（role_id="${a.role_id}"）]`
      }
      return `[已切换角色：${resp.role_id}]`
    },
  })
}

// ---------- 记忆纠错（T-86：对齐 MCP memory_get/memory_reject） ----------

/** 创建 memory_get 工具（单条记忆详情）。 */
export function createMemoryGetTool(client: SgmeClient) {
  return defineTool({
    name: 'memory_get',
    description: [
      '查询单条 SGME 记忆详情（内容/维度/状态 + 溯源 + 归档链）。',
      'memory_id 来自 memory_search 结果；用于核实记忆准确性。',
    ].join(' '),
    parameters: {
      memory_id: {
        type: 'string',
        required: true,
        description: '记忆 id（memory_search 返回）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { memory_id: string }
      const resp = await client.memoryGet(a.memory_id)
      if (!resp) {
        return `[memory_get 失败：记忆不存在或 Gateway 不可达（memory_id="${a.memory_id}"）]`
      }
      return JSON.stringify(resp, null, 2)
    },
  })
}

/** 创建 memory_reject 工具（标记记忆不采用）。 */
export function createMemoryRejectTool(client: SgmeClient) {
  return defineTool({
    name: 'memory_reject',
    description: [
      '标记记忆「不采用」（用户发现记忆有误时调用；不删除、可恢复，之后不再注入/检索）。',
      '需带纠错理由；幂等（重复调用更新理由）。',
    ].join(' '),
    parameters: {
      memory_id: {
        type: 'string',
        required: true,
        description: '记忆 id（memory_search / memory_get 返回）',
      },
      reason: {
        type: 'string',
        description: '纠错理由（用户说明的错误原因）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { memory_id: string; reason?: string }
      const resp = await client.memoryReject(a.memory_id, a.reason ?? null)
      if (!resp) {
        return `[memory_reject 失败：记忆不存在或 Gateway 不可达（memory_id="${a.memory_id}"）]`
      }
      return `[memory_reject 已标记不采用：memory_id=${resp.memory_id}（理由：${resp.reject_reason ?? '用户纠错'}）]`
    },
  })
}
