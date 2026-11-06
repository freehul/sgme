<script setup lang="ts">
import { computed, onUnmounted, ref, type Component } from 'vue'
import SettingsGeneral from './SettingsGeneral.vue'
import SettingsTtl from './SettingsTtl.vue'
import SettingsExtensions from './SettingsExtensions.vue'
import ProvidersView from '../llm/ProvidersView.vue'
import TemplatesView from '../templates/TemplatesView.vue'
import AgentsTab from './AgentsTab.vue'
import RegistryTab from './RegistryTab.vue'
import PromptsTab from './PromptsTab.vue'
import BackupTab from './BackupTab.vue'
import UpdateTab from './UpdateTab.vue'
import CredentialsTab from './CredentialsTab.vue'
import { ApiError } from '../../api/client'
import { getConfig, updateConfig } from '../../api/admin'
import { getRefineStatus, triggerRefineAsync, type RefineStatus } from '../../api/ops'
import { fmtTs } from '../../utils/format'

// 对齐参考设计：设置页单入口 + 标签页横向切分各配置功能区
// 「提炼」页签就地渲染（无独立组件）：手动触发批量提炼 + 队列水位（T-169 / D4 补 UI）
const TABS: Array<{ key: string; label: string; comp: Component | null; props?: Record<string, unknown> }> = [
  { key: 'general', label: '通用设置', comp: SettingsGeneral },
  // 供应商与降级链合并为单页（含新增/删除供应商；链/规则仍只读展示）
  { key: 'providers', label: '模型供应商', comp: ProvidersView },
  { key: 'ttl', label: 'TTL 配置', comp: SettingsTtl },
  { key: 'templates', label: '模板管理', comp: TemplatesView },
  { key: 'agents', label: 'Agent 管理', comp: AgentsTab },
  { key: 'registry', label: '维度注册表', comp: RegistryTab },
  { key: 'prompts', label: '提示词', comp: PromptsTab },
  { key: 'extensions', label: '扩展模块', comp: SettingsExtensions },
  { key: 'backup', label: '备份管理', comp: BackupTab },
  { key: 'credentials', label: '凭证管理', comp: CredentialsTab },
  { key: 'update', label: '更新', comp: UpdateTab },
  { key: 'refine', label: '提炼', comp: null },
]
const active = ref('general')
const currentTab = computed(() => TABS.find((t) => t.key === active.value) || TABS[0])


// ---------- 块级成本治理（D-80，2026-11-06）：读改 refine 段 ----------
// 后端写入口是 POST /v1/admin/config（{section, values}，部分更新 + 热生效）；
// 8 个键已在 SECTION_KEYS 白名单里，改完立即生效、无需重启。
interface D80Cfg {
  chunk_cache_enabled: boolean
  chunk_replay_enabled: boolean
  append_debounce_s: number
  append_min_new_messages: number
  chunk_replay_max_failures: number
}
const d80 = ref<D80Cfg | null>(null)
const d80Loading = ref(false)
const d80Saving = ref(false)
const d80Msg = ref('')
const d80Err = ref('')

function num(v: unknown, fallback: number): number {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

async function loadD80() {
  d80Loading.value = true
  d80Err.value = ''
  try {
    const cfg = await getConfig()
    const refine = ((cfg.config || {}) as Record<string, unknown>).refine as
      | Record<string, unknown>
      | undefined
    const r = refine || {}
    d80.value = {
      chunk_cache_enabled: r.chunk_cache_enabled !== false,
      chunk_replay_enabled: r.chunk_replay_enabled !== false,
      append_debounce_s: num(r.append_debounce_s, 300),
      append_min_new_messages: num(r.append_min_new_messages, 20),
      chunk_replay_max_failures: num(r.chunk_replay_max_failures, 20),
    }
  } catch (e) {
    d80.value = null
    d80Err.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    d80Loading.value = false
  }
}

async function saveD80() {
  if (!d80.value || d80Saving.value) return
  d80Saving.value = true
  d80Msg.value = ''
  d80Err.value = ''
  try {
    await updateConfig('refine', { ...d80.value })
    d80Msg.value = '已保存并热生效（refine 段部分更新）'
  } catch (e) {
    d80Err.value = '保存失败：' + (e instanceof ApiError ? e.message : String(e))
  } finally {
    d80Saving.value = false
  }
}
// ---------- 立即提炼（POST /v1/admin/refine/trigger-async，Admin Key） ----------
// 异步语义：立即返回 queued（不是 202），后台线程逐文件跑，结果异步落库；
// 失败由服务端批扫兜底。⚠️ 真实消耗 LLM 额度，所以按钮前有二次确认。
const triggering = ref(false)
const refineMsg = ref('')
const refineErr = ref('')
const refineStatus = ref<RefineStatus | null>(null)
const statusLoading = ref(false)
const statusErr = ref('')
let refreshTimer: number | null = null

// 切到「提炼」页签时懒加载队列（不打开就不请求，避免设置页多打一次 admin 端点）
function selectTab(key: string) {
  active.value = key
  if (key === 'refine') {
    if (!refineStatus.value && !statusLoading.value) void loadRefineStatus()
    if (!d80.value && !d80Loading.value) void loadD80()
  }
}

async function loadRefineStatus() {
  statusLoading.value = true
  statusErr.value = ''
  try {
    refineStatus.value = await getRefineStatus()
  } catch (e) {
    refineStatus.value = null
    statusErr.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    statusLoading.value = false
  }
}

// 触发后 3 秒自动刷新一次队列水位（后台线程已开跑，水位随之变化）
function scheduleStatusRefresh() {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer)
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null
    void loadRefineStatus()
  }, 3000)
}

async function runRefine() {
  if (triggering.value) return
  const ok = window.confirm(
    '确认立即触发批量提炼？\n\n' +
      '· 扫描「待提炼」的原始会话文件，每批最多 100 个\n' +
      '· 逐文件分块调用提炼模型，会真实消耗 LLM 额度\n' +
      '· 后台线程执行，提交后 3 秒自动刷新队列状态',
  )
  if (!ok) return
  triggering.value = true
  refineMsg.value = ''
  refineErr.value = ''
  try {
    const r = await triggerRefineAsync()
    refineMsg.value =
      '已提交：' + (r.status || 'queued') + '（' + (r.file_id || 'batch') + '）' +
      (r.note ? ' · ' + r.note : '') +
      ' · 3 秒后自动刷新队列状态'
    scheduleStatusRefresh()
  } catch (e) {
    refineErr.value = '触发失败：' + (e instanceof ApiError ? e.message : String(e))
  } finally {
    triggering.value = false
  }
}

const watermarkText = computed(() => {
  const wm = refineStatus.value?.watermark
  if (!wm) return '—'
  const age = wm.watermark_age_sec
  if (age == null || !Number.isFinite(age) || age < 0) return '从未提炼'
  if (age < 60) return '距上次 ' + Math.round(age) + ' 秒'
  if (age < 3600) return '距上次 ' + Math.round(age / 60) + ' 分钟'
  if (age < 86400) return '距上次 ' + (age / 3600).toFixed(1) + ' 小时'
  return '距上次 ' + (age / 86400).toFixed(1) + ' 天'
})

onUnmounted(() => {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="settings">
    <div class="head">
      <h2>设置</h2>
      <span class="sub">配置供应商 / 降级链 / TTL / 模板 / 扩展模块 / 备份 / 手动提炼</span>
    </div>

    <div class="tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        :class="{ active: active === t.key }"
        @click="selectTab(t.key)"
      >
        {{ t.label }}
      </button>
    </div>

    <div class="tab-body">
      <!-- 提炼页签：立即提炼 + 队列水位 -->
      <section v-if="active === 'refine'" class="panel refine-panel">
        <h3>
          立即提炼
          <span class="sub">POST /v1/admin/refine/trigger-async · Admin Key</span>
        </h3>
        <p class="desc">
          扫描「待提炼」的原始会话文件（L0），后台线程按 L2 提取 → L3 裁决 → L4 梦境依次跑。
          提交后立即返回 queued，进度看下方队列水位；最近批次与失败详情在
          <RouterLink to="/ops/health">健康总览</RouterLink> 页。
        </p>
        <p class="quota-note">
          ⚠️ 会真实消耗 LLM 额度：每批最多 100 个文件，逐文件分块调用提炼模型。
          模型全链不可用时该文件记失败，下次触发或服务端批扫会重试。
        </p>

        <div class="refine-actions">
          <button class="btn btn-primary" :disabled="triggering" @click="runRefine">
            {{ triggering ? '提交中…' : '立即提炼（批量，上限 100）' }}
          </button>
          <button class="btn" :disabled="statusLoading" @click="loadRefineStatus">
            {{ statusLoading ? '刷新中…' : '刷新队列状态' }}
          </button>
        </div>

        <p v-if="refineMsg" class="alert ok">{{ refineMsg }}</p>
        <p v-if="refineErr" class="alert err">{{ refineErr }}</p>
        <p v-if="statusErr" class="err">队列状态读取失败：{{ statusErr }}</p>

        <div v-if="refineStatus" class="refine-queue">
          <span class="q-item">待提炼 <b>{{ refineStatus.pending }}</b></span>
          <span class="q-item">已完成 <b>{{ refineStatus.completed }}</b></span>
          <span class="q-item">失败 <b :class="{ bad: refineStatus.failed > 0 }">{{ refineStatus.failed }}</b></span>
          <span class="q-item">raw 总数 <b>{{ refineStatus.total }}</b></span>
          <span class="q-item">水位 <b>{{ watermarkText }}</b></span>
        </div>
        <p v-if="refineStatus && refineStatus.pending === 0" class="desc">
          队列已清空——没有待提炼的会话文件。
        </p>
        <p v-else-if="refineStatus" class="desc">
          上次提炼：{{ fmtTs(refineStatus.watermark?.last_refined_at) }}
        </p>

        <!-- D-80（2026-11-06）：块级成本治理三开关（缓存 / 失败块补跑 / 去抖） -->
        <h3 class="d80-title">
          块级成本治理（D-80）
          <span class="sub">缓存 / 失败块补跑 / 触发去抖 · 保存即热生效</span>
        </h3>
        <p v-if="d80Err" class="err">读取或保存失败：{{ d80Err }}</p>
        <div v-if="d80" class="d80-form">
          <label class="d80-row">
            <input v-model="d80.chunk_cache_enabled" type="checkbox" />
            <span>
              块级结果缓存
              <em>同 prompt + 模型链指纹 的块直接复用，失败重跑零 token</em>
            </span>
          </label>
          <label class="d80-row">
            <input v-model="d80.chunk_replay_enabled" type="checkbox" />
            <span>
              失败块补跑
              <em>成功块照常提交并推进游标，失败块入队逐块补跑（不再整份重发）</em>
            </span>
          </label>
          <label class="d80-row">
            <span>触发去抖窗口（秒）</span>
            <input v-model.number="d80.append_debounce_s" type="number" min="0" step="30" />
          </label>
          <label class="d80-row">
            <span>最小新增消息数</span>
            <input v-model.number="d80.append_min_new_messages" type="number" min="0" />
          </label>
          <label class="d80-row">
            <span>单次入队上限（块）</span>
            <input v-model.number="d80.chunk_replay_max_failures" type="number" min="1" />
          </label>
          <div class="refine-actions">
            <button class="btn btn-primary" :disabled="d80Saving" @click="saveD80">
              {{ d80Saving ? '保存中…' : '保存并热生效' }}
            </button>
            <button class="btn" :disabled="d80Loading" @click="loadD80">
              {{ d80Loading ? '读取中…' : '刷新当前值' }}
            </button>
          </div>
          <p v-if="d80Msg" class="alert ok">{{ d80Msg }}</p>
        </div>
      </section>

      <component
        v-else
        :is="currentTab!.comp"
        v-bind="(currentTab && currentTab.props) || {}"
      />
    </div>
  </div>
</template>

<style scoped>
.tab-body { padding-top: 4px; }
.refine-panel { max-width: var(--content-max); }
.refine-panel .desc { line-height: 1.7; }
.quota-note {
  margin: var(--space-2) 0 var(--space-4);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--warn-soft);
  color: var(--warn);
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.refine-actions { display: flex; gap: var(--space-2); flex-wrap: wrap; align-items: center; }
.alert {
  margin: var(--space-3) 0 0;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
}
.alert.ok { background: var(--success-soft); color: var(--success); }
.alert.err { background: var(--danger-soft); color: var(--danger); }
.err { color: var(--danger); font-size: var(--fs-sm); margin: var(--space-2) 0; }
.refine-queue {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px solid var(--divider);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.q-item b { color: var(--text); font-weight: 600; margin-left: 2px; }
.q-item b.bad { color: var(--danger); }
</style>

.d80-title { margin-top: var(--space-4); }
.d80-form { display: flex; flex-direction: column; gap: var(--space-2); max-width: 720px; }
.d80-row { display: flex; align-items: center; gap: var(--space-2); line-height: 1.6; }
.d80-row span { flex: 1; }
.d80-row em { display: block; font-style: normal; opacity: 0.65; font-size: 12px; }
.d80-row input[type='number'] { width: 96px; }
