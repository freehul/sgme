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
        description: '维度过滤（注册表 id，如 identity/projects/status/focus/tasks/goals/ideas）',
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
 * 创建 wiki_search 工具（检索 L2 知识库）。
 *
 * 差异化能力：dsh-mnemon 只有记忆检索，SGME 额外提供场景化知识库。
 */
export function createWikiSearchTool(client: SgmeClient, defaultLimit: number) {
  return defineTool({
    name: 'wiki_search',
    description: [
      '检索 SGME 知识库（L2 场景 + wiki_pages）。',
      '用于查询已经过 L1.5 冲突提炼的结构化场景知识，比记忆池更精炼。',
      '与 memory_search 互补：memory 是原始记忆，wiki 是提炼后的场景。',
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
      const resp = await client.search({
        query: a.query,
        scopes: ['wiki', 'wiki_pages'],
        limit: a.limit ?? defaultLimit,
      })
      if (!resp) {
        return '[wiki_search 失败：SGME Gateway 不可达，稍后重试]'
      }
      if (resp.results.length === 0) {
        return `[wiki_search 无结果：query="${a.query}"]`
      }
      return formatSearchResults(resp.results)
    },
  })
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
 * 向 dsh ctx 注册全部工具（检索 + 信号消费）。
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
  ctx.tools.register(createSignalPullTool(client))
  ctx.tools.register(createSignalClaimTool(client))
  ctx.tools.register(createSignalAckTool(client))
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
