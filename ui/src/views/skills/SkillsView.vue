<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  deleteSkill, downloadSkillPackage, getCatalogVersion, getSkillContent, getSkillFile,
  getSkillManifest, listSkillPackageFiles, listSkillsIndex, putSkill, putSkillFile, renameSkill,
  type PackageFileEntry, type SkillContent, type SkillIndexItem, type SkillManifest,
} from '../../api/skills'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'
import { renderMarkdown } from '../../utils/markdown'

// 技能条目（L0 索引 + L1 摘要混合视图，T-106：改吃四级披露读侧端点）
interface SkillMeta {
  name: string
  description: string
  category: string
  tags: string[]
  pattern: string
  version: string
  source: string
}

const metas = ref<SkillMeta[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const searchQ = ref('')
const activeCat = ref('全部')

const detail = ref<SkillMeta | null>(null)
const detailBody = ref('')
const detailOpen = ref(false)
const editOpen = ref(false)
const editContent = ref('')
const editName = ref('')

// 技能包能力面：目录版本指纹 + 单技能 manifest + 包文件清单（整包下载后浏览器端解析）
const catalogVersion = ref('')
const manifest = ref<SkillManifest | null>(null)
const pkg = ref<{ entries: PackageFileEntry[]; sha256: string } | null>(null)
const pkgLoading = ref(false)
// 附件只读查看（点文件列表条目打开）
const fileOpen = ref(false)
const fileView = ref<{ path: string; text: string; sha256: string; truncated: boolean; binary: boolean } | null>(null)
const fileSaving = ref(false)
// 改名（墓碑制：旧名留 superseded_by，入向 uses 引用需自行跟进）
const renameOpen = ref(false)
const renameNew = ref('')
const renameError = ref('')

// 可安全以文本预览的扩展名（其余按二进制截断预览，避免乱码刷屏）
const TEXT_EXTS = new Set([
  'md', 'txt', 'py', 'json', 'yaml', 'yml', 'sh', 'toml', 'csv', 'html', 'css',
  'js', 'ts', 'bat', 'ps1', 'cfg', 'ini', 'xml', 'svg', '',
])
function isTextPath(path: string): boolean {
  const leaf = path.split('/').pop() || ''
  const dot = leaf.lastIndexOf('.')
  return TEXT_EXTS.has(dot > 0 ? leaf.slice(dot + 1).toLowerCase() : '')
}
function shortSha(sha: string): string {
  return sha ? `${sha.slice(0, 12)}…` : '—'
}
function fmtSize(n: number): string {
  if (!n) return '—'
  return n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`
}

const categories = computed(() => {
  const set = new Set<string>(['全部'])
  metas.value.forEach((s) => set.add(s.category || '未分类'))
  return [...set]
})

const statCards = computed(() => {
  const t = total.value || metas.value.length
  const cats = new Set(metas.value.map((s) => s.category)).size
  const described = metas.value.filter((s) => s.description).length
  const hot = metas.value.filter((s) => s.pattern === 'auto').length
  return [
    { label: '总技能数', value: String(t), icon: '🧰', bg: 'rgba(59,130,246,.1)', color: '#3B82F6' },
    { label: '分类数', value: String(cats), icon: '🏷', bg: 'rgba(16,185,129,.12)', color: '#0f9d72' },
    { label: '热集(auto)', value: String(hot), icon: '🔥', bg: 'rgba(245,158,11,.13)', color: '#b45309' },
    { label: '含描述', value: String(described), icon: '✅', bg: 'rgba(99,102,241,.1)', color: '#6366F1' },
  ]
})

const filtered = computed(() => {
  const q = searchQ.value.trim().toLowerCase()
  return metas.value.filter((s) => {
    if (activeCat.value !== '全部' && (s.category || '未分类') !== activeCat.value) return false
    if (!q) return true
    return (
      s.name.toLowerCase().includes(q) ||
      s.description.toLowerCase().includes(q) ||
      s.tags.some((t) => t.toLowerCase().includes(q))
    )
  })
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    // T-106：一次拉全量索引（limit=500 覆盖当前规模），不再逐个 getSkill 拉 content
    const idx = await listSkillsIndex()
    metas.value = idx.skills.map((s: SkillIndexItem) => ({
      name: s.name,
      description: s.description || '',
      category: s.category || '',
      tags: s.tags || [],
      pattern: (s as unknown as Record<string, unknown>).pattern as string || 'manual',
      version: s.version || '',
      source: s.source || '',
    }))
    total.value = idx.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
  // 技能包能力面：目录版本指纹（旁路加载，失败不阻塞列表）
  try {
    const cv = await getCatalogVersion()
    catalogVersion.value = cv.catalog_version || ''
  } catch {
    catalogVersion.value = ''
  }
}

async function openDetail(m: SkillMeta) {
  try {
    // 详情抽屉直接渲染 L2 全文（GET /v1/skills/{name}，agent key），修复只显示空大纲的问题
    const g: SkillContent = await getSkillContent(m.name)
    detail.value = { ...m }
    const src = g.source || m.source || 'git'
    const sha = (g.sha256 || '').slice(0, 12)
    detailBody.value = `> 来源: ${src}${sha ? ` · sha256: ${sha}…` : ''}\n\n${g.content || '(无正文)'}`
    detailOpen.value = true
    closePkg()
    await loadManifest(m.name)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}
function closeDetail() {
  detailOpen.value = false
  detail.value = null
  detailBody.value = ''
  closePkg()
}

// ---------- 技能包区块 ----------

/** 打开详情时拉单技能 manifest（package_sha256 版本指纹），包清单按需解析。
 * 拉不到（技能库未启用 / 内置协议技能不在 skills 表）静默——占位文案已解释。 */
async function loadManifest(name: string) {
  manifest.value = null
  try {
    manifest.value = await getSkillManifest(name)
  } catch {
    /* 占位文案兜底 */
  }
}

async function loadPackageFiles() {
  if (!detail.value || pkgLoading.value) return
  pkgLoading.value = true
  try {
    const r = await listSkillPackageFiles(detail.value.name)
    pkg.value = { entries: r.entries, sha256: r.sha256 }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    pkgLoading.value = false
  }
}

function closePkg() {
  pkg.value = null
  pkgLoading.value = false
}

async function downloadPackage() {
  if (!detail.value) return
  try {
    const r = await downloadSkillPackage(detail.value.name)
    if (r.files) error.value = ''
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

// ---------- 附件查看（只读预览 + 可选保存） ----------

async function openFile(entry: PackageFileEntry) {
  if (!detail.value) return
  // 二进制成员不预览（blob.text() 会出乱码），引导走整包下载
  if (!isTextPath(entry.path)) {
    fileView.value = {
      path: entry.path,
      text: `二进制文件（${fmtSize(entry.size)}），不提供在线预览——请用「下载整包 (tar.gz)」取回查看。`,
      sha256: entry.sha256,
      truncated: false,
      binary: true,
    }
    fileOpen.value = true
    return
  }
  fileOpen.value = true
  fileView.value = { path: entry.path, text: '加载中…', sha256: entry.sha256, truncated: false, binary: false }
  try {
    const r = await getSkillFile(detail.value.name, entry.path)
    const truncated = r.text.length > 200_000
    fileView.value = {
      path: entry.path,
      text: truncated ? r.text.slice(0, 200_000) : r.text,
      sha256: r.sha256,
      truncated,
      binary: false,
    }
  } catch (e) {
    fileOpen.value = false
    fileView.value = null
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function saveFile() {
  if (!detail.value || !fileView.value) return
  fileSaving.value = true
  try {
    await putSkillFile(detail.value.name, fileView.value.path, fileView.value.text)
    error.value = ''
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    fileSaving.value = false
  }
}

// ---------- 改名（二次确认；墓碑制，入向 uses 引用需跟进） ----------

function askRename() {
  if (!detail.value) return
  renameNew.value = detail.value.name
  renameError.value = ''
  renameOpen.value = true
}

async function doRename() {
  if (!detail.value) return
  const oldName = detail.value.name
  const newName = renameNew.value.trim()
  if (!newName || newName === oldName) {
    renameError.value = '请输入与当前不同的新名称'
    return
  }
  if (!confirm(`确定把技能「${oldName}」改名为「${newName}」吗？\n\n改名采用墓碑制：旧名将留 superseded_by 标记，所有引用旧名的 uses / 入向调用需要自行跟进更新。`)) return
  try {
    await renameSkill(oldName, newName)
    renameOpen.value = false
    error.value = ''
    closeDetail()
    await load()
  } catch (e) {
    renameError.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function openEdit(m?: SkillMeta) {
  const target = m || detail.value
  if (!target) return
  try {
    const g = await getSkillFull(target.name)
    editName.value = target.name
    editContent.value = g.content
    editOpen.value = true
    detailOpen.value = false
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}
function closeEdit() {
  editOpen.value = false
  editContent.value = ''
  editName.value = ''
}

// 编辑需要字节级全文——走旧 admin GET（治理版写侧 PUT 兼容）
async function getSkillFull(name: string): Promise<{ name: string; content: string }> {
  const mod = await import('../../api/skills')
  return mod.getSkill(name)
}

async function saveEdit() {
  if (!editName.value.trim()) return
  try {
    await putSkill(editName.value.trim(), editContent.value)
    closeEdit()
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function removeSkill(m: SkillMeta) {
  if (!confirm(`确定删除技能「${m.name}」吗？将先软删（deprecated 标记）。`)) return
  try {
    await deleteSkill(m.name)
    closeDetail()
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function copyCallCode(m: SkillMeta) {
  const code = `from sgme.operations.skills import skill_get\nresult = skill_get(${JSON.stringify(m.name)})`
  navigator.clipboard?.writeText(code).catch(() => {})
}

onMounted(load)
</script>

<template>
  <div class="skills">
    <div class="head">
      <h2>技能仓库</h2>
      <span class="sub">
        SGME Skills · {{ total }} 个技能（索引常驻 / 全文按需）
        <span v-if="catalogVersion" class="cat-ver" :title="`技能目录整体版本指纹：${catalogVersion}`">
          · 目录版本 <code>{{ catalogVersion.slice(0, 12) }}{{ catalogVersion.length > 12 ? '…' : '' }}</code>
        </span>
      </span>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <template v-else>
      <!-- 搜索 + 分类筛选 -->
      <div class="panel search-panel">
        <div class="search-row">
          <div class="input-wrap">
            <span class="gs-ico">🔎</span>
            <input v-model="searchQ" type="search" placeholder="搜索技能名称、描述或标签…" />
          </div>
        </div>
        <div class="tag-row">
          <span
            v-for="c in categories"
            :key="c"
            class="tag-capsule"
            :class="activeCat === c ? 'cap-active' : 'cap-pill'"
            @click="activeCat = c"
          >{{ c }}</span>
        </div>
      </div>

      <!-- 统计卡 -->
      <div class="stat-grid4">
        <div v-for="c in statCards" :key="c.label" class="stat-card4 panel">
          <div class="stat-ico" :style="{ background: c.bg, color: c.color }">
            <span class="stat-emoji">{{ c.icon }}</span>
          </div>
          <div class="stat-body">
            <div class="stat-v">{{ c.value }}</div>
            <div class="stat-l">{{ c.label }}</div>
          </div>
        </div>
      </div>

      <!-- 技能卡片网格 -->
      <p v-if="!filtered.length" class="empty">暂无技能。wiki 的 skill:* 页已迁移为正式技能；新技能经 PUT /v1/admin/skills/{name} 写入。</p>
      <div v-else class="grid">
        <div v-for="s in filtered" :key="s.name" class="skill-card panel" @click="openDetail(s)">
          <div class="card-top">
            <div class="skill-ico">🧰</div>
            <span class="tag-capsule" :class="s.pattern === 'auto' ? 'cap-hot' : 'cap-pill'">{{ s.pattern === 'auto' ? '🔥 热集' : '按需' }}</span>
          </div>
          <h3 class="skill-name">{{ s.name }}</h3>
          <p class="skill-desc">{{ s.description || '（无描述）' }}</p>
          <div class="tag-row">
            <span v-if="s.category" class="tag-pill">{{ s.category }}</span>
            <span v-for="t in s.tags.slice(0, 3)" :key="t" class="tag-pill">{{ t }}</span>
          </div>
        </div>
      </div>
    </template>

    <!-- 详情抽屉（L1 摘要：骨架+溯源） -->
    <div v-if="detailOpen && detail" class="overlay" @click.self="closeDetail">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">{{ detail.name }}</span>
          <button class="close" @click="closeDetail">✕</button>
        </div>
        <div class="drawer-body">
          <div class="detail-meta">
            <span class="tag-capsule" :class="detail.pattern === 'auto' ? 'cap-hot' : 'cap-pill'">{{ detail.pattern === 'auto' ? '🔥 热集' : '按需' }}</span>
            <span v-if="detail.version" class="tag-pill">v{{ detail.version }}</span>
            <span v-if="detail.category" class="tag-pill">{{ detail.category }}</span>
            <span v-for="t in detail.tags" :key="t" class="tag-pill">{{ t }}</span>
          </div>
          <div class="markdown-content" v-html="renderMarkdown(detailBody)" />

          <!-- 技能包区块：manifest 版本指纹 + 包文件清单 + 整包下载 -->
          <div class="pkg-block">
            <div class="pkg-head">
              <span class="pkg-title">📦 技能包</span>
              <button class="btn btn-sm" @click="loadPackageFiles">
                {{ pkgLoading ? '解析中…' : pkg ? '刷新清单' : '查看包文件' }}
              </button>
              <button class="btn btn-sm" @click="downloadPackage">下载整包 (tar.gz)</button>
            </div>
            <div v-if="manifest" class="pkg-manifest">
              <div class="pkg-row"><span class="pkg-k">版本</span><span>v{{ manifest.version || '—' }}</span></div>
              <div class="pkg-row"><span class="pkg-k">包指纹 (package_sha256)</span><code class="sha-code">{{ shortSha(manifest.package_sha256) }}</code></div>
              <div class="pkg-row"><span class="pkg-k">正文大小</span><span>{{ fmtSize(manifest.content_len) }}</span></div>
              <div class="pkg-row"><span class="pkg-k">更新时间</span><span>{{ fmtTs(manifest.updated_at) }}</span></div>
            </div>
            <p v-else class="pkg-empty">manifest 未加载（技能库未启用或技能不在索引中）</p>

            <template v-if="pkg">
              <p class="pkg-files-cap">
                包文件清单（{{ pkg.entries.length }} 个文件{{ pkg.sha256 ? ` · 聚合 sha256 ${shortSha(pkg.sha256)}` : '' }}，来自整包 MANIFEST.sha256）
              </p>
              <div class="pkg-files">
                <button
                  v-for="f in pkg.entries"
                  :key="f.path"
                  class="pkg-file"
                  :title="`查看附件：${f.path}（sha256 ${f.sha256}）`"
                  @click="openFile(f)"
                >
                  <span class="pkg-file-name">{{ f.path }}</span>
                  <span class="pkg-file-meta">
                    <code class="sha-code">{{ shortSha(f.sha256) }}</code>
                    <template v-if="f.size"> · {{ fmtSize(f.size) }}</template>
                  </span>
                </button>
              </div>
              <p class="pkg-hint">点文件名查看附件内容（只读预览，可编辑保存）</p>
            </template>
          </div>
        </div>
        <div class="drawer-foot">
          <button class="btn" @click="copyCallCode(detail)">复制调用代码</button>
          <button class="btn" @click="openEdit(detail)">查看/编辑全文</button>
          <button class="btn" @click="askRename">改名…</button>
          <button class="btn btn-danger" @click="removeSkill(detail)">删除</button>
        </div>
      </div>
    </div>

    <!-- 编辑抽屉 -->
    <div v-if="editOpen" class="overlay" @click.self="closeEdit">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">编辑 {{ editName }}</span>
          <button class="close" @click="closeEdit">✕</button>
        </div>
        <div class="drawer-body edit-body">
          <textarea v-model="editContent" class="edit-textarea" spellcheck="false" />
        </div>
        <div class="drawer-foot">
          <button class="btn" @click="closeEdit">取消</button>
          <button class="btn btn-primary" @click="saveEdit">保存技能</button>
        </div>
      </div>
    </div>

    <!-- 附件查看抽屉（GET /v1/skills/{name}/files/{relpath} 原文；可编辑保存） -->
    <div v-if="fileOpen && fileView" class="overlay" @click.self="fileOpen = false">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">📎 {{ fileView.path }}</span>
          <button class="close" @click="fileOpen = false">✕</button>
        </div>
        <div class="drawer-body">
          <p class="file-meta">
            sha256 <code class="sha-code">{{ shortSha(fileView.sha256) }}</code>
            <span v-if="fileView.truncated" class="file-warn">（内容过大，仅显示前 200 KB）</span>
          </p>
          <textarea
            v-model="fileView.text"
            class="edit-textarea file-text"
            spellcheck="false"
            :readonly="fileView.binary"
          />
        </div>
        <div class="drawer-foot">
          <button class="btn" @click="fileOpen = false">关闭</button>
          <button v-if="!fileView.binary" class="btn btn-primary" :disabled="fileSaving" @click="saveFile">
            {{ fileSaving ? '保存中…' : '保存修改' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 改名对话框（POST /v1/admin/skills/{name}/rename；二次确认在 doRename 内） -->
    <div v-if="renameOpen" class="overlay" @click.self="renameOpen = false">
      <div class="drawer drawer-sm">
        <div class="drawer-head">
          <span class="drawer-title">✏️ 改名：{{ detail?.name }}</span>
          <button class="close" @click="renameOpen = false">✕</button>
        </div>
        <div class="drawer-body">
          <p class="rename-warn">
            改名采用<strong>墓碑制</strong>：旧名将保留 superseded_by 标记，所有引用旧名的
            <code>uses</code> / 入向调用不会自动跟进，需自行更新。
          </p>
          <input
            v-model="renameNew"
            class="rename-input"
            placeholder="新技能名（小写字母/数字/连字符）"
            @keyup.enter="doRename"
          />
          <p v-if="renameError" class="error">{{ renameError }}</p>
        </div>
        <div class="drawer-foot">
          <button class="btn" @click="renameOpen = false">取消</button>
          <button class="btn btn-primary" @click="doRename">下一步（需再次确认）</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-panel { margin-bottom: 16px; }
.search-row { display: flex; gap: 8px; }
.input-wrap {
  flex: 1; display: flex; align-items: center; gap: 8px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
  padding: 4px 6px 4px 12px;
}
.input-wrap input { flex: 1; border: none; background: transparent; outline: none; padding: 6px 0; }
.tag-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.tag-capsule { cursor: pointer; padding: 4px 12px; border-radius: 999px; font-size: 12px; font-weight: 500; }
.cap-active { background: rgba(59,130,246,.12); color: #3B82F6; }
.cap-green { background: rgba(16,185,129,.12); color: #0f9d72; }
.cap-red { background: rgba(239,68,68,.12); color: #ef4444; }
.cap-hot { background: rgba(245,158,11,.15); color: #b45309; }
.cap-pill { background: var(--surface-muted); color: var(--text-muted); }
.tag-row .tag-pill { padding: 2px 10px; border-radius: 999px; background: var(--surface-muted); color: var(--text-muted); font-size: 11px; }

.stat-grid4 { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat-card4 { display: flex; gap: 12px; align-items: center; padding: 12px 16px; }
.stat-ico { width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center; font-size: 18px; }
.stat-body { display: flex; flex-direction: column; }
.stat-v { font-size: 22px; font-weight: 700; color: var(--text); letter-spacing: -0.4px; }
.stat-l { font-size: 12px; color: var(--text-muted); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
.skill-card { cursor: pointer; transition: border-color .12s, box-shadow .12s; }
.skill-card:hover { border-color: var(--brand); box-shadow: var(--shadow-md); }
.card-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.skill-ico { width: 34px; height: 34px; border-radius: 9px; background: rgba(59,130,246,.1); display: grid; place-items: center; font-size: 16px; }
.skill-name { margin: 0 0 6px; font-size: 16px; font-weight: 600; color: var(--text); }
.skill-desc { margin: 0 0 8px; font-size: 13px; color: var(--text-muted); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

.detail-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.edit-body { padding: var(--space-5); }
.edit-textarea { width: 100%; height: 100%; min-height: 60vh; font-family: var(--font-mono); font-size: 13px; line-height: 1.6; resize: none; }

/* 页头目录版本指纹 */
.cat-ver code { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }

/* 技能包区块 */
.pkg-block { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--divider); }
.pkg-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.pkg-title { font-weight: 600; font-size: 14px; color: var(--text); margin-right: 4px; }
.btn-sm { padding: 3px 10px; font-size: 12px; }
.pkg-manifest { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 6px 16px; margin-bottom: 10px; }
.pkg-row { display: flex; gap: 8px; align-items: baseline; font-size: 12px; color: var(--text); min-width: 0; }
.pkg-k { color: var(--text-muted); flex-shrink: 0; }
.sha-code { font-family: var(--font-mono); font-size: 11px; background: var(--surface-muted); padding: 1px 6px; border-radius: 4px; color: var(--text-muted); word-break: break-all; }
.pkg-empty { font-size: 12px; color: var(--text-muted); margin: 4px 0 8px; }
.pkg-files-cap { font-size: 12px; color: var(--text-muted); margin: 10px 0 6px; }
.pkg-files { display: flex; flex-direction: column; gap: 4px; max-height: 260px; overflow-y: auto; }
.pkg-file {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 5px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); cursor: pointer; text-align: left; font-size: 12px;
}
.pkg-file:hover { border-color: var(--brand); background: var(--brand-soft); }
.pkg-file-name { font-family: var(--font-mono); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pkg-file-meta { color: var(--text-muted); flex-shrink: 0; }
.pkg-hint { font-size: 11px; color: var(--text-muted); margin-top: 6px; }

/* 附件查看 */
.file-meta { font-size: 12px; color: var(--text-muted); margin: 0 0 8px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.file-warn { color: #b45309; }
.file-text { min-height: 55vh; }

/* 改名对话框 */
.drawer-sm { max-width: 460px; width: min(460px, 92vw); }
.rename-warn { font-size: 13px; line-height: 1.7; color: var(--text); background: rgba(245,158,11,.1); border: 1px solid rgba(245,158,11,.3); border-radius: 8px; padding: 10px 14px; margin: 0 0 12px; }
.rename-input { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--text); font-family: var(--font-mono); font-size: 13px; outline: none; }
.rename-input:focus { border-color: var(--brand); }

/* 轻量 markdown 渲染 */
.markdown-content { font-size: 14px; line-height: 1.7; color: var(--text); }
.markdown-content :deep(h1), .markdown-content :deep(h2), .markdown-content :deep(h3), .markdown-content :deep(h4) {
  font-weight: 600; margin: 18px 0 8px; color: var(--text);
}
.markdown-content :deep(h1) { font-size: 22px; padding-bottom: 6px; border-bottom: 1px solid var(--divider); }
.markdown-content :deep(h2) { font-size: 18px; }
.markdown-content :deep(h3) { font-size: 16px; }
.markdown-content :deep(p) { margin: 8px 0; }
.markdown-content :deep(code) { background: var(--surface-muted); padding: 2px 6px; border-radius: 4px; font-family: var(--font-mono); font-size: 13px; color: var(--danger); }
.markdown-content :deep(pre) { background: var(--code-bg); color: var(--code-text); padding: 14px; border-radius: 8px; overflow-x: auto; margin: 12px 0; }
.markdown-content :deep(pre code) { background: transparent; color: inherit; padding: 0; }
.markdown-content :deep(ul), .markdown-content :deep(ol) { padding-left: 22px; margin: 8px 0; }
.markdown-content :deep(li) { margin: 4px 0; }
.markdown-content :deep(blockquote) { border-left: 4px solid var(--brand); padding: 8px 14px; color: var(--text-muted); background: var(--brand-soft); border-radius: 0 8px 8px 0; margin: 10px 0; }
.markdown-content :deep(a) { color: var(--brand-text); }
</style>
