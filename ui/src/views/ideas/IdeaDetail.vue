<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { appendNote, getIdea, promoteIdea, setFlag, updateIdea, type Idea } from '../../api/ideas'
import { ApiError } from '../../api/client'

const route = useRoute()
const ideaId = computed(() => route.params.id as string)

const idea = ref<Idea | null>(null)
const loading = ref(false)
const error = ref('')

// 编辑
const editContent = ref('')
const editPriority = ref(50)
const editing = ref(false)
// 备注
const noteText = ref('')
// 升格
const promoteTitle = ref('')
const promoteContent = ref('')
const promoting = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getIdea(ideaId.value)
    idea.value = data.idea
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(ideaId, () => load(), { immediate: true })

function openEdit() {
  if (!idea.value) return
  editing.value = true
  editContent.value = idea.value.content
  editPriority.value = idea.value.priority
}

async function saveEdit() {
  if (!idea.value) return
  try {
    await updateIdea(idea.value.idea_id, { content: editContent.value, priority: editPriority.value })
    editing.value = false
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function saveNote() {
  if (!idea.value || !noteText.value.trim()) return
  try {
    await appendNote(idea.value.idea_id, noteText.value.trim())
    noteText.value = ''
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

async function togglePromoted() {
  if (!idea.value) return
  const next = idea.value.custom_flag === 'promoted' ? null : 'promoted'
  try {
    await setFlag(idea.value.idea_id, next)
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}

function openPromote() {
  if (!idea.value) return
  promoting.value = true
  promoteTitle.value = idea.value.content.slice(0, 30)
  promoteContent.value = idea.value.content
}

async function doPromote() {
  if (!idea.value || !promoteTitle.value.trim()) return
  try {
    await promoteIdea(idea.value.idea_id, { title: promoteTitle.value.trim(), content: promoteContent.value })
    promoting.value = false
    load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  }
}
</script>

<template>
  <div class="detail">
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="loading" class="empty">加载中…</p>

    <div v-else-if="idea" class="card">
      <div class="head">
        <h2>创意详情</h2>
        <div class="badges">
          <span class="prio">P{{ idea.priority }}</span>
          <span v-if="idea.custom_flag" class="flag">{{ idea.custom_flag }}</span>
          <span v-if="idea.status !== 'active'" class="status">{{ idea.status }}</span>
        </div>
      </div>

      <p class="content">{{ idea.content }}</p>

      <div class="meta">
        <span>创建 {{ new Date(idea.created_at).toLocaleString() }}</span>
        <span>更新 {{ new Date(idea.updated_at).toLocaleString() }}</span>
        <span v-if="idea.source_ref">来源 {{ idea.source_ref }}</span>
      </div>

      <div class="actions">
        <button @click="openEdit">编辑</button>
        <button @click="togglePromoted">{{ idea.custom_flag === 'promoted' ? '取消升格' : '升格标记' }}</button>
        <button class="primary" @click="openPromote">升格为需求</button>
      </div>
    </div>

    <!-- 备注区 -->
    <div v-if="idea" class="panel">
      <h3>备注（{{ idea.notes.length }}）</h3>
      <div class="note-list">
        <div v-for="(n, i) in idea.notes" :key="i" class="note-item">
          <span class="note-ts">{{ new Date(n.ts).toLocaleString() }}</span>
          <p>{{ n.text }}</p>
        </div>
        <p v-if="!idea.notes.length" class="empty">暂无备注</p>
      </div>
      <div class="note-add">
        <input v-model="noteText" placeholder="追加备注…" @keyup.enter="saveNote" />
        <button :disabled="!noteText.trim()" @click="saveNote">追加</button>
      </div>
    </div>

    <!-- 编辑弹层 -->
    <div v-if="editing" class="modal">
      <div class="modal-box">
        <h3>编辑创意</h3>
        <textarea v-model="editContent" rows="4"></textarea>
        <label>优先级 <input v-model.number="editPriority" type="number" min="0" max="100" /></label>
        <div class="modal-actions">
          <button @click="editing = false">取消</button>
          <button class="primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>

    <!-- 升格弹层 -->
    <div v-if="promoting" class="modal">
      <div class="modal-box">
        <h3>升格为需求</h3>
        <label>需求标题 *</label>
        <input v-model="promoteTitle" />
        <label>需求描述</label>
        <textarea v-model="promoteContent" rows="3"></textarea>
        <div class="modal-actions">
          <button @click="promoting = false">取消</button>
          <button class="primary" :disabled="!promoteTitle.trim()" @click="doPromote">升格并创建需求</button>
        </div>
      </div>
    </div>
  </div>
</template>