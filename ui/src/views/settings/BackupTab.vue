<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { createBackup, listBackups, restoreBackup, syncSkills, type Snapshot } from '../../api/admin'
import { ApiError } from '../../api/client'

const snapshots = ref<Snapshot[]>([])
const loading = ref(false)
const error = ref('')
const info = ref('')
const busy = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listBackups()
    snapshots.value = data.snapshots
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function backup() {
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    const res = await createBackup('incremental')
    info.value = `快照已创建: ${JSON.stringify(res)}`
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function restore(s: Snapshot) {
  if (!confirm(`确定从快照 ${s.snapshot_id} 恢复？恢复前会自动再备份。`)) return
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    const res = await restoreBackup(s.snapshot_id)
    info.value = `恢复完成: ${JSON.stringify(res)}`
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function sync() {
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    const res = await syncSkills('both')
    info.value = `技能同步: ${JSON.stringify(res)}`
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="backup">
    <div class="head">
      <h2>备份与技能</h2>
      <div class="actions">
        <button :disabled="busy" @click="sync">技能同步</button>
        <button :disabled="busy" @click="backup">创建快照</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info">{{ info }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-if="snapshots.length" class="list">
      <div v-for="s in snapshots" :key="s.snapshot_id" class="card">
        <div class="card-head">
          <code class="inline">{{ s.snapshot_id }}</code>
          <span class="level">{{ s.level }}</span>
          <button class="btn btn-danger btn-sm" :disabled="busy" @click="restore(s)">恢复</button>
        </div>
        <p class="path">{{ s.path }}</p>
      </div>
    </div>
    <p v-else-if="!loading" class="empty">暂无快照。</p>
  </div>
</template>