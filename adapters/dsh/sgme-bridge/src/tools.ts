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
import { buildInjectionText } from './context.js'
import type { SgmeClient } from './sgme-client.js'
import type { SgmeEventSubscriber } from './events.js'

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
    // skills 层无 content（只有 name/description），故为可选——必须兜底后再取 .length，
    // 否则 TypeError 崩整个工具调用（2026-08-29 修复）。
    content?: string
    title?: string
    name?: string
    description?: string
    category?: string | null
    routes?: string[]
  }>,
): string {
  const lines: string[] = []
  for (const r of results) {
    const titlePrefix = r.title ? `「${r.title}」` : ''
    const routes = r.routes && r.routes.length > 0 ? ` [${r.routes.join(',')}]` : ''
    // 内容兜底：skills 层无 content，退到 name + description（先给技能名，否则模型无从调用）
    const raw =
      r.content ??
      (r.name ? (r.description ? `${r.name} — ${r.description}` : r.name) : (r.description ?? ''))
    // 内容截断（避免超长结果撑爆上下文）
    const content = raw.length > 500 ? raw.slice(0, 500) + '…' : raw
    lines.push(`## ${r.rank}. [${r.source}]${titlePrefix}${routes}\n${content}`)
  }
  return lines.join('\n\n')
}

/**
 * 向 dsh ctx 注册全部工具（检索 + 信号消费 + 三池登记 + 角色 + 记忆纠错 + 技能层 + 运维/写侧）。
 *
 * 调用方：index.ts apply() 内调用，传入 ctx 和 client。
 */
export function registerTools(
  ctx: { tools: { register: (tool: ReturnType<typeof defineTool>) => () => void } },
  client: SgmeClient,
  defaultLimit: number,
  eventSubscriber?: SgmeEventSubscriber | null,
): void {
  ctx.tools.register(createMemorySearchTool(client, defaultLimit))
  ctx.tools.register(createWikiSearchTool(client, defaultLimit))
  ctx.tools.register(createWikiPagesTool(client, defaultLimit))
  ctx.tools.register(createWikiPageTool(client))
  ctx.tools.register(createWikiPageUpdateTool(client))
  ctx.tools.register(createWikiPageAddTool(client))
  ctx.tools.register(createInjectTool(client))
  ctx.tools.register(createSignalPullTool(client))
  ctx.tools.register(createSignalClaimTool(client, eventSubscriber ?? null))
  ctx.tools.register(createSignalAckTool(client, eventSubscriber ?? null))
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
  // 技能层（ST-36 读侧：检索 → 摘要审核 → 全文注入 / 列表 / 冷启动包）
  ctx.tools.register(createSkillSearchTool(client, defaultLimit))
  ctx.tools.register(createSkillDigestTool(client))
  ctx.tools.register(createSkillGetTool(client))
  ctx.tools.register(createSkillListTool(client, defaultLimit))
  ctx.tools.register(createSkillColdstartTool(client))
  // SGME 1.2.2 对齐（T-166+）：聚合答案 / 健康 / 统计 / 信号清空 / 配置 / 提炼 / 技能写侧
  ctx.tools.register(createAnswerTool(client))
  ctx.tools.register(createHealthTool(client))
  ctx.tools.register(createStatsTool(client))
  ctx.tools.register(createMemoryUnrejectTool(client))
  ctx.tools.register(createSignalClearTool(client))
  ctx.tools.register(createWikiEvolveTriggerTool(client))
  ctx.tools.register(createConfigGetTool(client))
  ctx.tools.register(createConfigUpdateTool(client))
  ctx.tools.register(createRefineStatusTool(client))
  ctx.tools.register(createRefineTriggerTool(client))
  ctx.tools.register(createRefineBatchTool(client))
  ctx.tools.register(createSkillMaterializeTool(client))
  ctx.tools.register(createSkillPutTool(client))
  ctx.tools.register(createSkillDeleteTool(client))
  ctx.tools.register(createSkillRenameTool(client))
}

// ---------- 画像注入（T-88：agent 可主动按场景切换注入） ----------

/** 创建 inject 工具（按场景模式拉取 SGME 画像，agent 主动注入）。 */
export function createInjectTool(client: SgmeClient) {
  return defineTool({
    name: 'inject',
    description: [
      '按场景模式拉取 SGME 画像注入（POST /v1/inject，Agent Key）。',
      '场景模式：daily 日常画像 / coding 编码（技术栈/踩坑/工作方式）/ work 工作（目标/关系）/ full 全量。',
      '低频使用：切换模式会使当轮 DeepSeek 前缀缓存失效（该轮历史输入按未命中计费，约 ¥0.22/次），同一会话内请勿反复切换。',
    ].join(' '),
    parameters: {
      mode: {
        type: 'string',
        required: true,
        enum: ['daily', 'full', 'coding', 'work'],
        description: '画像注入模式模板名',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { mode: string }
      const profile = await client.inject({ mode: a.mode })
      if (!profile) {
        return '[inject 失败：SGME Gateway 不可达或模式无效，稍后重试]'
      }
      return buildInjectionText(profile, null)
    },
  })
}

// ---------- 技能层（ST-36 四级披露读侧，对齐 MCP skill_* 工具） ----------

/**
 * 创建 skill_search 工具（技能检索，先搜后取的第一步）。
 *
 * 走 POST /v1/search scope=["skills"]（HTTP 侧唯一入口；服务端无 /v1/skills/search）。
 * ⚠️ 只返回 name/description/category，**不含正文**——拿到 name 后必须再调
 * skill_digest 审核或直接 skill_get 拉全文，不要把 description 当技能内容用。
 */
export function createSkillSearchTool(client: SgmeClient, defaultLimit: number) {
  return defineTool({
    name: 'skill_search',
    description: [
      '检索 SGME 技能库（BM25+向量融合，全量技能）。需要某项专业能力但不确定 SGME 有没有时必用。',
      '只返回技能名与触发描述，**不含正文**——选定后调 skill_get 拉全文再执行。',
      '范式（SGME 1.1.0）：技能不预载，按需检索→拉全文→注入，不要凭空编造操作步骤。',
    ].join(' '),
    parameters: {
      query: {
        type: 'string',
        required: true,
        description: '检索关键词或能力描述（如 "docker 部署"、"pdf 提取"）',
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
      const a = args as unknown as { query: string; limit?: number }
      const hits = await client.skillSearch(a.query, a.limit ?? defaultLimit)
      if (!hits) {
        return '[skill_search 失败：SGME Gateway 不可达或技能模块未启用，稍后重试]'
      }
      if (hits.length === 0) {
        return `[skill_search 无结果：query="${a.query}"（可换更宽泛的关键词重试）]`
      }
      const lines = hits.map(
        (s, i) => `## ${i + 1}. ${s.name}${s.category ? ` [${s.category}]` : ''}\n${s.description}`,
      )
      return lines.join('\n\n') + '\n\n（调 skill_get 传 name 拉全文后执行）'
    },
  })
}

/** 创建 skill_digest 工具（L1 摘要：字段 + 正文骨架 + uses 依赖，审核用）。 */
export function createSkillDigestTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_digest',
    description: [
      '查看技能摘要（L1）：frontmatter 字段 + 正文小节骨架 + uses 依赖清单。',
      '用于执行前审核——先看骨架判断是否对症、有没有依赖要一并拉，再决定要不要 skill_get 全文。',
    ].join(' '),
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '技能名（skill_search 返回，kebab-case）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { name: string }
      const d = await client.skillDigest(a.name)
      if (!d) {
        return `[skill_digest 失败：技能不存在或 Gateway 不可达（name="${a.name}"，先 skill_search 确认）]`
      }
      const deps = d.uses.length > 0 ? `\n依赖 uses: ${d.uses.join(', ')}` : ''
      const sections = d.sections.length > 0 ? `\n正文骨架:\n${d.sections.map((s) => '  ' + s).join('\n')}` : ''
      return [
        `# ${d.name}${d.category ? ` [${d.category}]` : ''}${d.version ? ` v${d.version}` : ''}`,
        d.description,
        deps,
        sections,
      ]
        .filter(Boolean)
        .join('\n')
    },
  })
}

/** 创建 skill_get 工具（L2 全文：显式注入上下文；section 可只取一节省 token）。 */
export function createSkillGetTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_get',
    description: [
      '拉取技能全文（L2）并注入上下文——拿到后按其步骤执行，不要凭空编造。',
      '正文较长时传 section 只取该标题下的内容，省 token（节名不对会 404，先 skill_digest 看骨架确认）。',
    ].join(' '),
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '技能名（skill_search / skill_digest 返回）',
      },
      section: {
        type: 'string',
        description: '只取该小节，传纯标题如 "前置条件"；带 # 前缀的骨架行会自动剥离，两种写法都行',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { name: string; section?: string }
      const d = await client.skillGet(a.name, a.section ?? null)
      if (!d) {
        return `[skill_get 失败：技能不存在或 Gateway 不可达（name="${a.name}"，先 skill_search 确认）]`
      }
      const tag = d.truncated_by_section ? `（已按 section="${d.section}" 截取）` : ''
      return `<!-- skill: ${d.name}${tag} -->\n${d.content}`
    },
  })
}

/** 创建 skill_list 工具（L0 索引列表，分页浏览全量）。 */
export function createSkillListTool(client: SgmeClient, defaultLimit: number) {
  return defineTool({
    name: 'skill_list',
    description: [
      '列出 SGME 技能库索引（L0：name/description/category，分页浏览全量）。',
      '不确定有没有某个技能时用 skill_search 检索更精准；本工具适合浏览摸底。',
    ].join(' '),
    parameters: {
      limit: {
        type: 'number',
        description: `返回条数上限（默认 ${defaultLimit}）`,
      },
      offset: {
        type: 'number',
        description: '分页偏移（默认 0）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { limit?: number; offset?: number }
      const resp = await client.skillList(a.limit ?? defaultLimit, a.offset ?? 0)
      if (!resp) {
        return '[skill_list 失败：SGME Gateway 不可达或技能模块未启用，稍后重试]'
      }
      if (resp.skills.length === 0) {
        return `[skill_list 无技能（offset=${resp.offset}）]`
      }
      const lines = resp.skills.map(
        (s) => `- ${s.name}${s.category ? ` [${s.category}]` : ''} — ${s.description.slice(0, 120)}`,
      )
      return [
        `技能库共 ${resp.total} 个，本次返回 ${resp.returned} 个（offset=${resp.offset}${resp.budget ? `，默认窗口 ${resp.budget}` : ''}）`,
        ...lines,
      ].join('\n')
    },
  })
}

/**
 * 创建 skill_coldstart 工具（冷启动包）。
 *
 * SGME 1.1.0 范式：只返回 1 个《技能检索协议》+ SGME 操作手册，**全量技能不预载**。
 * 会话开始调一次即知道「要用技能时先检索、再拉全文」的正确姿势。
 */
export function createSkillColdstartTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_coldstart',
    description: [
      '拉取技能冷启动包（SGME 1.1.0 范式）——仅注入 1 个《技能检索协议》+ SGME 操作手册。',
      '会话开始调一次：协议教你怎么按需检索技能，操作手册讲 SGME 自身怎么用。',
      '全量技能不预载，需要时用 skill_search 检索、skill_get 拉全文。',
    ].join(' '),
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(_args, _exec) {
      const resp = await client.skillColdstart()
      if (!resp) {
        return '[skill_coldstart 失败：SGME Gateway 不可达或技能模块未启用，稍后重试]'
      }
      const parts: string[] = []
      const items = resp.index?.items ?? []
      for (const s of items) {
        // 冷启动项带 content 全文（服务端已烘焙），直接注入
        const body = (s as { content?: string }).content ?? s.description
        parts.push(`<!-- skill: ${s.name} -->\n${body}`)
      }
      if (resp.manual?.content) {
        parts.push(`<!-- sgme-manual: ${resp.manual.title} -->\n${resp.manual.content}`)
      }
      if (parts.length === 0) return '[skill_coldstart 冷启动包为空]'
      const hotNote =
        resp.hotset && resp.hotset.length > 0
          ? `\n\n（常驻热集：${resp.hotset.map((s) => s.name).join(', ')}）`
          : ''
      return parts.join('\n\n') + hotNote
    },
  })
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
export function createSignalClaimTool(client: SgmeClient, eventSubscriber: SgmeEventSubscriber | null) {
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
      // 2026-08-18 修复（兜底铁律）：认领或已被消费都同步本地队列，
      // 防「提醒反复注入但 signal_pull 为空」死循环（此前 care 被静默消费后队列永不标记）
      eventSubscriber?.markConsumed([a.event_id])
      return claimed
        ? `[signal_claim 认领成功：event_id=${a.event_id}，请主动关怀用户后调 signal_ack 回执]`
        : `[signal_claim 已被消费：event_id=${a.event_id}，跳过]`
    },
  })
}

/** 创建 signal_ack 工具（写消费回执）。 */
export function createSignalAckTool(client: SgmeClient, eventSubscriber: SgmeEventSubscriber | null) {
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
      // 2026-08-18 修复：回执成功后同步本地队列，防重复提醒
      if (ok) eventSubscriber?.markConsumed([a.event_id])
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
      'project_id 用纯英文且一律大写；新建时 path 必填。',
    ].join(' '),
    parameters: {
      project_id: {
        type: 'string',
        required: true,
        description: '项目 id（纯英文，一律大写，如 DHVS）',
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

// ---------- 聚合答案 / 健康 / 统计 / 记忆撤销（SGME 1.2.2 对齐） ----------

/**
 * 创建 answer 工具（T-149 聚合答案：跨会话计数/列举/时序推理）。
 *
 * 与 memory_search 的分工：search 返回候选条目让模型自己读；answer 多走一步
 * LLM 答案合成，适合「我一共提过几次 X」「什么时候改的 Y」这类聚合问题。
 */
export function createAnswerTool(client: SgmeClient) {
  return defineTool({
    name: 'answer',
    description: [
      '向 SGME 提聚合型问题并直接拿答案（跨会话计数/列举/时序推理）。',
      '适用：「我一共提过几次 X」「Y 是什么时候改的」「列出所有做过 Z 的项目」。',
      '比 memory_search 多一步 LLM 答案合成——纯检索用 memory_search，要结论用本工具。',
      '会消耗一次 LLM 调用；LLM 全链不可用时返回失败提示。',
    ].join(' '),
    parameters: {
      query: {
        type: 'string',
        required: true,
        description: '自然语言问题',
      },
      question_type: {
        type: 'string',
        enum: ['temporal', 'aggregate', 'generic'],
        description: '题型（temporal=时序 / aggregate=计数列举 / generic=通用；省略自动分派）',
      },
      limit: {
        type: 'number',
        description: '检索候选条数（默认 8；服务端上限 20）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { query: string; question_type?: string; limit?: number }
      const resp = await client.answer(a.query, a.question_type ?? null, a.limit)
      if (!resp) {
        return '[answer 失败：SGME Gateway 不可达、LLM 全链不可用，或模块未启用，稍后重试]'
      }
      const meta = [
        resp.question_type ? `题型=${resp.question_type}` : '',
        resp.candidates_used !== undefined ? `候选=${resp.candidates_used}` : '',
        resp.provider ? `模型=${resp.provider}` : '',
      ].filter(Boolean).join(' ')
      const evidence = Array.isArray(resp.evidence) && resp.evidence.length > 0
        ? `\n\n依据（${resp.evidence.length} 条）：\n` +
          resp.evidence
            .slice(0, 5)
            .map((e, i) => {
              const c = (e as { content?: string }).content
              return `${i + 1}. ${c ? String(c).slice(0, 200) : JSON.stringify(e).slice(0, 200)}`
            })
            .join('\n')
        : ''
      return `${resp.answer ?? '(服务端未返回答案)'}${meta ? `\n\n[${meta}]` : ''}${evidence}`
    },
  })
}

/** 创建 health 工具（连接/版本/LLM/提炼水位/向量水位自检）。 */
export function createHealthTool(client: SgmeClient) {
  return defineTool({
    name: 'health',
    description: [
      'SGME 健康自检：服务版本、LLM 可用性、提炼水位与是否停摆、向量库水位。',
      '排查「记忆检索没结果」「刚说的话没进记忆」时先跑本工具定位是哪一环断了。',
    ].join(' '),
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(_args, _exec) {
      const h = await client.health()
      if (!h) {
        return '[health 失败：SGME Gateway 不可达——本插件是桥接插件，请确认 SGME 本体在运行]'
      }
      const lines = [
        `status=${h.status} version=${h.version ?? '?'}`,
        `LLM: ${h.llm?.available ? '可用' : '不可用'}（${h.llm?.provider ?? '?'}/${h.llm?.model ?? '?'}）`,
        `提炼: 水位 ${h.refinement?.watermark_age_sec ?? '?'}s 队列 ${h.refinement?.queue_depth ?? '?'}` +
          ` ${h.refinement?.stalled ? '⚠️ 疑似停摆' : '正常'}`,
        `向量: ${h.vector?.available ? '可用' : '不可用'}` +
          `（记忆 ${h.vector?.memory_vectors ?? '?'} / 场景 ${h.vector?.scene_vectors ?? '?'}）`,
      ]
      return lines.join('\n')
    },
  })
}

/** 创建 stats 工具（记忆/原始层计数 + 维度分布 + 水位 + 注册 Agent）。 */
export function createStatsTool(client: SgmeClient) {
  return defineTool({
    name: 'stats',
    description: [
      'SGME 统计概览：记忆总数/归档数、原始文件各状态计数、维度分布、提炼水位、已注册 agent。',
      '用户问「记忆库现在多少条」「哪些维度用得最多」时用；需 Admin Key。',
    ].join(' '),
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(_args, _exec) {
      const s = await client.stats()
      if (!s) {
        return '[stats 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]'
      }
      const dims = Object.entries(s.dimension_distribution ?? {})
        .sort((a, b) => Number(b[1]) - Number(a[1]))
        .slice(0, 10)
        .map(([k, v]) => `${k}=${v}`)
        .join(', ')
      return [
        `记忆: ${s.memories?.total ?? '?'} 条（归档 ${s.memories?.archived ?? '?'}）`,
        `原始文件: 共 ${s.raw_files?.total ?? '?'}（new ${s.raw_files?.new ?? '?'} / refined ${s.raw_files?.refined ?? '?'} / error ${s.raw_files?.error ?? '?'}）`,
        `提炼水位: ${s.refinement?.last_refined_at ?? '?'}（${s.refinement?.watermark_age_sec ?? '?'}s 前，队列 ${s.refinement?.queue_depth ?? '?'}）`,
        `维度分布: ${dims || '-'}`,
        `已注册 agent: ${(s.agents ?? []).map((a) => `${a.agent_id}(${a.role})`).join(', ') || '-'}`,
      ].join('\n')
    },
  })
}

/** 创建 memory_unreject 工具（撤销「不采用」，T-163 补齐）。 */
export function createMemoryUnrejectTool(client: SgmeClient) {
  return defineTool({
    name: 'memory_unreject',
    description: [
      '撤销记忆的「不采用」标记，恢复为 active（重新参与注入与检索）。',
      '用于 memory_reject 误操作后的恢复；memory_id 来自 memory_search 结果。',
    ].join(' '),
    parameters: {
      memory_id: {
        type: 'string',
        required: true,
        description: '记忆 id（memory_search / memory_get 返回）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { memory_id: string }
      const resp = await client.memoryUnreject(a.memory_id)
      if (!resp) {
        return `[memory_unreject 失败：记忆不存在或 Gateway 不可达（memory_id="${a.memory_id}"）]`
      }
      return `[memory_unreject 已恢复：memory_id=${resp.memory_id} status=${resp.status}]`
    },
  })
}

// ---------- 信号清空 / 自进化 / 配置 / 提炼（运维侧） ----------

/** 创建 signal_clear 工具（批量清空未消费信号，T-87）。 */
export function createSignalClearTool(client: SgmeClient) {
  return defineTool({
    name: 'signal_clear',
    description: [
      '批量清空 SGME 未消费信号（全部标记已消费，幂等；二次调用 consumed=0）。',
      '用于信号堆积（如历史 anomaly_warn/memory_updated）时的一次性清理。',
      '⚠️ 清空后 pull/SSE 不再推送这些信号——仅在用户明确要求清理时调用。需 Admin Key。',
    ].join(' '),
    parameters: {
      signal_type: {
        type: 'string',
        description: '只清空该类型（如 anomaly_warn / care_daily）；省略=全部类型',
      },
      subscriber_id: {
        type: 'string',
        description: '同步推进该订阅者的持久游标（如 dsh）；省略则不推进',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { signal_type?: string; subscriber_id?: string }
      const resp = await client.signalClear(a.signal_type ?? null, a.subscriber_id ?? null)
      if (!resp) {
        return '[signal_clear 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]'
      }
      return `[signal_clear 已完成：消费 ${resp.consumed} 条（type=${resp.type ?? '全部'}，subscriber=${resp.subscriber_id ?? '-'}）]`
    },
  })
}

/**
 * 创建 wiki_evolve_trigger 工具（自进化 W4）。
 *
 * session-sync 在 turn/end 后已自动触发（evolveEnabled 默认 true）；
 * 本工具用于手动补触发——例如某轮没触发到、或想对指定会话立即提炼经验。
 */
export function createWikiEvolveTriggerTool(client: SgmeClient) {
  return defineTool({
    name: 'wiki_evolve_trigger',
    description: [
      '手动触发 SGME 自进化（会话经验 → 写回 wiki 手册）。',
      '插件每轮结束已自动触发，本工具用于手动补触发（如指定某会话立即提炼）。',
      '服务端有费用门禁与规则闸门兜底（消息块不足会跳过），但仍会计入 LLM 调用。',
    ].join(' '),
    parameters: {
      session_key: {
        type: 'string',
        description: '指定会话（如 dsh-<会话id>）；省略则由服务端按游标处理',
      },
      min_rounds: {
        type: 'number',
        description: '费用门禁：会话消息块下限（默认 5）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { session_key?: string; min_rounds?: number }
      const resp = await client.evolveTrigger(a.session_key ?? null, a.min_rounds ?? 5)
      if (!resp) {
        return '[wiki_evolve_trigger 失败：SGME Gateway 不可达或自进化模块未启用，稍后重试]'
      }
      return `[wiki_evolve_trigger 已触发：status=${resp.status}]`
    },
  })
}

/** 创建 config_get 工具（读服务端运行时配置）。 */
export function createConfigGetTool(client: SgmeClient) {
  return defineTool({
    name: 'config_get',
    description: [
      '读取 SGME 服务端运行时配置（整体读或按段读：l1/l2/refine/search/backup 等）。',
      '用于核实服务端实际生效的配置值（如提炼开关、检索参数）。需 Admin Key。',
    ].join(' '),
    parameters: {
      section: {
        type: 'string',
        description: '配置段名（l1/l2/refine/search/backup）；省略返回全部配置',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { section?: string }
      const resp = await client.configGet(a.section ?? null)
      if (!resp) {
        return `[config_get 失败：SGME Gateway 不可达、未配置 Admin Key，或段名不存在${a.section ? `（section="${a.section}"）` : ''}]`
      }
      const writable = resp.writable_sections?.length
        ? `\n\n可写段：${resp.writable_sections.join(', ')}`
        : ''
      return JSON.stringify(resp.config ?? resp, null, 2) + writable
    },
  })
}

/**
 * 创建 config_update 工具（改服务端运行时配置）。
 *
 * ⚠️ 破坏面最大的工具：热生效 + 落盘，改错会直接改变记忆引擎的运行行为。
 * 描述里显式加护栏，且要求 section 必填（避免整段误覆盖）。
 */
export function createConfigUpdateTool(client: SgmeClient) {
  return defineTool({
    name: 'config_update',
    description: [
      '更新 SGME 服务端配置段（部分更新，合并后落盘并热生效）。',
      '⚠️ 会改变记忆引擎的实际运行行为（如提炼开关、检索参数）且立即生效。',
      '仅在用户明确要求修改服务端配置时调用；不确定当前值先用 config_get 读。需 Admin Key。',
    ].join(' '),
    parameters: {
      section: {
        type: 'string',
        required: true,
        description: '要更新的配置段名（l1/l2/refine/search/backup）',
      },
      values: {
        type: 'object',
        required: true,
        additionalProperties: true,
        description: '该段的键值对（只传要改的键，未传的保留）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { section: string; values: Record<string, unknown> }
      const resp = await client.configUpdate(a.section, a.values ?? {})
      if (!resp) {
        return `[config_update 失败：SGME Gateway 不可达、未配置 Admin Key，或段名/取值非法（section="${a.section}"）]`
      }
      return `[config_update 已生效：section=${resp.section ?? a.section} status=${resp.status}]`
    },
  })
}

/**
 * 创建 refine_status 工具（提炼监控）。
 *
 * 服务端 refine_status 只有 MCP 侧（无 HTTP 端点），故此处以
 * GET /v1/admin/refine_runs + 提炼水位组合近似——结论等价，
 * 待服务端补 GET /v1/admin/refine/status 后可收敛为单次调用（已登记待办）。
 */
export function createRefineStatusTool(client: SgmeClient) {
  return defineTool({
    name: 'refine_status',
    description: [
      '查看 SGME 提炼状态：最近提炼批次记录（含 error/running）+ 待提炼队列与水位。',
      '用于排查「会话入库了但没变成记忆」——看队列是否堆积、最近批次是否报错。需 Admin Key。',
    ].join(' '),
    parameters: {
      limit: {
        type: 'number',
        description: '返回最近批次条数（默认 10）',
      },
      status: {
        type: 'string',
        enum: ['running', 'ok', 'error'],
        description: '只看该状态的批次（省略=全部，含 error/running）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { limit?: number; status?: string }
      const runs = await client.refineRuns({ limit: a.limit ?? 10, status: a.status ?? null })
      if (!runs) {
        return '[refine_status 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]'
      }
      const lines = (runs.items ?? []).map((r, i) => {
        const fileId = String((r as { file_id?: unknown }).file_id ?? '?')
        const status = String((r as { status?: unknown }).status ?? '?')
        const stage = String((r as { stage?: unknown }).stage ?? '-')
        const started = String((r as { started_at?: unknown }).started_at ?? '?')
        return `${i + 1}. [${status}] ${stage} ${fileId}（${started}）`
      })
      const head = `提炼记录：共 ${runs.total} 条，本次返回 ${runs.count} 条`
      return head + (lines.length > 0 ? '\n' + lines.join('\n') : '\n（无记录）')
    },
  })
}

/**
 * 创建 refine_trigger 工具（同步触发提炼）。
 *
 * ⚠️ 同步阻塞且真实消耗 LLM 额度：批量场景应走 refine_batch（异步排队即返）。
 */
export function createRefineTriggerTool(client: SgmeClient) {
  return defineTool({
    name: 'refine_trigger',
    description: [
      '同步触发提炼：指定 file_id 提炼单个会话原文，或扫 status=new 批量提炼。',
      '⚠️ 同步阻塞直到完成，且真实消耗 LLM 额度；批量任务优先用 refine_batch（异步）。',
      '仅在用户明确要求立即提炼时调用。需 Admin Key。',
    ].join(' '),
    parameters: {
      file_id: {
        type: 'string',
        description: '单个会话原文 id；省略则批量扫 status=new',
      },
      limit: {
        type: 'number',
        description: '批量上限（默认 100）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { file_id?: string; limit?: number }
      const resp = await client.refineTriggerSync(a.file_id ?? null, a.limit)
      if (!resp) {
        return `[refine_trigger 失败：SGME Gateway 不可达、未配置 Admin Key，或 file_id 不存在${a.file_id ? `（"${a.file_id}"）` : ''}]`
      }
      if (resp.triggered === 'file') {
        return `[refine_trigger 单文件完成：file_id=${resp.file_id} status=${resp.status ?? '?'} 记忆 ${resp.memories_count ?? '?'} 条${resp.error ? ` 错误=${resp.error}` : ''}]`
      }
      return `[refine_trigger 批量完成：处理 ${resp.processed ?? '?'} 个文件，共产出记忆 ${resp.total_memories ?? '?'} 条]`
    },
  })
}

/** 创建 refine_batch 工具（异步批量提炼，排队即返）。 */
export function createRefineBatchTool(client: SgmeClient) {
  return defineTool({
    name: 'refine_batch',
    description: [
      '异步批量提炼：后台线程执行，立即返回排队结果（不阻塞对话）。',
      '⚠️ 会真实消耗 LLM 额度；仅在用户明确要求补提炼时调用。失败由服务端批扫兜底。需 Admin Key。',
    ].join(' '),
    parameters: {
      file_id: {
        type: 'string',
        description: '只提炼该文件；省略则批量扫 status=new',
      },
      limit: {
        type: 'number',
        description: '批量上限（默认 100）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { file_id?: string; limit?: number }
      const resp = await client.triggerRefine({
        file_id: a.file_id ?? null,
        ...(a.limit !== undefined ? { limit: a.limit } : {}),
      })
      if (!resp) {
        return '[refine_batch 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]'
      }
      return `[refine_batch 已排队：status=${resp.status}（后台执行，可用 refine_status 查看进度）]`
    },
  })
}

// ---------- 技能 L3 物化 + 写侧（ST-36 M3） ----------

/** 创建 skill_materialize 工具（L3：字节保真落盘成真文件）。 */
export function createSkillMaterializeTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_materialize',
    description: [
      '把 SGME 技能物化成真文件：字节保真写盘 dest_dir/<name>/SKILL.md，返回路径与 sha256。',
      '⚠️ 落盘发生在 SGME **服务端**：dest_dir 是服务端可写路径、返回的 path 也是服务端路径。',
      'SGME 与 agent 同机部署时可直接读该文件；跨机（如 agent 在 PC、SGME 在 NAS 容器）时',
      'agent 本地拿不到产物，需两端共享挂载该目录才能访问——此时请改用 skill_get 取正文。',
    ].join(' '),
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '技能名（skill_search / skill_list 返回，kebab-case）',
      },
      dest_dir: {
        type: 'string',
        required: true,
        description: '目标目录（技能会写到 <dest_dir>/<name>/SKILL.md）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { name: string; dest_dir: string }
      const resp = await client.skillMaterialize(a.name, a.dest_dir)
      if (!resp) {
        return `[skill_materialize 失败：技能不存在、dest_dir 非法或 Gateway 不可达（name="${a.name}"）]`
      }
      return `[skill_materialize 已落盘（服务端路径）：${resp.path}\nsha256=${resp.sha256}]`
    },
  })
}

/**
 * 创建 skill_put 工具（写入/覆盖技能）。
 *
 * ⚠️ 服务端会走 lint 门禁 + 三层查重，通过后落盘并 git commit 到技能源仓——
 * 属写侧治理动作，护栏写进描述。
 */
export function createSkillPutTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_put',
    description: [
      '写入/覆盖 SGME 技能（content 传 SKILL.md 全文，服务端自动解析 frontmatter）。',
      '⚠️ 服务端过 lint 门禁 + 三层查重后落盘并提交技能源仓（同名同内容/同内容异名会 409 拒绝）。',
      '仅在用户明确要求沉淀技能时调用；写入前建议先 skill_search 查重。需 Admin Key。',
      '正文有 8K 上限（超限会被 lint 拦截，历史存量入库可传 skip_limits）。',
    ].join(' '),
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '技能名（kebab-case）',
      },
      content: {
        type: 'string',
        required: true,
        description: 'SKILL.md 全文（含 frontmatter）',
      },
      skip_limits: {
        type: 'boolean',
        description: '超限从拒绝降为警告（仅历史存量整体入库用，默认 false）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { name: string; content: string; skip_limits?: boolean }
      const resp = await client.skillPut(a.name, a.content, a.skip_limits ?? false)
      if (!resp) {
        return `[skill_put 失败：lint 门禁拦截 / 查重拒绝 / 未配置 Admin Key / Gateway 不可达（name="${a.name}"）]`
      }
      return `[skill_put 已写入：name=${a.name}（落盘并提交技能源仓）]`
    },
  })
}

/** 创建 skill_delete 工具（删除技能，默认软删）。 */
export function createSkillDeleteTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_delete',
    description: [
      '删除 SGME 技能：默认软删（标记 deprecated，可恢复）；hard=true 物理删目录。',
      '⚠️ 有入向 uses 引用时服务端会 409 拒绝，需 force=true 强制——属破坏性操作。',
      '仅在用户明确要求删除时才调用。需 Admin Key。',
    ].join(' '),
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '技能名',
      },
      hard: {
        type: 'boolean',
        description: '物理删除（默认 false=软删标记 deprecated）',
      },
      force: {
        type: 'boolean',
        description: '强制清理入向引用后删除（默认 false）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { name: string; hard?: boolean; force?: boolean }
      const resp = await client.skillDelete(a.name, a.hard ?? false, a.force ?? false)
      if (!resp) {
        return `[skill_delete 失败：技能不存在、存在入向引用且未 force、未配置 Admin Key 或 Gateway 不可达（name="${a.name}"）]`
      }
      return `[skill_delete 已完成：name=${a.name}（${a.hard ? '物理删除' : '软删 deprecated'}）]`
    },
  })
}

/** 创建 skill_rename 工具（墓碑制改名）。 */
export function createSkillRenameTool(client: SgmeClient) {
  return defineTool({
    name: 'skill_rename',
    description: [
      '技能改名（墓碑制：写新名副本 + 旧位置留 superseded_by 墓碑 + 登记 tombstones.json，永不原地改名）。',
      '⚠️ 需服务端 skills.source_dirs 指向 git 技能仓；新名已占用或过不了门禁会被拒。',
      '仅在用户明确要求改名时调用。需 Admin Key。',
    ].join(' '),
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '旧技能名',
      },
      new_name: {
        type: 'string',
        required: true,
        description: '新技能名（kebab-case）',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value as string }],
    },
    async execute(args, _exec) {
      const a = args as unknown as { name: string; new_name: string }
      const resp = await client.skillRename(a.name, a.new_name)
      if (!resp) {
        return `[skill_rename 失败：旧名不存在 / 新名被占用 / 未配置 source_dirs / Gateway 不可达（"${a.name}" → "${a.new_name}"）]`
      }
      return `[skill_rename 已完成：${a.name} → ${a.new_name}（旧位置留墓碑）]`
    },
  })
}
