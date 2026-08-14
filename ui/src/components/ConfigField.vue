<script setup lang="ts">
// ConfigField.vue：递归结构化配置字段渲染器（按值类型智能选择控件）
import { computed } from 'vue'

const props = defineProps<{ label: string; value: unknown }>()
const emit = defineEmits<{ (e: 'update', v: unknown): void }>()

const type = computed(() => {
  const v = props.value
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'boolean') return 'bool'
  if (typeof v === 'number') return 'number'
  if (typeof v === 'string') return 'string'
  if (Array.isArray(v)) return 'array'
  if (typeof v === 'object') return 'object'
  return 'string'
})

function onStr(e: Event) {
  emit('update', (e.target as HTMLInputElement).value)
}
function onNum(e: Event) {
  const n = Number((e.target as HTMLInputElement).value)
  emit('update', Number.isFinite(n) ? n : props.value)
}
function onBool(e: Event) {
  emit('update', (e.target as HTMLInputElement).checked)
}
function onArr(e: Event) {
  const s = (e.target as HTMLInputElement).value
  emit('update', s.split(',').map((x) => x.trim()).filter(Boolean))
}
function onSub(k: string, v: unknown) {
  emit('update', { ...(props.value as Record<string, unknown>), [k]: v })
}
</script>

<template>
  <div class="cfield">
    <label class="clabel">{{ label }}</label>

    <input v-if="type === 'bool'" type="checkbox" class="cbool" :checked="!!value" @change="onBool" />
    <input v-else-if="type === 'number'" type="number" class="cinput" :value="value as number" @input="onNum" />
    <input v-else-if="type === 'string'" class="cinput" :value="value as string" @input="onStr" />
    <input
      v-else-if="type === 'array'"
      class="cinput"
      :value="(value as string[]).join(', ')"
      placeholder="逗号分隔"
      @change="onArr"
    />
    <div v-else-if="type === 'object'" class="cobj">
      <ConfigField
        v-for="(v, k) in value as Record<string, unknown>"
        :key="k"
        :label="k"
        :value="v"
        @update="onSub(k, $event)"
      />
    </div>
    <span v-else class="mono cval">{{ value }}</span>
  </div>
</template>

<style scoped>
.cfield {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--divider);
}
.cfield:last-child { border-bottom: none; }
.clabel {
  width: 180px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--text);
  font-family: var(--font-mono);
}
.cinput {
  flex: 1;
  min-width: 0;
}
.cbool {
  width: 18px;
  height: 18px;
  accent-color: var(--brand);
}
.cobj {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--surface-muted);
  border: 1px solid var(--divider);
  border-radius: var(--radius);
  padding: 4px 12px;
}
.cval {
  color: var(--text-muted);
  font-size: 12px;
}
</style>