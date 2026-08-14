<script setup lang="ts">
// ConfigSectionEditor.vue：某个配置段的递归结构化编辑表单
import { ref, watch } from 'vue'
import ConfigField from './ConfigField.vue'

const props = defineProps<{ config: Record<string, unknown> }>()
const emit = defineEmits<{ (e: 'dirty', v: boolean): void }>()

const draft = ref<Record<string, unknown>>({})

watch(
  () => props.config,
  (c) => {
    draft.value = JSON.parse(JSON.stringify(c || {}))
  },
  { immediate: true, deep: true },
)

function onSub(k: string, v: unknown) {
  draft.value = { ...draft.value, [k]: v }
  emit('dirty', true)
}
</script>

<template>
  <div class="config-editor">
    <ConfigField
      v-for="(v, k) in draft"
      :key="k"
      :label="k"
      :value="v"
      @update="onSub(k, $event)"
    />
    <p v-if="!Object.keys(draft).length" class="empty">该模块暂无配置项</p>
  </div>
</template>