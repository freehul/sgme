<script setup lang="ts">
// 角色人生编辑器（2.0 §4.2，T-20）：三栏布局——
//   左栏 基线层（传记/三观/爱好小习惯/语气音色，工作台写定，静态"来源层"）
//   中栏 性格六槽位（语言风格/情绪反应/价值观取舍/社交姿态/冲突处理/亲密表达，呈现层）
//   右栏 试对话（装配预览）+ 演化时间线（共同经历/心情，带触发引用）
// 数据源：/v1/admin/roles*（roles.ts）；演化层契约先行，后端未上线时按空态显示
import { computed, onMounted, reactive, ref } from 'vue'
import {
  archiveRole, appendEvolution, assembleRole, emptyEvolution, generatePersona, getActiveRole,
  getEvolution, getPersona, getRole, listRoles, saveRole, setActiveRole,
  type AssembleResp, type EvolutionEntry, type EvolutionKind, type RoleCardData, type RoleItem,
} from '../../api/roles'
import { ApiError } from '../../api/client'
import { fmtTs } from '../../utils/format'

// ---------- 六槽位定义（key=契约中文字段名；hint=一句话说明） ----------
const SLOT_DEFS: Array<{ key: string; label: string; hint: string; ph: string }> = [
  { key: '语言风格', label: '语言风格', hint: '句式长短、用词习惯、口头禅', ph: '例：短句为主，先给结论；生气时反问，不绕弯' },
  { key: '情绪反应', label: '情绪反应', hint: '喜怒哀乐怎么表达、恢复快慢', ph: '例：难过时话变少，不直说但会被察觉' },
  { key: '价值观取舍', label: '价值观取舍', hint: '两难时优先什么、底线在哪', ph: '例：诚实优先于安慰，但拒绝表演式抱歉' },
  { key: '社交姿态', label: '社交姿态', hint: '主动还是被动、距离感、幽默度', ph: '例：熟人面前话多爱调侃，生人面前先观察' },
  { key: '冲突处理', label: '冲突处理', hint: '争执时的姿态：回避/对峙/调解', ph: '例：先听完整再表态，对事不对人，不翻旧账' },
  { key: '亲密表达', label: '亲密表达', hint: '对亲近的人如何表达在乎', ph: '例：不说软话，用行动表达——记住小事、随叫随到' },
]

// ---------- 演化时间线分组（kind → 标题/说明，契约四类） ----------
const EVOLUTION_TABS: Array<{ kind: EvolutionKind; label: string; empty: string }> = [
  { kind: 'timeline', label: '共同经历', empty: '还没有共同经历记录。它在对话中自然发生，由提炼管线或手动追加。' },
  { kind: 'feelings', label: '心情想法', empty: '还没有心情记录。隐私层想法会带触发引用出现在这里。' },
  { kind: 'personality_changes', label: '性格演化', empty: '性格槽位尚无演化记录（次版 D-71：重大事件后版本化更新）。' },
  { kind: 'values_changes', label: '三观演化', empty: '三观尚无演化记录（次版：想法挂梦境周期提炼后产生）。' },
]

const roles = ref<RoleItem[]>([])
const loading = ref(false)
const error = ref('')

// 选中角色
const selectedId = ref<string | null>(null)
const detail = ref<RoleCardData | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const busy = ref(false)
const msg = ref('')

// persona 区（保留现有能力）
const personaText = ref('')
const personaMsg = ref('')

// 当前角色（换皮不换芯）
const activeRoleId = ref<string | null>(null)
// 装配预览（试对话区）
const assembleData = ref<AssembleResp | null>(null)
const assembleLoading = ref(false)

// 基线层表单（左栏）：从 extensions.sgme_profile 读写
const profile = reactive({ biography: '', values: '', hobbies: '', voice: '' })

// 六槽位（中栏）：key 为契约中文字段名
const slots = reactive<Record<string, string>>({})
for (const s of SLOT_DEFS) slots[s.key] = ''

// 演化时间线（右栏下）
const evolution = reactive(emptyEvolution())
const evoLoading = ref(false)
const evoError = ref('')
const evoTab = ref<EvolutionKind>('timeline')
const addKind = ref<EvolutionKind>('timeline')
const addContent = ref('')
const addTrigger = ref('')
const evoBusy = ref(false)

// 顶部新建表单（保持精简：完整人生在下方三栏编辑）
const isNew = ref(false)
const newId = ref('')
const newForm = reactive({ name: '', description: '', personality: '' })

// 详情编辑表单（角色卡基础字段，保留现有 persona 编辑能力）
const editing = ref(false)
const form = reactive<RoleCardData>({
  name: '', description: '', personality: '', scenario: '',
  first_mes: '', mes_example: '', system_prompt: '', post_history_instructions: '',
})

// sgme_profile 未保存标记（任何基线层/槽位输入置位）
const profileDirty = ref(false)
function markProfileDirty() {
  profileDirty.value = true
}

// sgme_profile 缺失视为未启用（兼容 1.x 老卡，中栏提示补写基线）
const hasProfile = computed(() => !!detail.value?.extensions?.sgme_profile)

// 演化页签当前组与空态文案
const currentEvolutionTab = computed(() => EVOLUTION_TABS.find((t) => t.kind === evoTab.value)!)
const evolutionEntries = computed<EvolutionEntry[]>(() => evolution[evoTab.value] || [])

const TRIGGER_HINTS: Record<string, string> = {
  mood_low: '情绪低落', overwork: '过劳', todo_due: '待办到期',
  medical_day: '就医日', bedtime: '睡前', weekend: '周末',
  goal_stagnant: '目标停滞', major_decision: '重大决策',
}

// ---------- D-61（2026-11-04）：关怀三档（按角色独立配置；新角色默认 silent） ----------
const CARE_LEVEL_OPTIONS = [
  { value: 'silent', label: '静默关怀（只记录不推送）' },
  { value: 'session', label: '会话内关怀（聊天中自然带出）' },
  { value: 'push', label: '主动推送（关键信号才推）' },
] as const
const careLevel = ref<'silent' | 'session' | 'push'>('silent')
const careLevelLabel = computed(
  () => CARE_LEVEL_OPTIONS.find((o) => o.value === careLevel.value)?.label || '静默关怀',
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await listRoles()
    roles.value = data.roles
    try {
      const ar = await getActiveRole()
      activeRoleId.value = ar.role_id
    } catch {
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

// 把角色卡 extensions.sgme_profile 灌进左右两栏表单
function fillProfileForms(d: RoleCardData) {
  const p = d.extensions?.sgme_profile || {}
  profile.biography = p.biography ?? ''
  profile.values = p.values ?? ''
  profile.hobbies = p.hobbies ?? ''
  profile.voice = p.voice ?? ''
  for (const s of SLOT_DEFS) slots[s.key] = p.personality_slots?.[s.key] ?? ''
}

async function select(id: string) {
  selectedId.value = id
  editing.value = false
  isNew.value = false
  detail.value = null
  personaText.value = ''
  personaMsg.value = ''
  msg.value = ''
  assembleData.value = null
  Object.assign(evolution, emptyEvolution())
  evoError.value = ''
  detailLoading.value = true
  detailError.value = ''
  try {
    const data = await getRole(id)
    const d = data.role.data
    detail.value = d
    form.name = d.name
    form.description = d.description
    form.personality = d.personality ?? ''
    form.scenario = d.scenario ?? ''
    form.first_mes = d.first_mes ?? ''
    form.mes_example = d.mes_example ?? ''
    form.system_prompt = d.system_prompt ?? ''
    form.post_history_instructions = d.post_history_instructions ?? ''
    fillProfileForms(d)
    // D-61：档位随角色卡载入（缺省 = 静默，老卡零迁移）
    careLevel.value = d.extensions?.sgme_care?.care_level ?? 'silent'
    // persona（404 = 未生成）
    try {
      const p = await getPersona(id)
      personaText.value = p.persona || ''
    } catch (pe) {
      if (pe instanceof ApiError && pe.status === 404) personaText.value = ''
      else personaMsg.value = pe instanceof ApiError ? pe.message : String(pe)
    }
    // 演化层（契约先行：404 = 后端未上线，按空态显示不报错）
    loadEvolution(id)
  } catch (e) {
    detailError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

// ---------- 基线层 + 六槽位保存（左栏/中栏共用一次保存） ----------
async function saveProfile() {
  if (!selectedId.value || !detail.value) return
  busy.value = true
  msg.value = ''
  try {
    const data: RoleCardData = { ...detail.value, name: detail.value.name, description: detail.value.description }
    data.extensions = {
      ...detail.value.extensions,
      sgme_profile: {
        biography: profile.biography,
        values: profile.values,
        hobbies: profile.hobbies,
        voice: profile.voice,
        personality_slots: { ...slots },
      },
    }
    await saveRole(selectedId.value, data)
    profileDirty.value = false
    await load()
    await select(selectedId.value)
    msg.value = '人设已保存（基线层 + 性格六槽位）'
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// ---------- D-61 关怀档位保存（按角色独立；改动即保存） ----------
async function saveCareLevel() {
  if (!selectedId.value || !detail.value) return
  busy.value = true
  msg.value = ''
  try {
    const data: RoleCardData = {
      ...detail.value,
      name: detail.value.name,
      description: detail.value.description,
    }
    data.extensions = {
      ...(detail.value.extensions || {}),
      sgme_care: {
        ...(detail.value.extensions?.sgme_care || {}),
        care_level: careLevel.value,
      },
    }
    await saveRole(selectedId.value, data)
    await load()
    await select(selectedId.value)
    msg.value = `关怀档位已设为「${careLevelLabel.value}」`
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// ---------- persona（保留现有能力） ----------
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

// ---------- 详情基础字段编辑（角色卡原生字段） ----------
function startEdit() {
  editing.value = true
}

async function saveForm() {
  if (!selectedId.value) return
  busy.value = true
  msg.value = ''
  try {
    await saveRole(selectedId.value, { ...detail.value, ...form })
    editing.value = false
    await load()
    await select(selectedId.value)
    msg.value = '已保存'
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
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
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

// ---------- 当前角色 / 装配预览（试对话区） ----------
async function makeActive() {
  if (!selectedId.value) return
  busy.value = true
  msg.value = ''
  try {
    await setActiveRole(selectedId.value)
    activeRoleId.value = selectedId.value
    msg.value = `「${detail.value?.name}」已是当前沟通角色（换皮不换芯，记忆不变）`
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
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
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    assembleLoading.value = false
  }
}

const isActiveRole = computed(() => !!selectedId.value && selectedId.value === activeRoleId.value)

// ---------- 演化时间线 ----------
async function loadEvolution(id: string) {
  evoLoading.value = true
  evoError.value = ''
  try {
    const data = await getEvolution(id)
    evolution.timeline = data.timeline || []
    evolution.feelings = data.feelings || []
    evolution.personality_changes = data.personality_changes || []
    evolution.values_changes = data.values_changes || []
  } catch (e) {
    // 契约：文件不存在返回 200 空结构；走到这里是后端未上线/网络错误
    if (e instanceof ApiError && e.status === 404) {
      Object.assign(evolution, emptyEvolution())
    } else {
      evoError.value = e instanceof ApiError ? e.message : String(e)
    }
  } finally {
    evoLoading.value = false
  }
}

async function addEvolution() {
  if (!selectedId.value || !addContent.value.trim()) return
  evoBusy.value = true
  evoError.value = ''
  try {
    await appendEvolution(selectedId.value, {
      kind: addKind.value,
      content: addContent.value.trim(),
      trigger_ref: addTrigger.value.trim() || undefined,
      ts: new Date().toISOString(),
    })
    addContent.value = ''
    addTrigger.value = ''
    await loadEvolution(selectedId.value)
  } catch (e) {
    evoError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    evoBusy.value = false
  }
}

// ---------- 新建角色 ----------
function startNew() {
  isNew.value = true
  newId.value = ''
  newForm.name = ''
  newForm.description = ''
  newForm.personality = ''
}

async function createRole() {
  const id = newId.value.trim()
  if (!id) { msg.value = '角色 id 必填（小写字母/数字/-/_）'; return }
  if (!newForm.name.trim()) { msg.value = '名称必填'; return }
  busy.value = true
  msg.value = ''
  try {
    await saveRole(id, {
      name: newForm.name.trim(),
      description: newForm.description.trim(),
      personality: newForm.personality,
    })
    isNew.value = false
    await load()
    await select(id)
  } catch (e) {
    msg.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="roles">
    <!-- 顶部：角色列表切换 / 新建 -->
    <div class="head">
      <h2>角色人生编辑器<span class="sub">基线层写定人生 · 六槽位呈现性格 · 演化层记录关系</span></h2>
      <div class="filters">
        <select class="input" :value="selectedId ?? ''" @change="select(($event.target as HTMLSelectElement).value)">
          <option value="" disabled>切换角色…</option>
          <option v-for="r in roles" :key="r.role_id" :value="r.role_id">
            {{ r.name }}（{{ r.role_id }}）{{ activeRoleId === r.role_id ? ' ✓当前' : '' }}
          </option>
        </select>
        <button @click="startNew">＋ 新建角色</button>
        <button v-if="detail" class="danger" :disabled="busy" @click="archive">归档角色</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="msg" class="info">{{ msg }}</p>

    <!-- 新建表单 -->
    <div v-if="isNew" class="panel new-form">
      <h3>新建角色</h3>
      <div class="form-grid">
        <div>
          <label>角色 id（小写字母/数字/-/_）</label>
          <input v-model="newId" placeholder="如 butler / companion" />
        </div>
        <div>
          <label>名称 *</label>
          <input v-model="newForm.name" placeholder="如 管家" />
        </div>
        <div class="span-3">
          <label>描述 *</label>
          <textarea v-model="newForm.description" rows="2" placeholder="一句话介绍这个角色" />
        </div>
        <div class="span-3">
          <label>性格（可留空，建议在三栏里写六槽位）</label>
          <textarea v-model="newForm.personality" rows="2" placeholder="行为指令优于形容词：他低落时先共情再谈事" />
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" :disabled="busy" @click="createRole">创建</button>
        <button class="btn" @click="isNew = false">取消</button>
      </div>
    </div>

    <p v-if="loading" class="empty">加载中…</p>
    <div v-else-if="!roles.length && !isNew" class="empty">
      暂无角色。点击「新建角色」创建，或从内置模板开始（管家/伴侣/朋友/导师）。
    </div>

    <!-- 三栏主体 -->
    <div v-else-if="detail" class="editor">
      <!-- ===== 左栏：基线层 ===== -->
      <section class="col col-left">
        <div class="col-head">
          <h3>基线层<span class="sub">静态人生，写定不改</span></h3>
          <span v-if="!hasProfile" class="warn-tag">未启用</span>
        </div>
        <p class="col-note">传记是性格的「因」，六槽位是「果」。基线不进每轮注入，token 不涨。</p>

        <div class="field">
          <label>传记 <span class="hint">成长轨迹、人生经历——塑造性格的因</span></label>
          <textarea v-model="profile.biography" rows="9" placeholder="例：在南方小城长大，独子，父亲严厉母亲温和……" @input="markProfileDirty" />
        </div>
        <div class="field">
          <label>三观 <span class="hint">价值观、信念</span></label>
          <textarea v-model="profile.values" rows="6" placeholder="例：专业能力是最硬的通货；答应的事必须做到……" @input="markProfileDirty" />
        </div>
        <div class="field">
          <label>爱好小习惯 <span class="hint">让角色有生活的质感</span></label>
          <textarea v-model="profile.hobbies" rows="5" placeholder="例：泡茶必须现烧水；周末长跑；记账精确到分……" @input="markProfileDirty" />
        </div>
        <div class="field">
          <label>语气 / 音色基线 <span class="hint">语音接入时作参考</span></label>
          <input v-model="profile.voice" placeholder="例：低沉平稳，语速偏慢，句尾少用语气词" @input="markProfileDirty" />
        </div>
      </section>

      <!-- ===== 中栏：性格六槽位 ===== -->
      <section class="col col-mid">
        <div class="col-head">
          <h3>性格六槽位<span class="sub">呈现层，每轮注入</span></h3>
          <span v-if="profileDirty" class="dirty-tag">未保存</span>
        </div>
        <p class="col-note">按 D-69 做工：写行为指令与示例台词，不堆形容词。</p>

        <div v-for="s in SLOT_DEFS" :key="s.key" class="field">
          <label>{{ s.label }} <span class="hint">{{ s.hint }}</span></label>
          <textarea v-model="slots[s.key]" rows="4" :placeholder="s.ph" @input="markProfileDirty" />
        </div>

        <div class="mid-actions">
          <button class="btn btn-primary" :disabled="busy" @click="saveProfile">保存人设</button>
          <span v-if="!hasProfile" class="hint-text">当前角色卡还没有 sgme_profile，首次保存后启用基线层</span>
        </div>

        <!-- persona（保留现有能力） -->
        <div class="persona-block">
          <h4>沟通画像（persona）</h4>
          <div class="actions">
            <button v-if="!personaText" class="btn btn-sm" :disabled="busy" @click="genPersona">生成 persona（LLM 四层扫描）</button>
            <button v-else class="btn btn-sm" :disabled="busy" @click="genPersona">重新生成</button>
          </div>
          <p v-if="personaMsg" class="note-text">{{ personaMsg }}</p>
          <pre v-if="personaText" class="persona-text">{{ personaText }}</pre>
          <p v-else class="empty small">未生成。persona 是 LLM 基于你的记忆生成的沟通画像（唯一物化例外）。</p>
        </div>
      </section>

      <!-- ===== 右栏：试对话 + 演化时间线 ===== -->
      <section class="col col-right">
        <div class="col-head">
          <h3>试对话<span class="sub">装配预览</span></h3>
          <div class="head-actions">
            <button class="btn btn-sm" :disabled="busy || assembleLoading" @click="previewAssemble">
              {{ assembleLoading ? '装配中…' : '预览提示词' }}
            </button>
            <button v-if="!isActiveRole" class="btn btn-sm btn-primary" :disabled="busy" @click="makeActive">设为当前角色</button>
            <span v-else class="active-badge">✓ 当前角色</span>
          </div>
        </div>
        <template v-if="assembleData">
          <p class="col-note">注入 {{ assembleData.profile_blocks.length }} 个画像块{{ assembleData.persona ? ' + persona' : '' }}</p>
          <pre class="persona-text tall">{{ assembleData.system_prompt }}</pre>
          <p v-if="assembleData.care_policy" class="note-text">关怀策略：{{ JSON.stringify(assembleData.care_policy) }}</p>
        </template>
        <p v-else class="empty small">点「预览提示词」看这个角色装配后会怎么说话。</p>

        <!-- 演化时间线 -->
        <div class="col-head evo-head">
          <h3>演化时间线<span class="sub">动态层</span></h3>
        </div>
        <div class="evo-tabs">
          <button
            v-for="t in EVOLUTION_TABS" :key="t.kind"
            class="evo-tab" :class="{ on: evoTab === t.kind }"
            @click="evoTab = t.kind"
          >
            {{ t.label }}<span v-if="evolution[t.kind].length" class="count">{{ evolution[t.kind].length }}</span>
          </button>
        </div>
        <p v-if="evoLoading" class="empty small">加载中…</p>
        <p v-else-if="evoError" class="error">{{ evoError }}</p>
        <div v-else-if="evolutionEntries.length" class="evo-list">
          <div v-for="(e, i) in evolutionEntries" :key="e.id ?? i" class="evo-item">
            <div class="evo-ts">{{ fmtTs(e.ts) }}</div>
            <p class="evo-content">{{ e.content }}</p>
            <p v-if="e.trigger_ref" class="evo-trigger">↳ 触发：{{ e.trigger_ref }}</p>
          </div>
        </div>
        <p v-else class="empty small">{{ currentEvolutionTab.empty }}</p>

        <!-- 手动追加（演化层写入通道） -->
        <div class="evo-add">
          <div class="evo-add-row">
            <select v-model="addKind" class="input">
              <option value="timeline">共同经历</option>
              <option value="feelings">心情想法</option>
              <option value="personality_changes">性格演化</option>
              <option value="values_changes">三观演化</option>
            </select>
            <input v-model="addTrigger" placeholder="触发引用（可选）：如 他说了X" />
          </div>
          <textarea v-model="addContent" rows="2" placeholder="追加一条记录，例：一起熬过了发版夜" />
          <button class="btn btn-sm" :disabled="evoBusy || !addContent.trim()" @click="addEvolution">
            {{ evoBusy ? '追加中…' : '追加记录' }}
          </button>
        </div>
      </section>
    </div>

    <!-- 详情未选中 / 加载态 -->
    <div v-else-if="selectedId && detailLoading" class="empty">加载中…</div>
    <div v-else-if="selectedId && detailError" class="error center">{{ detailError }}</div>
    <div v-else-if="!isNew" class="empty">从上方切换角色开始编辑。</div>

    <!-- 角色卡基础信息 + 编辑 + 操作（保留现有能力，收进底部区块） -->
    <template v-if="detail">
      <div class="card base-card">
        <div class="card-head">
          <span class="title">{{ detail.name }}</span>
          <span class="mono role-id-tag">{{ selectedId }}</span>
          <span class="meta">更新 {{ fmtTs(detail.updated_at) }} · 创建 {{ fmtTs(detail.created_at) }}</span>
        </div>
        <p class="desc">{{ detail.description }}</p>
        <!-- D-61 关怀三档：按角色独立设置（默认静默）；改动即保存 -->
        <div class="care-level-row">
          <label>关怀档位</label>
          <select v-model="careLevel" :disabled="busy" @change="saveCareLevel">
            <option v-for="o in CARE_LEVEL_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <span class="care-level-note">当前：{{ careLevelLabel }}</span>
        </div>
        <!-- D-61：管家类「就医/安全」提醒只做首次接入推荐提示，不默认开启 -->
        <p
          v-if="careLevel === 'silent' && detail.extensions?.sgme_care?.trigger_rules?.medical_day"
          class="care-level-hint"
        >
          检测到就医类触发规则：建议对本角色开通「主动推送」（就医/安全类），当前仍为静默——不会自动推送。
        </p>
        <div v-if="detail.extensions?.sgme_care" class="care-line">
          <span class="tag">主动关怀：{{ detail.extensions.sgme_care.greeting_templates?.length || 0 }} 条问候模板</span>
          <span v-for="(d, k) in detail.extensions.sgme_care.trigger_rules" :key="k" class="tag">
            {{ TRIGGER_HINTS[k] || k }}
          </span>
        </div>
        <div class="actions">
          <button class="btn btn-sm" @click="startEdit">{{ editing ? '收起编辑' : '编辑角色卡字段' }}</button>
        </div>

        <div v-if="editing" class="form-grid edit-form">
          <div>
            <label>名称 *</label>
            <input v-model="form.name" />
          </div>
          <div>
            <label>性格（角色卡原生字段）</label>
            <textarea v-model="form.personality" rows="2" />
          </div>
          <div>
            <label>场景</label>
            <textarea v-model="form.scenario" rows="2" />
          </div>
          <div>
            <label>开场白</label>
            <textarea v-model="form.first_mes" rows="2" />
          </div>
          <div>
            <label>示例对话</label>
            <textarea v-model="form.mes_example" rows="2" />
          </div>
          <div>
            <label>系统提示（支持 char/user 宏占位）</label>
            <textarea v-model="form.system_prompt" rows="3" />
          </div>
          <div class="span-3">
            <label>回复后指令</label>
            <textarea v-model="form.post_history_instructions" rows="2" />
          </div>
          <div class="span-3 actions">
            <button class="btn btn-primary btn-sm" :disabled="busy" @click="saveForm">保存</button>
            <button class="btn btn-sm" @click="editing = false; select(selectedId!)">取消</button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ===== 三栏编辑器骨架 ===== */
.editor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--space-4);
  align-items: start;
}
@media (max-width: 1400px) {
  .editor { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .col-right { grid-column: 1 / -1; }
}
@media (max-width: 900px) {
  .editor { grid-template-columns: minmax(0, 1fr); }
}
.col {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  min-width: 0;
}
.col-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.col-head h3 {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.col-head .sub {
  font-weight: 400;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.head-actions { display: flex; gap: var(--space-2); align-items: center; }
.col-note {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.warn-tag {
  font-size: var(--fs-xs);
  color: var(--warn);
  border: 1px solid var(--warn);
  border-radius: 10px;
  padding: 1px 8px;
}
.dirty-tag {
  font-size: var(--fs-xs);
  color: var(--danger);
  border: 1px solid var(--danger);
  border-radius: 10px;
  padding: 1px 8px;
}

/* ===== 字段 ===== */
.field { margin-bottom: var(--space-3); }
.field label {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: 600;
  margin-bottom: 4px;
}
.field .hint {
  font-weight: 400;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.field textarea,
.field input {
  width: 100%;
  box-sizing: border-box;
}
.mid-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: var(--space-2) 0 var(--space-4);
}
.hint-text { font-size: var(--fs-xs); color: var(--text-muted); }

/* ===== persona 块（中栏底部） ===== */
.persona-block {
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.persona-block h4 { margin: 0 0 var(--space-2); font-size: var(--fs-sm); }
.persona-text {
  background: var(--surface-muted);
  border-radius: var(--radius);
  padding: 10px;
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  max-height: 260px;
  overflow: auto;
  margin: var(--space-2) 0 0;
}
.persona-text.tall { max-height: 320px; }
.empty.small { padding: var(--space-4); font-size: var(--fs-sm); }
.active-badge {
  font-size: var(--fs-xs);
  color: var(--success);
  border: 1px solid var(--success);
  border-radius: 10px;
  padding: 1px 8px;
  white-space: nowrap;
}

/* ===== 演化时间线 ===== */
.evo-head { margin-top: var(--space-4); }
.evo-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: var(--space-2);
}
.evo-tab {
  padding: 3px 10px;
  font-size: var(--fs-xs);
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  color: var(--text-muted);
}
.evo-tab.on {
  background: var(--brand-soft);
  border-color: var(--brand);
  color: var(--brand-text);
}
.evo-tab .count { margin-left: 3px; opacity: 0.7; }
.evo-list { max-height: 300px; overflow: auto; }
.evo-item {
  border-left: 2px solid var(--border-strong);
  padding: 2px 0 8px 10px;
  margin-bottom: 4px;
}
.evo-ts { font-size: var(--fs-xs); color: var(--text-muted); }
.evo-content { margin: 2px 0; font-size: var(--fs-sm); white-space: pre-wrap; }
.evo-trigger { margin: 0; font-size: var(--fs-xs); color: var(--brand-text); }
.evo-add {
  margin-top: var(--space-3);
  border-top: 1px dashed var(--border);
  padding-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.evo-add-row { display: flex; gap: 6px; }
.evo-add-row select { flex: 0 0 auto; }
.evo-add-row input { flex: 1; min-width: 0; }
.evo-add textarea { width: 100%; box-sizing: border-box; }
.evo-add .btn { align-self: flex-end; }

/* ===== 顶部新建 / 底部角色卡 ===== */
.new-form { margin-bottom: var(--space-4); }
.form-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
}
.form-grid label {
  display: block;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-bottom: 4px;
}
.form-grid input,
.form-grid textarea {
  width: 100%;
  box-sizing: border-box;
}
.span-3 { grid-column: 1 / -1; }
.actions { display: flex; gap: var(--space-2); margin-top: var(--space-2); }
.base-card { margin-top: var(--space-4); }
.base-card .title { font-weight: 600; }
.role-id-tag { opacity: 0.6; font-size: var(--fs-xs); }
.base-card .meta { font-size: var(--fs-xs); color: var(--text-muted); margin-left: auto; }
.desc { font-size: var(--fs-sm); color: var(--text); margin: var(--space-2) 0; white-space: pre-wrap; }
.care-line { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: var(--space-2); }
.care-level-row { display: flex; align-items: center; gap: 8px; margin: var(--space-2) 0; font-size: var(--fs-sm); }
.care-level-row select { padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); }
.care-level-note { color: var(--text-muted); font-size: var(--fs-xs); }
.care-level-hint { margin: 0 0 var(--space-2); font-size: var(--fs-xs); color: var(--warning, #d48806); }
.edit-form { margin-top: var(--space-3); }
.center { text-align: center; padding: var(--space-6); }
</style>
