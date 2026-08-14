<script setup lang="ts">
import { onMounted, watch, ref, reactive } from 'vue'
import { getLlm, getLlmHealth, upsertProvider, deleteProvider, setActiveEmbedding, updateChains, type LlmStatus, type ProviderHealth, type EmbeddingProvider, type LlmChainNode } from '../../api/llm'
import { ApiError } from '../../api/client'

// 可选用 sections 控制仅渲染部分区块（供设置页拆分为「供应商」「降级链」两个标签）
const props = withDefaults(defineProps<{ sections?: string[] }>(), { sections: () => ['chain', 'providers', 'rules', 'embedding'] })

const status = ref<LlmStatus | null>(null)
const health = ref<Record<string, ProviderHealth>>({})
const loading = ref(true)
const probing = ref(false)
const error = ref('')

// 向量模型切换
const embeddingSwitching = ref(false)
const embeddingError = ref('')
const embeddingOk = ref('')

const show = (s: string) => props.sections.includes(s)

const CHAIN_LABEL: Record<string, string> = {
  refinement: '提炼降级链',
}

// ---------- 降级链本地编辑态（T-44） ----------
const chainEdit = reactive<Record<string, LlmChainNode[]>>({})
const chainDirty = ref(false)
const chainSaving = ref(false)
const chainError = ref('')
const chainOk = ref('')

// 从 status 同步到编辑态
function syncChainEdit() {
  for (const [name, nodes] of Object.entries(status.value?.chains || {})) {
    chainEdit[name] = JSON.parse(JSON.stringify(nodes))
  }
  chainDirty.value = false
  chainError.value = ''
  chainOk.value = ''
}

// 新增/编辑/排序/删除节点
function addNode(chainName: string) {
  const nodes = chainEdit[chainName] || []
  nodes.push({ provider: '', model: '' })
  chainEdit[chainName] = nodes
  chainDirty.value = true
}
function moveNode(chainName: string, idx: number, dir: -1 | 1) {
  const nodes = chainEdit[chainName]
  const target = idx + dir
  if (!nodes || target < 0 || target >= nodes.length) return
  const tmp = nodes[idx]
  nodes[idx] = nodes[target]
  nodes[target] = tmp
  chainDirty.value = true
}
function removeNode(chainName: string, idx: number) {
  chainEdit[chainName].splice(idx, 1)
  chainDirty.value = true
}
async function saveChain() {
  chainSaving.value = true
  chainError.value = ''
  chainOk.value = ''
  try {
    // 校验：非 rule 节点必须指定 provider；rule 节点必须指定 rule
    for (const [name, nodes] of Object.entries(chainEdit)) {
      for (const node of nodes) {
        if (!node.provider) {
          chainError.value = `链「${CHAIN_LABEL[name] || name}」存在缺 provider 的节点，请补全或删除`
          return
        }
      }
    }
    await updateChains(JSON.parse(JSON.stringify(chainEdit)))
    chainOk.value = '降级链已保存'
    chainDirty.value = false
    await load()
  } catch (e) {
    chainError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    chainSaving.value = false
  }
}

// watch status 首次就绪时同步编辑态
watch(status, (s) => { if (s) syncChainEdit() })

// 新增/编辑供应商表单
const showForm = ref(false)
const form = ref({
  provider: '',
  base_url: '',
  api_key_env: '',
  model: '',
  context_window: '',
  timeout_s: '',
  health_endpoint: '',
  vector_capable: false,
})
const saving = ref(false)
const formError = ref('')
const formOk = ref('')
// 全局操作反馈（保存供应商后表单关闭，改用顶部横幅提示）
const flashOk = ref('')
function flash(msg: string) {
  flashOk.value = msg
  setTimeout(() => { flashOk.value = '' }, 3000)
}

function resetForm() {
  form.value = { provider: '', base_url: '', api_key_env: '', model: '', context_window: '', timeout_s: '', health_endpoint: '', vector_capable: false }
  formError.value = ''
  formOk.value = ''
}

function openAdd() {
  resetForm()
  showForm.value = true
}

function openEdit(p: { provider: string; base_url?: string; api_key_env?: string; model?: string; context_window?: number; timeout_s?: number; health_endpoint?: string; vector_capable?: boolean }) {
  form.value = {
    provider: p.provider,
    base_url: p.base_url || '',
    api_key_env: p.api_key_env || '',
    model: p.model || '',
    context_window: p.context_window != null ? String(p.context_window) : '',
    timeout_s: p.timeout_s != null ? String(p.timeout_s) : '',
    health_endpoint: p.health_endpoint || '',
    vector_capable: !!p.vector_capable,
  }
  formError.value = ''
  formOk.value = ''
  showForm.value = true
}

async function save() {
  saving.value = true
  formError.value = ''
  formOk.value = ''
  try {
    const payload: Record<string, unknown> = {
      base_url: form.value.base_url.trim(),
      api_key_env: form.value.api_key_env.trim(),
      vector_capable: form.value.vector_capable,
    }
    if (form.value.model.trim()) payload.model = form.value.model.trim()
    if (form.value.context_window.trim()) payload.context_window = Number(form.value.context_window)
    if (form.value.timeout_s.trim()) payload.timeout_s = Number(form.value.timeout_s)
    if (form.value.health_endpoint.trim()) payload.health_endpoint = form.value.health_endpoint.trim()
    await upsertProvider(form.value.provider.trim(), payload)
    formOk.value = `已保存供应商「${form.value.provider.trim()}」`
    showForm.value = false
    await load()
    flash(`已保存模型供应商「${form.value.provider.trim()}」${form.value.vector_capable ? '（向量模型）' : ''}`)
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function remove(p: { provider: string }) {
  if (!confirm(`确认删除供应商「${p.provider}」？（若被降级链引用的供应商将被后端拒绝删除）`)) return
  error.value = ''
  try {
    await deleteProvider(p.provider)
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    status.value = await getLlm()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function probe() {
  if (!status.value) return
  probing.value = true
  error.value = ''
  try {
    const data = await getLlmHealth()
    health.value = data.health
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    probing.value = false
  }
}

// 切换当前向量提供商（统一供应商模型：从 vector_capable 供应商中选）
async function switchEmbedding(p: EmbeddingProvider) {
  if (!confirm(`确认将向量模型切换为「${p.display_name || p.provider}」？\n切换后向量检索/提炼预筛将使用该提供商。`)) return
  embeddingSwitching.value = true
  embeddingError.value = ''
  embeddingOk.value = ''
  try {
    await setActiveEmbedding(p.provider)
    embeddingOk.value = `已切换向量提供商为「${p.display_name || p.provider}」`
    await load()
  } catch (e) {
    embeddingError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    embeddingSwitching.value = false
  }
}

function isActiveEmbedding(name: string): boolean {
  return status.value?.vector_current === name
}

// 统一供应商模型（T-44）：vector_capable=true 的供应商 → 向量模型候选
function vectorProviders() {
  if (!status.value) return []
  return Object.values(status.value.providers).filter((p) => p.vector_capable)
}

function ctxWin(n: number | undefined): string {
  if (!n) return '—'
  if (n >= 1048576) return `${(n / 1048576).toFixed(0)}M`
  if (n >= 1024) return `${(n / 1024).toFixed(0)}K`
  return String(n)
}

function healthOf(provider: string): ProviderHealth | undefined {
  return health.value[provider]
}

onMounted(load)
</script>

<template>
  <div class="providers">
    <div class="head">
      <h2>模型供应商</h2>
      <div class="filters">
        <button v-if="show('providers')" class="btn btn-primary" @click="openAdd">＋ 新增供应商</button>
        <button class="btn" :disabled="probing" @click="probe">{{ probing ? '探测中…' : '刷新健康' }}</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="flashOk" class="info">{{ flashOk }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <!-- 新增/编辑供应商表单 -->
    <section v-if="show('providers') && showForm" class="panel">
      <h3>{{ form.provider && status?.providers?.[form.provider] ? '编辑供应商' : '新增供应商' }}</h3>
      <div class="pform">
        <label>供应商名（id）<input v-model="form.provider" placeholder="如 ollama" /></label>
        <label>Base URL *<input v-model="form.base_url" placeholder="https://api.xxx.com/v1" /></label>
        <label>密钥环境变量名 *（只存变量名，禁明文）<input v-model="form.api_key_env" placeholder="如 MY_PROVIDER_KEY" /></label>
        <label>默认模型<input v-model="form.model" placeholder="model-name" /></label>
        <label>上下文窗口（token）<input v-model="form.context_window" type="number" placeholder="如 32768" /></label>
        <label>超时（秒）<input v-model="form.timeout_s" type="number" placeholder="如 120" /></label>
        <label>健康端点<input v-model="form.health_endpoint" placeholder="如 /models" /></label>
        <label class="check">
          <input v-model="form.vector_capable" type="checkbox" />
          向量模型（该供应商同时用作 embedding）
        </label>
      </div>
      <p v-if="formError" class="error">{{ formError }}</p>
      <p v-if="formOk" class="info">{{ formOk }}</p>
      <div class="modal-actions">
        <button class="btn" :disabled="saving" @click="showForm = false">取消</button>
        <button class="btn btn-primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </div>
    </section>

    <template v-if="status">
      <!-- 模型供应商（T-47：改名 + 移最上） -->
      <section v-if="show('providers')" class="panel">
        <div class="panel-head">
          <h3>模型供应商</h3>
        </div>
        <div class="provider-grid">
          <div v-for="p in Object.values(status.providers)" :key="p.provider" class="provider-card">
            <div class="card-head">
              <span class="prio">{{ p.provider }}</span>
              <span v-if="p.vector_capable" class="vec-tag">向量</span>
              <span v-if="healthOf(p.provider)" class="status" :class="healthOf(p.provider)!.available ? 'ok' : 'error'">
                {{ healthOf(p.provider)!.available ? '正常' : '不可用' }}
              </span>
              <span v-else class="status">未探测</span>
              <span class="card-ops">
                <button class="btn btn-sm" @click="openEdit(p)">编辑</button>
                <button class="btn btn-sm danger" @click="remove(p)">删除</button>
              </span>
            </div>
            <div class="p-row"><span class="p-label">模型</span><span class="p-val">{{ p.model || '—' }}</span></div>
            <div class="p-row"><span class="p-label">Base URL</span><code class="inline">{{ p.base_url || '—' }}</code></div>
            <div class="p-row"><span class="p-label">上下文</span><span class="p-val">{{ ctxWin(p.context_window) }}</span></div>
            <div class="p-row"><span class="p-label">密钥</span><span class="p-val mono">{{ p.api_key_env || '无需' }}</span></div>
            <div class="p-row"><span class="p-label">超时</span><span class="p-val">{{ p.timeout_s ?? '—' }}s</span></div>
            <div v-if="healthOf(p.provider)" class="hint-block" :class="healthOf(p.provider)!.available ? 'ok' : 'err'">
              <template v-if="healthOf(p.provider)!.available">连通 · {{ healthOf(p.provider)!.latency_ms }}ms</template>
              <template v-else>{{ healthOf(p.provider)!.error }}</template>
            </div>
          </div>
          <p v-if="!Object.keys(status.providers).length" class="empty">无供应商（降级链为空）</p>
        </div>
      </section>

      <!-- 模型降级链（T-47：改名 + 可编辑） -->
      <section v-if="show('chain')" class="panel">
        <div class="panel-head">
          <h3>模型降级链</h3>
          <div class="chain-ops">
            <button v-if="chainDirty" class="btn btn-sm" :disabled="chainSaving" @click="syncChainEdit">撤销</button>
            <button v-if="chainDirty" class="btn btn-sm btn-primary" :disabled="chainSaving" @click="saveChain">{{ chainSaving ? '保存中…' : '保存' }}</button>
          </div>
        </div>
        <p v-if="chainError" class="error">{{ chainError }}</p>
        <p v-if="chainOk" class="info">{{ chainOk }}</p>
        <div v-for="(_, chainName) in chainEdit" :key="chainName" class="chain-block">
          <div class="chain-title">
            <span class="tag">{{ CHAIN_LABEL[chainName] || chainName }}</span>
            <span class="meta">{{ chainEdit[chainName].length }} 级 · 逐级回退，最后一级兜底</span>
            <span class="chain-title-ops">
              <button class="btn btn-sm" @click="addNode(chainName)">＋ 节点</button>
            </span>
          </div>
          <div class="chain-flow">
            <template v-for="(node, i) in chainEdit[chainName]" :key="`${chainName}-${i}`">
              <div class="chain-node" :class="{ rule: node.provider === 'rule' }">
                <div class="node-top">
                  <span class="node-idx">{{ i + 1 }}</span>
                  <select v-model="node.provider" class="node-provider" :class="{ 'empty-mark': !node.provider }">
                    <option value="" disabled>选择供应商…</option>
                    <option value="rule">规则兜底</option>
                    <option v-for="p in Object.keys(status.providers)" :key="p" :value="p">{{ p }}</option>
                  </select>
                  <span v-if="healthOf(node.provider)" class="hdot" :class="healthOf(node.provider)!.available ? 'ok' : 'err'" />
                  <span class="node-sort">
                    <button class="btn btn-xs" :disabled="i === 0" @click="moveNode(chainName, i, -1)">↑</button>
                    <button class="btn btn-xs" :disabled="i === chainEdit[chainName].length - 1" @click="moveNode(chainName, i, 1)">↓</button>
                    <button class="btn btn-xs danger" @click="removeNode(chainName, i)">✕</button>
                  </span>
                </div>
                <div v-if="node.provider === 'rule'" class="node-model">
                  <input v-model="node.rule" placeholder="rule 动作（如 drop_batch）" class="node-input mono" />
                </div>
                <div v-else class="node-model">
                  <input v-model="node.model" placeholder="模型名（如 deepseek-v4-flash）" class="node-input mono" />
                </div>
                <div v-if="node.provider !== 'rule'" class="node-meta">
                  <span>{{ ctxWin(node.context_window) }} ctx</span>
                  <span class="mono">{{ node.base_url }}</span>
                </div>
              </div>
              <div v-if="i < chainEdit[chainName].length - 1" class="chain-arrow">→</div>
            </template>
          </div>
        </div>
      </section>

      <!-- 向量模型（统一供应商模型 T-44：从 vector_capable 供应商中选） -->
      <section v-if="show('embedding')" class="panel">
        <div class="chain-title">
          <h3>向量模型</h3>
          <span class="meta">当前：{{ status.vector_current }}</span>
          <button class="btn btn-sm" :disabled="embeddingSwitching" @click="load">{{ embeddingSwitching ? '切换中…' : '刷新' }}</button>
        </div>
        <p v-if="embeddingError" class="error">{{ embeddingError }}</p>
        <p v-if="embeddingOk" class="info">{{ embeddingOk }}</p>
        <div class="provider-grid">
          <div v-for="p in vectorProviders()" :key="p.provider" class="provider-card" :class="{ active: isActiveEmbedding(p.provider) }">
            <div class="card-head">
              <span class="prio">{{ p.display_name || p.provider }}</span>
              <span v-if="healthOf(p.provider)" class="status" :class="healthOf(p.provider)!.available ? 'ok' : 'error'">
                {{ healthOf(p.provider)!.available ? '正常' : '不可用' }}
              </span>
              <span v-if="isActiveEmbedding(p.provider)" class="status ok">使用中</span>
              <span v-else class="card-ops">
                <button class="btn btn-sm" :disabled="embeddingSwitching" @click="switchEmbedding(p)">设为当前</button>
              </span>
            </div>
            <div class="p-row"><span class="p-label">默认模型</span><span class="p-val">{{ p.model || (p.models || [])[0] || '—' }}</span></div>
            <div v-if="p.models?.length" class="p-row"><span class="p-label">可用模型</span><span class="p-val">{{ p.models.join(', ') }}</span></div>
            <div class="p-row"><span class="p-label">Base URL</span><code class="inline">{{ p.base_url || '—' }}</code></div>
            <div class="p-row"><span class="p-label">密钥</span><span class="p-val mono">{{ p.api_key_env || '无需' }}</span></div>
            <div v-if="healthOf(p.provider)" class="hint-block" :class="healthOf(p.provider)!.available ? 'ok' : 'err'">
              <template v-if="healthOf(p.provider)!.available">连通 · {{ healthOf(p.provider)!.latency_ms }}ms</template>
              <template v-else>{{ healthOf(p.provider)!.error }}（点「刷新健康」探测）</template>
            </div>
          </div>
          <p v-if="!vectorProviders().length" class="empty">无向量供应商（请在「供应商」中新增并勾选「向量模型」标签）</p>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.pform {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.pform label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.card-ops {
  margin-left: auto;
  display: inline-flex;
  gap: 6px;
}
.chain-block { margin-bottom: 20px; }
.chain-block:last-child { margin-bottom: 0; }
.chain-title { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.chain-flow { display: flex; align-items: stretch; gap: 8px; flex-wrap: nowrap; overflow-x: auto; padding-bottom: 4px; }
.chain-node { flex: 1; min-width: 180px; border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 12px 14px; background: var(--surface); }
.chain-node.rule { border-style: dashed; background: var(--surface-muted); }
.node-top { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.node-idx { width: 20px; height: 20px; border-radius: 50%; background: var(--brand-soft); color: var(--brand-text); font-size: 12px; font-weight: 700; display: grid; place-items: center; }
.node-name { font-weight: 600; font-size: 14px; }
.hdot { width: 9px; height: 9px; border-radius: 50%; margin-left: auto; }
.hdot.ok { background: var(--success); }
.hdot.err { background: var(--danger); }
.node-model { font-size: 13px; color: var(--text); margin-bottom: 4px; }
.node-meta { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: var(--text-muted); }
.chain-arrow { align-self: center; color: var(--text-faint); font-size: 18px; flex-shrink: 0; }
.provider-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.provider-card { border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 14px 16px; background: var(--surface); }
.provider-card.active { border-color: var(--success); box-shadow: 0 0 0 1px var(--success); }
.p-row { display: flex; align-items: center; gap: 10px; font-size: 13px; margin: 5px 0; }
.p-label { width: 76px; flex-shrink: 0; color: var(--text-muted); font-size: 12px; }
.p-val { color: var(--text); word-break: break-all; }
.hint-block { margin-top: 8px; padding: 6px 10px; border-radius: var(--radius); font-size: 12px; }
.hint-block.ok { background: var(--success-soft); color: var(--success); }
.hint-block.err { background: var(--danger-soft); color: var(--danger); }

/* T-47：面板头（标题 + 右侧操作） */
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.panel-head h3 { margin: 0; }
.chain-ops { display: inline-flex; gap: 6px; }
.chain-title-ops { margin-left: auto; }
.chain-title { flex-wrap: wrap; }

/* 可编辑链节点 */
.node-provider { font-size: 13px; font-weight: 600; padding: 2px 6px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); max-width: 130px; }
.node-provider.empty-mark { color: var(--text-faint); }
.node-sort { margin-left: 4px; display: inline-flex; gap: 2px; }
.node-model input.node-input { width: 100%; font-size: 12px; padding: 3px 6px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); }
.btn-xs { padding: 0 6px; font-size: 12px; line-height: 22px; }

/* 供应商表单 checkbox */
.pform label.check { flex-direction: row; align-items: center; gap: 8px; color: var(--text); }

/* 向量标签 */
.vec-tag { font-size: 11px; padding: 1px 7px; border-radius: 99px; background: var(--brand-soft); color: var(--brand-text); font-weight: 600; }
</style>