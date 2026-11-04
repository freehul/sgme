<script setup lang="ts">
// 待审候选详情弹层（父组件传完整 ReviewItem 展示，操作仍走 ReviewQueue 的 API 调用）
// 轻量实现：仅展示 + 发事件，不做路由跳转；路由由主 agent 统一接入
import { computed } from 'vue'
import type { ReviewItem } from '../../api/review'
import { fmtTs } from '../../utils/format'
import { confidenceBadge, dimTagsOf } from './reviewMeta'

const props = defineProps<{
  item: ReviewItem | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'approve', item: ReviewItem): void
  (e: 'reject', item: ReviewItem): void
}>()

const fields = computed(() => {
  if (!props.item) return []
  const conf = confidenceBadge(props.item)
  return [
    { label: '记忆 ID', value: props.item.memory_id, mono: true },
    { label: '置信度', value: conf.label === '—' ? '' : `${conf.label}（${conf.hint}）` },
    { label: '维度', value: dimTagsOf(props.item).join('、') },
    { label: '类型 memory_type', value: props.item.memory_type },
    { label: '类型 type', value: props.item.type },
    { label: '主体', value: props.item.subject },
    { label: '路由 route', value: props.item.route },
    { label: '场景标签', value: props.item.scene_key },
    { label: '优先级', value: props.item.priority != null ? String(props.item.priority) : '' },
    { label: '创建时间', value: fmtTs(props.item.created_at) },
    { label: '发生时间', value: props.item.occurred_at ? fmtTs(props.item.occurred_at) : '' },
  ].filter((f) => f.value)
})
</script>

<template>
  <div v-if="item" class="modal" @click.self="emit('close')">
    <div class="modal-box">
      <h3>待审候选详情</h3>
      <p class="content">{{ item.content }}</p>
      <div class="meta" style="flex-direction: column; align-items: stretch; gap: 4px">
        <span v-for="f in fields" :key="f.label">
          <b class="note">{{ f.label }}：</b><span :class="{ mono: f.mono }">{{ f.value }}</span>
        </span>
        <span v-if="item.notes">
          <b class="note">备注 notes：</b><span style="white-space: pre-wrap">{{ item.notes }}</span>
        </span>
      </div>
      <div class="modal-actions">
        <button @click="emit('close')">关闭</button>
        <button @click="emit('reject', item)">拒绝…</button>
        <button class="primary" @click="emit('approve', item)">放行…</button>
      </div>
    </div>
  </div>
</template>
