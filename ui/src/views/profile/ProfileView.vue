<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { injectProfile, type InjectResponse } from '../../api/memory'
import { ApiError } from '../../api/client'
import PersonaSection from './PersonaSection.vue'

const mode = ref('daily')
const MODES = [
  { value: 'daily', label: '日常' },
  { value: 'coding', label: '编码' },
  { value: 'work', label: '工作' },
  { value: 'full', label: '全量' },
]
const data = ref<InjectResponse | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await injectProfile(mode.value)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    data.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="profile">
    <div class="head">
      <h2>用户画像</h2>
      <div class="opts">
        <select v-model="mode" @change="load">
          <option v-for="m in MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
        </select>
        <button class="btn btn-primary" :disabled="loading" @click="load">刷新画像</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>
    <p v-else-if="!data" class="empty">无画像数据。</p>

    <template v-else>
      <div class="meta-bar">
        <span>模式: {{ data.stats.mode }}</span>
        <span>块数: {{ data.blocks.length }}</span>
        <span>预估 tokens: {{ data.stats.tokens_est }}</span>
        <span v-if="data.tier0?.present" class="tier0-ok">Tier0 摘要 ✓</span>
        <span v-else class="tier0-miss">Tier0 缺失（静态降级）</span>
      </div>

      <div class="blocks">
        <section v-for="(b, i) in data.blocks" :key="i" class="block" :class="{ empty: !b.present }">
          <h3 class="block-title">{{ b.title }}</h3>
          <p v-if="!b.items.length" class="dim-muted">（无内容）</p>
          <div v-for="(item, j) in b.items" :key="j" class="item">
            <p class="content">{{ item.content }}</p>
            <span v-if="item.relative_time" class="rel">{{ item.relative_time }}</span>
          </div>
        </section>
      </div>
    </template>

    <PersonaSection />
  </div>
</template>

<style scoped>
.meta-bar { display: flex; gap: 16px; color: var(--muted); font-size: 13px; margin-bottom: 12px; }
.tier0-ok { color: var(--success); }
.tier0-miss { color: var(--warn); }
.blocks { display: flex; flex-direction: column; gap: 14px; }
.block { border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; background: var(--surface); }
.block.empty { opacity: 0.6; }
.block-title { margin: 0 0 8px; font-size: 15px; font-weight: 600; }
.item { padding: 6px 0; border-bottom: 1px dashed var(--border); }
.item:last-child { border-bottom: none; }
.item .content { margin: 0; font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
.rel { font-size: 12px; color: var(--text-muted); }
</style>
