/**
 * events.ts — SGME 事件流订阅（SSE 长连，2026-08-18 用户选方案 2）
 *
 * 目标：DSH 常驻时实时接收 SGME 主动事件（care_* 关怀 / anomaly_warn 异常 /
 * memory_updated），缓存到内存 + 本地文件，下一轮对话由 context.ts 注入提醒，
 * agent 再调 signal_pull 消费（claim → 关怀 → ack）。
 *
 * 可靠性：
 * - 断线重连：指数退避 1s → 30s 封顶；重连请求带 Last-Event-ID（SGME 断线补偿）
 * - 事件持久化：~/.sgme/event-queue-<agentId>.json（进程重启不丢）
 * - 故障隔离：任何异常只 log，绝不阻塞 dsh 主循环
 */
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'

/** SGME 事件信封（对齐 /v1/events/stream 的 SSE data 字段）。 */
export interface SgmeEvent {
  event_id: string
  type: string
  source: string
  payload: Record<string, unknown>
  ts: string
}

/** 订阅配置。 */
export interface EventSubscribeConfig {
  baseUrl: string
  agentKey: string
  agentId: string
}

/** 重连退避参数（毫秒）。 */
const RETRY_BASE_MS = 1000
const RETRY_MAX_MS = 30_000

/**
 * SSE 订阅器。
 *
 * - start()：建立长连（fetch + ReadableStream 解析 SSE），断线自动重连
 * - pendingEvents()：未消费事件（内存队列 + 文件恢复）
 * - markConsumed(ids)：事件消费后标记（agent 调 signal_pull 消费时同步）
 * - stop()：断开（插件卸载时调用）
 */
export class SgmeEventSubscriber {
  private config: EventSubscribeConfig
  private aborter: AbortController | null = null
  private retryTimer: NodeJS.Timeout | null = null
  private retryMs = RETRY_BASE_MS
  private stopped = false
  private lastEventId = ''
  private queue: SgmeEvent[] = []
  private consumedIds = new Set<string>()
  private notifiedIds = new Set<string>()
  private readonly queuePath: string

  constructor(config: EventSubscribeConfig) {
    this.config = config
    this.queuePath = join(homedir(), '.sgme', `event-queue-${config.agentId}.json`)
    this.restoreQueue()
  }

  /** 启动订阅（幂等；已在连则忽略）。 */
  start(): void {
    if (this.aborter || this.stopped) return
    this.stopped = false
    void this.connect()
  }

  /** 停止订阅（插件卸载）。 */
  stop(): void {
    this.stopped = true
    if (this.aborter) this.aborter.abort()
    this.aborter = null
    if (this.retryTimer) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }

  /** 未消费事件（供 context.ts 注入提醒）。 */
  pendingEvents(): SgmeEvent[] {
    return this.queue.filter((e) => !this.consumedIds.has(e.event_id))
  }

  /** 未消费且未提醒过的事件（context.ts 注入提醒的唯一来源）。
   *
   * 2026-08-20 修复（上下文爆增根因）：此前 context 用 pendingEvents() 判断，
   * 未消费事件每轮重复注入 → 上下文持续膨胀。引入 notifiedIds：
   * 同一事件只提醒一次，之后即使未消费也不再重复注入。
   */
  unnotifiedEvents(): SgmeEvent[] {
    return this.queue.filter(
      (e) => !this.consumedIds.has(e.event_id) && !this.notifiedIds.has(e.event_id),
    )
  }

  /** 标记事件已提醒（防重复注入）。 */
  markNotified(eventIds: string[]): void {
    for (const id of eventIds) this.notifiedIds.add(id)
    this.persistQueue()
  }

  /** 标记事件已消费（agent 消费后调用，防重复提醒）。 */
  markConsumed(eventIds: string[]): void {
    for (const id of eventIds) this.consumedIds.add(id)
    this.persistQueue()
  }

  /** 建立 SSE 连接（一次）；断线/错误时按退避重连。 */
  private async connect(): Promise<void> {
    if (this.stopped) return
    const { baseUrl, agentKey, agentId } = this.config
    const url = `${baseUrl.replace(/\/$/, '')}/v1/events/stream?subscriber_id=${encodeURIComponent(agentId)}`
    const headers: Record<string, string> = {}
    if (agentKey) headers['X-API-Key'] = agentKey
    if (this.lastEventId) headers['Last-Event-ID'] = this.lastEventId

    const aborter = new AbortController()
    this.aborter = aborter
    try {
      const resp = await fetch(url, { headers, signal: aborter.signal })
      if (!resp.ok) {
        throw new Error(`SSE HTTP ${resp.status}`)
      }
      this.retryMs = RETRY_BASE_MS
      if (!resp.body) throw new Error('SSE 无响应体')
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let nl: number
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl).trim()
          buffer = buffer.slice(nl + 1)
          if (line.startsWith('data:')) {
            const data = line.slice(5).trim()
            if (data) this.handleEvent(data)
          } else if (line.startsWith('id:')) {
            this.lastEventId = line.slice(3).trim()
          }
        }
      }
      throw new Error('SSE 流结束')
    } catch (err) {
      if (this.stopped) return
      const msg = err instanceof Error ? err.message : String(err)
      this.retryTimer = setTimeout(() => {
        this.retryMs = Math.min(this.retryMs * 2, RETRY_MAX_MS)
        void this.connect()
      }, this.retryMs)
      console.warn(`[dsh-sgme] 事件流断开（${msg}），${this.retryMs / 1000}s 后重连`)
    } finally {
      if (this.aborter === aborter) this.aborter = null
    }
  }

  /** 处理一条 SSE 事件（入队 + 持久化）。 */
  private handleEvent(data: string): void {
    try {
      const ev = JSON.parse(data) as SgmeEvent
      if (!ev || !ev.event_id || !ev.type) return
      if (this.queue.some((e) => e.event_id === ev.event_id)) return
      this.queue.push(ev)
      this.persistQueue()
    } catch {
      // 非 JSON 行忽略
    }
  }

  /** 队列持久化（~/.sgme/event-queue-<agentId>.json）。 */
  private persistQueue(): void {
    try {
      const dir = join(homedir(), '.sgme')
      mkdirSync(dir, { recursive: true })
      writeFileSync(this.queuePath, JSON.stringify({
        queue: this.queue.slice(-200),
        consumedIds: [...this.consumedIds].slice(-500),
        notifiedIds: [...this.notifiedIds].slice(-500),
      }), 'utf-8')
    } catch (err) {
      console.warn('[dsh-sgme] 事件队列持久化失败:', err instanceof Error ? err.message : err)
    }
  }

  /** 进程启动时从文件恢复队列。 */
  private restoreQueue(): void {
    try {
      if (!existsSync(this.queuePath)) return
      const data = JSON.parse(readFileSync(this.queuePath, 'utf-8'))
      if (Array.isArray(data.queue)) this.queue = data.queue
      if (Array.isArray(data.consumedIds)) this.consumedIds = new Set(data.consumedIds)
      if (Array.isArray(data.notifiedIds)) this.notifiedIds = new Set(data.notifiedIds)
    } catch {
      // 损坏则丢弃（不阻塞）
    }
  }
}
