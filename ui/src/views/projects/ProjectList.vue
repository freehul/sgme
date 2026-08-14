<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { createProject, listProjects, updateProject, type Project } from '../../api/projects'
import { ApiError } from '../../api/client'

const rows = ref<Project[]>([])
const total = ref(0)
const page = ref(1)
const limit = 20
const q = ref('')
const loading = ref(false)
const error = ref('')

const editing = ref<Project | null>(null)
const editMilestone = ref('')
const editGit = ref('')
const editPath = ref('')

// 新建弹层（2026-08-13 用户定：项目由用户主动立项）
const creating = ref(false)
const newId = ref('')
const newName = ref('')
const newPath = ref('')
const newGit = ref('')
const newMilestone = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listProjects({ page: page.value, limit, q: q.value || undefined })
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

function openCreate() {
  creating.value = true
  newId.value = ''
  newName.value = ''
  newPath.value = ''
  newGit.value = ''
  newMilestone.value = ''
}

async function saveCreate() {
  if (!newId.value.trim() || !newPath.value.trim()) return
  try {
    await createProject({
      project_id: newId.value.trim(),
      path: newPath.value.trim(),
      name: newName.value.trim() || undefined,
      git_repo: newGit.value.trim() || undefined,
      milestone: newMilestone.value.trim() || undefined,
    })
    creating.value = false
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function openEdit(p: Project) {
  editing.value = p
  editMilestone.value = p.milestone || ''
  editGit.value = p.git_repo || ''
  editPath.value = p.path || ''
}

async function saveEdit() {
  if (!editing.value) return
  try {
    await updateProject(editing.value.project_id, {
      milestone: editMilestone.value || null,
      git_repo: editGit.value || null,
      path: editPath.value || undefined,
    })
    editing.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}
</script>

<template>
  <div class="projects">
    <div class="head">
      <h2>项目注册表</h2>
      <div class="filters">
        <input v-model="q" placeholder="检索项目名" @keyup.enter="search" />
        <button @click="search">检索</button>
        <button class="primary" @click="openCreate">新建项目</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length" class="empty">暂无项目。点击「新建项目」主动立项。</p>

    <div v-else class="list">
      <div v-for="p in rows" :key="p.project_id" class="card">
        <div class="card-head">
          <span class="card-title">{{ p.name }}</span>
          <span v-if="p.milestone" class="milestone">{{ p.milestone }}</span>
        </div>
        <p class="path">{{ p.path }}</p>
        <p v-if="p.git_repo" class="git">{{ p.git_repo }}</p>
        <div class="meta">
          <span v-if="p.last_active_at">最近活跃 {{ new Date(p.last_active_at).toLocaleString() }}</span>
        </div>
        <div class="actions">
          <button @click="openEdit(p)">编辑</button>
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
        <h3>新建项目（立项）</h3>
        <label>项目 ID（纯英文）*</label>
        <input v-model="newId" placeholder="如 sgme" autofocus />
        <label>展示名</label>
        <input v-model="newName" placeholder="缺省同 ID" />
        <label>项目路径 *</label>
        <input v-model="newPath" placeholder="D:/Projects/..." />
        <label>git 仓库（可选）</label>
        <input v-model="newGit" />
        <label>当前里程碑（可选）</label>
        <input v-model="newMilestone" />
        <div class="modal-actions">
          <button @click="creating = false">取消</button>
          <button class="primary" :disabled="!newId.trim() || !newPath.trim()" @click="saveCreate">立项</button>
        </div>
      </div>
    </div>

    <div v-if="editing" class="modal">
      <div class="modal-box">
        <h3>编辑项目 {{ editing.name }}</h3>
        <label>路径</label>
        <input v-model="editPath" />
        <label>git 仓库</label>
        <input v-model="editGit" />
        <label>当前里程碑</label>
        <input v-model="editMilestone" />
        <div class="modal-actions">
          <button @click="editing = null">取消</button>
          <button class="primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
