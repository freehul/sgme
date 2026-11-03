/**
 * skill-cache.ts — SGME 技能落盘缓存（设计 5.2.1 / D-59，任务 T-406）。
 *
 * 与 Python 真源 ``adapters/_shared/skill_cache.py`` **同语义移植**（语言不同无法
 * 逐字节共用；改动需双端同步，``CACHE_PROTOCOL_VERSION`` 与清单文件名由
 * ``tests/test_skill_cache_sync.py`` 校验同值）。
 *
 * 两级失效：
 *   1. 目录级：GET /v1/skills/catalog-version 与本地 ``.sgme-catalog.json`` 比对，
 *      相同则本次会话不再逐个比对（进程内只拉一次）；
 *   2. 单技能：GET /v1/skills/{name}/manifest 的 ``package_sha256`` 与本地
 *      ``.sgme-manifest.json`` 的 ``remote_sha`` 比对，相同直接用本地副本（零包传输）。
 *
 * ⚠️ 两套哈希口径不同、永不相等（2026-11-03 实测，Python 真源同款修正）：
 *   - ``package_sha256``：服务端对技能 DB 行确定性打包的**不透明缓存指纹**，
 *     只用于「是否同一份远端包」的比对，落盘时记进本清单的 ``remote_sha``；
 *   - ``content_hash``（= 响应头 X-Skill-Sha256）：包内 MANIFEST.sha256 的聚合哈希，
 *     用于逐文件校验，不能拿去和 package_sha256 比（比了缓存永不命中且首次必拒绝落盘）。
 *   3. 未命中才 GET /v1/skills/{name}/package：按包内 MANIFEST.sha256 逐文件校验 +
 *      文件集双向对照 + 响应头对账，全部通过后在 ``.<name>.fetch-*`` 临时目录组装，
 *      最后 rename **原子替换**（失败回滚、不留残件）。
 *
 * 降级纪律：目录/清单端点不可用（旧版服务端）视为「不可比」→ 直接拉包；
 * 只有包下载/校验失败才抛错。缓存是优化，不是正确性依赖。
 *
 * 不建全局技能缓存目录、不预热（设计 5.2.1「不建」）：落点永远由调用方给的 destDir 决定。
 */
import { createHash } from 'node:crypto'
import { promises as fsp } from 'node:fs'
import path from 'node:path'
import { gunzipSync } from 'node:zlib'

/** 缓存协议版本：与 Python 真源同值（跨语言一致性由测试守护）。 */
export const CACHE_PROTOCOL_VERSION = 1
/** 本地工作副本清单（放在技能目录内，随技能目录一起原子替换）。 */
export const MANIFEST_FILE = '.sgme-manifest.json'
/** 目录级缓存状态（放在 stateDir，不放进技能目录）。 */
export const CATALOG_FILE = '.sgme-catalog.json'
/** 包内清单文件名（归档根，与 <name>/ 平级）。 */
export const PACKAGE_MANIFEST = 'MANIFEST.sha256'

/** 缓存运行时错误（消息面向用户，不含密钥）。 */
export class SkillCacheError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SkillCacheError'
  }
}

/** HTTP 非 2xx：带状态码，调用方据此决定是否回退（404/405 = 端点未部署）。 */
export class HTTPStatusError extends SkillCacheError {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'HTTPStatusError'
    this.status = status
  }
}

/** 服务端单技能清单（GET /v1/skills/{name}/manifest）。 */
export interface SkillManifest {
  name: string
  version: string
  package_sha256: string
  content_len?: number
  updated_at?: string
}

/** 本地清单里的单文件记录。 */
export interface SkillFileEntry {
  path: string
  sha256: string
}

/** 本地工作副本清单（.sgme-manifest.json）。 */
export interface SkillLocalManifest {
  name: string
  version: string
  /** 包内 MANIFEST.sha256 的聚合哈希（= 响应头 X-Skill-Sha256，逐文件校验用）。 */
  content_hash: string
  /**
   * 落盘时见到的服务端 ``package_sha256``（**不透明缓存指纹**，非聚合哈希）。
   * 二级比对（缓存命中）只认它；旧清单缺该字段时按空串处理 → 保守重下（自愈）。
   */
  remote_sha: string
  files: SkillFileEntry[]
  fetched_at: string
  /** 落盘时见到的服务端目录版本：只在该版本下才允许一级短路（防旧副本误判最新）。 */
  catalog_version: string
}

/** 技能包字节 + 响应头。 */
export interface SkillPackage {
  data: Buffer
  headers: Record<string, string>
}

/** 缓存所需的最小传输面（生产由 SgmeClient 适配，测试可注入假实现）。 */
export interface SkillCacheTransport {
  getCatalogVersion(): Promise<string | null>
  getManifest(name: string): Promise<SkillManifest | null>
  /** 失败抛 HTTPStatusError（带状态码）或 SkillCacheError。 */
  getPackage(name: string): Promise<SkillPackage>
}

/** ensureSkill 的返回值（path 是本地技能目录）。 */
export interface SkillInstallResult {
  path: string
  sha256: string
  files: number
  cached: boolean
}

// ---------- 通用工具 ----------

function sha256(data: Buffer | string): string {
  return createHash('sha256').update(data).digest('hex')
}

function nowIso(): string {
  return new Date().toISOString()
}

/** 技能名校验：必须是单个路径组件（防经技能名逃逸落点目录）。 */
export function validateSkillName(name: string): string {
  const n = String(name ?? '').trim()
  if (!n) throw new SkillCacheError('技能名不能为空')
  if (/[/\\\r\n\0]/.test(n) || n === '.' || n === '..') {
    throw new SkillCacheError(`技能名非法（应为单个技能名、不含路径分隔符）：${JSON.stringify(n)}`)
  }
  return n
}

/** 包内相对路径归一化 + 安全校验（拒绝绝对路径 / 盘符 / `..` / 空段）。 */
function normalizeRel(rel: string): string {
  const p = String(rel ?? '').replace(/\\/g, '/')
  if (!p || p.startsWith('/') || /^[A-Za-z]:/.test(p)) {
    throw new SkillCacheError(`包内路径非法（拒绝）: ${JSON.stringify(rel)}`)
  }
  const segs = p.split('/')
  if (segs.some((s) => s === '' || s === '.' || s === '..')) {
    throw new SkillCacheError(`包内路径非法（拒绝）: ${JSON.stringify(rel)}`)
  }
  return p
}

// ---------- tar.gz 解析（只认服务端归档契约：普通文件，无链接） ----------

interface TarEntry {
  path: string
  data: Buffer
}

function readCString(buf: Buffer, start: number, len: number): string {
  const slice = buf.subarray(start, start + len)
  const end = slice.indexOf(0)
  return (end === -1 ? slice : slice.subarray(0, end)).toString('utf8')
}

/** PAX 扩展头里取 path=（长路径 / 非 ASCII 路径会被 Python tarfile 拆到 PAX 头）。 */
function parsePaxPath(data: Buffer): string | null {
  let offset = 0
  while (offset < data.length) {
    const space = data.indexOf(0x20, offset)
    if (space === -1) return null
    const len = Number.parseInt(data.subarray(offset, space).toString('ascii'), 10)
    if (!Number.isFinite(len) || len <= 0) return null
    const record = data.subarray(offset, offset + len).toString('utf8')
    const keyStart = record.indexOf(' ') + 1
    const eq = record.indexOf('=')
    if (eq > keyStart) {
      const key = record.slice(keyStart, eq)
      const value = record.slice(eq + 1).replace(/\n$/, '')
      if (key === 'path') return value
    }
    offset += len
  }
  return null
}

function readTarGz(raw: Buffer): TarEntry[] {
  let tar: Buffer
  try {
    tar = gunzipSync(raw)
  } catch (e) {
    throw new SkillCacheError(`包解压失败（tar.gz 无效或截断）: ${(e as Error).message}`)
  }
  const entries: TarEntry[] = []
  let offset = 0
  let paxPath: string | null = null
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512)
    if (header.every((b) => b === 0)) break
    const name = readCString(header, 0, 100)
    const sizeText = readCString(header, 124, 12).trim()
    const size = sizeText ? Number.parseInt(sizeText, 8) : 0
    if (!Number.isFinite(size) || size < 0) throw new SkillCacheError('包解压失败（tar 头非法）')
    const type = header[156] === 0 ? '0' : String.fromCharCode(header[156] as number)
    const prefix = readCString(header, 345, 155)
    const fullName = prefix ? `${prefix}/${name}` : name
    const dataStart = offset + 512
    const dataEnd = dataStart + size
    if (dataEnd > tar.length) throw new SkillCacheError('包解压失败（tar 截断）')
    const data = tar.subarray(dataStart, dataEnd)
    if (type === 'x') {
      paxPath = parsePaxPath(data) ?? paxPath
    } else if (type === 'g') {
      // 全局 PAX 头：无路径语义，跳过
    } else if (type === '1' || type === '2') {
      throw new SkillCacheError(`包内出现链接成员（不符合归档契约，拒绝）: ${fullName}`)
    } else if (type === '5') {
      paxPath = null
    } else if (type === '0') {
      entries.push({ path: paxPath ?? fullName, data: Buffer.from(data) })
      paxPath = null
    } else {
      throw new SkillCacheError(`包内出现特殊成员（不符合归档契约，拒绝）: ${fullName}`)
    }
    offset = dataStart + Math.ceil(size / 512) * 512
  }
  return entries
}

function parsePackageManifest(raw: Buffer): SkillFileEntry[] {
  const entries: SkillFileEntry[] = []
  for (const line of raw.toString('utf8').split(/\r?\n/)) {
    if (!line.trim()) continue
    const sep = line.indexOf('  ')
    if (sep === -1) throw new SkillCacheError(`包内 MANIFEST 行格式非法: ${JSON.stringify(line.slice(0, 80))}`)
    const digest = line.slice(0, sep).trim().toLowerCase()
    const rel = normalizeRel(line.slice(sep + 2))
    if (digest.length !== 64) throw new SkillCacheError(`包内 MANIFEST 行格式非法: ${JSON.stringify(line.slice(0, 80))}`)
    entries.push({ path: rel, sha256: digest })
  }
  if (entries.length === 0) throw new SkillCacheError('包内 MANIFEST 为空——拒绝落盘')
  return entries
}

// ---------- 缓存主体 ----------

/**
 * 两级失效的技能落盘缓存。
 *
 * @param transport 传输面（生产用 SgmeClient 适配；测试注入假实现）
 * @param stateDir  目录级状态（.sgme-catalog.json）落点；缺省时用 destDir 兜底
 */
export class SkillCache {
  private readonly transport: SkillCacheTransport
  private readonly stateDir: string | null
  private readonly catalogChecked = new Map<string, { changed: boolean; version: string }>()

  constructor(transport: SkillCacheTransport, stateDir?: string | null) {
    this.transport = transport
    this.stateDir = stateDir ? path.resolve(stateDir) : null
  }

  private catalogPath(stateDir: string | null): string | null {
    const dir = stateDir ?? this.stateDir
    return dir ? path.join(dir, CATALOG_FILE) : null
  }

  private async readCatalog(file: string | null): Promise<{ catalog_version?: string } | null> {
    if (!file) return null
    try {
      const data = JSON.parse(await fsp.readFile(file, 'utf8')) as { catalog_version?: string }
      return typeof data === 'object' && data ? data : null
    } catch {
      return null
    }
  }

  private async writeCatalog(file: string, version: string): Promise<void> {
    await fsp.mkdir(path.dirname(file), { recursive: true })
    const tmp = `${file}.tmp-${process.pid}-${Date.now()}`
    await fsp.writeFile(tmp, JSON.stringify({ catalog_version: version, checked_at: nowIso() }, null, 2) + '\n', 'utf8')
    await fsp.rename(tmp, file)
  }

  /** 拉目录版本并与本地记录比对；返回 true = 有变化（需要做二级比对）。 */
  async refreshCatalog(force = false): Promise<boolean> {
    const file = this.catalogPath(null)
    if (!file) throw new SkillCacheError('未指定 stateDir，无法做目录级缓存比对')
    const local = await this.readCatalog(file)
    const version = await this.transport.getCatalogVersion()
    if (!version) throw new SkillCacheError('目录版本响应缺少 catalog_version')
    if (!force && local?.catalog_version === version) return false
    await this.writeCatalog(file, version)
    return true
  }

  /**
   * 目录级短路判定（同进程同 stateDir 只真的拉一次）。
   *
   * 返回 `{changed, version}`：changed=true 表示版本变了/未知（继续二级比对），
   * version 是本次见到的服务端目录版本（拉不到时空串）。端点不可用按「未知」处理。
   */
  private async catalogChanged(stateDir: string): Promise<{ changed: boolean; version: string }> {
    const key = path.resolve(stateDir)
    const memo = this.catalogChecked.get(key)
    if (memo !== undefined) return memo
    const file = this.catalogPath(key)
    const local = await this.readCatalog(file)
    let changed = true
    let version = ''
    let wrote = false
    try {
      version = (await this.transport.getCatalogVersion()) ?? ''
      if (!version) {
        changed = true
      } else if (local?.catalog_version === version) {
        changed = false
      } else {
        changed = true
        if (file) {
          await this.writeCatalog(file, version)
          wrote = true
        }
      }
    } catch {
      changed = true
      version = ''
    }
    // 本次调用必须按 changed 走二级比对（刚写的状态还没被任何副本消费）；
    // 写盘后本进程**后续**调用已同步，可短路。
    this.catalogChecked.set(key, wrote ? { changed: false, version } : { changed, version })
    return { changed, version }
  }

  /** 读本地 .sgme-manifest.json；缺失/损坏/名字不符返回 null。 */
  async readManifest(name: string, destDir: string): Promise<SkillLocalManifest | null> {
    try {
      validateSkillName(name)
    } catch {
      return null
    }
    const file = path.join(path.resolve(destDir), name, MANIFEST_FILE)
    try {
      const data = JSON.parse(await fsp.readFile(file, 'utf8')) as SkillLocalManifest
      if (!data || data.name !== name || !Array.isArray(data.files)) return null
      return data
    } catch {
      return null
    }
  }

  /** 本地文件自检：清单内文件都在且 sha256 未变（外部改动即失效）。 */
  private async filesIntact(target: string, manifest: SkillLocalManifest): Promise<boolean> {
    if (manifest.files.length === 0) return false
    for (const item of manifest.files) {
      if (!item || typeof item.path !== 'string' || typeof item.sha256 !== 'string') return false
      let rel: string
      try {
        rel = normalizeRel(item.path)
      } catch {
        return false
      }
      try {
        const data = await fsp.readFile(path.join(target, rel))
        if (sha256(data) !== item.sha256.toLowerCase()) return false
      } catch {
        return false
      }
    }
    return true
  }

  /**
   * 保证 `<destDir>/<name>` 是服务端最新副本；返回落盘结果。
   *
   * 命中（目录级短路或单技能 package_sha256 相同且本地文件完好）时零包传输；
   * 未命中才下载 + 校验 + 原子替换。`cached` 记录本次是否命中。
   */
  async ensureSkill(name: string, destDir: string): Promise<SkillInstallResult> {
    const skill = validateSkillName(name)
    const dest = path.resolve(destDir)
    const target = path.join(dest, skill)
    const local = await this.readManifest(skill, dest)
    const intact = local ? await this.filesIntact(target, local) : false
    // 目录级（一级）：无论本地有没有副本都先建立/刷新目录状态
    const catalog = await this.catalogChanged(dest)
    // 只在本副本就是该目录版本下取的（manifest 记了 catalog_version）时才短路：
    // 防「上次下载失败但目录版本已记下」导致旧副本被误判为最新。
    const sameCatalog = catalog.version !== '' && local?.catalog_version === catalog.version
    if (intact && local && !catalog.changed && sameCatalog) {
      return { path: target, sha256: local.content_hash, files: local.files.length, cached: true }
    }
    const remote = await this.transport.getManifest(skill)
    // 二级比对：远端 package_sha256 只能与本地记录的 remote_sha 比（同为服务端指纹）；
    // 拿 content_hash 比 → 口径不同、永不相等，缓存形同虚设（2026-11-03 修复）。
    if (intact && local && remote && (remote.package_sha256 ?? '') === (local.remote_sha ?? '')) {
      return { path: target, sha256: local.content_hash, files: local.files.length, cached: true }
    }
    return this.downloadAndInstall(skill, dest, remote, catalog.version)
  }

  /** 拉包 → 校验 → 临时目录组装 → 原子替换（失败回滚、不留残件）。 */
  private async downloadAndInstall(
    name: string,
    dest: string,
    remote: SkillManifest | null,
    catalogVersion: string,
  ): Promise<SkillInstallResult> {
    const pkg = await this.transport.getPackage(name)
    const entries = readTarGz(pkg.data)
    const top = new Set(entries.map((e) => normalizeRel(e.path).split('/')[0]))
    if (top.size !== 2 || !top.has(name) || !top.has(PACKAGE_MANIFEST)) {
      throw new SkillCacheError(
        `包顶层布局异常 ${JSON.stringify([...top].sort())}（期望 ${JSON.stringify([name, PACKAGE_MANIFEST])}）——拒绝落盘`,
      )
    }
    const manifestEntry = entries.find((e) => normalizeRel(e.path) === PACKAGE_MANIFEST)
    if (!manifestEntry) throw new SkillCacheError(`包内缺少 ${PACKAGE_MANIFEST}——拒绝落盘`)
    const fileEntries = parsePackageManifest(manifestEntry.data)
    const expected = new Set(fileEntries.map((e) => e.path))
    const actual = new Set(
      entries
        .map((e) => normalizeRel(e.path))
        .filter((rel) => rel !== PACKAGE_MANIFEST)
        .map((rel) => rel.slice(name.length + 1)),
    )
    const missing = [...expected].filter((p) => !actual.has(p))
    const extra = [...actual].filter((p) => !expected.has(p))
    if (missing.length || extra.length) {
      throw new SkillCacheError(
        `包内文件集与 MANIFEST 不一致（缺 ${JSON.stringify(missing.slice(0, 3))} / 多 ${JSON.stringify(extra.slice(0, 3))}）——拒绝落盘`,
      )
    }
    for (const e of fileEntries) {
      const entry = entries.find((x) => normalizeRel(x.path) === `${name}/${e.path}`)
      if (!entry || sha256(entry.data) !== e.sha256) {
        throw new SkillCacheError(`包内文件 sha256 校验失败: ${e.path}——拒绝落盘`)
      }
    }
    const aggregate = sha256(manifestEntry.data)
    const headerHash = String(pkg.headers['x-skill-sha256'] ?? '').trim().toLowerCase()
    if (headerHash && headerHash !== aggregate) {
      throw new SkillCacheError('包聚合哈希与响应头 X-Skill-Sha256 不一致——拒绝落盘')
    }
    const headerFiles = String(pkg.headers['x-skill-files'] ?? '').trim()
    if (headerFiles && headerFiles !== String(fileEntries.length)) {
      throw new SkillCacheError('包文件数与响应头 X-Skill-Files 不一致——拒绝落盘')
    }
    // 注意：这里**不**拿 manifest.package_sha256 与 aggregate 对账——两者口径不同
    // （服务端 DB 行打包指纹 vs 包内 MANIFEST 聚合哈希），比对必误报「拒绝落盘」。
    // package_sha256 只落进清单的 remote_sha 供下次二级比对（2026-11-03 修复）。

    await fsp.mkdir(dest, { recursive: true })
    const tmpRoot = await fsp.mkdtemp(path.join(dest, `.${name}.fetch-`))
    const staged = path.join(tmpRoot, 'staged')
    try {
      for (const entry of entries) {
        const rel = normalizeRel(entry.path)
        if (rel === PACKAGE_MANIFEST) continue
        const out = path.join(staged, rel)
        await fsp.mkdir(path.dirname(out), { recursive: true })
        await fsp.writeFile(out, entry.data)
      }
      const manifest: SkillLocalManifest = {
        name,
        version: remote?.version ?? '',
        content_hash: aggregate,
        // 服务端指纹（不透明）：下次二级比对用它，不能用 aggregate 代替
        remote_sha: String(remote?.package_sha256 ?? '').trim().toLowerCase(),
        files: fileEntries,
        fetched_at: nowIso(),
        catalog_version: catalogVersion,
      }
      const stagedSkill = path.join(staged, name)
      await fsp.mkdir(stagedSkill, { recursive: true })
      await fsp.writeFile(path.join(stagedSkill, MANIFEST_FILE), JSON.stringify(manifest, null, 2) + '\n', 'utf8')

      const target = path.join(dest, name)
      let backup: string | null = null
      try {
        await fsp.rename(target, path.join(tmpRoot, '.previous'))
        backup = path.join(tmpRoot, '.previous')
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e
      }
      try {
        await fsp.rename(stagedSkill, target)
      } catch (e) {
        if (backup) await fsp.rename(backup, target)
        throw new SkillCacheError(`落盘失败（已回滚原目录）: ${(e as Error).message}`)
      }
      return { path: target, sha256: aggregate, files: fileEntries.length, cached: false }
    } finally {
      await fsp.rm(tmpRoot, { recursive: true, force: true }).catch(() => undefined)
    }
  }
}
