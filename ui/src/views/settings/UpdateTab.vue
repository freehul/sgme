<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import {
  getHealth,
  requestUpdate,
  getUpdateRequest,
  checkUpdate,
  type HealthStatus,
  type UpdateRequest,
  type UpdateCheck,
} from '../../api/dashboard'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

const RELEASE_BASE = 'https://github.com/freehul/sgme/releases'
function releaseUrl(v: string): string {
  return `${RELEASE_BASE}/tag/${v}`
}

const health = ref<HealthStatus | null>(null)
const updateStatus = ref<UpdateRequest | null>(null)
const checking = ref(false)
const updating = ref(false)
const error = ref('')
const info = ref('')

let timer: number | undefined
function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = undefined
  }
}
function startPolling() {
  stopPolling()
  timer = window.setInterval(async () => {
    try {
      const r = await getUpdateRequest()
      updateStatus.value = r.request
      if (!r.request || r.request.status !== 'pending') stopPolling()
    } catch {
      /* 忽略轮询错误 */
    }
  }, 5000)
}

async function load() {
  error.value = ''
  try {
    const [h, r] = await Promise.all([getHealth(), getUpdateRequest()])
    health.value = h
    updateStatus.value = r.request
    if (updateStatus.value && updateStatus.value.status === 'pending') startPolling()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function checkNow() {
  checking.value = true
  error.value = ''
  info.value = ''
  try {
    const res: UpdateCheck = await checkUpdate()
    await load()
    if (res.update_error) {
      info.value = `检查完成，但版本检测失败：${res.update_error}`
    } else if (res.update_available) {
      info.value = `发现新版本 ${res.latest_version}`
    } else {
      info.value = `已是最新（最新 ${res.latest_version || health.value?.version || '未知'}）`
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    checking.value = false
  }
}

async function doUpdate() {
  const latest = health.value?.latest_version
  if (!latest || !health.value?.update_available) return
  if (
    !confirm(
      `确认更新到 ${latest}？\n\n主机代理将自动：\n1. 备份配置与数据\n2. 拉取最新代码并重建\n3. 失败自动回滚`,
    )
  )
    return
  updating.value = true
  error.value = ''
  info.value = '已提交更新请求，等待主机代理执行…'
  try {
    await requestUpdate(latest)
    startPolling()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    updating.value = false
  }
}

function statusText(s: UpdateRequest): string {
  if (s.status === 'pending') return `进行中（提交于 ${fmtTs(s.requested_at)}）`
  if (s.status === 'done') return '已完成'
  if (s.status === 'failed') return `失败：${s.error || '未知原因'}`
  return s.status
}
function statusClass(s: string): string {
  if (s === 'done') return 'tag tag-ok'
  if (s === 'failed') return 'tag tag-err'
  return 'tag tag-pending'
}

onMounted(load)
onUnmounted(stopPolling)
</script>

<template>
  <div class="update-tab">
    <div class="head">
      <h2>更新</h2>
      <p class="sub">检查 SGME 新版本并触发主机自动更新（git pull → 重建镜像 → 重启服务）</p>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info" :class="{ err: info.includes('失败') }">{{ info }}</p>

    <div class="card">
      <div class="row"><span class="k">当前版本</span><span class="v mono">{{ health?.version || '—' }}</span></div>
      <div class="row"><span class="k">最新版本</span><span class="v mono">{{ health?.latest_version || '—' }}</span></div>
      <div class="row">
        <span class="k">更新状态</span>
        <span class="v">
          <span v-if="health?.update_available" class="tag tag-new">有可用更新</span>
          <span v-else class="tag tag-ok">已是最新</span>
        </span>
      </div>
      <div class="row"><span class="k">上次检测</span><span class="v">{{ health?.update_checked_at ? fmtTs(health.update_checked_at) : '—' }}</span></div>
      <div v-if="health?.update_error" class="row"><span class="k">检测错误</span><span class="v err">{{ health.update_error }}</span></div>
    </div>

    <div class="actions">
      <button :disabled="checking" @click="checkNow">{{ checking ? '检查中…' : '检查更新' }}</button>
      <button
        class="primary"
        :disabled="updating || !!updateStatus || !health?.update_available || !health?.latest_version"
        @click="doUpdate"
      >
        {{ updateStatus ? '更新进行中…' : '更新' }}
      </button>
      <a
        v-if="health?.latest_version && health.update_available"
        :href="releaseUrl(health.latest_version)"
        target="_blank"
        rel="noopener"
        class="link"
      >查看更新说明</a>
    </div>

    <div v-if="updateStatus" class="status">
      <span class="k">更新请求：</span>
      <span :class="statusClass(updateStatus.status)">{{ statusText(updateStatus) }}</span>
      <span class="tgt">目标 {{ updateStatus.target_version }}</span>
    </div>

    <p class="hint">
      提示：版本检测比对 GitHub Releases 的最新 tag。若「更新」按钮置灰，说明当前运行版本已 ≥ 最新 Release，无需更新；
      如需部署未发版的新提交，请先在 GitHub 发布高于当前版本的新 Release。
    </p>
  </div>
</template>

<style scoped>
.update-tab { max-width: 720px; }
.head { margin-bottom: 16px; }
.head h2 { margin: 0 0 4px; font-size: 18px; }
.sub { margin: 0; color: var(--text-muted); font-size: 13px; }
.error { color: var(--danger); font-size: 13px; margin: 8px 0; }
.info { color: var(--brand); font-size: 13px; margin: 8px 0; }
.info.err { color: var(--danger); }
.card {
  border: 1px solid var(--border, rgba(128, 128, 128, .25));
  border-radius: 8px;
  padding: 8px 14px;
  margin: 12px 0;
}
.row {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  font-size: 14px;
  border-bottom: 1px dashed rgba(128, 128, 128, .15);
}
.row:last-child { border-bottom: none; }
.k { color: var(--text-muted); }
.v { color: var(--text); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.v.err { color: var(--danger); }
.actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
button {
  padding: 7px 16px;
  border: 1px solid var(--border, rgba(128, 128, 128, .3));
  border-radius: 6px;
  background: var(--surface, #1e293b);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
}
button:disabled { opacity: .5; cursor: not-allowed; }
button.primary { background: var(--brand); color: #fff; border: none; }
.link { color: var(--brand); font-size: 13px; text-decoration: none; }
.link:hover { text-decoration: underline; }
.status { font-size: 13px; margin: 10px 0; }
.status .k { color: var(--text-muted); }
.tgt { color: var(--text-muted); margin-left: 8px; font-family: ui-monospace, monospace; }
.tag { padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.tag-new { background: rgba(59, 130, 246, .18); color: var(--brand); }
.tag-ok { background: rgba(34, 197, 94, .15); color: #22c55e; }
.tag-err { background: rgba(239, 68, 68, .15); color: var(--danger); }
.tag-pending { background: rgba(234, 179, 8, .15); color: #eab308; }
.hint { color: var(--text-muted); font-size: 12px; line-height: 1.6; margin-top: 16px; }
</style>
