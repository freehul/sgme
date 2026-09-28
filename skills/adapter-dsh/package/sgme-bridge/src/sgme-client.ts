/**
 * sgme-client.ts — SGME Gateway HTTP 客户端
 *
 * 封装 4 个 SGME 端点调用，故障隔离（失败只 log + 返回 null，绝不抛异常阻塞 dsh 主循环）。
 *
 * 契约来源：sgme/server/routes_memory.py / routes_admin.py / routes_care.py（2026-08-14 调研确认，T-86 扩充）
 * - POST /v1/search        — Agent Key — 记忆+wiki 检索
 * - POST /v1/inject         — Agent Key — 画像注入（注意：max_tokens 协议接受但不消费）
 * - POST /v1/append         — Agent Key — L0 写入
 * - POST /v1/admin/refine/trigger_async — Admin Key — 触发提炼（实际返回 200，非 202）
 * - POST /v1/admin/ideas|demands|projects — Admin Key — 三池登记（T-86）
 * - GET/PUT /v1/admin/roles* + /v1/admin/care/active-role — Agent Key — 角色模板（T-86）
 * - GET /v1/memory/{id} + POST /v1/memory/{id}/reject — Agent Key — 记忆纠错（T-86）
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
  scopes?: string[]              // 默认 ["memory"]；可选值：memory/wiki/scenes/wiki_pages/sessions（T-207：L0 原文正文 FTS）
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
  // 实测（2026-08-29，SGME 1.1.0）：wiki 场景层实际返回 `wiki_scene`（非 `wiki`/`scenes`），
  // 技能层返回 `skills`。旧声明的 'wiki'/'scenes' 保留仅为历史契约兼容。
  source: 'memory' | 'wiki' | 'scenes' | 'wiki_pages' | 'wiki_scene' | 'skills'
  // skills 层不返回 content（只给 name/description/category），故为可选；
  // 消费方必须先兜底再取 .length（tools.ts formatSearchResults 已兜底）。
  content?: string
  memory_id?: string             // memory 层独有
  page_id?: string               // wiki_pages 层独有
  title?: string                 // wiki/wiki_pages 层独有
  name?: string                  // skills 层独有（技能名）
  description?: string           // skills 层独有（触发描述）
  category?: string | null       // skills 层独有
  score?: number                 // memory / skills 层独有序分
  routes?: string[]
  trace?: Record<string, unknown>
}

// ---------- 技能层类型（/v1/skills*，Agent Key；ST-36 四级披露读侧） ----------

/** L0 索引项（GET /v1/skills 的 skills[] 元素）。 */
export interface SkillSummary {
  name: string
  description: string
  category: string | null
  tags: string[]
  source: string | null
  version: string | null
}

/** GET /v1/skills 列表响应（total=全量数，returned=实返回数，budget=默认截断窗口）。 */
export interface SkillsListResponse {
  skills: SkillSummary[]
  total: number
  returned: number
  offset: number
  budget: number
}

/** L1 摘要（GET /v1/skills/{name}/digest）：frontmatter 字段 + 正文骨架 + uses 依赖。 */
export interface SkillDigest {
  name: string
  description: string
  version: string | null
  pattern: string | null
  category: string | null
  tags: string[]
  uses: string[]
  sections: string[]
}

/** L2 全文（GET /v1/skills/{name}?section=）。 */
export interface SkillDetail {
  name: string
  content: string
  sha256: string | null
  section: string | null
  truncated_by_section: boolean
  source: string | null
}

/** 冷启动包（GET /v1/skills/coldstart）：索引 + 热集 + SGME 操作手册。 */
export interface SkillsColdstartResponse {
  index: { items: SkillSummary[]; total?: number }
  hotset: SkillSummary[]
  manual: { page_id: string; title: string; content: string } | null
}

/**
 * 技能小节名归一化（供 skillGet 的 section 参数使用）。
 *
 * ⚠️ 契约坑（2026-08-29 实测 SGME 1.1.0）：服务端 section 参数要**纯标题文本**
 * （`前置条件`），而 skill_digest 的 sections 骨架给的是**带 # 前缀的原样行**
 * （`## 前置条件`）——照抄骨架传上去必 404，且错误文案误导为「技能不存在」。
 * 本函数剥掉 # 前缀与两侧空白，让两种写法都能命中。
 */
export function normalizeSkillSection(section: string | null | undefined): string | null {
  if (!section) return null
  const stripped = section.replace(/^#+\s*/, '').trim()
  return stripped || null
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

/** /v1/wiki/pages/{page_id} 更新请求体（PATCH，W5 方案 v0.3 §5.5）。 */
export interface WikiPageUpdateRequest {
  content: string
  append?: boolean            // 默认 true 追加
  title?: string | null
  category?: string | null
  tags?: string[] | null
  description?: string | null
  author?: string | null
}

/** /v1/wiki/pages/{page_id} 更新响应体。 */
export interface WikiPageUpdateResponse {
  page_id: string
  status: string              // appended / updated / noop
}

/** /v1/wiki/pages 创建请求体（POST，T-55：原样入库，幂等 upsert）。 */
export interface WikiPageCreateRequest {
  title: string
  content: string
  category?: string | null
  tags?: string[] | null
  source_type?: string | null // text / file / url
  source_url?: string | null
  source_file?: string | null
  description?: string | null // L1 摘要（描述即索引）
  author?: string | null
  status?: string | null
  supersedes?: string | null
}

/** /v1/wiki/pages 创建响应体。 */
export interface WikiPageCreateResponse {
  page_id: string
  status: string              // created / updated
}

/** /v1/wiki/search 结果项（执行通道：含 skill 手册，不过滤；FTS5 BM25 + LIKE 兜底）。 */
export interface WikiSearchResult {
  page_id: string
  title: string
  snippet: string
  tags?: string[] | string    // 服务端 tags 列可能为 JSON 字符串或数组，消费侧防御解析
}

/** /v1/wiki/search 响应体。 */
export interface WikiSearchResponse {
  results: WikiSearchResult[]
}

/** /v1/health 响应（健康检查；含版本/LLM/提炼水位/向量水位）。 */
export interface HealthResponse {
  status: string
  version?: string
  llm?: {
    available?: boolean
    provider?: string
    model?: string
    error?: string | null
  }
  refinement?: {
    watermark_age_sec?: number
    queue_depth?: number
    last_refined_at?: string
    stalled?: boolean
    heartbeat_ok?: boolean
  }
  vector?: {
    available?: boolean
    engine?: string
    memory_vectors?: number
    scene_vectors?: number
  }
}

// ---------- 三池登记类型（T-86：创意/待办/项目，Admin Key） ----------

/** POST /v1/admin/ideas 请求体（用户主动提出才记录）。 */
export interface IdeaAddRequest {
  content: string
  priority?: number | null         // 0-100，缺省 50
  source_ref?: string | null       // 溯源（如会话标识）
}

/** POST /v1/admin/ideas 响应体。 */
export interface IdeaAddResponse {
  idea: Record<string, unknown>
  created: boolean
}

/** POST /v1/admin/demands 请求体（跨项目统一待办池）。 */
export interface DemandCreateRequest {
  title: string                    // 一句概括
  content?: string | null
  priority?: number | null         // 0-100，缺省 50
  project_id?: string | null       // 自由标记（未登记项目允许，服务端只回 warning）
  origin_idea_id?: string | null   // 从创意升格
  source_ref?: string | null
}

/** POST /v1/admin/demands 响应体（条目字段 + warnings）。 */
export interface DemandCreateResponse {
  demand_id: string
  title: string
  status: string
  warnings?: string[]
  [k: string]: unknown
}

/** POST /v1/admin/projects 请求体（upsert，二次登记=更新）。 */
export interface ProjectRegisterRequest {
  project_id: string               // 纯英文
  path?: string | null             // 新建时必填（服务端校验）
  name?: string | null
  git_repo?: string | null
  milestone?: string | null
}

/** POST /v1/admin/projects 响应体。 */
export interface ProjectRegisterResponse {
  project_id: string
  [k: string]: unknown
}

// ---------- 角色模板类型（T-86：ST-29 换皮不换芯，Agent Key） ----------

/** 角色卡轻量摘要（GET /v1/admin/roles 列表项）。 */
export interface RoleSummary {
  role_id: string
  name: string
  description?: string | null
  [k: string]: unknown
}

/** GET /v1/admin/roles 响应体。 */
export interface RoleListResponse {
  roles: RoleSummary[]
  total: number
}

/** GET /v1/admin/roles/{role_id}/assemble 响应体（角色沟通提示词装配产物）。 */
export interface RoleAssembleResponse {
  role_id: string
  role_name: string
  system_prompt: string            // 含 {{char}}/{{user}} 宏，消费方拼接
  care_policy?: Record<string, unknown> | null
  persona?: string | null
  profile_blocks?: Array<Record<string, unknown>>
}

/** GET/PUT /v1/admin/care/active-role 响应体。 */
export interface RoleActiveResponse {
  role_id: string | null
  status?: string
}

// ---------- 记忆纠错类型（T-86：Agent Key） ----------

/** GET /v1/memory/{id} 响应体（http_payload 投影：memory + 溯源 + 归档链）。 */
export interface MemoryDetailResponse {
  memory: {
    memory_id: string
    content: string
    status?: string
    [k: string]: unknown
  }
  sources?: Array<Record<string, unknown>>
  archive_chain?: Array<Record<string, unknown>>
}

/** POST /v1/memory/{id}/reject 响应体。 */
export interface MemoryRejectResponse {
  memory_id: string
  status: string                   // rejected
  reject_reason?: string
}

/** POST /v1/memory/{id}/unreject 响应体（T-163 补齐；无 MCP 对端，data 即响应体）。 */
export interface MemoryUnrejectResponse {
  memory_id: string
  status: string                   // active
}

// ---------- 聚合答案类型（T-149：跨会话计数/列举/时序推理） ----------

/** POST /v1/answer 请求体（AnswerRequest）。 */
export interface AnswerRequest {
  query: string
  question_type?: string | null    // temporal / aggregate / generic / null=自动分派
  limit?: number                   // 检索候选条数，服务端再 min(limit, 20)
}

/** POST /v1/answer 响应体（比 search 多一步 LLM 答案合成）。 */
export interface AnswerResponse {
  answer: string
  question_type: string
  evidence?: Array<Record<string, unknown>>
  provider?: string | null
  usage?: Record<string, unknown> | null
  prompt_meta?: Record<string, unknown> | null
  candidates_used?: number
}

// ---------- 统计 / 配置（运维读侧，Admin Key） ----------

/** GET /v1/admin/stats 响应体（http_payload 投影后的历史契约形态）。 */
export interface StatsResponse {
  memories: { total: number; archived: number }
  raw_files: { total: number; new: number; refined: number; error: number; archived: number }
  dimension_distribution: Record<string, number>
  refinement: {
    watermark_age_sec: number | null
    last_refined_at: string | null
    queue_depth: number
  }
  agents: Array<{ agent_id: string; role: string }>
}

/** GET /v1/admin/config 响应体（整体读带 writable_sections；单段读带 section）。 */
export interface ConfigGetResponse {
  config: Record<string, unknown>
  writable_sections?: string[]
  section?: string
}

/** PUT /v1/admin/config 请求体（单段或多段部分更新，合并后落盘热生效）。 */
export interface ConfigUpdateRequest {
  section: string | null           // null = 请求体 values 本身就是「段名 → 段内容」
  values: Record<string, unknown>
}

/** PUT /v1/admin/config 响应体。 */
export interface ConfigUpdateResponse {
  status: string
  config: Record<string, unknown>
  section?: string
}

// ---------- 提炼监控（Admin Key） ----------

/** POST /v1/admin/refine/trigger 响应体（同步提炼，file 或 batch 形态）。 */
export interface RefineTriggerSyncResponse {
  triggered: string                // "file" / "batch"
  file_id?: string
  status?: string
  memories_count?: number
  processed?: number
  total_memories?: number
  error?: string | null
  [k: string]: unknown
}

/** GET /v1/admin/refine_runs 分页信封（契约 §5.5.2）。 */
export interface RefineRunsResponse {
  items: Array<Record<string, unknown>>
  count: number
  total: number
  page: number
  limit: number
  generated_at?: string
}

// ---------- 信号批量清空（T-87，Admin Key） ----------

/** POST /v1/admin/events/consume_all 响应体。 */
export interface SignalClearResponse {
  consumed: number
  type: string | null
  subscriber_id: string | null
}

// ---------- 技能写侧 + L3 物化 ----------

/** POST /v1/skills/{name}/materialize 请求体（L3：字节保真落盘 dest_dir/<name>/SKILL.md）。 */
export interface SkillMaterializeRequest {
  dest_dir: string
}

/** POST /v1/skills/{name}/materialize 响应体。 */
export interface SkillMaterializeResponse {
  name: string
  path: string
  sha256: string
}

/**
 * 技能写侧响应（PUT / DELETE / rename 共用）。
 *
 * 成功统一为 ``{ok: true, ...}``（具体字段随操作而异）；失败由非 2xx 状态码 +
 * ``error.details`` 表达（lint_failed/referenced/conflict → 400/409），
 * 客户端按 null 归一到「失败」。
 */
export interface SkillWriteResponse {
  ok?: boolean
  [k: string]: unknown
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

  /** SGME 健康检查（GET /v1/health，免鉴权——Bearer 可选，不强制 X-API-Key）。失败返回 null。 */
  async health(): Promise<HealthResponse | null> {
    const url = this.baseUrl + '/v1/health'
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
      const resp = await fetch(url, { signal: ctrl.signal })
      clearTimeout(timer)
      if (!resp.ok) return null
      return (await resp.json()) as HealthResponse
    } catch {
      return null
    }
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

  /** 统一 PATCH 请求，返回 [data, error]。失败时 data=null。 */
  private async patch<T>(path: string, body: unknown): Promise<[T | null, string | null]> {
    const url = `${this.baseUrl}${path}`
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
      const resp = await fetch(url, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': this.agentKey,
        },
        body: JSON.stringify(body),
        signal: ctrl.signal,
        // 防代理劫持：不读环境变量代理（与 post/get 一致）
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

  /**
   * 统一 PUT 请求（T-86：设置当前角色用），返回 [data, error]。失败时 data=null。
   *
   * ``keyType`` 默认 agent（角色切换等读侧语义）；技能写侧（skill_put）需传 admin。
   */
  private async put<T>(
    path: string,
    body: unknown,
    keyType: 'agent' | 'admin' = 'agent',
  ): Promise<[T | null, string | null]> {
    const key = keyType === 'agent' ? this.agentKey : this.adminKey
    const url = `${this.baseUrl}${path}`
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
      const resp = await fetch(url, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': key,
        },
        body: JSON.stringify(body),
        signal: ctrl.signal,
        // 防代理劫持：不读环境变量代理（与 post/get/patch 一致）
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

  /**
   * 统一 DELETE 请求（技能删除用），返回 [data, error]。失败时 data=null。
   *
   * query 参数（hard/force 等）由调用方拼进 path——服务端读的是 Query 而非 body。
   */
  private async del<T>(path: string, keyType: 'agent' | 'admin'): Promise<[T | null, string | null]> {
    const key = keyType === 'agent' ? this.agentKey : this.adminKey
    const url = `${this.baseUrl}${path}`
    try {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
      const resp = await fetch(url, {
        method: 'DELETE',
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

  /** 检索 wiki 知识库页面（GET /v1/wiki/search，执行通道——含 skill 手册，不过滤；Agent Key）。失败返回 null。 */
  async wikiSearch(query: string, limit = 10): Promise<WikiSearchResponse | null> {
    const params = new URLSearchParams({ q: query, limit: String(limit) })
    const [data, err] = await this.get<WikiSearchResponse>(
      `/v1/wiki/search?${params.toString()}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] wikiSearch failed: ${err}`)
      return null
    }
    return data
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

  /** 更新 wiki 页面（PATCH /v1/wiki/pages/{id}，Agent Key）。失败返回 null。 */
  async wikiUpdatePage(pageId: string, body: WikiPageUpdateRequest): Promise<WikiPageUpdateResponse | null> {
    const [data, err] = await this.patch<WikiPageUpdateResponse>(
      `/v1/wiki/pages/${encodeURIComponent(pageId)}`,
      body,
    )
    if (err) {
      console.warn(`[sgme-bridge] wikiUpdatePage failed: ${err}`)
      return null
    }
    return data
  }

  /** 创建 wiki 页面（POST /v1/wiki/pages，Agent Key；T-55 幂等 upsert）。失败返回 null。 */
  async wikiCreatePage(body: WikiPageCreateRequest): Promise<WikiPageCreateResponse | null> {
    const [data, err] = await this.post<WikiPageCreateResponse>(
      '/v1/wiki/pages',
      body,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] wikiCreatePage failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 技能层（ST-36 四级披露读侧，Agent Key） ----------

  /** L0 索引列表（GET /v1/skills；分页浏览全量）。失败返回 null。 */
  async skillList(limit = 50, offset = 0): Promise<SkillsListResponse | null> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    const [data, err] = await this.get<SkillsListResponse>(`/v1/skills?${params.toString()}`, 'agent')
    if (err) {
      console.warn(`[sgme-bridge] skillList failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 技能检索（POST /v1/search scope=["skills"]，BM25+向量融合）。
   *
   * ⚠️ 契约要点：skills 层结果**不含 content/title**，只给 name/description/category，
   * 消费方必须走 skillDigest / skillGet 取正文，不可直接把 description 当全文用。
   * 失败返回 null。
   */
  async skillSearch(query: string, limit = 5): Promise<SkillSummary[] | null> {
    const [data, err] = await this.post<SearchResponse>(
      '/v1/search',
      { query, scopes: ['skills'], limit },
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillSearch failed: ${err}`)
      return null
    }
    if (!data?.results) return null
    // 统一搜索 skills 层投影 → L0 索引项形态（category 缺失兜 null）
    return data.results.map((r) => ({
      name: r.name ?? '',
      description: r.description ?? '',
      category: r.category ?? null,
      tags: [],
      source: r.source ?? 'skills',
      version: null,
    }))
  }

  /** L1 摘要（GET /v1/skills/{name}/digest；审核媒介层：骨架 + uses 依赖）。失败返回 null。 */
  async skillDigest(name: string): Promise<SkillDigest | null> {
    const [data, err] = await this.get<SkillDigest>(
      `/v1/skills/${encodeURIComponent(name)}/digest`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillDigest failed: ${err}`)
      return null
    }
    return data
  }

  /** L2 全文（GET /v1/skills/{name}?section=；section 给定时只取该节省 token）。失败返回 null。 */
  async skillGet(name: string, section?: string | null): Promise<SkillDetail | null> {
    // section 归一化见 normalizeSkillSection——服务端只认纯标题，骨架行带 # 前缀
    const normalized = normalizeSkillSection(section ?? null)
    const qs = normalized ? `?section=${encodeURIComponent(normalized)}` : ''
    const [data, err] = await this.get<SkillDetail>(
      `/v1/skills/${encodeURIComponent(name)}${qs}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillGet failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 冷启动包（GET /v1/skills/coldstart）。
   *
   * SGME 1.1.0 范式：只索引 1 个《技能检索协议》skill + SGME 操作手册，
   * 全量技能不预载——agent 按协议「先 skill_search 检索、再 skill_get 拉全文注入」。
   * 失败返回 null。
   */
  async skillColdstart(): Promise<SkillsColdstartResponse | null> {
    const [data, err] = await this.get<SkillsColdstartResponse>('/v1/skills/coldstart', 'agent')
    if (err) {
      console.warn(`[sgme-bridge] skillColdstart failed: ${err}`)
      return null
    }
    return data
  }

  /** 自进化触发（POST /v1/wiki/evolve/trigger，Agent Key；W4 自动闭环）。失败返回 null。 */
  async evolveTrigger(sessionKey?: string | null, minRounds = 5): Promise<{ status: string } | null> {
    const [data, err] = await this.post<{ status: string }>(
      '/v1/wiki/evolve/trigger',
      { session_key: sessionKey ?? null, min_rounds: minRounds },
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] evolveTrigger failed: ${err}`)
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

  // ---------- 三池登记（T-86：创意/待办/项目，Admin Key） ----------

  /** 添加创意（POST /v1/admin/ideas，Admin Key；用户主动提出才记录）。失败返回 null。 */
  async ideaAdd(body: IdeaAddRequest): Promise<IdeaAddResponse | null> {
    const [data, err] = await this.post<IdeaAddResponse>('/v1/admin/ideas', body, 'admin')
    if (err) {
      console.warn(`[sgme-bridge] ideaAdd failed: ${err}`)
      return null
    }
    return data
  }

  /** 新建待办（POST /v1/admin/demands，Admin Key；跨项目统一待办池）。失败返回 null。 */
  async demandCreate(body: DemandCreateRequest): Promise<DemandCreateResponse | null> {
    const [data, err] = await this.post<DemandCreateResponse>('/v1/admin/demands', body, 'admin')
    if (err) {
      console.warn(`[sgme-bridge] demandCreate failed: ${err}`)
      return null
    }
    return data
  }

  /** 登记项目（POST /v1/admin/projects，Admin Key；upsert，二次登记=更新）。失败返回 null。 */
  async projectRegister(body: ProjectRegisterRequest): Promise<ProjectRegisterResponse | null> {
    const [data, err] = await this.post<ProjectRegisterResponse>('/v1/admin/projects', body, 'admin')
    if (err) {
      console.warn(`[sgme-bridge] projectRegister failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 角色模板（T-86：ST-29 换皮不换芯，Agent Key） ----------

  /** 角色列表（GET /v1/admin/roles，Agent Key）。失败返回 null。 */
  async roleList(): Promise<RoleListResponse | null> {
    const [data, err] = await this.get<RoleListResponse>('/v1/admin/roles', 'agent')
    if (err) {
      console.warn(`[sgme-bridge] roleList failed: ${err}`)
      return null
    }
    return data
  }

  /** 装配角色沟通提示词（GET /v1/admin/roles/{role_id}/assemble，Agent Key）。失败返回 null。 */
  async roleAssemble(roleId: string, injectMode?: string | null): Promise<RoleAssembleResponse | null> {
    const params = new URLSearchParams()
    if (injectMode) params.set('inject_mode', injectMode)
    const qs = params.toString()
    const [data, err] = await this.get<RoleAssembleResponse>(
      `/v1/admin/roles/${encodeURIComponent(roleId)}/assemble${qs ? `?${qs}` : ''}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] roleAssemble failed: ${err}`)
      return null
    }
    return data
  }

  /** 读取当前沟通角色（GET /v1/admin/care/active-role，Agent Key）。失败返回 null。 */
  async roleActiveGet(): Promise<RoleActiveResponse | null> {
    const [data, err] = await this.get<RoleActiveResponse>('/v1/admin/care/active-role', 'agent')
    if (err) {
      console.warn(`[sgme-bridge] roleActiveGet failed: ${err}`)
      return null
    }
    return data
  }

  /** 设置当前沟通角色（PUT /v1/admin/care/active-role，Agent Key）。失败返回 null。 */
  async roleActiveSet(roleId: string): Promise<RoleActiveResponse | null> {
    const [data, err] = await this.put<RoleActiveResponse>(
      '/v1/admin/care/active-role',
      { role_id: roleId },
    )
    if (err) {
      console.warn(`[sgme-bridge] roleActiveSet failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 记忆纠错（T-86：Agent Key） ----------

  /** 单条记忆详情（GET /v1/memory/{id}，Agent Key；含溯源 + 归档链）。失败返回 null。 */
  async memoryGet(memoryId: string): Promise<MemoryDetailResponse | null> {
    const [data, err] = await this.get<MemoryDetailResponse>(
      `/v1/memory/${encodeURIComponent(memoryId)}`,
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] memoryGet failed: ${err}`)
      return null
    }
    return data
  }

  /** 标记记忆不采用（POST /v1/memory/{id}/reject，Agent Key；不删除、可恢复）。失败返回 null。 */
  async memoryReject(memoryId: string, reason?: string | null): Promise<MemoryRejectResponse | null> {
    const [data, err] = await this.post<MemoryRejectResponse>(
      `/v1/memory/${encodeURIComponent(memoryId)}/reject`,
      { reason: reason ?? null },
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] memoryReject failed: ${err}`)
      return null
    }
    return data
  }

  /** 撤销「不采用」（POST /v1/memory/{id}/unreject，Agent Key；恢复为 active）。失败返回 null。 */
  async memoryUnreject(memoryId: string): Promise<MemoryUnrejectResponse | null> {
    const [data, err] = await this.post<MemoryUnrejectResponse>(
      `/v1/memory/${encodeURIComponent(memoryId)}/unreject`,
      {},
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] memoryUnreject failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 聚合答案（T-149：跨会话计数/列举/时序推理） ----------

  /**
   * 聚合答案（POST /v1/answer，Agent Key）。
   *
   * 比 search 多一步：检索候选 → 题型分派 → LLM 生成答案。会消耗 LLM 调用，
   * 与 search 的纯检索不是一回事。LLM 全链不可用时服务端返回 503 → null。
   */
  async answer(
    query: string,
    questionType?: string | null,
    limit?: number,
  ): Promise<AnswerResponse | null> {
    const [data, err] = await this.post<AnswerResponse>(
      '/v1/answer',
      { query, question_type: questionType ?? null, ...(limit !== undefined ? { limit } : {}) },
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] answer failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 统计 / 配置（运维读侧，Admin Key） ----------

  /** 统计：记忆/原始层计数 + 维度分布 + 提炼水位 + 注册 Agent（GET /v1/admin/stats）。失败返回 null。 */
  async stats(): Promise<StatsResponse | null> {
    const [data, err] = await this.get<StatsResponse>('/v1/admin/stats', 'admin')
    if (err) {
      console.warn(`[sgme-bridge] stats failed: ${err}`)
      return null
    }
    return data
  }

  /** 读配置：整体读（section 省略）或单段读（GET /v1/admin/config[/{section}]）。失败返回 null。 */
  async configGet(section?: string | null): Promise<ConfigGetResponse | null> {
    const path = section
      ? `/v1/admin/config/${encodeURIComponent(section)}`
      : '/v1/admin/config'
    const [data, err] = await this.get<ConfigGetResponse>(path, 'admin')
    if (err) {
      console.warn(`[sgme-bridge] configGet failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 更新配置段（POST /v1/admin/config，Admin Key；服务端 POST 与 PUT 等价）。
   *
   * ⚠️ 热生效 + 落盘 sgme.yaml——会真实改变服务端运行行为，非试验性调用。
   * section=null 时 values 的键即段名（单段形态）。
   */
  async configUpdate(
    section: string | null,
    values: Record<string, unknown>,
  ): Promise<ConfigUpdateResponse | null> {
    const [data, err] = await this.post<ConfigUpdateResponse>(
      '/v1/admin/config',
      { section, values },
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] configUpdate failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 信号批量清空（T-87，Admin Key） ----------

  /**
   * 批量清空/全部消费未消费信号（POST /v1/admin/events/consume_all，Admin Key）。
   *
   * 幂等（二次调用 consumed=0）；subscriberId 传则同步推进该订阅者持久游标。
   */
  async signalClear(
    eventType?: string | null,
    subscriberId?: string | null,
  ): Promise<SignalClearResponse | null> {
    const params = new URLSearchParams()
    if (eventType) params.set('type', eventType)
    if (subscriberId) params.set('subscriber_id', subscriberId)
    const qs = params.toString()
    const [data, err] = await this.post<SignalClearResponse>(
      `/v1/admin/events/consume_all${qs ? `?${qs}` : ''}`,
      {},
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] signalClear failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 提炼监控（Admin Key） ----------

  /**
   * 同步触发提炼（POST /v1/admin/refine/trigger，Admin Key）。
   *
   * ⚠️ 同步阻塞：file_id 给定时处理单文件，否则批量扫 status=new。
   * 会真实消耗 LLM 额度；批量场景优先用 triggerRefine（异步排队即返）。
   */
  async refineTriggerSync(
    fileId?: string | null,
    limit?: number,
  ): Promise<RefineTriggerSyncResponse | null> {
    const [data, err] = await this.post<RefineTriggerSyncResponse>(
      '/v1/admin/refine/trigger',
      { file_id: fileId ?? null, ...(limit !== undefined ? { limit } : {}) },
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] refineTriggerSync failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 提炼记录分页（GET /v1/admin/refine_runs，Admin Key）。
   *
   * 做「提炼状态」观测用——服务端 refine_status 只有 MCP 侧无 HTTP 端点，
   * 故以本端点 + stats 近似（默认不做 status 过滤，error/running 默认可见）。
   */
  async refineRuns(opts?: {
    page?: number
    limit?: number
    stage?: string | null
    status?: string | null
    since?: string | null
    until?: string | null
  }): Promise<RefineRunsResponse | null> {
    const params = new URLSearchParams()
    if (opts?.page !== undefined) params.set('page', String(opts.page))
    if (opts?.limit !== undefined) params.set('limit', String(opts.limit))
    if (opts?.stage) params.set('stage', opts.stage)
    if (opts?.status) params.set('status', opts.status)
    if (opts?.since) params.set('since', opts.since)
    if (opts?.until) params.set('until', opts.until)
    const qs = params.toString()
    const [data, err] = await this.get<RefineRunsResponse>(
      `/v1/admin/refine_runs${qs ? `?${qs}` : ''}`,
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] refineRuns failed: ${err}`)
      return null
    }
    return data
  }

  // ---------- 技能 L3 物化 + 写侧（ST-36 M3） ----------

  /**
   * 技能 L3 物化：字节保真写盘 dest_dir/<name>/SKILL.md（POST /v1/skills/{name}/materialize，Agent Key）。
   *
   * 不走 LLM 转写；返回落盘路径与 sha256，供脚本执行场景取真文件。
   */
  async skillMaterialize(name: string, destDir: string): Promise<SkillMaterializeResponse | null> {
    const [data, err] = await this.post<SkillMaterializeResponse>(
      `/v1/skills/${encodeURIComponent(name)}/materialize`,
      { dest_dir: destDir },
      'agent',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillMaterialize failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 写入/覆盖技能（PUT /v1/admin/skills/{name}，Admin Key；SKILL.md 全文自动解析 frontmatter）。
   *
   * ⚠️ 服务端过 lint 门禁 + 三层查重，会落盘并 git commit 到技能源仓。
   */
  async skillPut(
    name: string,
    content: string,
    skipLimits = false,
  ): Promise<SkillWriteResponse | null> {
    const [data, err] = await this.put<SkillWriteResponse>(
      `/v1/admin/skills/${encodeURIComponent(name)}`,
      { content, skip_limits: skipLimits },
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillPut failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 删除技能（DELETE /v1/admin/skills/{name}，Admin Key）。
   *
   * 默认软删（deprecated 标记）；hard=true 物理删目录。有入向 uses 引用且
   * 未 force → 409 被拒。
   */
  async skillDelete(
    name: string,
    hard = false,
    force = false,
  ): Promise<SkillWriteResponse | null> {
    const params = new URLSearchParams()
    if (hard) params.set('hard', 'true')
    if (force) params.set('force', 'true')
    const qs = params.toString()
    const [data, err] = await this.del<SkillWriteResponse>(
      `/v1/admin/skills/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`,
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillDelete failed: ${err}`)
      return null
    }
    return data
  }

  /**
   * 技能改名（POST /v1/admin/skills/{name}/rename，Admin Key；墓碑制永不原地改名）。
   *
   * 写新名副本 + 旧位置留 superseded_by 墓碑 + 登记 tombstones.json。
   * 需服务端 skills.source_dirs 指向 git 技能仓。
   */
  async skillRename(name: string, newName: string): Promise<SkillWriteResponse | null> {
    const [data, err] = await this.post<SkillWriteResponse>(
      `/v1/admin/skills/${encodeURIComponent(name)}/rename`,
      { new_name: newName },
      'admin',
    )
    if (err) {
      console.warn(`[sgme-bridge] skillRename failed: ${err}`)
      return null
    }
    return data
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
