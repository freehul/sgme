<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listAgents, registerAgent, revokeAgent, type Agent } from '../../api/admin'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

const agents = ref<Agent[]>([])
const loading = ref(false)
const error = ref('')

const showNew = ref(false)
const newId = ref('')
const newScope = ref('')
const newModel = ref('')
const busy = ref(false)
const issuedKey = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listAgents()
    agents.value = data.agents
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function register() {
  if (!newId.value.trim()) return
  busy.value = true
  error.value = ''
  issuedKey.value = ''
  try {
    const scope = newScope.value.trim() ? newScope.value.split(',').map(s => s.trim()).filter(Boolean) : []
    const res = await registerAgent(newId.value.trim(), scope, newModel.value.trim() || undefined)
    issuedKey.value = res.api_key
    showNew.value = false
    newId.value = ''
    newScope.value = ''
    newModel.value = ''
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function revoke(a: Agent) {
  if (!confirm(`吊销 Agent ${a.agent_id}？将删除其 ${a.key_count} 个 Key。`)) return
  error.value = ''
  try {
    await revokeAgent(a.agent_id)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

onMounted(load)
</script>

<template>
  <div class="agents">
    <div class="head">
      <h2>Agent 管理</h2>
      <button @click="showNew = !showNew">＋ 签发 Key</button>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-if="showNew" class="new-form">
      <input v-model="newId" placeholder="agent_id（如 hermes-01）" />
      <input v-model="newScope" placeholder="scope（逗号分隔，可选）" />
      <input v-model="newModel" placeholder="agent_model（如 deepseek/deepseek-v4-flash，可选）" />
      <button class="btn btn-primary" :disabled="busy" @click="register">签发</button>
    </div>

    <div v-if="issuedKey" class="key-reveal">
      <strong>密钥仅此一次展示，请妥善保存：</strong>
      <code>{{ issuedKey }}</code>
      <button class="btn btn-sm" @click="issuedKey = ''">关闭</button>
    </div>

    <div v-if="agents.length" class="list">
      <div v-for="a in agents" :key="a.agent_id" class="card">
        <div class="card-head">
          <code class="inline">{{ a.agent_id }}</code>
          <span class="role">{{ a.role }}</span>
          <span v-for="s in a.scope" :key="s" class="scope">{{ s }}</span>
          <span v-if="a.agent_model" class="scope" :title="'声明的提炼模型'">🧠 {{ a.agent_model }}</span>
          <span class="keys">{{ a.key_count }} key</span>
        </div>
        <div class="meta">
          <span>注册: {{ fmtTs(a.registered_at) }}</span>
          <span>最近活跃: {{ fmtTs(a.last_seen_at) }}</span>
          <span>指纹: <code class="inline">{{ a.key_ref }}</code></span>
        </div>
        <button class="btn btn-danger btn-sm" @click="revoke(a)">吊销</button>
      </div>
    </div>
  </div>
</template>