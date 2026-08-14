<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  createDemand,
  DEMAND_STATUS,
  listDemands,
  setDemandStatus,
  updateDemand,
  type Demand,
} from '../../api/demands'
import { listProjects, type Project } from '../../api/projects'
import { ApiError } from '../../api/client'

const rows = ref<Demand[]>([])
const total = ref(0)
const page = ref(1)
const limit = 20
const statusFilter = ref('')
const projectFilter = ref('')
const q = ref('')
const loading = ref(false)
const error = ref('')

// 新建弹层
const creating = ref(false)
const newTitle = ref('')
const newContent = ref('')
const newPriority = ref(50)
const newProject = ref('')
// 编辑弹层
const editing = ref<Demand | null>(null)
const editTitle = ref('')
const editContent = ref('')
const editPriority = ref(50)
const editProject = ref('')

const projects = ref<Project[]>([])

async function loadProjects() {
  try {
    const data = await listProjects({ limit: 200 })
    projects.value = data.items
  } catch {
    projects.value = [] // 项目池加载失败不阻断待办页
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listDemands({
      page: page.value,
      limit,
      status: statusFilter.value || undefined,
      project_id: projectFilter.value || undefined,
      q: q.value || undefined,
    })
    rows.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  loadProjects()
})

function search() {
  page.value = 1
  load()
}

function openCreate() {
  creating.value = true
  newTitle.value = ''
  newContent.value = ''
  newPriority.value = 50
  newProject.value = ''
}

async function saveCreate() {
  if (!newTitle.value.trim()) return
  try {
    await createDemand({
      title: newTitle.value.trim(),
      content: newContent.value.trim() || undefined,
      priority: newPriority.value,
      project_id: newProject.value || undefined,
    })
    creating.value = false
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function openEdit(d: Demand) {
  editing.value = d
  editTitle.value = d.title
  editContent.value = d.content || ''
  editPriority.value = d.priority
  editProject.value = d.project_id || ''
}

async function saveEdit() {
  if (!editing.value || !editTitle.value.trim()) return
  try {
    await updateDemand(editing.value.demand_id, {
      title: editTitle.value.trim(),
      content: editContent.value.trim() || undefined,
      priority: editPriority.value,
      project_id: editProject.value || null,
    })
    editing.value = null
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function changeStatus(d: Demand, status: string) {
  try {
    await setDemandStatus(d.demand_id, status)
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}
</script>

<template>
  <div class="demands">
    <div class="head">
      <h2>待办</h2>
      <div class="filters">
        <input v-model="q" placeholder="检索标题/内容" @keyup.enter="search" />
        <select v-model="projectFilter" @change="search">
          <option value="">全部项目</option>
          <option v-for="p in projects" :key="p.project_id" :value="p.project_id">{{ p.name }}</option>
        </select>
        <select v-model="statusFilter" @change="search">
          <option value="">全部状态</option>
          <option v-for="(label, key) in DEMAND_STATUS" :key="key" :value="key">{{ label }}</option>
        </select>
        <button @click="search">检索</button>
        <button class="primary" @click="openCreate">新建待办</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!rows.length" class="empty">暂无待办。点击「新建待办」添加（可指定所属项目，按项目过滤）。</p>

    <div v-else class="list">
      <div v-for="d in rows" :key="d.demand_id" class="card">
        <div class="card-head">
          <span class="prio">P{{ d.priority }}</span>
          <span class="status" :class="d.status">{{ DEMAND_STATUS[d.status] }}</span>
          <span v-if="d.project_id" class="proj">{{ d.project_id }}</span>
          <p class="title">{{ d.title }}</p>
        </div>
        <p v-if="d.content" class="content">{{ d.content }}</p>
        <div class="meta">
          <span v-if="d.origin_idea_id">来源创意</span>
        </div>
        <div class="actions">
          <button @click="openEdit(d)">编辑</button>
          <select :value="d.status" @change="changeStatus(d, ($event.target as HTMLSelectElement).value)">
            <option v-for="(label, key) in DEMAND_STATUS" :key="key" :value="key">{{ label }}</option>
          </select>
          <span class="ts">加入 {{ new Date(d.created_at).toLocaleString() }}<template v-if="d.resolved_at"> · 完成 {{ new Date(d.resolved_at).toLocaleString() }}</template></span>
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
        <h3>新建待办</h3>
        <label>标题 *</label>
        <input v-model="newTitle" autofocus />
        <label>描述</label>
        <textarea v-model="newContent" rows="3"></textarea>
        <label>优先级 <input v-model.number="newPriority" type="number" min="0" max="100" /></label>
        <label>所属项目
          <select v-model="newProject">
            <option value="">（未指定）</option>
            <option v-for="p in projects" :key="p.project_id" :value="p.project_id">{{ p.name }}</option>
          </select>
        </label>
        <div class="modal-actions">
          <button @click="creating = false">取消</button>
          <button class="primary" :disabled="!newTitle.trim()" @click="saveCreate">保存</button>
        </div>
      </div>
    </div>

    <!-- 编辑弹层 -->
    <div v-if="editing" class="modal">
      <div class="modal-box">
        <h3>编辑待办</h3>
        <label>标题 *</label>
        <input v-model="editTitle" />
        <label>描述</label>
        <textarea v-model="editContent" rows="3"></textarea>
        <label>优先级 <input v-model.number="editPriority" type="number" min="0" max="100" /></label>
        <label>所属项目
          <select v-model="editProject">
            <option value="">（未指定）</option>
            <option v-for="p in projects" :key="p.project_id" :value="p.project_id">{{ p.name }}</option>
          </select>
        </label>
        <div class="modal-actions">
          <button @click="editing = null">取消</button>
          <button class="primary" :disabled="!editTitle.trim()" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
