<script setup lang="ts">
// TTL 处置看板：D-52 五档处置（valid_expired/renewed/anchor/sleeping/archived）按天审计
// 数据源 GET /v1/admin/ops/ttl-disposition?days=N（后端未就绪时显示空态，不白屏）
import { computed, onMounted, ref } from 'vue'
import {
  getTtlDisposition,
  type TtlDispositionItem,
  type TtlDispositionTotal,
} from '../../api/ops'
import { ApiError } from '../../api/client'

const days = ref<7 | 30 | 90>(30)
const items = ref<TtlDispositionItem[]>([])
const total = ref<TtlDispositionTotal | null>(null)
const loading = ref(false)
const error = ref('')

const DAY_OPTIONS: Array<7 | 30 | 90> = [7, 30, 90]

// 顶部四张汇总卡：本月（查询窗口内）续期 / 降档 / 休眠 / 归档
const summaryCards = computed(() => {
  const t = total.value
  return [
    { key: 'renewed', label: '续期', value: t?.renewed ?? 0, cls: 'dimtag-green' },
    { key: 'valid_expired', label: '降档', value: t?.valid_expired ?? 0, cls: 'dimtag-yellow' },
    { key: 'sleeping', label: '休眠', value: t?.sleeping ?? 0, cls: 'dimtag-blue' },
    { key: 'archived', label: '归档', value: t?.archived ?? 0, cls: 'dimtag-red' },
  ]
})

// 表格按天倒序；行合计列
const rows = computed(() =>
  [...items.value].sort((a, b) => (a.day < b.day ? 1 : -1)).map((it) => ({
    ...it,
    rowTotal: it.valid_expired + it.renewed + it.anchor + it.sleeping + it.archived,
  })),
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await getTtlDisposition(days.value)
    items.value = r.items || []
    total.value = r.total || null
  } catch (e) {
    // 端点 404 / 服务不可达等：清空数据并显示友好提示
    items.value = []
    total.value = null
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function switchDays(d: 7 | 30 | 90) {
  if (days.value === d) return
  days.value = d
  load()
}

onMounted(load)
</script>

<template>
  <div class="ttl-view">
    <div class="head">
      <h2>TTL 处置 <span class="sub">D-52 五档处置审计（按天）</span></h2>
      <div class="days-switch">
        <button
          v-for="d in DAY_OPTIONS"
          :key="d"
          :class="{ active: days === d }"
          :disabled="loading"
          @click="switchDays(d)"
        >
          近 {{ d }} 天
        </button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}（接口可能尚未上线，稍后重试或联系管理员）</p>

    <!-- 顶部汇总卡 -->
    <section class="panel">
      <h3>处置汇总 <span class="sub">近 {{ days }} 天累计</span></h3>
      <div class="summary-grid">
        <div v-for="c in summaryCards" :key="c.key" class="stat-card">
          <span class="v" :class="c.cls">{{ c.value }}</span>
          <span class="l">{{ c.label }}</span>
        </div>
      </div>
      <p v-if="total" class="desc">
        锚点（判等锚定，不参与 TTL 退出）{{ total.anchor }} 条；窗口内共处置
        {{ total.renewed + total.valid_expired + total.sleeping + total.archived }} 条（不含锚点）。
      </p>
    </section>

    <!-- 按天明细表 -->
    <section class="panel">
      <h3>按天明细 <span class="sub">共 {{ rows.length }} 天</span></h3>
      <p v-if="loading" class="empty">加载中…</p>
      <div v-else-if="!rows.length" class="empty">
        {{ error ? '暂无可显示的处置数据' : '暂无 TTL 处置记录——窗口内没有记忆触发续期/降档/休眠/归档' }}
      </div>
      <table v-else class="tbl">
        <thead>
          <tr>
            <th>日期</th>
            <th>续期</th>
            <th>降档</th>
            <th>锚点</th>
            <th>休眠</th>
            <th>归档</th>
            <th>合计</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.day">
            <td class="mono">{{ r.day }}</td>
            <td>{{ r.renewed }}</td>
            <td>{{ r.valid_expired }}</td>
            <td>{{ r.anchor }}</td>
            <td>{{ r.sleeping }}</td>
            <td>{{ r.archived }}</td>
            <td>{{ r.rowTotal }}</td>
          </tr>
        </tbody>
      </table>
      <p class="desc">
        五档语义：续期 = update/merge 续 TTL；降档 = 过期退出注入；锚点 = 判等锚定不退出；休眠 = 长期未用转休眠；归档 = 归档保留可溯源。
      </p>
    </section>
  </div>
</template>

<style scoped>
.days-switch {
  display: inline-flex;
  gap: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.days-switch button {
  padding: 6px 14px;
  border: none;
  background: var(--surface);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  cursor: pointer;
}
.days-switch button + button {
  border-left: 1px solid var(--border);
}
.days-switch button.active {
  background: var(--brand);
  color: #fff;
}
.days-switch button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-3);
}
@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
/* 汇总数字用胶囊色着色（stat-card .v 默认继承 --text，这里覆盖为档位色） */
.summary-grid .stat-card .v {
  font-size: 26px;
}
</style>
