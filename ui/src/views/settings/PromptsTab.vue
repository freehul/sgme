<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listPrompts, activatePrompt, getPromptMetrics, type PromptStage } from '../../api/admin'
import { ApiError } from '../../api/client'

const stages = ref<PromptStage[]>([])
const loading = ref(false)
const error = ref('')

const STAGE_LABEL: Record<string, string> = {
  l1_extraction: 'L1 抽取',
  l1_conflict: 'L1.5 冲突提炼',
  l2_scene: 'L2 场景',
  tier0_summary: 'Tier0 摘要',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listPrompts()
    stages.value = data.stages
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function activate(stage: string, version: string) {
  if (!confirm(`激活 ${stage} 的版本 ${version}？`)) return
  error.value = ''
  try {
    await activatePrompt({ stage, version })
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function showMetrics() {
  error.value = ''
  try {
    const data = await getPromptMetrics()
    alert(`指标:\n${JSON.stringify(data, null, 2)}`)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

onMounted(load)
</script>

<template>
  <div class="prompts">
    <div class="head">
      <h2>提示词管理</h2>
      <button @click="showMetrics">查看指标</button>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-if="stages.length" class="list">
      <div v-for="s in stages" :key="s.stage" class="card">
        <div class="card-head">
          <span class="card-title">{{ STAGE_LABEL[s.stage] || s.stage }}</span>
          <span class="builtin">当前: {{ s.active }}</span>
          <span v-if="s.ab?.enabled" class="flag">A/B 已启用</span>
        </div>
        <div class="versions">
          <div v-for="v in s.versions" :key="v.version" class="ver" :class="{ current: v.version === s.active }">
            <code>{{ v.version }}</code>
            <span class="va">{{ v.created_at ? new Date(v.created_at).toLocaleDateString() : '' }}</span>
            <button v-if="v.version !== s.active" @click="activate(s.stage, v.version)">激活</button>
          </div>
          <p v-if="!s.versions.length" class="empty">无版本</p>
        </div>
      </div>
    </div>
  </div>
</template>