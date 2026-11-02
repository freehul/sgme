<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  deleteCredential,
  exportCredentials,
  listCredentials,
  restoreCredential,
  writeCredential,
  type CredentialMeta,
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
const formError = ref('')

const KEY_NAME_RE = /^[A-Z][A-Z0-9_]*$/
// 后端保留名（引导/主密钥空间不进库），前端同规则预检
const RESERVED = new Set(['SGME_CRED_KEY', 'SGME_ADMIN_KEY', 'SGME_AGENT_KEY'])

function openCreate() {
  editingKey.value = null
  formKey.value = ''
  formValue.value = ''
  formNote.value = ''
  formError.value = ''
  showForm.value = true
}

function openEdit(c: CredentialMeta) {
  editingKey.value = c.key_name
  formKey.value = c.key_name
  formValue.value = ''
  formNote.value = c.description || ''
  formError.value = ''
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
  if (!formValue.value) {
    formError.value = '值必填（不回显，请重新输入完整值）'
    return
  }
  busy.value = true
  try {
    await writeCredential(key, formValue.value, formNote.value.trim() || undefined)
    showForm.value = false
    // 立即抹去内存中的明文（表单关闭即清空，不留引用）
    formValue.value = ''
    info.value = `已保存凭据 ${key}`
    filter.value = ''
    await load()
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : String(e)
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
      值一律加密存储、永不回显；写入 / 删除 / 恢复需要管理员 Key，读取需要 Agent（或管理员）Key。
      删除为软删，可通过「恢复凭据」找回。
    </p>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info">{{ info }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <table v-else-if="filtered.length" class="tbl">
      <thead>
        <tr>
          <th>键名</th>
          <th>备注</th>
          <th>更新时间</th>
          <th>更新人</th>
          <th>状态</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in filtered" :key="c.key_name">
          <td><code class="inline">{{ c.key_name }}</code></td>
          <td class="note-cell">{{ c.description || '—' }}</td>
          <td class="ts-cell">{{ fmtTs(c.updated_at) }}</td>
          <td class="by-cell">{{ c.updated_by }}</td>
          <td><span class="st st-ok">正常</span></td>
          <td class="act-cell">
            <button class="btn btn-sm" :disabled="busy" @click="openEdit(c)">更新</button>
            <button class="btn btn-sm btn-danger" :disabled="busy" @click="remove(c)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
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
        <label>
          {{ editingKey ? '新值 *（不回显旧值）' : '值 *' }}
          <input
            v-model="formValue"
            type="password"
            autocomplete="new-password"
            placeholder="输入凭据值（保存后不可再查看）"
          />
        </label>
        <label>
          备注（可选）
          <input v-model="formNote" placeholder="用途说明，仅自己可见" />
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
  overflow: hidden;
  text-overflow: ellipsis;
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
</style>
