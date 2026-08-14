<script setup lang="ts">
import { useRouter } from 'vue-router'
const router = useRouter()
</script>

<template>
  <div class="not-found">
    <div class="nf-code">404</div>
    <h2>页面未找到</h2>
    <p class="empty">您访问的页面不存在或已被移动。</p>
    <button class="btn" @click="router.push('/dashboard')">返回总览</button>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
  gap: 12px;
}
.nf-code {
  font-size: 72px;
  font-weight: 700;
  color: var(--brand);
  line-height: 1;
}
</style>
