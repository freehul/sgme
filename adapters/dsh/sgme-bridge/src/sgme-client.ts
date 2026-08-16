/**
 * sgme-client.ts — SGME Gateway HTTP 客户端
 *
 * 封装 4 个 SGME 端点调用，故障隔离（失败只 log + 返回 null，绝不抛异常阻塞 dsh 主循环）。
 *
 * 契约来源：sgme/server/routes_memory.py / routes_admin.py（2026-08-14 调研确认）
 * - POST /v1/search        — Agent Key — 记忆+wiki 检索
 * - POST /v1/inject         — Agent Key — 画像注入（注意：max_tokens 协议接受但不消费）
 * - POST /v1/append         — Agent Key — L0 写入
 * - POST /v1/admin/refine/trigger_async — Admin Key — 触发提炼（实际返回 200，非 202）
 */

/** SGME 客户端配置（由插件 Config 注入）。 */
export interface SgmeClientConfig {
  baseUrl: string
  agentKey: string
  adminKey: string
  agentId: string
  timeoutMs?: number
}

// ---------- 请求/响应类型（严格对齐 SGME Pydantic 模型） ----------

/** /v1/search 请求体（SearchRequest）。 */
export interface SearchRequest {
  query: string
  scopes?: string[]              // 默认 ["memory"]；可选值：memory/wiki/scenes/wiki_pages
  dimensions?: string[] | null   // 维度标签过滤，用注册表 id
  match?: 'any' | 'all'          // 维度匹配语义，默认 "any"
  limit?: number                 // 每层结果上限，默认 10
  include_sources?: boolean      // 是否展开溯源 trace，默认 true
}

/** /v1/search 响应体（http_payload 投影）。 */
export interface SearchResponse {
  results: SearchResult[]
  meta: {
    routes: string[]
    rrf_k: number
  }
}

export interface SearchResult {
  rank: number
  source: 'memory' | 'wiki' | 'scenes' | 'wiki_pages'
  content: string
  memory_id?: string             // memory 层独有
  page_id?: string               // wiki_pages 层独有
  title?: string                 // wiki/wiki_pages 层独有
  routes?: string[]
  trace?: Record<string, unknown>
}

/** /v1/inject 请求体（InjectRequest）。 */
export interface InjectRequest {
  mode?: string | null                       // 模板名（daily/coding/work/full）；与 custom_filter 二选一
  max_tokens?: number | null                 // 协议接受但不消费（服务端内部估算）
  custom_filter?: {
    dimensions?: string[]
    memory_types?: string[]
    match?: 'any' | 'all'
    limit?: number
  } | null
}

/** /v1/inject 响应体。 */
export interface InjectResponse {
  blocks: InjectBlock[]
  stats: {
    mode: string
    queries: number
    tokens_est: number
    tier0_present: boolean
    note?: string
  }
  tier0: {
    present: boolean
    content: string | null
  }
}

export interface InjectBlock {
  title: string
  items: Array<Record<string, unknown>>
  present?: boolean             // Tier0 块独有
}

/** /v1/append 请求体（AppendRequest）。 */
export interface AppendRequest {
  session_key: string           // 会话标识（幂等/追加锚点之一）
  started_at: string            // ISO 8601 起始时间（锚点之二）
  content: string               // 消息块文本，行首格式 "# {ISO} {role}" / "## {ISO} {role}"
  agent_id?: string | null      // 缺省时按鉴权 Key 反查兜底
  agent_model?: string | null
  ended_at?: string | null
  source_type?: string          // 默认 "session"
  metadata?: Record<string, unknown> | null
}

/** /v1/append 响应体（3 种形态联合：新建/幂等/追加）。 */
export interface AppendResponse {
  file_id: string
  path: string
  status: string
  idempotent?: boolean          // 幂等命中时 true
  appended?: boolean            // 追加段时 true
}

/** /v1/admin/refine/trigger_async 请求体（RefineTriggerRequest）。 */
export interface RefineTriggerRequest {
  file_id?: string | null       // 指定单文件；null/空串 → 批量扫 status=new
  limit?: number                // 批量上限，默认 100；必须为正整数
}

/** /v1/admin/refine/trigger_async 响应体。 */
export interface RefineTriggerResponse {
  triggered: 'async'
  file_id: string
  status: 'queued'
  note: string
}

/** 关怀信号信封（/v1/admin/care/signals 返回，ST-27 T-59）。 */
export interface CareSignal {
  event_id: string
  type: string
  source: string
  payload: string        // JSON 字符串，消费方自行 parse
  ts: string
  consumed_at: string | null
  consumed_by: string | null
}

// ---------- wiki 知识库类型（/v1/wiki/*，Agent Key） ----------

/** /v1/wiki/pages 列表响应（轻量字段，不含正文；W5，方案 v0.3 §5.5）。 */
export interface WikiPagesResponse {
  pages: WikiPageSummary[]
  total: number
  limit: number
  offset: number
}

/** 页面轻量摘要（L1 展示用：title + description）。 */
export interface WikiPageSummary {
  page_id: string
  title: string
  category: string | null
  tags: string[]
  source_type: string | null
  source_url: string | null
  source_file: string | null
  ingested_at: string | null
  updated_at: string | null
  description?: string | null
}

/** 页面详情（含正文全文）。 */
export interface WikiPage extends WikiPageSummary {
  content: string
  content_seg?: string | null
}

// ---------- 客户端实现 ----------

/**
 * SGME HTTP 客户端。
 *
 * 防代理劫持：fetch 不读 HTTP_PROXY 环境变量（防 Clash 劫持 localhost），
 * 用显式 127.0.0.1（由 baseUrl 配置保证）+ dispatcher 禁用代理。
 *
 * 故障隔离：所有方法失败返回 null，绝不抛异常（调用方按 null 判断降级）。
 */
export class SgmeClient {
  private readonly baseUrl: string
  private readonly agentKey: string
  private readonly adminKey: string
  readonly agentId: string
  private readonly timeoutMs: number

  constructor(config: SgmeClientConfig) {
    this.baseUrl = config.baseUrl.replace(/\/+$/, '')
    this.agentKey = config.agentKey
    this.adminKey = config.adminKey
    this.agentId = config.agentId
    this.timeoutMs = config.timeoutMs ?? 5000
  }

  /** 统一 POST 请求，返回 [data, error]。失败时 data=null。 */
  private async post<T>(
    path: string,
    body: unknown,
    keyType: 'agent' | 'admin',
  ): Promise<[T | null, string | null]> {
    const key = keyType === 'agent' ? this.agentKey : this.adminKey
    const url = `${this.baseUrl}${path}`
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': key,
        },
        body: JSON.stringify(body),
        signal: ctrl.signal,
        // 防代理劫持：不读环境变量代理（Node fetch 默认不读 HTTP_PROXY，但显式声明防 undici 版本差异）
        ...({} as Record<string, unknown>),
      })
      clearTimeout(timer)
      if (!resp.ok) {
        const text = await resp.text().catch(() => '')
        return [null, `HTTP ${resp.status}: ${text.slice(0, 200)}`]
      }
      const data = (await resp.json()) as T
      return [data, null]
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      return [null, `fetch error: ${msg}`]
    }
  }

  /** 记忆+wiki 检索（POST /v1/search，Agent Key）。失败返回 null。 */
  async search(req: SearchRequest): Promise<SearchResponse | null> {
    const [data, err] = await this.post<SearchResponse>('/v1/search', req, 'agent')
    if (err) {
      console.warn(`[sgme-bridge] search failed: ${err}`)
      return null
    }
    return data
  }

  /** 画像注入（POST /v1/inject，Agent Key）。失败返回 null。 */
  async inject(req: InjectRequest): Promise<InjectResponse | null> {
    const [data, err] = await this.post<InjectResponse>('/v1/inject', req, 'agent')
    if (err) {
      console.warn(`[sgme-bridge] inject failed: ${err}`)
      return null
    }
    return data
  }

  /** L0 写入（POST /v1/append，Agent Key）。失败返回 null。 */
  async append(req: AppendRequest): Promise<AppendResponse | null> {
    const [data, err] = await this.post<AppendResponse>('/v1/append', req, 'agent')
    if (err) {
      console.warn(`[sgme-bridge] append failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 触发批量提炼（POST /v1/admin/refine/trigger_async，Admin Key）。
   * 实际返回 200（非 202），兼容两种状态码。失败返回 null。
   */
  async triggerRefine(req: RefineTriggerRequest): Promise<RefineTriggerResponse | null> {
    const [data, err] = await this.post<RefineTriggerResponse>(
      '/v1/admin/refine/trigger_async',
      req,
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] triggerRefine failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 信号消费（ST-27 T-59：agent 成为消费者，谁消费谁标记） ----------

  /** GET 请求（信号拉取用，与 POST 并列；同样防代理 + 故障隔离）。 */
  private async get<T>(path: string, keyType: 'agent' | 'admin'): Promise<[T | null, string | null]> {
    const key = keyType === 'agent' ? this.agentKey : this.adminKey
    const url = `${this.baseUrl}${path}`
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
      const resp = await fetch(url, {
        method: 'GET',
        headers: { 'X-API-Key': key },
        signal: ctrl.signal,
      })
      clearTimeout(timer)
      if (!resp.ok) {
        const text = await resp.text().catch(() => '')
        return [null, `HTTP ${resp.status}: ${text.slice(0, 200)}`]
      }
      const data = (await resp.json()) as T
      return [data, null]
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      return [null, `fetch error: ${msg}`]
    }
  }

  /** 拉取未消费关怀信号（GET /v1/admin/care/signals?unconsumed_only=true）。失败返回 null。 */
  async pullCareSignals(signalType?: string | null, limit = 20): Promise<CareSignal[] | null> {
    const params = new URLSearchParams({ unconsumed_only: 'true', limit: String(limit) })
    if (signalType) params.set('signal_type', signalType)
    const [data, err] = await this.get<{ signals: CareSignal[] }>(
      `/v1/admin/care/signals?${params.toString()}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] pullCareSignals failed: ${err}`)
      return null
    }
    return data?.signals ?? null
  }

  /** 列出 wiki 页面（GET /v1/wiki/pages，Agent Key；按 category 可选过滤）。失败返回 null。 */
  async wikiListPages(category?: string | null, limit = 50, offset = 0): Promise<WikiPagesResponse | null> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (category) params.set('category', category)
    const [data, err] = await this.get<WikiPagesResponse>(
      `/v1/wiki/pages?${params.toString()}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] wikiListPages failed: ${err}`)
      return null
    }
    return data
  }

  /** 取 wiki 页面详情（GET /v1/wiki/pages/{id}，Agent Key）。失败返回 null。 */
  async wikiGetPage(pageId: string): Promise<WikiPage | null> {
    const [data, err] = await this.get<WikiPage>(
      `/v1/wiki/pages/${encodeURIComponent(pageId)}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] wikiGetPage failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 原子认领信号（POST /v1/admin/care/signals/{id}/consume）。
   * 返回 true=本次认领成功 / false=已被他人消费（409）或失败 / null=网关不可达。
   */
  async claimSignal(eventId: string): Promise<boolean | null> {
    const [data, err] = await this.post<{ status: string }>(
      `/v1/admin/care/signals/${eventId}/consume`,
      {},
      'agent',
    )
    if (err) {
      // 409 = 已被他人消费（原子抢失败），不算错误，返回 false
      if (err.startsWith('HTTP 409')) return false
      console.warn(`[sgme-bridge] claimSignal failed: ${err}`)
      return null
    }
    return data?.status === 'consumed'
  }

  /** 写消费回执（POST /v1/admin/care/signals/{id}/ack）。返回是否写入成功。 */
  async ackSignal(
    eventId: string,
    status: 'claimed' | 'acked' | 'failed',
    result?: string,
  ): Promise<boolean> {
    const [data, err] = await this.post<{ status: string }>(
      `/v1/admin/care/signals/${eventId}/ack`,
      { status, result },
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] ackSignal failed: ${err}`)
      return false
    }
    return data?.status === status
  }
}

// ---------- 辅助：L0 格式化（与 reasonix bridge.py to_l0 对齐） ----------

/** 消息角色。 */
export type MessageRole = 'user' | 'assistant' | 'tool'

/** 单条消息（dsh 会话消息的抽象表示）。 */
export interface SessionMessage {
  role: MessageRole
  content: string
  ts: string                    // ISO 8601 时间戳
  toolName?: string             // role=tool 时工具名
}

/**
 * 消息列表 → SGME L0 消息块文本。
 *
 * 格式（与 reasonix bridge.py to_l0 完全一致，对齐 sgme/raw/store.py parse_body_messages）：
 * - user：`# {ts} user\n{content}`
 * - assistant：`## {ts} assistant\n{content}`
 * - tool：`## {ts} tool\n**tool**: {name}\n{content}`
 */
export function toL0(messages: SessionMessage[]): string {
  const blocks: string[] = []
  for (const m of messages) {
    if (m.role === 'user') {
      blocks.push(`# ${m.ts} user\n${m.content}`)
    } else if (m.role === 'tool') {
      blocks.push(`## ${m.ts} tool\n**tool**: ${m.toolName ?? 'tool'}\n${m.content}`)
    } else {
      blocks.push(`## ${m.ts} assistant\n${m.content}`)
    }
  }
  return blocks.join('\n\n') + '\n'
}
