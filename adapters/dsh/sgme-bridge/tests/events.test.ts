/**
 * events.test.ts — SSE 事件订阅器测试（2026-08-18）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { rmSync } from 'node:fs'

// mock homedir → tmp（避免污染真实 ~/.sgme）
const tmpDir = '/tmp/dsh-sgme-test'
vi.mock('node:os', () => ({ homedir: () => tmpDir }))

import { SgmeEventSubscriber, type SgmeEvent } from '../src/events.js'

function sseStream(lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  const chunks = lines.map((l) => encoder.encode(l + '\n'))
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(c)
      controller.close()
    },
  })
}

const ev1: SgmeEvent = { event_id: 'e1', type: 'care_daily', source: 'care', payload: { msg: '你好' }, ts: '2026-08-18T00:00:00Z' }
const ev2: SgmeEvent = { event_id: 'e2', type: 'anomaly_warn', source: 'health', payload: { stalled: false }, ts: '2026-08-18T00:00:01Z' }

describe('SgmeEventSubscriber', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    // 清理持久化队列文件（跨用例隔离）
    rmSync(tmpDir, { recursive: true, force: true })
  })

  it('解析 SSE 事件并入队', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      body: sseStream([
        'id: e1',
        'data: ' + JSON.stringify(ev1),
        '',
        'id: e2',
        'data: ' + JSON.stringify(ev2),
        '',
      ]),
    }) as unknown as Response)
    vi.stubGlobal('fetch', fetchMock)

    const sub = new SgmeEventSubscriber({
      baseUrl: 'http://127.0.0.1:9910',
      agentKey: 'agt_test',
      agentId: 'dsh-test',
    })
    sub.start()
    // 等待异步连接完成
    await new Promise((r) => setTimeout(r, 100))
    const pending = sub.pendingEvents()
    expect(pending.length).toBe(2)
    expect(pending[0].type).toBe('care_daily')
    expect(pending[1].type).toBe('anomaly_warn')
    // 请求带 X-API-Key 和 subscriber_id
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(String(url)).toContain('subscriber_id=dsh-test')
    expect((init.headers as Record<string, string>)['X-API-Key']).toBe('agt_test')
    sub.stop()
  })

  it('相同 event_id 去重', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      body: sseStream(['data: ' + JSON.stringify(ev1), '', 'data: ' + JSON.stringify(ev1), '']),
    }) as unknown as Response))

    const sub = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub.start()
    await new Promise((r) => setTimeout(r, 100))
    expect(sub.pendingEvents().length).toBe(1)
    sub.stop()
  })

  it('markConsumed 后不再提醒', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      body: sseStream(['data: ' + JSON.stringify(ev1), '']),
    }) as unknown as Response))

    const sub = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub.start()
    await new Promise((r) => setTimeout(r, 100))
    expect(sub.pendingEvents().length).toBe(1)
    sub.markConsumed(['e1'])
    expect(sub.pendingEvents().length).toBe(0)
    sub.stop()
  })

  it('HTTP 错误后按退避重连（fetch 被再次调用）', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 403 } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true, status: 200,
        body: sseStream(['data: ' + JSON.stringify(ev2), '']),
      } as unknown as Response)
    vi.stubGlobal('fetch', fetchMock)

    const sub = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub.start()
    // 等待首次失败 + 重连（退避 1s 内）
    await new Promise((r) => setTimeout(r, 1500))
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(sub.pendingEvents().some((e) => e.event_id === 'e2')).toBe(true)
    sub.stop()
  })
})
