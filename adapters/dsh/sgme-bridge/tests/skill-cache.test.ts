/**
 * skill-cache 测试（D-59 / T-406）：dsh 适配器的技能落盘缓存。
 *
 * 覆盖：
 * - 首次落盘（文件 + .sgme-manifest.json 字段）
 * - **manifest 命中不下载**（D-59 验收核心）：第二次零包传输
 * - 目录级短路：catalog_version 未变 → 连 manifest 都不拉
 * - 本地文件被改 → 重新拉取
 * - 包校验失败 → 报错 + 旧副本完好 + 无临时残件
 * - 包内 ../ 成员拒绝
 * - skill_materialize 工具接入（本地路径 + 旧服务端 404 回退）
 *
 * 数据全部低熵假值；不触网（假传输层）。
 */
import { createHash } from 'node:crypto'
import { gzipSync } from 'node:zlib'
import { promises as fsp } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import {
  CATALOG_FILE,
  HTTPStatusError,
  MANIFEST_FILE,
  PACKAGE_MANIFEST,
  SkillCache,
  SkillCacheError,
  validateSkillName,
} from '../src/skill-cache.js'
import type { SkillCacheTransport, SkillManifest } from '../src/skill-cache.js'
import { createSkillMaterializeTool } from '../src/tools.js'
import type { SgmeClient } from '../src/sgme-client.js'

// ---------- 构造服务端同构包（tar.gz：根 MANIFEST.sha256 + <name>/…） ----------

function sha256(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex')
}

function tarHeader(name: string, size: number): Buffer {
  const h = Buffer.alloc(512)
  h.write(name, 0, 100, 'utf8')
  h.write('0000644\0', 100, 8, 'ascii')
  h.write('0000000\0', 108, 8, 'ascii')
  h.write('0000000\0', 116, 8, 'ascii')
  h.write(`${size.toString(8).padStart(11, '0')}\0`, 124, 12, 'ascii')
  h.write('00000000000\0', 136, 12, 'ascii')
  h.write('        ', 148, 8, 'ascii') // 校验和占位（8 空格）
  h.write('0', 156, 1, 'ascii')
  h.write('ustar\0', 257, 6, 'ascii')
  h.write('00', 263, 2, 'ascii')
  let sum = 0
  for (const b of h) sum += b
  h.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 8, 'ascii')
  return h
}

function pad512(data: Buffer): Buffer {
  const rest = data.length % 512
  return rest === 0 ? data : Buffer.concat([data, Buffer.alloc(512 - rest)])
}

function buildTarGz(entries: Array<{ path: string; data: Buffer }>): Buffer {
  const parts: Buffer[] = []
  for (const e of entries) {
    parts.push(tarHeader(e.path, e.data.length), pad512(e.data))
  }
  parts.push(Buffer.alloc(1024))
  return gzipSync(Buffer.concat(parts))
}

function buildPackage(
  name: string,
  files: Record<string, Buffer>,
  opts: { tamper?: boolean } = {},
): { data: Buffer; sha: string } {
  const rels = Object.keys(files).sort()
  const lines = rels.map((rel, i) => {
    const digest = opts.tamper && i === 0 ? '0'.repeat(64) : sha256(files[rel] as Buffer)
    return `${digest}  ${rel}`
  })
  const manifest = Buffer.from(`${lines.join('\n')}\n`, 'utf8')
  const entries = [
    { path: PACKAGE_MANIFEST, data: manifest },
    ...rels.map((rel) => ({ path: `${name}/${rel}`, data: files[rel] as Buffer })),
  ]
  return { data: buildTarGz(entries), sha: sha256(manifest) }
}

// ---------- 假传输层 ----------

class FakeTransport implements SkillCacheTransport {
  catalog = 'cat-v1'
  skills = new Map<string, { files: Record<string, Buffer>; data: Buffer; sha: string; version: string }>()
  catalogRequests = 0
  manifestRequests = 0
  packageDownloads = 0

  setSkill(name: string, files: Record<string, Buffer>, opts: { tamper?: boolean; version?: string } = {}): void {
    const built = buildPackage(name, files, opts)
    this.skills.set(name, { files, data: built.data, sha: built.sha, version: opts.version ?? '1.0.0' })
  }

  async getCatalogVersion(): Promise<string | null> {
    this.catalogRequests += 1
    return this.catalog
  }

  async getManifest(name: string): Promise<SkillManifest | null> {
    this.manifestRequests += 1
    const s = this.skills.get(name)
    if (!s) return null
    return { name, version: s.version, package_sha256: s.sha, content_len: 1, updated_at: '2026-11-02T00:00:00+08:00' }
  }

  async getPackage(name: string): Promise<{ data: Buffer; headers: Record<string, string> }> {
    this.packageDownloads += 1
    const s = this.skills.get(name)
    if (!s) throw new HTTPStatusError(404, 'HTTP 404: not found')
    return { data: s.data, headers: { 'x-skill-sha256': s.sha, 'x-skill-files': String(Object.keys(s.files).length) } }
  }
}

const tmpDirs: string[] = []
async function tmpDir(): Promise<string> {
  const d = await fsp.mkdtemp(path.join(os.tmpdir(), 'sgme-skill-cache-'))
  tmpDirs.push(d)
  return d
}

afterEach(async () => {
  await Promise.all(tmpDirs.splice(0).map((d) => fsp.rm(d, { recursive: true, force: true })))
})

const FILES: Record<string, Buffer> = {
  'SKILL.md': Buffer.from('# demo\n', 'utf8'),
  'scripts/run.py': Buffer.from("print('hi')\n", 'utf8'),
}

describe('SkillCache（dsh / D-59）', () => {
  it('首次落盘：文件 + 清单字段齐全', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    t.setSkill('demo-skill', FILES)
    const cache = new SkillCache(t, dir)

    const r = await cache.ensureSkill('demo-skill', dir)
    expect(r.cached).toBe(false)
    expect(r.sha256).toBe(t.skills.get('demo-skill')?.sha)
    expect(r.files).toBe(2)
    expect(await fsp.readFile(path.join(dir, 'demo-skill', 'scripts', 'run.py'), 'utf8')).toBe("print('hi')\n")

    const manifest = JSON.parse(await fsp.readFile(path.join(dir, 'demo-skill', MANIFEST_FILE), 'utf8'))
    expect(manifest.name).toBe('demo-skill')
    expect(manifest.version).toBe('1.0.0')
    expect(manifest.content_hash).toBe(r.sha256)
    expect(manifest.files.map((f: { path: string }) => f.path).sort()).toEqual(['SKILL.md', 'scripts/run.py'])
    expect(manifest.fetched_at).toBeTruthy()
  })

  it('目录版本未变 → 第二次零请求短路', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    t.setSkill('demo-skill', FILES)
    await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    const manifestCalls = t.manifestRequests
    const packageCalls = t.packageDownloads

    const again = await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    expect(again.cached).toBe(true)
    expect(t.manifestRequests).toBe(manifestCalls)
    expect(t.packageDownloads).toBe(packageCalls)
  })

  it('manifest 命中不下载（目录状态缺失时走二级比对）', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    t.setSkill('demo-skill', FILES)
    await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    await fsp.rm(path.join(dir, CATALOG_FILE))

    const again = await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    expect(again.cached).toBe(true)
    expect(t.packageDownloads).toBe(1)
    expect(t.manifestRequests).toBe(2)
  })

  it('本地文件被外部改动 → 重新拉取恢复', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    t.setSkill('demo-skill', FILES)
    await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    await fsp.writeFile(path.join(dir, 'demo-skill', 'SKILL.md'), 'tampered\n', 'utf8')

    const r = await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    expect(r.cached).toBe(false)
    expect(t.packageDownloads).toBe(2)
    expect(await fsp.readFile(path.join(dir, 'demo-skill', 'SKILL.md'), 'utf8')).toBe('# demo\n')
  })

  it('包校验失败：报错 + 旧副本完好 + 无临时残件', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    t.setSkill('demo-skill', FILES)
    await new SkillCache(t, dir).ensureSkill('demo-skill', dir)

    t.catalog = 'cat-v2'
    t.setSkill('demo-skill', { 'SKILL.md': Buffer.from('# v2\n', 'utf8') }, { tamper: true, version: '2.0.0' })
    await expect(new SkillCache(t, dir).ensureSkill('demo-skill', dir)).rejects.toThrow(/sha256 校验失败/)
    expect(await fsp.readFile(path.join(dir, 'demo-skill', 'SKILL.md'), 'utf8')).toBe('# demo\n')
    const leftovers = (await fsp.readdir(dir)).filter((n) => n.startsWith('.demo-skill.fetch-'))
    expect(leftovers).toEqual([])
  })

  it('下载失败后不得把旧副本当最新（重试仍要下载）', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    t.setSkill('demo-skill', FILES)
    await new SkillCache(t, dir).ensureSkill('demo-skill', dir)

    t.catalog = 'cat-v2'
    t.setSkill('demo-skill', { 'SKILL.md': Buffer.from('# v2\n', 'utf8') }, { tamper: true, version: '2.0.0' })
    await expect(new SkillCache(t, dir).ensureSkill('demo-skill', dir)).rejects.toThrow(/sha256 校验失败/)

    t.setSkill('demo-skill', { 'SKILL.md': Buffer.from('# v2\n', 'utf8') }, { version: '2.0.0' })
    const retry = await new SkillCache(t, dir).ensureSkill('demo-skill', dir)
    expect(retry.cached).toBe(false)
    expect(t.packageDownloads).toBe(3)
    expect(await fsp.readFile(path.join(dir, 'demo-skill', 'SKILL.md'), 'utf8')).toBe('# v2\n')
  })

  it('拒绝包内 ../ 成员', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    const evil = buildTarGz([{ path: '../escape.txt', data: Buffer.from('bad') }])
    t.skills.set('demo-skill', { files: {}, data: evil, sha: '0'.repeat(64), version: '1' })
    await expect(new SkillCache(t, dir).ensureSkill('demo-skill', dir)).rejects.toThrow(SkillCacheError)
    expect((await fsp.readdir(dir)).filter((n) => n.startsWith('.demo-skill.fetch-'))).toEqual([])
  })

  it('技能名穿越被拒绝且不触网', async () => {
    const dir = await tmpDir()
    const t = new FakeTransport()
    expect(() => validateSkillName('../evil')).toThrow()
    await expect(new SkillCache(t, dir).ensureSkill('../evil', dir)).rejects.toThrow()
    expect(t.catalogRequests).toBe(0)
  })
})

// ---------- 工具接入 ----------

class FakeClient {
  transport = new FakeTransport()
  serverMaterializeCalls = 0
  packageStatus = 200

  skillCatalogVersion() {
    return this.transport.getCatalogVersion()
  }

  skillManifest(name: string) {
    return this.transport.getManifest(name)
  }

  async skillPackage(name: string) {
    if (this.packageStatus !== 200) return { status: this.packageStatus, error: 'HTTP 404: not found' }
    const r = await this.transport.getPackage(name)
    return { status: 200, data: r.data, headers: r.headers }
  }

  async skillMaterialize(_name: string, _dest: string) {
    this.serverMaterializeCalls += 1
    return { path: '/server/side/demo-skill/SKILL.md', sha256: 'ab'.repeat(32) }
  }
}

type ToolLike = { execute: (args: unknown, exec?: unknown) => Promise<unknown> }

describe('skill_materialize 工具（dsh / D-60）', () => {
  it('本地落盘 + 第二次缓存命中零下载', async () => {
    const dir = await tmpDir()
    const fake = new FakeClient()
    fake.transport.setSkill('demo-skill', FILES)
    const tool = createSkillMaterializeTool(fake as unknown as SgmeClient) as unknown as ToolLike

    const first = String(await tool.execute({ name: 'demo-skill', dest_dir: dir }))
    expect(first).toContain('已落盘（本地）')
    expect(await fsp.readFile(path.join(dir, 'demo-skill', 'SKILL.md'), 'utf8')).toBe('# demo\n')

    const second = String(await tool.execute({ name: 'demo-skill', dest_dir: dir }))
    expect(second).toContain('缓存命中')
    expect(fake.transport.packageDownloads).toBe(1)
    expect(fake.serverMaterializeCalls).toBe(0)
  })

  it('旧版服务端（包通道 404）→ 回退服务端 materialize', async () => {
    const dir = await tmpDir()
    const fake = new FakeClient()
    fake.packageStatus = 404
    const tool = createSkillMaterializeTool(fake as unknown as SgmeClient) as unknown as ToolLike

    const out = String(await tool.execute({ name: 'demo-skill', dest_dir: dir }))
    expect(out).toContain('旧版回退')
    expect(fake.serverMaterializeCalls).toBe(1)
  })
})
