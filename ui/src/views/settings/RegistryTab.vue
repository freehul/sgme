<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listRegistry, createDimension, updateDimension, createAlias, deleteAlias, type Dimension } from '../../api/admin'
import { ApiError } from '../../api/client'

const dims = ref<Dimension[]>([])
const loading = ref(false)
const error = ref('')

const showNew = ref(false)
const newDim = ref({ id: '', display_name: '', category: '动态', time_velocity: 'dynamic', ttl_days: null as number | null, description: '' })
const busy = ref(false)

const CATEGORIES = ['静态', '偏好', '动态']
const VELOCITIES = ['static', 'dynamic']
const CATEGORY_LABEL: Record<string, string> = { static: '静态', dynamic: '动态' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listRegistry()
    dims.value = data.dimensions
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function toggleActive(d: Dimension) {
  error.value = ''
  try {
    await updateDimension(d.id, { active: !d.active })
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function addDim() {
  if (!newDim.value.id.trim()) return
  busy.value = true
  error.value = ''
  try {
    await createDimension({
      id: newDim.value.id.trim(),
      display_name: newDim.value.display_name.trim(),
      category: newDim.value.category,
      time_velocity: newDim.value.time_velocity,
      ttl_days: newDim.value.ttl_days,
      description: newDim.value.description.trim() || undefined,
    })
    showNew.value = false
    newDim.value = { id: '', display_name: '', category: '动态', time_velocity: 'dynamic', ttl_days: null, description: '' }
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function addAlias(d: Dimension) {
  const alias = prompt(`为 ${d.id} 新增别名：`)
  if (!alias?.trim()) return
  error.value = ''
  try {
    await createAlias(alias.trim(), d.id)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function removeAlias(d: Dimension, alias: string) {
  if (!confirm(`删除别名 ${alias}？`)) return
  error.value = ''
  try {
    await deleteAlias(alias)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

onMounted(load)
</script>

<template>
  <div class="registry">
    <div class="head">
      <h2>维度注册表</h2>
      <button @click="showNew = !showNew">＋ 新增维度</button>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-if="showNew" class="new-form">
      <input v-model="newDim.id" placeholder="id（snake_case）" />
      <input v-model="newDim.display_name" placeholder="显示名" />
      <select v-model="newDim.category">
        <option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</option>
      </select>
      <select v-model="newDim.time_velocity">
        <option v-for="v in VELOCITIES" :key="v" :value="v">{{ CATEGORY_LABEL[v] }}</option>
      </select>
      <input v-model.number="newDim.ttl_days" type="number" placeholder="TTL 天（动态维度）" />
      <input v-model="newDim.description" placeholder="描述" />
      <button class="btn btn-primary" :disabled="busy" @click="addDim">创建</button>
    </div>

    <div v-if="dims.length" class="list">
      <div v-for="d in dims" :key="d.id" class="card">
        <div class="card-head">
          <code class="inline">{{ d.id }}</code>
          <span class="card-title">{{ d.display_name }}</span>
          <span class="cat">{{ d.category }}</span>
          <span class="cat">{{ CATEGORY_LABEL[d.time_velocity] || d.time_velocity }}</span>
          <span v-if="d.ttl_days != null" class="ttl">TTL {{ d.ttl_days }}d</span>
          <span class="status" :class="{ off: !d.active }">{{ d.active ? '启用' : '停用' }}</span>
          <button @click="toggleActive(d)">{{ d.active ? '停用' : '启用' }}</button>
        </div>
        <p v-if="d.description" class="desc">{{ d.description }}</p>
        <div class="aliases">
          <span v-for="a in d.aliases" :key="a" class="alias" @click="removeAlias(d, a)">{{ a }} ✕</span>
          <button class="add-alias" @click="addAlias(d)">＋别名</button>
        </div>
      </div>
    </div>
  </div>
</template>