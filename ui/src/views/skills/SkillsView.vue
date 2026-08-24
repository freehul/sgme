<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { deleteSkill, getSkill, listSkills, putSkill, type SkillsList } from '../../api/skills'
import { ApiError } from '../../api/client'
import { renderMarkdown } from '../../utils/markdown'

// 技能类型（分类/状态/描述）元数据映射
interface SkillMeta {
  name: string
  description: string
  category: string
  tags: string[]
  enabled: boolean
  content: string
}

const base = ref<SkillsList | null>(null)
const metas = ref<SkillMeta[]>([])
const loading = ref(false)
const error = ref('')

const searchQ = ref('')
const activeCat = ref('全部')

const detail = ref<SkillMeta | null>(null)
const detailOpen = ref(false)
const editOpen = ref(false)
const editContent = ref('')
const editName = ref('')

// 解析 SKILL.md 头部 frontmatter（约定同 Hermes skills：--- name/description/tags ---）
function parseSkill(name: string, content: string): SkillMeta {
  let description = ''
  let category = ''
  let tags: string[] = []
  const m = content.match(/^---\s*\n([\s\S]*?)\n---\s*\n/)
  if (m) {
    const fm = m[1]
    const desc = fm.match(/^description:\s*(.+)$/m)
    if (desc) description = desc[1].trim()
    const cat = fm.match(/^category:\s*(.+)$/m)
    if (cat) category = cat[1].trim()
    const tg = fm.match(/^tags:\s*\[([^\]]*)\]/m)
    if (tg) tags = tg[1].split(',').map((t) => t.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean)
  }
  if (!description) {
    // 回退：取正文首个非标题段落第一行
    const body = content.replace(/^---[\s\S]*?---\s*\n?/, '')
    const par = body.split('\n').find((l) => l.trim() && !l.trim().startsWith('#'))
    if (par) description = par.trim().slice(0, 120)
  }
  if (!category) {
    const cats = tags.length ? tags : []
    category = cats[0] || '未分类'
  }
  return { name, description, category, tags, enabled: true, content }
}

const categories = computed(() => {
  const set = new Set<string>(['全部'])
  metas.value.forEach((s) => set.add(s.category))
  return [...set]
})

const statCards = computed(() => {
  const total = metas.value.length
  const cats = new Set(metas.value.map((s) => s.category)).size
  const described = metas.value.filter((s) => s.description).length
  const chars = metas.value.reduce((a, s) => a + s.content.length, 0)
  return [
    { label: '总技能数', value: String(total), icon: '🧰', bg: 'rgba(59,130,246,.1)', color: '#3B82F6' },
    { label: '分类数', value: String(cats), icon: '🏷', bg: 'rgba(16,185,129,.12)', color: '#0f9d72' },
    { label: '含描述', value: String(described), icon: '✅', bg: 'rgba(245,158,11,.13)', color: '#b45309' },
    { label: '内容(KB)', value: (chars / 1024).toFixed(1), icon: '📄', bg: 'rgba(99,102,241,.1)', color: '#6366F1' },
  ]
})

const filtered = computed(() => {
  const q = searchQ.value.trim().toLowerCase()
  return metas.value.filter((s) => {
    if (activeCat.value !== '全部' && s.category !== activeCat.value) return false
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
    base.value = await listSkills()
    const contents = await Promise.all(base.value.skills.map((n) => getSkill(n)))
    metas.value = contents.map((c) => parseSkill(c.name, c.content))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function openDetail(m: SkillMeta) {
  try {
    const g = await getSkill(m.name)
    detail.value = parseSkill(g.name, g.content)
    detailOpen.value = true
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}
function closeDetail() {
  detailOpen.value = false
  detail.value = null
}

function openEdit(m?: SkillMeta) {
  const target = m || detail.value
  if (!target) return
  editName.value = target.name
  editContent.value = target.content
  editOpen.value = true
  detailOpen.value = false
}
function closeEdit() {
  editOpen.value = false
  editContent.value = ''
  editName.value = ''
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
  if (!confirm(`确定删除技能「${m.name}」吗？此操作不可撤销。`)) return
  try {
    await deleteSkill(m.name)
    closeDetail()
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function copyCallCode(m: SkillMeta) {
  const code = `from sgme import use_skill\nresult = use_skill(${JSON.stringify(m.name)}, {...})`
  navigator.clipboard?.writeText(code).catch(() => {})
}

onMounted(load)
</script>

<template>
  <div class="skills">
    <div class="head">
      <h2>技能仓库</h2>
      <span class="sub">{{ base?.mode }} 模式 · {{ base?.total }} 个技能</span>
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
      <p v-if="!filtered.length" class="empty">暂无技能。写入 SKILL.md 到仓库目录后刷新可见。</p>
      <div v-else class="grid">
        <div v-for="s in filtered" :key="s.name" class="skill-card panel" @click="openDetail(s)">
          <div class="card-top">
            <div class="skill-ico">🧰</div>
            <span class="tag-capsule" :class="s.enabled ? 'cap-green' : 'cap-red'">{{ s.enabled ? '已启用' : '已禁用' }}</span>
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

    <!-- 详情抽屉 -->
    <div v-if="detailOpen && detail" class="overlay" @click.self="closeDetail">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">{{ detail.name }}</span>
          <button class="close" @click="closeDetail">✕</button>
        </div>
        <div class="drawer-body">
          <div class="detail-meta">
            <span class="tag-capsule" :class="detail.enabled ? 'cap-green' : 'cap-red'">{{ detail.enabled ? '已启用' : '已禁用' }}</span>
            <span v-if="detail.category" class="tag-pill">{{ detail.category }}</span>
            <span v-for="t in detail.tags" :key="t" class="tag-pill">{{ t }}</span>
          </div>
          <div class="markdown-content" v-html="renderMarkdown(detail.content)" />
        </div>
        <div class="drawer-foot">
          <button class="btn" @click="copyCallCode(detail)">复制调用代码</button>
          <button class="btn" @click="openEdit(detail)">编辑</button>
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