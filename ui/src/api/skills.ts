// 技能仓库 API 封装（T-106：读侧四级披露端点 sgme/server/routes_skills.py；agent key 鉴权）
// 技能包通道（T-281：manifest/整包/单文件读；写侧资产 PUT 与 rename 见 routes_skills_admin）
import { api } from './client'

// L0 索引条目（GET /v1/skills?limit=&offset=）
export interface SkillIndexItem {
  name: string
  description: string
  category: string
  tags: string[]
  pattern?: string
  version?: string
  source?: string
}

export interface SkillsIndex {
  skills: SkillIndexItem[]
  total: number
  returned: number
  offset?: number
  budget?: number
}

// L1 摘要（GET /v1/skills/{name}/digest）
export interface SkillDigest {
  name: string
  description: string
  version: string
  pattern: string
  category: string
  tags: string[]
  uses: string[]
  sections: unknown[]
  sha256: string
  source: string
  origin_path?: string
}

export interface SkillsList {
  enabled: boolean
  mode: string
  path: string
  total: number
  skills: string[]
}

export interface SkillGet {
  name: string
  content: string
}

// L2 全文（agent key；详情页正文渲染用，GET /v1/skills/{name}）
export interface SkillContent {
  name: string
  content: string
  sha256?: string
  source?: string
}

export interface SkillPut {
  name: string
  path: string
}

/** L0 全量索引（分页拉取；limit=500 覆盖当前 400+ 规模） */
export function listSkillsIndex(limit = 500, offset = 0) {
  return api.get<SkillsIndex>(
    `/v1/skills?limit=${limit}&offset=${offset}`
  )
}

/** L1 摘要（审核媒介层） */
export function getSkillDigest(name: string) {
  return api.get<SkillDigest>(`/v1/skills/${encodeURIComponent(name)}/digest`)
}

/** 冷启动包（T-106 M5）：索引全量 + 热集全文 + SGME 操作手册 */
export interface ColdstartPack {
  index: { items: SkillIndexItem[]; total: number }
  hotset: { name: string; content: string; sha256: string }[]
  manual: { page_id: string; title: string; content: string } | null
}

export function getColdstart() {
  return api.get<ColdstartPack>('/v1/skills/coldstart')
}

// ---------- 旧 admin 直写接口（写侧仍走治理版 PUT/DELETE，见 routes_skills_admin） ----------

export function listSkills() {
  return api.get<SkillsList>('/v1/admin/skills')
}

export function getSkill(name: string) {
  return api.get<SkillGet>(`/v1/admin/skills/${encodeURIComponent(name)}`)
}

/** L2 全文（agent key；详情页正文渲染，GET /v1/skills/{name}） */
export function getSkillContent(name: string) {
  return api.get<SkillContent>(`/v1/skills/${encodeURIComponent(name)}`)
}

export function putSkill(name: string, content: string) {
  return api.put<SkillPut>(`/v1/admin/skills/${encodeURIComponent(name)}`, { content })
}

export function deleteSkill(name: string) {
  return api.delete<{ deleted: boolean }>(`/v1/admin/skills/${encodeURIComponent(name)}`)
}

// ---------- 技能包通道（T-281 / D-59：catalog-version、manifest、整包、单文件） ----------

/** 目录版本指纹（GET /v1/skills/catalog-version；skills 库未启用时值为 "disabled"） */
export interface CatalogVersion {
  catalog_version: string
}

export function getCatalogVersion() {
  return api.get<CatalogVersion>('/v1/skills/catalog-version')
}

/** 单技能清单（GET /v1/skills/{name}/manifest）。注意：后端契约无 files 字段——
 * 文件清单在整包 tar.gz 的 MANIFEST.sha256 里（见 parseSkillPackage）。 */
export interface SkillManifest {
  name: string
  version: string
  package_sha256: string
  content_len: number
  updated_at: string
}

export function getSkillManifest(name: string) {
  return api.get<SkillManifest>(`/v1/skills/${encodeURIComponent(name)}/manifest`)
}

/** 触发浏览器下载整包 tar.gz（fetch blob 后 a[download]，带 X-API-Key，不能用裸 <a href>）。
 * 返回响应头（X-Skill-Sha256 聚合哈希 / X-Skill-Files 文件数）供 UI 展示。 */
export async function downloadSkillPackage(name: string) {
  const { blob, headers } = await api.blob(`/v1/skills/${encodeURIComponent(name)}/package`)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${name}.tar.gz`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
  return { sha256: headers.get('X-Skill-Sha256') || '', files: headers.get('X-Skill-Files') || '' }
}

/** 单文件附件原文（GET /v1/skills/{name}/files/{relpath}；octet-stream + X-File-Sha256） */
export async function getSkillFile(name: string, relpath: string) {
  const { blob, headers } = await api.blob(
    `/v1/skills/${encodeURIComponent(name)}/files/${relpath.split('/').map(encodeURIComponent).join('/')}`
  )
  return { text: await blob.text(), sha256: headers.get('X-File-Sha256') || '' }
}

/** 写技能资产文件（PUT /v1/admin/skills/{name}/files/{relpath}；admin key。
 * 服务端白名单：仅 references/ scripts/ assets/ 首段目录，其他路径会被 400 拒绝） */
export function putSkillFile(name: string, relpath: string, content: string) {
  return api.put<{ ok: boolean; path?: string }>(
    `/v1/admin/skills/${encodeURIComponent(name)}/files/${relpath.split('/').map(encodeURIComponent).join('/')}`,
    { content }
  )
}

/** 技能改名（POST /v1/admin/skills/{name}/rename；墓碑制——旧名留 superseded_by 墓碑） */
export function renameSkill(name: string, newName: string) {
  return api.post<{ ok: boolean }>(`/v1/admin/skills/${encodeURIComponent(name)}/rename`, {
    new_name: newName,
  })
}

// ---------- tar.gz 浏览器端解析（包文件清单在归档根 MANIFEST.sha256，无独立 JSON 端点） ----------

export interface PackageFileEntry {
  path: string
  sha256: string
  size: number
}

/** 清单文本 → 文件条目（canonical 行格式：`<64位sha256>  <relpath>`，两个空格分隔） */
export function parseManifestText(text: string): PackageFileEntry[] {
  const entries: PackageFileEntry[] = []
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    const m = line.match(/^([0-9a-f]{64})\s\s(.+)$/)
    if (m) entries.push({ path: m[2], sha256: m[1], size: 0 })
  }
  return entries
}

/** 解包 tar.gz 成员（DecompressionStream gzip + 512 字节 tar 头顺序扫描）。
 * 返回 归档内路径 → 字节（含根 MANIFEST.sha256；pax/gnu 扩展头只取 path 不留成员）。
 * 浏览器不支持 DecompressionStream 时抛出带提示的异常。 */
export async function extractTarGz(blob: Blob): Promise<Map<string, Uint8Array>> {
  if (typeof DecompressionStream !== 'function') {
    throw new Error('当前浏览器不支持 DecompressionStream，无法在线解析包清单（可下载整包查看）')
  }
  const ds = new DecompressionStream('gzip')
  const stream = blob.stream().pipeThrough(ds)
  const buf = new Uint8Array(await new Response(stream).arrayBuffer())

  const members = new Map<string, Uint8Array>()
  let pendingName: string | null = null // pax/gnu 长文件名：作用于下一个真实条目
  let off = 0
  while (off + 512 <= buf.length) {
    const header = buf.subarray(off, off + 512)
    if (header.every((b) => b === 0)) break // 两块全零 = 结束
    const nameBytes = header.subarray(0, 100)
    const nameEnd = nameBytes.indexOf(0)
    let name = new TextDecoder().decode(nameBytes.subarray(0, nameEnd < 0 ? 100 : nameEnd))
    const sizeStr = new TextDecoder().decode(header.subarray(124, 136)).replace(/\0.*$/, '').trim()
    const size = parseInt(sizeStr || '0', 8) || 0
    const type = String.fromCharCode(header[156] || 48)
    const dataStart = off + 512
    const data = buf.subarray(dataStart, dataStart + size)
    off = dataStart + Math.ceil(size / 512) * 512 // 数据按 512 对齐

    if (type === 'x' || type === 'g') {
      // pax 扩展头：记录形如 "<len> path=<value>\n"，取 path 供下一个条目用
      const text = new TextDecoder().decode(data)
      const m = text.match(/(?:^|\n)\d+ path=([^\n]+)\n/)
      pendingName = m ? m[1] : pendingName
      continue
    }
    if (type === 'L') {
      // gnu 长文件名：数据即下一个条目名
      pendingName = new TextDecoder().decode(data).replace(/\0.*$/, '')
      continue
    }
    if (type !== '0' && type !== '\0' && type !== '7') continue // 目录/链接等不收
    if (pendingName) {
      name = pendingName
      pendingName = null
    }
    members.set(name, new Uint8Array(data)) // 拷贝出独立视图（底层大缓冲不释放）
  }
  return members
}

/** 下载技能包并解析出文件清单：根 MANIFEST.sha256 → [{path, sha256, size}]。
 * size 从 tar 成员头补齐（清单行本身不带 size）。 */
export async function listSkillPackageFiles(
  name: string
): Promise<{ entries: PackageFileEntry[]; sha256: string; files: string }> {
  const { blob, headers } = await api.blob(`/v1/skills/${encodeURIComponent(name)}/package`)
  const members = await extractTarGz(blob)
  const manifestBlob = members.get('MANIFEST.sha256')
  if (!manifestBlob) throw new Error('包内缺少 MANIFEST.sha256（清单缺失，请检查服务端版本）')
  const entries = parseManifestText(new TextDecoder().decode(manifestBlob))
  for (const e of entries) {
    const data = members.get(`${name}/${e.path}`)
    if (data) e.size = data.length
  }
  return {
    entries,
    sha256: headers.get('X-Skill-Sha256') || '',
    files: headers.get('X-Skill-Files') || '',
  }
}
