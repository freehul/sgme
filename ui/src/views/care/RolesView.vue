<script setup lang="ts">
// 角色管理页（T-39）：角色列表（master）+ 详情/编辑/persona（detail）
// 数据源：/v1/admin/roles*（SGME-CareEngine设计-v0.1 §角色层）
import { computed, onMounted, ref } from 'vue'
import {
  archiveRole, assembleRole, generatePersona, getActiveRole, getPersona, getRole, listRoles,
  saveRole, setActiveRole,
  type AssembleResp, type RoleCardData, type RoleItem,
} from '../../api/roles'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

const roles = ref<RoleItem[]>([])
const loading = ref(false)
const error = ref('')

// 选中角色
const selectedId = ref<string | null>(null)
const detail = ref<RoleCardData | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const busy = ref(false)

// persona 区
const personaText = ref('')
const personaLoading = ref(false)
const personaMsg = ref('')

// 当前角色（T-40）
const activeRoleId = ref<string | null>(null)
// 装配预览（T-40）
const assembleData = ref<AssembleResp | null>(null)
const assembleLoading = ref(false)

// 编辑表单（新建/编辑共用）
const editing = ref(false)
const isNew = ref(false)
const newId = ref('')
const form = ref<RoleCardData>({
  name: '', description: '', personality: '', scenario: '',
  first_mes: '', mes_example: '', system_prompt: '', post_history_instructions: '',
})

const CARE_TYPE_LABEL: Record<string, string> = {
  care_todo_due: '待办提醒', care_mood: '情绪关怀',
  care_overwork: '过劳预警', care_daily: '每日问候',
}

const TRIGGER_HINTS: Record<string, string> = {
  mood_low: '情绪低落', overwork: '过劳', todo_due: '待办到期',
  dialysis_day: '透析日', bedtime: '睡前', weekend: '周末',
  goal_stagnant: '目标停滞', major_decision: '重大决策',
}

function careSummary(r: RoleItem): string {
  const c = detail?.value?.extensions?.sgme_care
  if (!c?.trigger_rules) return ''
  return Object.keys(c.trigger_rules)
    .map((k) => TRIGGER_HINTS[k] || k)
    .join(' · ')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listRoles()
    roles.value = data.roles
    try {
      const ar = await getActiveRole()
      activeRoleId.value = ar.role_id
    } catch (ae) {
      // 当前角色读取失败不阻塞列表
    }
    if (selectedId.value && !roles.value.some((r) => r.role_id === selectedId.value)) {
      selectedId.value = null
      detail.value = null
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function select(id: string) {
  selectedId.value = id
  editing.value = false
  isNew.value = false
  detail.value = null
  personaText.value = ''
  personaMsg.value = ''
  detailLoading.value = true
  detailError.value = ''
  try {
    const data = await getRole(id)
    const d = data.role.data
    detail.value = d
    form.value = {
      name: d.name, description: d.description, personality: d.personality ?? '',
      scenario: d.scenario ?? '', first_mes: d.first_mes ?? '',
      mes_example: d.mes_example ?? '', system_prompt: d.system_prompt ?? '',
      post_history_instructions: d.post_history_instructions ?? '',
    }
    // 尝试读 persona（404 = 未生成）
    try {
      const p = await getPersona(id)
      personaText.value = p.persona || ''
    } catch (pe) {
      if (pe instanceof ApiError && pe.status === 404) personaText.value = ''
      else personaMsg.value = pe instanceof ApiError ? pe.message : String(pe)
    }
  } catch (e) {
    detailError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

function startNew() {
  selectedId.value = null
  isNew.value = true
  editing.value = true
  newId.value = ''
  detail.value = null
  personaText.value = ''
  personaMsg.value = ''
  form.value = {
    name: '', description: '', personality: '', scenario: '',
    first_mes: '', mes_example: '', system_prompt: '', post_history_instructions: '',
  }
}

function startEdit() {
  editing.value = true
}

async function save() {
  if (isNew.value) {
    const id = newId.value.trim()
    if (!id) { personaMsg.value = '角色 id 必填（小写字母/数字/-/_）'; return }
    selectedId.value = id
  }
  if (!selectedId.value) return
  busy.value = true
  personaMsg.value = ''
  try {
    await saveRole(selectedId.value, form.value)
    personaMsg.value = '已保存'
    await load()
    if (!isNew.value) await select(selectedId.value)
    else { isNew.value = false; editing.value = false; await select(selectedId.value) }
  } catch (e) {
    personaMsg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function archive() {
  if (!selectedId.value) return
  if (!confirm(`归档角色「${detail.value?.name}」？（原件保留在 .archive/，可恢复）`)) return
  busy.value = true
  try {
    await archiveRole(selectedId.value)
    selectedId.value = null
    detail.value = null
    await load()
  } catch (e) {
    personaMsg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function genPersona() {
  if (!selectedId.value) return
  busy.value = true
  personaMsg.value = ''
  try {
    const r = await generatePersona(selectedId.value)
    personaMsg.value = `persona 已生成（${r.provider || 'LLM'}）`
    const p = await getPersona(selectedId.value)
    personaText.value = p.persona || ''
  } catch (e) {
    personaMsg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// ---------- 当前角色 / 装配预览（T-40） ----------

async function makeActive() {
  if (!selectedId.value) return
  busy.value = true
  personaMsg.value = ''
  try {
    await setActiveRole(selectedId.value)
    activeRoleId.value = selectedId.value
    personaMsg.value = `「${detail.value?.name}」已是当前沟通角色（换皮不换芯，记忆不变）`
  } catch (e) {
    personaMsg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function previewAssemble() {
  if (!selectedId.value) return
  assembleLoading.value = true
  assembleData.value = null
  try {
    assembleData.value = await assembleRole(selectedId.value, 'daily')
  } catch (e) {
    personaMsg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    assembleLoading.value = false
  }
}

const isActiveRole = computed(() => !!selectedId.value && selectedId.value === activeRoleId.value)

const hasCarePolicy = computed(() => !!detail.value?.extensions?.sgme_care)

onMounted(load)
</script>

<template>
  <div class="roles">
    <div class="head">
      <h2>角色管理</h2>
      <div class="filters">
        <button @click="startNew">＋ 新建角色</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div class="split">
      <!-- 左侧：角色卡片列表 -->
      <div class="list-pane">
        <p v-if="loading" class="empty">加载中…</p>
        <p v-else-if="!roles.length" class="empty">暂无角色。点击「新建角色」创建，或从内置模板开始（管家/伴侣/朋友/导师）。</p>
        <div v-else class="role-cards">
          <div
            v-for="r in roles"
            :key="r.role_id"
            class="role-card"
            :class="{ active: selectedId === r.role_id }"
            @click="select(r.role_id)"
          >
            <div class="role-card-head">
              <span class="role-name">{{ r.name }}</span>
              <span class="role-id mono">{{ r.role_id }}</span>
            </div>
            <div class="role-desc">{{ r.description }}</div>
            <div class="role-meta">
              <span v-if="careSummary(r)" class="care-summary">{{ careSummary(r) }}</span>
              <span class="role-ts">{{ fmtTs(r.updated_at) }}</span>
            </div>
            <div v-if="activeRoleId === r.role_id" class="active-badge">✓ 当前角色</div>
          </div>
        </div>
      </div>

      <!-- 右侧：详情 / 编辑 -->
      <div class="detail-pane">
        <div v-if="!selectedId && !isNew" class="empty">从左侧选择一个角色查看详情，或新建角色</div>
        <p v-else-if="detailLoading" class="empty">加载中…</p>
        <p v-else-if="detailError" class="error">{{ detailError }}</p>

        <!-- 新建表单 -->
        <template v-else-if="isNew">
          <div class="detail-head"><h3>新建角色</h3></div>
          <div class="form">
            <label>角色 id（小写字母/数字/-/_）</label>
            <input v-model="newId" placeholder="如 butler / companion" />
            <label>名称 *</label>
            <input v-model="form.name" placeholder="如 管家" />
            <label>描述 *</label>
            <textarea v-model="form.description" rows="3" placeholder="一句话介绍这个角色" />
            <label>性格</label>
            <textarea v-model="form.personality" rows="3" placeholder="行为指令优于形容词：他低落时先共情再谈事" />
            <label>开场白</label>
            <textarea v-model="form.first_mes" rows="2" />
            <label>系统提示（可选，支持 char/user 宏占位）</label>
            <textarea v-model="form.system_prompt" rows="4" />
            <p v-if="personaMsg" class="note-text">{{ personaMsg }}</p>
            <div class="actions">
              <button class="btn btn-primary" :disabled="busy" @click="save">创建</button>
              <button class="btn" @click="isNew = false; editing = false">取消</button>
            </div>
          </div>
        </template>

        <!-- 详情 / 编辑 -->
        <template v-else-if="detail">
          <div class="detail-head">
            <h3>{{ detail.name }}</h3>
            <span class="role-id-tag mono">{{ selectedId }}</span>
          </div>

          <section class="detail-block">
            <h4>描述</h4>
            <p class="role-desc">{{ detail.description }}</p>
            <div class="meta">
              <span>更新: {{ fmtTs(detail.updated_at) }}</span>
              <span>创建: {{ fmtTs(detail.created_at) }}</span>
            </div>
          </section>

          <section v-if="hasCarePolicy" class="detail-block">
            <h4>主动关怀策略</h4>
            <p v-if="detail.extensions?.sgme_care?.greeting_templates?.length" class="meta">
              问候模板: {{ detail.extensions!.sgme_care!.greeting_templates!.length }} 条
            </p>
            <ul class="trigger-list">
              <li v-for="(desc, key) in detail.extensions?.sgme_care?.trigger_rules || {}" :key="key">
                <span class="trigger-key">{{ TRIGGER_HINTS[key] || key }}</span>
                <span class="trigger-desc">{{ desc }}</span>
              </li>
            </ul>
            <p v-if="detail.extensions?.sgme_care?.frequency" class="meta">
              频率: {{ JSON.stringify(detail.extensions.sgme_care.frequency) }}
            </p>
          </section>

          <!-- persona 区 -->
          <section class="detail-block">
            <h4>沟通画像（persona）</h4>
            <div class="actions">
              <button v-if="!personaText" class="btn btn-primary" :disabled="busy" @click="genPersona">
                生成 persona（LLM 四层扫描）
              </button>
              <button v-else class="btn" :disabled="busy" @click="genPersona">重新生成</button>
            </div>
            <p v-if="personaMsg" class="note-text">{{ personaMsg }}</p>
            <pre v-if="personaText" class="persona-text">{{ personaText }}</pre>
            <p v-else class="empty">未生成。persona 是 LLM 基于你的记忆生成的沟通画像（唯一物化例外）。</p>
          </section>

          <!-- 编辑表单 -->
          <section v-if="editing" class="detail-block">
            <h4>编辑角色</h4>
            <div class="form">
              <label>名称 *</label>
              <input v-model="form.name" />
              <label>描述 *</label>
              <textarea v-model="form.description" rows="3" />
              <label>性格</label>
              <textarea v-model="form.personality" rows="3" />
              <label>场景</label>
              <textarea v-model="form.scenario" rows="2" />
              <label>开场白</label>
              <textarea v-model="form.first_mes" rows="2" />
              <label>示例对话</label>
              <textarea v-model="form.mes_example" rows="3" />
              <label>系统提示</label>
              <textarea v-model="form.system_prompt" rows="4" />
              <label>回复后指令</label>
              <textarea v-model="form.post_history_instructions" rows="2" />
              <div class="actions">
                <button class="btn btn-primary" :disabled="busy" @click="save">保存</button>
                <button class="btn" @click="editing = false; select(selectedId!)">取消</button>
              </div>
            </div>
          </section>

          <!-- 装配预览（T-40） -->
          <section class="detail-block">
            <h4>沟通装配预览</h4>
            <div class="actions">
              <button class="btn" :disabled="busy || assembleLoading" @click="previewAssemble">
                {{ assembleLoading ? '预览中…' : '预览「这个角色会怎么跟我说话」' }}
              </button>
              <button v-if="!isActiveRole" class="btn btn-primary" :disabled="busy" @click="makeActive">
                设为当前角色
              </button>
              <span v-else class="active-badge">✓ 当前角色</span>
            </div>
            <template v-if="assembleData">
              <h5 class="asm-title">系统提示（注入 {{ assembleData.profile_blocks.length }} 个画像块）</h5>
              <pre class="persona-text">{{ assembleData.system_prompt }}</pre>
              <p v-if="assembleData.persona" class="note-text">persona 已生成，装配时一并注入</p>
              <p v-if="assembleData.care_policy" class="note-text">
                关怀策略：{{ JSON.stringify(assembleData.care_policy) }}
              </p>
            </template>
          </section>

          <section class="detail-block">
            <h4>操作</h4>
            <div class="actions">
              <button class="btn" :disabled="busy" @click="startEdit">编辑</button>
              <button class="danger" :disabled="busy" @click="archive">归档角色</button>
            </div>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.role-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.role-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.role-card:hover { border-color: var(--brand); }
.role-card.active { border-color: var(--brand); background: color-mix(in srgb, var(--brand) 8%, transparent); }
.role-card-head { display: flex; justify-content: space-between; align-items: baseline; }
.role-name { font-weight: 600; font-size: 15px; }
.role-id { font-size: 12px; opacity: 0.6; }
.role-desc { font-size: 13px; opacity: 0.85; margin: 4px 0; }
.role-meta { display: flex; justify-content: space-between; font-size: 12px; opacity: 0.6; }
.care-summary { color: var(--brand); }
.role-id-tag { font-size: 12px; opacity: 0.6; }
.active-badge {
  display: inline-block;
  margin-top: 6px;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  color: var(--success);
  border: 1px solid var(--success);
}
.asm-title { margin: 8px 0 4px; font-size: 13px; opacity: 0.8; }
.trigger-list { list-style: none; padding: 0; margin: 6px 0; }
.trigger-list li { display: flex; gap: 8px; font-size: 13px; padding: 2px 0; }
.trigger-key { color: var(--brand); white-space: nowrap; }
.trigger-desc { opacity: 0.85; }
.persona-text {
  background: var(--surface-muted);
  border-radius: 6px;
  padding: 10px;
  font-size: 13px;
  white-space: pre-wrap;
  max-height: 320px;
  overflow: auto;
}
.form { display: flex; flex-direction: column; gap: 6px; }
.form label { font-size: 12px; opacity: 0.7; margin-top: 4px; }
.actions { display: flex; gap: 8px; margin-top: 8px; }
</style>
