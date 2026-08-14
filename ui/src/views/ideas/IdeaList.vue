<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  appendNote,
  createIdea,
  listIdeas,
  promoteIdea,
  restoreIdea,
  setFlag,
  softDeleteIdea,
  updateIdea,
  type Idea,
} from '../../api/ideas'
import { ApiError } from '../../api/client'

const router = useRouter()

const rows = ref<Idea[]>([])
const total = ref(0)
const page = ref(1)
const limit = 20
const q = ref('')
const flagFilter = ref('')
const loading = ref(false)
const error = ref('')

// 编辑/备注/升格弹层状态
const editing = ref<Idea | null>(null)
const editContent = ref('')
const editPriority = ref(50)
const noting = ref<Idea | null>(null)
const noteText = ref('')
const promoting = ref<Idea | null>(null)
const promoteTitle = ref('')
const promoteContent = ref('')

// 新建弹层（2026-08-13 用户定：创意由用户主动提出）
const creating = ref(false)
const newContent = ref('')
const newPriority = ref(50)
const newSource = ref('')

function openCreate() {
  creating.value = true
  newContent.value = ''
  newPriority.value = 50
  newSource.value = ''
}

async function saveCreate() {
  if (!newContent.value.trim()) return
  try {
    await createIdea({
      content: newContent.value.trim(),
      priority: newPriority.value,
      source_ref: newSource.value.trim() || undefined,
    })
    creating.value = false
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      limit,
      q: q.value || undefined,
      custom_flag: flagFilter.value || undefined,
    }
    const data = await listIdeas(params)
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function search() {
  page.value = 1
  load()
}

function openEdit(idea: Idea) {
  editing.value = idea
  editContent.value = idea.content
  editPriority.value = idea.priority
}

async function saveEdit() {
  if (!editing.value) return
  try {
    await updateIdea(editing.value.idea_id, {
      content: editContent.value,
      priority: editPriority.value,
    })
    editing.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function openNote(idea: Idea) {
  noting.value = idea
  noteText.value = ''
}

async function saveNote() {
  if (!noting.value || !noteText.value.trim()) return
  try {
    await appendNote(noting.value.idea_id, noteText.value.trim())
    noting.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function toggleFlag(idea: Idea) {
  const next = idea.custom_flag === 'promoted' ? null : 'promoted'
  try {
    await setFlag(idea.idea_id, next)
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function remove(idea: Idea) {
  if (!confirm(`确认删除创意「${idea.content.slice(0, 20)}」？（可恢复）`)) return
  try {
    await softDeleteIdea(idea.idea_id)
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function restore(idea: Idea) {
  try {
    await restoreIdea(idea.idea_id)
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function openPromote(idea: Idea) {
  promoting.value = idea
  promoteTitle.value = idea.content.slice(0, 30)
  promoteContent.value = idea.content
}

async function doPromote() {
  if (!promoting.value || !promoteTitle.value.trim()) return
  try {
    await promoteIdea(promoting.value.idea_id, {
      title: promoteTitle.value.trim(),
      content: promoteContent.value,
    })
    promoting.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function viewDetail(idea: Idea) {
  router.push({ name: 'idea-detail', params: { id: idea.idea_id } })
}
</script>

<template>
  <div class="ideas">
    <div class="head">
      <h2>创意池</h2>
      <div class="filters">
        <input v-model="q" placeholder="检索内容" @keyup.enter="search" />
        <select v-model="flagFilter" @change="search">
          <option value="">全部标记</option>
          <option value="promoted">已升格</option>
          <option value="pending">待处理</option>
        </select>
        <button @click="search">检索</button>
        <button class="primary" @click="openCreate">新建创意</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length" class="empty">暂无创意。点击「新建创意」记录你的想法（创意由你主动提出，不再自动捕获）。</p>

    <div v-else class="list">
      <div v-for="idea in rows" :key="idea.idea_id" class="card">
        <div class="card-head">
          <span class="prio">P{{ idea.priority }}</span>
          <span v-if="idea.custom_flag" class="flag">{{ idea.custom_flag }}</span>
          <span v-if="idea.status !== 'active'" class="status">{{ idea.status }}</span>
          <p class="content">{{ idea.content }}</p>
        </div>
        <div class="meta">
          <span v-if="idea.notes.length">备注 {{ idea.notes.length }} 条</span>
        </div>
        <div class="actions">
          <button @click="viewDetail(idea)">详情</button>
          <button @click="openEdit(idea)">编辑</button>
          <button @click="openNote(idea)">备注</button>
          <button @click="toggleFlag(idea)">{{ idea.custom_flag === 'promoted' ? '取消升格' : '升格' }}</button>
          <button v-if="idea.status === 'active'" class="danger" @click="remove(idea)">删除</button>
          <button v-else @click="restore(idea)">恢复</button>
          <span class="ts">{{ new Date(idea.updated_at).toLocaleString() }}</span>
        </div>
      </div>

      <div class="pager">
        <button :disabled="page <= 1" @click="page--; load()">上一页</button>
        <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
        <button :disabled="page * limit >= total" @click="page++; load()">下一页</button>
      </div>
    </div>

    <!-- 新建弹层 -->
    <div v-if="creating" class="modal">
      <div class="modal-box">
        <h3>新建创意</h3>
        <textarea v-model="newContent" rows="4" placeholder="记录你的想法/点子/灵感…" autofocus></textarea>
        <label>优先级 <input v-model.number="newPriority" type="number" min="0" max="100" /></label>
        <label>来源（可选）<input v-model="newSource" placeholder="如：对话 2026-08-13" /></label>
        <div class="modal-actions">
          <button @click="creating = false">取消</button>
          <button class="primary" :disabled="!newContent.trim()" @click="saveCreate">保存</button>
        </div>
      </div>
    </div>

    <!-- 编辑弹层 -->
    <div v-if="editing" class="modal">
      <div class="modal-box">
        <h3>编辑创意</h3>
        <textarea v-model="editContent" rows="4"></textarea>
        <label>优先级 <input v-model.number="editPriority" type="number" min="0" max="100" /></label>
        <div class="modal-actions">
          <button @click="editing = null">取消</button>
          <button class="primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>

    <!-- 备注弹层 -->
    <div v-if="noting" class="modal">
      <div class="modal-box">
        <h3>追加备注</h3>
        <textarea v-model="noteText" rows="3" placeholder="输入备注内容"></textarea>
        <div class="modal-actions">
          <button @click="noting = null">取消</button>
          <button class="primary" :disabled="!noteText.trim()" @click="saveNote">追加</button>
        </div>
      </div>
    </div>

    <!-- 升格弹层 -->
    <div v-if="promoting" class="modal">
      <div class="modal-box">
        <h3>升格为需求</h3>
        <label>需求标题 *</label>
        <input v-model="promoteTitle" />
        <label>需求描述</label>
        <textarea v-model="promoteContent" rows="3"></textarea>
        <div class="modal-actions">
          <button @click="promoting = null">取消</button>
          <button class="primary" :disabled="!promoteTitle.trim()" @click="doPromote">升格并创建需求</button>
        </div>
      </div>
    </div>
  </div>
</template>