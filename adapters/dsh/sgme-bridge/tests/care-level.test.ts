/**
 * D-61（2026-11-04）：dsh 适配器消费侧——关怀档位过滤与客户端取档测试。
 *
 * 口径（设计 §6.4）：silent=不注入提醒（信号池仍可 signal_pull）；
 * session=全部提醒；push=只推关键信号（care_todo_due / care_mood）；
 * anomaly_warn 永远透传。
 */
import { describe, it, expect, vi } from 'vitest'
import { SgmeClient } from '../src/sgme-client.js'
import { filterEventsByCareLevel } from '../src/context.js'
import type { SgmeEvent } from '../src/events.js'

function ev(type: string, id = 'e1'): SgmeEvent {
  return {
    event_id: id,
    type,
    source: 'care',
    payload: {},
    ts: '2026-11-04T00:00:00Z',
  } as unknown as SgmeEvent
}

function makeClient(): SgmeClient {
  return new SgmeClient({
    baseUrl: 'http://127.0.0.1:9910',
    agentKey: 'agt_' + 'placeholder',
    adminKey: 'adm_' + 'placeholder',
    agentId: 'dsh',
    timeoutMs: 500,
  })
}

describe('filterEventsByCareLevel（D-61）', () => {
  const events = [
    ev('care_todo_due', 'a'),
    ev('care_daily', 'b'),
    ev('anomaly_warn', 'c'),
    ev('memory_updated', 'd'),
  ]

  it('silent：care_* 全部不注入，其它照常', () => {
    const out = filterEventsByCareLevel(events, 'silent')
    expect(out.map((e) => e.event_id)).toEqual(['c', 'd'])
  })

  it('session：全部保留（旧行为）', () => {
    expect(filterEventsByCareLevel(events, 'session')).toHaveLength(4)
  })

  it('push：只保留关键 care 信号 + 非 care 事件', () => {
    const out = filterEventsByCareLevel(events, 'push')
    expect(out.map((e) => e.event_id)).toEqual(['a', 'c', 'd'])
  })
})

describe('SgmeClient.activeCareLevel（D-61）', () => {
  it('无当前角色 → silent', async () => {
    const c = makeClient()
    vi.spyOn(c, 'roleActiveGet').mockResolvedValue({ role_id: null })
    expect(await c.activeCareLevel()).toBe('silent')
  })

  it('角色卡 care_level=push → push', async () => {
    const c = makeClient()
    vi.spyOn(c, 'roleActiveGet').mockResolvedValue({ role_id: 'butler' })
    vi.spyOn(c, 'roleAssemble').mockResolvedValue({
      role_id: 'butler', role_name: '管家', system_prompt: 'x', care_level: 'push',
    })
    expect(await c.activeCareLevel()).toBe('push')
  })

  it('角色卡缺 care_level → silent（新角色默认档）', async () => {
    const c = makeClient()
    vi.spyOn(c, 'roleActiveGet').mockResolvedValue({ role_id: 'x' })
    vi.spyOn(c, 'roleAssemble').mockResolvedValue({
      role_id: 'x', role_name: 'x', system_prompt: 'y',
    })
    expect(await c.activeCareLevel()).toBe('silent')
  })

  it('接口失败 → null（调用方保旧行为）', async () => {
    const c = makeClient()
    vi.spyOn(c, 'roleActiveGet').mockResolvedValue(null)
    expect(await c.activeCareLevel()).toBeNull()
  })
})
