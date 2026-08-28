/**
 * B86 回归验证（多轮 step 上下文不爆增）——2026-08-20
 *
 * 证明目标：0.3.1 下 N 轮 step 的事件提醒注入量恒定（只 1 次），不随轮次增长。
 * 复刻 context.ts 的真实注入循环（unnotifiedEvents → 注入 → markNotified）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { existsSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { SgmeEventSubscriber } from '../src/events.js'
import type { SgmeEvent } from '../src/events.js'

const AGENT_ID = 'b86-regression-test' // 独立 agentId，避免与其他测试文件持久化互相污染

function makeEvent(id: string, type = 'care_daily'): SgmeEvent {
  return {
    event_id: id,
    type,
    source: 'care',
    payload: { message: '这是一条很长的关怀内容'.repeat(50) }, // 模拟完整 payload（旧版注入物）
    ts: '2026-08-20T00:00:00Z',
  }
}

/** 复刻 context.ts 注入循环：返回每轮注入的文本列表。 */
function runSteps(sub: SgmeEventSubscriber, steps: number): string[] {
  const injectedTexts: string[] = []
  for (let step = 0; step < steps; step++) {
    const unnotified = sub.unnotifiedEvents()
    if (unnotified.length) {
      // 与 context.ts 相同：只放 event_id 摘要，不放完整 payload
      const text = '【SGME 事件提醒】' + unnotified.map((e: SgmeEvent) => e.event_id).join(', ')
      injectedTexts.push(text)
      sub.markNotified(unnotified.map((e: SgmeEvent) => e.event_id))
    }
  }
  return injectedTexts
}

describe('B86 回归：多轮 step 事件提醒不重复注入', () => {
  let sub: SgmeEventSubscriber

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    // 清理上次运行的持久化队列（防跨运行/跨文件污染导致 unnotified 空）
    const qp = join(homedir(), '.sgme', 'event-queue-' + AGENT_ID + '.json')
    if (existsSync(qp)) rmSync(qp)
    sub = new SgmeEventSubscriber({ baseUrl: 'http://x', agentKey: '', agentId: AGENT_ID })
  })

  it('20 轮 step：仅第 1 轮注入 1 次，后 19 轮零注入', () => {
    for (let i = 0; i < 3; i++) sub['queue'].push(makeEvent('evt-' + i))
    const injected = runSteps(sub, 20)
    expect(injected.length).toBe(1)          // 只注入 1 次
    expect(injected[0]).toContain('evt-0')   // 含 event_id
    expect(injected[0]).not.toContain('这是一条很长的关怀内容') // 不含完整 payload
  })

  it('旧实现对照：若用 pendingEvents 判断，20 轮会注入 20 次（证明差异存在）', () => {
    for (let i = 0; i < 3; i++) sub['queue'].push(makeEvent('old-' + i))
    // 旧逻辑：每轮都看 pendingEvents（未消费即可见）→ 每轮都注入
    let oldInjects = 0
    for (let step = 0; step < 20; step++) {
      if (sub.pendingEvents().length) oldInjects++
    }
    expect(oldInjects).toBe(20)              // 旧实现确实会注入 20 次
    // 新实现同场景
    const injected = runSteps(sub, 20)
    expect(injected.length).toBe(1)          // 新实现只 1 次
  })

  it('30 轮 × 10 事件：注入总量恒定（1 次），上下文零增长', () => {
    for (let i = 0; i < 10; i++) sub['queue'].push(makeEvent('big-' + i))
    const injected = runSteps(sub, 30)
    expect(injected.length).toBe(1)
    expect((injected[0] ?? '').length).toBeLessThan(300) // 摘要文本极小
  })

  it('事件被 claim 消费后（markConsumed），剩余 step 永不再提醒', () => {
    sub['queue'].push(makeEvent('consume-me'))
    sub.markNotified(['consume-me'])
    sub.markConsumed(['consume-me'])
    const injected = runSteps(sub, 10)
    expect(injected.length).toBe(0)
  })

  it('新事件到达后只再注入 1 次（增量注入，不重放历史）', () => {
    sub['queue'].push(makeEvent('first'))
    runSteps(sub, 5) // 注入 first
    sub['queue'].push(makeEvent('second')) // 新事件到达
    const injected = runSteps(sub, 10)
    expect(injected.length).toBe(1)
    expect(injected[0]).toContain('second')
    expect(injected[0]).not.toContain('first') // 历史事件不重放
  })
})
