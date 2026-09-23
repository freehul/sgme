/**
 * events.test.ts — SSE 事件订阅器测试（2026-08-18）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { resolve } from 'node:path'

// mock homedir → 项目内、按用例唯一的目录（避免污染真实 ~/.sgme）
//
// ⚠️ 两条硬约束（2026-09-18 实锤，改这里必读）：
// 1. **不要写 `/tmp/...`**：Windows 下会被解析为 `<盘符>:\tmp`（系统临时区）。
// 2. **不要用 `rmSync(dir, { recursive: true })` 清理**：宿主会注入
//    node-safe-delete-shim 拦截 Node 的递归删除（无论路径在哪），
//    整条 `pnpm run verify` 会被判为批量删除而失败。
//    → 隔离改由「每用例独立 homedir、不删除」实现（残留目录已被 .gitignore 覆盖）。
//    残留会随运行次数累积（2026-09-24 实测 85 个目录 / 378K）——测试**不依赖既有残留**
//    （目录名带运行级唯一前缀），可随时人工清理：删除整个 `.tmp-events-test/` 即可，
//    不要在测试代码里加 `rmSync`。（图形界面删除即回收站，符合原件不删纪律。）
//
// ⚠️ 目录名必须带**运行级唯一前缀**：早期只用 case-<n>，而 caseSeq 每次运行都从 1 起，
//    于是复用上次运行的目录、读到残留的 notifiedIds，用例一开始就「已提醒」→ 断言失败
//    （2026-09-18 实测：b86 4 例 + events 2 例挂，根因即此）。
const runId = `${process.pid}-${Date.now()}`
let caseHome = ''
let caseSeq = 0
vi.mock('node:os', () => ({ homedir: () => caseHome }))

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
    // 每用例独立 homedir（跨用例 + 跨运行隔离）——不删旧目录，见文件头约束
    caseHome = resolve(process.cwd(), '.tmp-events-test', `${runId}-case-${++caseSeq}`)
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
    expect(pending[0]?.type).toBe('care_daily')
    expect(pending[1]?.type).toBe('anomaly_warn')
    // 请求带 X-API-Key 和 subscriber_id
    const firstCall = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    const [url, init] = firstCall
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

  it('unnotifiedEvents 只返回未消费且未提醒过的事件', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      body: sseStream([
        'data: ' + JSON.stringify(ev1), '',
        'data: ' + JSON.stringify(ev2), '',
      ]),
    }) as unknown as Response))

    const sub = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub.start()
    await new Promise((r) => setTimeout(r, 100))

    // 初始：两个事件都未提醒
    expect(sub.unnotifiedEvents().map((e) => e.event_id)).toEqual(['e1', 'e2'])
    // 标记 e1 已提醒 → unnotified 只剩 e2
    sub.markNotified(['e1'])
    expect(sub.unnotifiedEvents().map((e) => e.event_id)).toEqual(['e2'])
    // 消费 e2 → unnotified 空
    sub.markConsumed(['e2'])
    expect(sub.unnotifiedEvents().length).toBe(0)
    sub.stop()
  })

  it('markNotified 不改变 pendingEvents（未消费仍可见）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      body: sseStream(['data: ' + JSON.stringify(ev1), '']),
    }) as unknown as Response))

    const sub = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub.start()
    await new Promise((r) => setTimeout(r, 100))
    expect(sub.pendingEvents().length).toBe(1)
    sub.markNotified(['e1'])
    // 已提醒但仍未消费 → pendingEvents 仍可见（供 signal_pull 消费）
    expect(sub.pendingEvents().length).toBe(1)
    // 但 unnotified 已空（不再重复注入）
    expect(sub.unnotifiedEvents().length).toBe(0)
    sub.stop()
  })

  it('notifiedIds 持久化（重启后不重复提醒）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      body: sseStream(['data: ' + JSON.stringify(ev1), '']),
    }) as unknown as Response))

    const sub1 = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub1.start()
    await new Promise((r) => setTimeout(r, 100))
    sub1.markNotified(['e1'])
    sub1.stop()

    // 模拟进程重启：新实例从文件恢复
    const sub2 = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: 'k', agentId: 'd' })
    sub2.start()
    await new Promise((r) => setTimeout(r, 100))
    // e1 已提醒过 → unnotified 为空（不重复注入）
    expect(sub2.unnotifiedEvents().length).toBe(0)
    sub2.stop()
  })
})
