<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  deleteCredential,
  downloadCredentialFile,
  exportCredentials,
  listCredentials,
  restoreCredential,
  uploadCredentialFile,
  writeCredential,
  type CredentialMeta,
  type CredentialWriteInput,
} from '../../api/credentials'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

const items = ref<CredentialMeta[]>([])
const loading = ref(false)
const error = ref('')
const info = ref('')
const busy = ref(false)

const filter = ref('')

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter(
    (c) =>
      c.key_name.toLowerCase().includes(q) ||
      (c.description || '').toLowerCase().includes(q),
  )
})

// ---------- 新建 / 更新弹窗 ----------

const showForm = ref(false)
// 仅编辑模式（已有键补填值）；新建模式必填
const editingKey = ref<string | null>(null)
const formKey = ref('')
const formValue = ref('')
const formNote = ref('')
// D-37 v1.1 元数据（编辑时提交空串 "" = 清空该字段）
const formType = ref('')
const formScope = ref('')
const formAgents = ref('')
const formExpires = ref('') // datetime-local 本地值，提交前转 ISO
const scopeExtra = ref('') // 后端返回的非标准 scope 取值（原样保留，避免编辑备注时被清空）
const formError = ref('')
// D-37 v1.2 文件类型：新建必选；编辑留空 = 不替换现有文件
const formFile = ref<File | null>(null)
const fileInputEl = ref<HTMLInputElement | null>(null)
// 编辑目标的原始行（用于展示「当前文件」并提供「该键是否已有文件内容」判定）
const editingMeta = ref<CredentialMeta | null>(null)

const KEY_NAME_RE = /^[A-Z][A-Z0-9_]*$/
// 后端保留名（引导/主密钥空间不进库），前端同规则预检
const RESERVED = new Set(['SGME_CRED_KEY', 'SGME_ADMIN_KEY', 'SGME_AGENT_KEY'])

// ---------- D-37 v1.1 元数据：类型 / 范围 / 过期时间 ----------

// 类型下拉（留空 = 未设置；编辑时留空提交空串 = 清空）
const CRED_TYPES = [
  { value: '', label: '（未设置）' },
  { value: 'api_key', label: 'API Key' },
  { value: 'token', label: 'Token' },
  { value: 'password', label: '密码' },
  { value: 'other', label: '其他' },
  // D-37 v1.2：文件 / 二进制「重要数据」（走 /v1/credentials/{key}/file 通道）
  { value: 'file', label: '文件（重要数据）' },
]

function credTypeLabel(v: string | null): string {
  if (!v) return '—'
  // 列表类型列对文件类型用短名；弹窗下拉仍显示「文件（重要数据）」全称
  if (v === 'file') return '文件'
  return CRED_TYPES.find((t) => t.value === v)?.label || v
}

// 范围编码：all=全部 agent / admin=仅管理员 / agents:<英文逗号分隔名单>
const AGENTS_PREFIX = 'agents:'

// agents:xxx,yyy → "xxx, yyy"（列表展示用）
function scopeAgentsText(scope: string | null): string {
  if (!scope || !scope.startsWith(AGENTS_PREFIX)) return ''
  return scope
    .slice(AGENTS_PREFIX.length)
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .join(', ')
}

// 范围下拉：非标准取值（后端将来扩展）原样保留为一项
const scopeChoices = computed(() => {
  const list = [
    { value: '', label: '（未设置）' },
    { value: 'all', label: '全部 agent' },
    { value: 'admin', label: '仅管理员' },
    { value: AGENTS_PREFIX, label: '指定 agent…' },
  ]
  if (scopeExtra.value) list.push({ value: scopeExtra.value, label: scopeExtra.value })
  return list
})

// 表单 → 提交用范围串（agents: 时拼接规范化后的名单）
function scopeToSubmit(): string {
  if (formScope.value !== AGENTS_PREFIX) return formScope.value
  const list = formAgents.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return AGENTS_PREFIX + list.join(',')
}

// datetime-local（无时区，按浏览器本地时区）↔ ISO 8601 互转
function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function isoToLocalInput(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const ymd = d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
  const hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes())
  return ymd + 'T' + hm
}

function localInputToIso(v: string): string {
  if (!v) return ''
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? '' : d.toISOString()
}

// ---------- D-37 v1.2 文件（重要数据）：大小 / 校验 / 选择 ----------

// 与后端默认值 credentials.max_file_bytes 一致（服务端可调小，故 413 仍要兜住）
const MAX_FILE_BYTES = 2 * 1024 * 1024

function fmtFileSize(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / (1024 * 1024)).toFixed(2) + ' MB'
}

const isFileType = computed(() => formType.value === 'file')
// 是否必须选文件：新建必选；编辑时仅当该键当前没有文件内容（非文件键）才必选
const fileRequired = computed(
  () => isFileType.value && (!editingKey.value || editingMeta.value?.cred_type !== 'file'),
)

function resetFormFile() {
  formFile.value = null
  // 原生 input 的已选文件不会被 v-model 清掉，必须直接置空
  if (fileInputEl.value) fileInputEl.value.value = ''
}

// 关闭弹窗或切走「文件」类型时立即丢弃已选文件引用：
// 文件 input 卸载后选择态不再可见，留着旧引用会被误当作本次要上传的文件
watch([showForm, isFileType], () => {
  if (!showForm.value || !isFileType.value) formFile.value = null
})

function onFilePick(ev: Event) {
  const input = ev.target as HTMLInputElement
  const picked = input.files && input.files.length ? input.files[0] : null
  formError.value = ''
  if (picked && picked.size > MAX_FILE_BYTES) {
    formFile.value = null
    input.value = ''
    formError.value = '文件 ' + fmtFileSize(picked.size) + ' 超过 2 MiB 上限，请压缩或拆分后再上传'
    return
  }
  formFile.value = picked
}

// 统一错误文案：ApiError 沿用服务端 message；413 = 文件超过后端上限
function toErrText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 413 || e.code === 'ERR_TOO_LARGE') {
      return '文件超过服务端 2 MiB 上限，已被拒绝（' + e.message + '）。请压缩或拆分后再上传。'
    }
    return e.message
  }
  return String(e)
}

function openCreate() {
  editingKey.value = null
  formKey.value = ''
  formValue.value = ''
  formNote.value = ''
  formType.value = ''
  formScope.value = ''
  formAgents.value = ''
  formExpires.value = ''
  scopeExtra.value = ''
  formError.value = ''
  editingMeta.value = null
  resetFormFile()
  showForm.value = true
}

function openEdit(c: CredentialMeta) {
  editingKey.value = c.key_name
  formKey.value = c.key_name
  formValue.value = ''
  formNote.value = c.description || ''
  formType.value = c.cred_type || ''
  // 范围回填：all / admin 直填，agents: 拆出名单，其它取值原样保住
  const rawScope = c.scope || ''
  scopeExtra.value = ''
  formAgents.value = ''
  if (!rawScope || rawScope === 'all' || rawScope === 'admin') {
    formScope.value = rawScope
  } else if (rawScope.startsWith(AGENTS_PREFIX)) {
    formScope.value = AGENTS_PREFIX
    formAgents.value = scopeAgentsText(rawScope)
  } else {
    formScope.value = rawScope
    scopeExtra.value = rawScope
  }
  formExpires.value = isoToLocalInput(c.expires_at)
  formError.value = ''
  editingMeta.value = c
  resetFormFile()
  showForm.value = true
}

// 后端 key_name 校验规则（sgme/credentials KEY_NAME_RE + 保留名）同步前端预检
function validateKeyName(name: string): string {
  if (!KEY_NAME_RE.test(name)) return '键名非法：须为大写字母起始的大写字母/数字/下划线（例：DEEPSEEK_API_KEY）'
  if (RESERVED.has(name)) return '键名为保留名（引导/主密钥空间，不进库）'
  return ''
}

async function saveForm() {
  formError.value = ''
  const key = formKey.value.trim()
  const invalid = validateKeyName(key)
  if (invalid) {
    formError.value = invalid
    return
  }
  const editing = !!editingKey.value
  const fileMode = formType.value === 'file'
  if (fileMode) {
    // 文件类型不填 value：新建必选文件；编辑留空 = 不替换（仅当该键已有文件内容时可留空）
    if (!formFile.value && fileRequired.value) {
      formError.value = editing
        ? '该键当前没有文件内容，请选择要上传的文件'
        : '请选择要上传的文件'
      return
    }
  } else if (!editing && !formValue.value) {
    // 新建必填值；编辑留空 = 只更新元数据（不强制重填值）
    formError.value = '值必填（不回显，请重新输入完整值）'
    return
  }
  const scope = scopeToSubmit()
  if (formScope.value === AGENTS_PREFIX && scope === AGENTS_PREFIX) {
    formError.value = '范围选「指定 agent」时，请填写至少一个 agent 标识（英文逗号分隔）'
    return
  }
  if (formExpires.value && !localInputToIso(formExpires.value)) {
    formError.value = '过期时间格式无效'
    return
  }
  const note = formNote.value.trim()
  // 过期时间：datetime-local 本地值 → ISO；留空 = 清空（编辑）/ 不设置（新建）
  const expiresIso = localInputToIso(formExpires.value)
  busy.value = true
  try {
    let doneText = ''
    if (fileMode) {
      // 1) 先落文件：新建 = 创建「文件」键；编辑 = 替换文件内容（留空则跳过）
      const picked = formFile.value
      let uploadedSize = 0
      if (picked) {
        const uploaded = await uploadCredentialFile(key, picked)
        uploadedSize = uploaded.size
      }
      // 2) 元数据：文件键可只改元数据（不带 value）。新建仅在填了备注 / 范围 / 过期时间时补写；
      //    编辑一律显式上送（空串 "" = 清空），与值类型的编辑语义一致
      if (editing || note || scope || expiresIso) {
        await writeCredential({
          key_name: key,
          description: note,
          scope,
          cred_type: 'file',
          expires_at: expiresIso,
        })
      }
      doneText = picked
        ? '已保存文件 ' + key + '（' + picked.name + '，' + fmtFileSize(uploadedSize) + '）'
        : '已更新凭据 ' + key + '（文件未替换）'
    } else if (editing) {
      // 编辑：元数据一律显式上送（空串 "" = 清空）；value 仅在填写时才上送（= 不改值）
      const input: CredentialWriteInput = {
        key_name: key,
        description: note,
        scope,
        cred_type: formType.value,
        expires_at: expiresIso,
      }
      if (formValue.value) input.value = formValue.value
      await writeCredential(input)
      doneText = '已更新凭据 ' + key
    } else {
      // 新建：value 必填；未填的元数据按 null 上送（= 不设置）
      await writeCredential({
        key_name: key,
        value: formValue.value,
        description: note || null,
        scope: scope || null,
        cred_type: formType.value || null,
        expires_at: expiresIso || null,
      })
      doneText = '已保存凭据 ' + key
    }
    showForm.value = false
    // 立即抹去内存中的明文 / 文件引用（表单关闭即清空，不留引用）
    formValue.value = ''
    formAgents.value = ''
    formExpires.value = ''
    resetFormFile()
    info.value = doneText
    filter.value = ''
    await load()
  } catch (e) {
    formError.value = toErrText(e)
  } finally {
    busy.value = false
  }
}

// ---------- 删除 / 恢复 ----------

async function remove(c: CredentialMeta) {
  if (!confirm(`确定删除凭据 ${c.key_name}？\n\n删除为软删，可随时在「恢复凭据」中恢复；期间该键不可写入（写入会提示先恢复）。`)) return
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    await deleteCredential(c.key_name)
    info.value = `已删除 ${c.key_name}（软删，可恢复）`
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

const showRestore = ref(false)
const restoreKey = ref('')
const restoreError = ref('')

async function submitRestore() {
  restoreError.value = ''
  const key = restoreKey.value.trim()
  const invalid = validateKeyName(key)
  if (invalid) {
    restoreError.value = invalid
    return
  }
  busy.value = true
  try {
    await restoreCredential(key)
    showRestore.value = false
    info.value = `已恢复 ${key}`
    restoreKey.value = ''
    filter.value = ''
    await load()
  } catch (e) {
    restoreError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// ---------- 导出（危险操作：二次确认 + 明文落盘提示） ----------

function triggerDownload(content: string, filename: string) {
  const blob = new Blob([content], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// 二进制下载（D-37 v1.2）：fetch 回来的 Blob 直接交给浏览器，页面不留引用（10s 后释放）
function triggerBlobDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

// 简单转义防止注入生成的 JSON 文件内容之外——值按 JSON 字符串序列化本身安全
async function doExport() {
  const n = items.value.length
  const first = confirm(
    `即将导出全部 ${n} 条凭据的明文值。\n\n` +
      '风险提示：\n' +
      '· 导出内容包含全部密钥明文，等同于交出所有凭据；\n' +
      '· 文件将下载到本机（sgme-credentials-export.json），请勿上传或分享；\n' +
      '· 导出操作会在服务端记入安全审计日志。\n\n确定继续？',
  )
  if (!first) return
  // 二次确认：显式输入 DELETE 类关键词（大小写不敏感），防误触
  const second = prompt('这是高危操作。请输入 EXPORT 确认（输入其他内容或留空将取消）：')
  if ((second || '').trim().toUpperCase() !== 'EXPORT') {
    info.value = '已取消导出'
    return
  }
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    // 明文只在内存瞬时存在：取回即组装下载，随后引用即弃；不进 console / 不落任何存储
    const values = await exportCredentials()
    const count = Object.keys(values).length
    triggerDownload(JSON.stringify({ credentials: values }, null, 2), 'sgme-credentials-export.json')
    info.value = `已导出 ${count} 条凭据明文（已下载 sgme-credentials-export.json，请妥善保管并及时删除）`
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// ---------- 文件下载（D-37 v1.2） ----------

// 文件键的 GET /v1/credentials/{key} 返回 400，内容一律走 /file 通道
async function downloadFile(c: CredentialMeta) {
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    // 字节只在 Blob 中瞬时存在：取回即交给浏览器下载，随后释放，不进 console / 存储
    const { blob, filename } = await downloadCredentialFile(c.key_name)
    // 文件名优先用 Content-Disposition 返回值，回退列表里的 filename
    const name = filename || c.filename || c.key_name
    triggerBlobDownload(blob, name)
    info.value = '已下载 ' + c.key_name + '（' + name + '，' + fmtFileSize(blob.size) + '）'
  } catch (e) {
    error.value = toErrText(e)
  } finally {
    busy.value = false
  }
}

// ---------- 列表 ----------

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listCredentials()
    items.value = data.credentials
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="creds">
    <div class="head">
      <h2>凭证管理</h2>
      <div class="actions">
        <input
          v-model="filter"
          class="cred-filter"
          placeholder="按键名 / 备注过滤…"
        />
        <button :disabled="busy" @click="showRestore = true">恢复凭据…</button>
        <button class="btn btn-danger" :disabled="busy" @click="doExport">导出明文…</button>
        <button class="btn btn-primary" :disabled="busy" @click="openCreate">＋ 新建凭据</button>
        <button :disabled="busy" @click="load">刷新</button>
      </div>
    </div>

    <p class="hint">
      值一律加密存储、永不回显；写入 / 删除 / 恢复需要管理员 Key。读取优先用管理员 Key
      ——已过期与「仅管理员」凭据只有管理员能看到（Agent Key 读不到；403 = 无权限或已过期）。
      删除为软删，可通过「恢复凭据」找回。文件（重要数据）类型支持上传 / 下载原始二进制
      （单条上限 2 MiB；文件键的「取单值」端点返回 400，内容只走下载通道）。
    </p>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info">{{ info }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-else-if="filtered.length" class="tbl-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th>键名</th>
            <th>备注</th>
            <th>类型</th>
            <th>范围</th>
            <th>过期时间</th>
            <th>最近使用</th>
            <th>更新时间</th>
            <th>更新人</th>
            <th>状态</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in filtered" :key="c.key_name">
            <td><code class="inline">{{ c.key_name }}</code></td>
            <td class="note-cell">
              <div class="note-text">{{ c.description || '—' }}</div>
              <div v-if="c.cred_type === 'file'" class="file-meta" :title="c.file_sha256 || ''">
                <span class="file-name">{{ c.filename || '（未命名文件）' }}</span>
                <span v-if="c.file_size != null" class="file-size">· {{ fmtFileSize(c.file_size) }}</span>
              </div>
            </td>
            <td class="type-cell">
              <span v-if="c.cred_type" class="tag" :title="c.cred_type">{{ credTypeLabel(c.cred_type) }}</span>
              <span v-else class="dash">—</span>
            </td>
            <td class="scope-cell">
              <span v-if="!c.scope" class="dash">—</span>
              <span v-else-if="c.scope === 'all'" class="scope">全部 agent</span>
              <span v-else-if="c.scope === 'admin'" class="scope scope-admin">仅管理员</span>
              <span v-else class="scope-agents" :title="c.scope">{{ scopeAgentsText(c.scope) || c.scope }}</span>
            </td>
            <td class="ts-cell" :class="{ 'ts-expired': c.expired }">{{ fmtTs(c.expires_at) }}</td>
            <td class="ts-cell">{{ fmtTs(c.last_used_at) }}</td>
            <td class="ts-cell">{{ fmtTs(c.updated_at) }}</td>
            <td class="by-cell">{{ c.updated_by }}</td>
            <td>
              <span v-if="c.expired" class="st st-danger">已过期</span>
              <span v-else class="st st-ok">正常</span>
            </td>
            <td class="act-cell">
              <button
                v-if="c.cred_type === 'file'"
                class="btn btn-sm"
                :disabled="busy"
                @click="downloadFile(c)"
              >
                下载
              </button>
              <button class="btn btn-sm" :disabled="busy" @click="openEdit(c)">更新</button>
              <button class="btn btn-sm btn-danger" :disabled="busy" @click="remove(c)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="empty">
      {{ filter ? '无匹配凭据。' : '暂无凭据。点击「＋ 新建凭据」添加（例：DEEPSEEK_API_KEY）。' }}
      已删除的键请用「恢复凭据」找回。
    </p>

    <!-- 新建 / 更新弹窗 -->
    <div v-if="showForm" class="modal" @click.self="showForm = false">
      <div class="modal-box">
        <h3>{{ editingKey ? `更新凭据 ${editingKey}` : '新建凭据' }}</h3>
        <label>
          键名 *
          <input
            v-model="formKey"
            :disabled="!!editingKey"
            placeholder="如 DEEPSEEK_API_KEY"
            spellcheck="false"
          />
        </label>
        <!-- D-37 v1.2：文件类型隐藏 value 输入，改为文件选择（新建必选；编辑留空 = 不替换） -->
        <label v-if="isFileType">
          {{ editingKey ? '新文件（留空 = 不替换现有文件）' : '文件 *' }}
          <input ref="fileInputEl" type="file" @change="onFilePick" />
          <span class="file-hint">单条上限 2 MiB；保存后字节不可在线查看，只能下载。</span>
          <span v-if="editingMeta && editingMeta.cred_type === 'file'" class="file-hint">
            当前文件：{{ editingMeta.filename || '（未命名）' }}
            <template v-if="editingMeta.file_size != null">
              （{{ fmtFileSize(editingMeta.file_size) }}）
            </template>
          </span>
        </label>
        <label v-else>
          {{ editingKey ? '新值（留空 = 只更新元数据；不回显旧值）' : '值 *' }}
          <input
            v-model="formValue"
            type="password"
            autocomplete="new-password"
            :placeholder="editingKey ? '留空表示不改值' : '输入凭据值（保存后不可再查看）'"
          />
        </label>
        <label>
          备注（可选）
          <input v-model="formNote" placeholder="用途说明，仅自己可见" />
        </label>
        <div class="form-row">
          <label>
            类型
            <select v-model="formType">
              <option v-for="t in CRED_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option>
            </select>
          </label>
          <label>
            范围
            <select v-model="formScope">
              <option v-for="s in scopeChoices" :key="s.value" :value="s.value">{{ s.label }}</option>
            </select>
          </label>
        </div>
        <label v-if="formScope === AGENTS_PREFIX">
          指定 agent（英文逗号分隔）
          <input v-model="formAgents" placeholder="如 dsh,hermes,zcode" spellcheck="false" />
        </label>
        <label>
          过期时间（可选，留空 = 不过期）
          <input v-model="formExpires" type="datetime-local" />
        </label>
        <p v-if="formError" class="error">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn" :disabled="busy" @click="showForm = false">取消</button>
          <button class="btn btn-primary" :disabled="busy" @click="saveForm">
            {{ busy ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 恢复弹窗（后端列表只返回未删键，已删键名不在列表中，需手动输入） -->
    <div v-if="showRestore" class="modal" @click.self="showRestore = false">
      <div class="modal-box">
        <h3>恢复凭据</h3>
        <p class="hint">
          输入已删除的键名进行恢复（列表默认不显示已删凭据，恢复后重新出现在列表中）。
        </p>
        <label>
          键名 *
          <input
            v-model="restoreKey"
            placeholder="如 DEEPSEEK_API_KEY"
            spellcheck="false"
            @keyup.enter="submitRestore"
          />
        </label>
        <p v-if="restoreError" class="error">{{ restoreError }}</p>
        <div class="modal-actions">
          <button class="btn" :disabled="busy" @click="showRestore = false">取消</button>
          <button class="btn btn-primary" :disabled="busy" @click="submitRestore">
            {{ busy ? '恢复中…' : '恢复' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cred-filter {
  width: 220px;
}

.note-cell {
  color: var(--text-muted);
  max-width: 320px;
}

/* D-37 v1.2：文件行在备注下方补一行「文件名 · 大小」，不挤压行高 */
.note-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-meta {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-top: 2px;
  max-width: 320px;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-faint);
}

.file-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
}

.file-size {
  white-space: nowrap;
}

.ts-cell {
  white-space: nowrap;
  color: var(--text-muted);
}

.by-cell {
  color: var(--text-faint);
  font-size: var(--fs-sm);
}

.act-cell {
  white-space: nowrap;
  text-align: right;
}

.act-cell .btn + .btn {
  margin-left: var(--space-1);
}

.st {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: var(--fs-sm);
}

.st-ok {
  background: var(--st-ok-soft);
  color: var(--st-ok);
}

.st-danger {
  background: var(--st-danger-soft);
  color: var(--st-danger);
}

/* ---------- D-37 v1.1 新增列（类型 / 范围 / 过期时间 / 最近使用） ---------- */
/* 列数增至 10：窄屏时横向滚动，避免挤压换行 */
.tbl-wrap {
  overflow-x: auto;
}

.tbl-wrap .tbl {
  min-width: 1060px;
}

.type-cell {
  white-space: nowrap;
}

.scope-cell {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dash {
  color: var(--text-faint);
}

.scope-admin {
  background: var(--st-warn-soft);
  color: var(--st-warn);
}

.scope-agents {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}

.ts-expired {
  color: var(--st-danger);
}

/* 弹窗：类型 / 范围并排，避免弹窗过高 */
.form-row {
  display: flex;
  gap: var(--space-3);
}

.form-row > label {
  flex: 1;
  min-width: 0;
}

/* datetime-local 未被全局表单样式覆盖，补齐与其他输入框一致的外观 */
.modal-box input[type='datetime-local'] {
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
}

.modal-box input[type='datetime-local']:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

/* D-37 v1.2：文件选择框（原生 input 与全局表单外观对齐） */
.modal-box input[type='file'] {
  padding: 6px 8px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.modal-box input[type='file']:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

.file-hint {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
</style>
