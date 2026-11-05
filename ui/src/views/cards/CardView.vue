<script setup lang="ts">
// core 记忆卡编辑页（D-79，2026-11-05）
//
// 卡片 = 关于用户本人的稳定事实，每次注入必带。这里是它的唯一人工编辑入口。
// 两种填写方式并存：①本页直接填（changed_by=webui）②对话里让 AI 改（changed_by=agent）。
// 每次修改进 memory_card_history，下方可查。
import { computed, onMounted, ref } from 'vue'
import { ApiError } from '../../api/client'
import {
  cardHistory,
  cardHistoryOf,
  cardSchema,
  listCards,
  updateSlot,
  type CardHistoryItem,
  type CardItem,
  type CardSpec,
} from '../../api/cards'
import { fmtTs } from '../../utils/format'

const specs = ref<CardSpec[]>([])
const cards = ref<CardItem[]>([])
const limits = ref({ card_max_chars: 400, slot_max_chars: 120 })
const drafts = ref<Record<string, string>>({})
const history = ref<CardHistoryItem[]>([])
const loading = ref(false)
const saving = ref<string | null>(null)
const error = ref('')
const msg = ref('')

// 单卡修改历史（GET /v1/admin/cards/{card_id}/history?limit=50）——点某张卡的「历史」打开
const histOpen = ref(false)
const histCardId = ref('')
const histCardTitle = ref('')
const histItems = ref<CardHistoryItem[]>([])
const histLoading = ref(false)
const histError = ref('')

function key(cardId: string, slot: string): string {
  return cardId + '.' + slot
}

function isMulti(cardId: string, slot: string): boolean {
  const spec = specs.value.find((s) => s.card_id === cardId)
  return !!spec?.multi?.includes(slot)
}

function toText(v: string | string[] | undefined): string {
  if (Array.isArray(v)) return v.join('\n')
  return v ?? ''
}

function toValue(raw: string, multi: boolean): string | string[] | null {
  const t = raw.trim()
  if (!t) return null
  if (multi) return t.split('\n').map((x) => x.trim()).filter(Boolean)
  return t
}

function cardOf(cardId: string): CardItem | undefined {
  return cards.value.find((c) => c.card_id === cardId)
}

function dirtySlots(spec: CardSpec): string[] {
  const card = cardOf(spec.card_id)
  return spec.slots.filter((slot) => {
    const k = key(spec.card_id, slot)
    const before = toText(card?.slots?.[slot])
    return (drafts.value[k] ?? '').trim() !== before.trim()
  })
}

function cardChars(spec: CardSpec): number {
  let n = 0
  for (const slot of spec.slots) {
    n += (drafts.value[key(spec.card_id, slot)] ?? '').length
  }
  return n
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [sch, list] = await Promise.all([cardSchema(), listCards()])
    specs.value = sch.cards
    limits.value = sch.limits
    cards.value = list.cards
    const d: Record<string, string> = {}
    for (const spec of sch.cards) {
      const card = list.cards.find((c) => c.card_id === spec.card_id)
      for (const slot of spec.slots) {
        d[key(spec.card_id, slot)] = toText(card?.slots?.[slot])
      }
    }
    drafts.value = d
    const h = await cardHistory(20)
    history.value = h.items
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// 有未保存改动时，刷新前确认——避免误点把草稿冲掉
function hasUnsaved(): boolean {
  return specs.value.some((s) => dirtySlots(s).length > 0)
}

async function refresh() {
  if (hasUnsaved() && !window.confirm('有未保存的改动，刷新会丢弃，继续？')) return
  await load()
}

async function saveCard(spec: CardSpec) {
  const changed = dirtySlots(spec)
  if (!changed.length) {
    msg.value = '「' + spec.title + '」没有改动'
    return
  }
  saving.value = spec.card_id
  error.value = ''
  try {
    for (const slot of changed) {
      const raw = drafts.value[key(spec.card_id, slot)] ?? ''
      await updateSlot(spec.card_id, slot, toValue(raw, isMulti(spec.card_id, slot)))
    }
    // 关键：只同步本卡的内存快照，**不调 load()**——
    // load() 会重建全部 drafts，把其它卡正在编辑的内容冲掉（用户 2026-11-05 反馈）。
    const card = cardOf(spec.card_id)
    if (card) {
      for (const slot of changed) {
        const raw = drafts.value[key(spec.card_id, slot)] ?? ''
        const v = toValue(raw, isMulti(spec.card_id, slot))
        if (v === null) delete card.slots[slot]
        else card.slots[slot] = v
      }
      card.filled = Object.keys(card.slots).length > 0
      card.updated_by = 'webui'
    }
    msg.value = '「' + spec.title + '」已保存 ' + changed.length + ' 个槽位'
    // 历史单独拉（只写 history，不碰 drafts）
    try {
      const h = await cardHistory(20)
      history.value = h.items
    } catch { /* 历史拉取失败不影响保存结果 */ }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    saving.value = null
  }
}

// ---------- 单卡修改历史（含理由列；全卡历史见页尾「修改记录」，不带理由） ----------

async function loadCardHistory() {
  if (!histCardId.value) return
  histLoading.value = true
  histError.value = ''
  try {
    const r = await cardHistoryOf(histCardId.value, 50)
    histItems.value = r.items || []
  } catch (e) {
    histItems.value = []
    histError.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    histLoading.value = false
  }
}

function openHistory(spec: CardSpec) {
  histCardId.value = spec.card_id
  histCardTitle.value = spec.title
  histItems.value = []
  histOpen.value = true
  void loadCardHistory()
}

function closeHistory() {
  histOpen.value = false
  histCardId.value = ''
  histCardTitle.value = ''
  histItems.value = []
  histError.value = ''
}

const filledCount = computed(() => cards.value.filter((c) => c.filled).length)

onMounted(() => { void load() })
</script>

<template>
  <div class="cards-page">
    <div class="page-head">
      <div>
        <h2>关于你</h2>
        <p class="sub">
          这些是每次对话都会带上的信息——我是谁、家里有什么人、什么作息、在意什么。
          填得越准，我越像个知道你情况的人；空着也不会猜。
        </p>
        <p class="sub hint-ai">
          两种填法都行：这边直接改，或者直接在对话里告诉我「我一般 23 点睡」，我替你写。
          每次改动都留痕，下方可查。
        </p>
      </div>
      <div class="head-right">
        <span class="badge ok" v-if="filledCount">已填 {{ filledCount }} / {{ specs.length }}</span>
        <span class="badge warn" v-else>尚未填写</span>
        <button class="btn btn-sm" :disabled="loading" @click="refresh">刷新</button>
      </div>
    </div>

    <div v-if="error" class="alert err">{{ error }}</div>
    <div v-if="msg" class="alert ok">{{ msg }}</div>
    <div v-if="loading" class="empty">加载中…</div>

    <div v-else class="grid">
      <section v-for="spec in specs" :key="spec.card_id" class="panel card-panel">
        <div class="card-head">
          <span class="title">{{ spec.title }}</span>
          <span class="card-id">{{ spec.card_id }}</span>
          <span class="chars" :class="{ over: cardChars(spec) > limits.card_max_chars }">
            {{ cardChars(spec) }} / {{ limits.card_max_chars }}
          </span>
          <button class="btn btn-sm" @click="openHistory(spec)">历史</button>
          <button
            class="btn btn-sm btn-primary"
            :disabled="saving === spec.card_id || !dirtySlots(spec).length"
            @click="saveCard(spec)"
          >
            {{ saving === spec.card_id ? '保存中…' : '保存' }}
          </button>
        </div>
        <div class="slots">
          <label v-for="slot in spec.slots" :key="slot" class="slot">
            <span class="slot-name">{{ slot }}</span>
            <textarea
              v-if="isMulti(spec.card_id, slot)"
              v-model="drafts[key(spec.card_id, slot)]"
              :maxlength="limits.slot_max_chars * 4"
              rows="3"
              placeholder="一行一条，例如：儿子 15 岁 高一"
            />
            <input
              v-else
              v-model="drafts[key(spec.card_id, slot)]"
              :maxlength="limits.slot_max_chars"
              :placeholder="'未填写'"
            />
            <span class="slot-hint" v-if="spec.multi?.includes(slot)">多值槽（换行分隔）</span>
          </label>
        </div>
      </section>
    </div>

    <section class="panel history-panel">
      <h3>修改记录 <span class="sub">最近 20 条</span></h3>
      <div v-if="!history.length" class="empty">暂无记录</div>
      <table v-else class="hist">
        <thead>
          <tr><th>时间</th><th>卡片.槽位</th><th>变更</th><th>来源</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in history" :key="h.id">
            <td class="mono">{{ h.changed_at.slice(5, 16).replace('T', ' ') }}</td>
            <td class="mono">{{ h.card_id }}.{{ h.slot_key }}</td>
            <td class="diff">
              <span class="old" v-if="h.old_value">{{ h.old_value }}</span>
              <span class="arrow" v-if="h.old_value">→</span>
              <span class="new">{{ h.new_value || '（清除）' }}</span>
            </td>
            <td>{{ h.changed_by || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 单卡修改历史抽屉：GET /v1/admin/cards/{card_id}/history?limit=50（最新在前） -->
    <div v-if="histOpen" class="overlay" @click.self="closeHistory">
      <div class="drawer">
        <div class="drawer-head">
          <span class="drawer-title">修改历史 · {{ histCardTitle }}</span>
          <button class="close" @click="closeHistory">✕</button>
        </div>
        <div class="drawer-body hist-body">
          <p class="hist-sub">
            卡片 <code class="mono">{{ histCardId }}</code> · 单卡最近 50 条，最新在前
          </p>
          <p v-if="histError" class="alert err">{{ histError }}</p>
          <p v-else-if="histLoading" class="empty">加载中…</p>
          <p v-else-if="!histItems.length" class="empty">这张卡还没有修改记录</p>
          <div v-else class="hist-wrap">
            <table class="tbl hist-tbl">
              <thead>
                <tr><th>时间</th><th>槽位</th><th>变更</th><th>改的人</th><th>理由</th></tr>
              </thead>
              <tbody>
                <tr v-for="h in histItems" :key="h.id">
                  <td class="mono">{{ fmtTs(h.changed_at) }}</td>
                  <td class="mono">{{ h.slot_key }}</td>
                  <td class="diff">
                    <span class="old" v-if="h.old_value">{{ h.old_value }}</span>
                    <span class="arrow" v-if="h.old_value">→</span>
                    <span class="new">{{ h.new_value || '（清除）' }}</span>
                  </td>
                  <td>{{ h.changed_by || '—' }}</td>
                  <td class="reason">{{ h.reason || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div class="drawer-foot">
          <button class="btn" :disabled="histLoading" @click="loadCardHistory">刷新</button>
          <button class="btn" @click="closeHistory">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 全部走设计令牌（深浅主题自动跟随） */
.cards-page { padding: var(--space-5) var(--space-6) 60px; max-width: var(--content-max); }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-4); margin-bottom: var(--space-4); }
.page-head h2 { margin: 0 0 var(--space-1); font-size: var(--fs-xl); color: var(--text); }
.sub { margin: 0 0 var(--space-1); color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.7; max-width: 720px; }
.hint-ai { color: var(--text-faint); }
.head-right { display: flex; align-items: center; gap: var(--space-2); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: var(--space-4); }
.card-panel { padding: var(--space-3) var(--space-4); }
.card-head { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-3); }
.card-head .title { font-size: var(--fs-lg); font-weight: 600; color: var(--text); }
.card-id { font-size: var(--fs-xs); color: var(--text-faint); font-family: var(--font-mono); }
.chars { margin-left: auto; font-size: var(--fs-xs); color: var(--text-faint); font-variant-numeric: tabular-nums; }
.chars.over { color: var(--danger); font-weight: 600; }

.slots { display: flex; flex-direction: column; gap: var(--space-2); }
.slot { display: flex; flex-direction: column; gap: 4px; }
.slot-name { font-size: var(--fs-xs); color: var(--text-muted); }
.slot input, .slot textarea {
  width: 100%; box-sizing: border-box; padding: 6px 10px; font-size: var(--fs-sm);
  color: var(--text); background: var(--surface-muted);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  font-family: inherit; resize: vertical;
}
.slot input:focus, .slot textarea:focus { outline: none; border-color: var(--brand); background: var(--surface); }
.slot-hint { font-size: 11px; color: var(--text-faint); }

.history-panel { margin-top: var(--space-5); padding: var(--space-3) var(--space-4); }
.history-panel h3 { margin: 0 0 var(--space-3); font-size: var(--fs-lg); }
.hist { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.hist th { text-align: left; font-size: var(--fs-xs); color: var(--text-faint); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--border); }
.hist td { padding: 6px 8px; border-bottom: 1px solid var(--divider); color: var(--text); vertical-align: top; }
.mono { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); white-space: nowrap; }
.diff { max-width: 520px; word-break: break-word; }
.old { color: var(--text-faint); text-decoration: line-through; }
.arrow { color: var(--text-faint); margin: 0 6px; }
.new { color: var(--text); }

/* 单卡历史抽屉 */
.hist-body { padding: var(--space-4) var(--space-5); }
.hist-sub { margin: 0 0 var(--space-3); font-size: var(--fs-xs); color: var(--text-muted); }
.hist-sub code { background: var(--surface-muted); padding: 1px 6px; border-radius: var(--radius-sm); }
.hist-wrap { overflow-x: auto; }
.hist-tbl { min-width: 620px; }
.hist-tbl .reason { color: var(--text-muted); font-size: var(--fs-xs); max-width: 220px; word-break: break-word; }

.alert { padding: 8px 12px; border-radius: var(--radius-sm); font-size: var(--fs-sm); margin-bottom: var(--space-3); }
.alert.err { background: var(--danger-soft); color: var(--danger); }
.alert.ok { background: var(--success-soft); color: var(--success); }
</style>
