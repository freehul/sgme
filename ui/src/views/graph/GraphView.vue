<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as d3 from 'd3'
import { fetchGraph, type GraphData, type GraphLink, type GraphNode } from '../../api/graph'
import { ApiError } from '../../api/client'

const router = useRouter()

const data = ref<GraphData | null>(null)
const loading = ref(false)
const error = ref('')
const sceneLimit = ref(200)
const wikiLimit = ref(200)

// 选中节点详情（右侧面板）
const selected = ref<GraphNode | null>(null)
const selectedLinks = ref<{ target: string; rel_type?: string }[]>([])

const svgRef = ref<SVGSVGElement | null>(null)

// 节点类型样式
const TYPE_META: Record<string, { label: string; color: string; ico: string }> = {
  scene: { label: '场景', color: '#e07b39', ico: '🗂' },
  memory: { label: '记忆', color: '#3a7bd5', ico: '📖' },
  wiki: { label: 'Wiki', color: '#2e9e6b', ico: '📚' },
}

const typeColor = (t: string) => TYPE_META[t]?.color || '#888'
const typeLabel = (t: string) => TYPE_META[t]?.label || t
const typeIco = (t: string) => TYPE_META[t]?.ico || '•'

const stats = computed(() => data.value?.stats)

let sim: d3.Simulation<d3.SimulationNodeDatum, undefined> | null = null
let resizeObserver: ResizeObserver | null = null

async function load() {
  loading.value = true
  error.value = ''
  try {
    const g = await fetchGraph({ scene_limit: sceneLimit.value, wiki_limit: wikiLimit.value })
    data.value = g
    selected.value = null
    selectedLinks.value = []
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    // 先落定 loading=false，让模板把 <svg v-else> 渲染进 DOM（否则 svgRef 为 null）
    loading.value = false
  }
  // 数据/loading 已落定，DOM 已切到 svg，nextTick 等 DOM 刷新后再绘制
  await nextTick()
  renderGraph()
}

// ── D3 force 布局 ──
function renderGraph() {
  const svgEl = svgRef.value
  if (!svgEl || !data.value) return
  const g = data.value

  try {
    // 清理旧图
    d3.select(svgEl).selectAll('*').remove()
    if (sim) { sim.stop(); sim = null }

    // 尺寸：优先容器实测框（flex 布局未定时 svg.clientWidth 可能为 0），失败回退默认
    const host = svgEl.parentElement
    const rect = host?.getBoundingClientRect()
    const width = Math.max(Math.round(rect?.width || svgEl.clientWidth || 0), 320)
    const height = Math.max(Math.round(rect?.height || svgEl.clientHeight || 0), 480)

  const nodes = g.nodes.map((n) => ({ ...n }))
  const links = g.links.map((l) => ({ ...l }))

  // 节点 id → 索引（forceLink 需要）
  const idIndex = new Map(nodes.map((n, i) => [n.id, i]))

  const svg = d3.select(svgEl)
    .attr('viewBox', `0 0 ${width} ${height}`)
    .attr('width', width)
    .attr('height', height)

  // 缩放容器
  const zoomLayer = svg.append('g')
  svg.call(d3.zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.2, 4])
    .on('zoom', (ev) => zoomLayer.attr('transform', ev.transform.toString()))
  )

  // 边（先画，节点之上）
  const linkSel = zoomLayer.append('g')
    .attr('class', 'links')
    .selectAll('line')
    .data(links)
    .join('line')
    .attr('stroke', '#999')
    .attr('stroke-opacity', 0.5)
    .attr('stroke-width', 1)

  // 节点
  const nodeSel = zoomLayer.append('g')
    .attr('class', 'nodes')
    .selectAll('g')
    .data(nodes)
    .join('g')
    .attr('class', 'node')
    .style('cursor', 'pointer')
    .call(drag(sim)) // 先占位，sim 下面赋值

  // 节点视觉：圆 + 文字
  nodeSel.append('circle')
    .attr('r', (n: GraphNode & d3.SimulationNodeDatum) => {
      if (n.type === 'scene') return 12
      if (n.type === 'wiki') return 9
      return 7
    })
    .attr('fill', (n: GraphNode) => typeColor(n.type))
    .attr('stroke', '#fff')
    .attr('stroke-width', 1.5)

  nodeSel.append('text')
    .text((n: GraphNode) => n.label.slice(0, 10))
    .attr('dy', (n: GraphNode) => (n.type === 'scene' ? -16 : -11))
    .attr('text-anchor', 'middle')
    .attr('font-size', 10)
    .attr('fill', 'var(--text)')

  // 点击：记忆 → 详情，场景/wiki → 右侧详情面板
  nodeSel.on('click', (_ev, n: GraphNode) => {
    if (n.type === 'memory') {
      router.push(`/memories/${n.id}`)
      return
    }
    selected.value = n
    selectedLinks.value = g.links
      .filter((l) => l.source === n.id || l.target === n.id)
      .map((l) => ({ target: l.source === n.id ? l.target : l.source, rel_type: l.rel_type }))
  })

  // 节点标签 tooltip
  nodeSel.append('title').text((n: GraphNode) => n.label)

  // force 布局
  sim = d3.forceSimulation(nodes as d3.SimulationNodeDatum[])
    .force('link', d3.forceLink(links as d3.SimulationLinkDatum<d3.SimulationNodeDatum>[])
      .id((d: any) => d.id)
      .distance((l: any) => (l.type === 'scene_memory' ? 60 : 90))
      .strength(0.5))
    .force('charge', d3.forceManyBody().strength(-180))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide(18))
    .on('tick', () => {
      linkSel
        .attr('x1', (l: any) => l.source.x)
        .attr('y1', (l: any) => l.source.y)
        .attr('x2', (l: any) => l.target.x)
        .attr('y2', (l: any) => l.target.y)
      nodeSel.attr('transform', (d: any) => `translate(${d.x},${d.y})`)
    })

  // drag 需要 sim，重新绑定
  nodeSel.call(drag(sim))
  } catch (e) {
    // 渲染异常显式暴露（避免静默空白），不阻塞页面其余部分
    error.value = '图谱渲染失败：' + (e instanceof Error ? e.message : String(e))
  }
}

function drag(simulation: d3.Simulation<d3.SimulationNodeDatum, undefined> | null) {
  function dragstarted(event: d3.D3DragEvent<SVGGElement, GraphNode, GraphNode>, d: any) {
    if (!event.active && simulation) simulation.alphaTarget(0.3).restart()
    d.fx = d.x
    d.fy = d.y
  }
  function dragged(event: d3.D3DragEvent<SVGGElement, GraphNode, GraphNode>, d: any) {
    d.fx = event.x
    d.fy = event.y
  }
  function dragended(event: d3.D3DragEvent<SVGGElement, GraphNode, GraphNode>, d: any) {
    if (!event.active && simulation) simulation.alphaTarget(0)
    d.fx = null
    d.fy = null
  }
  return d3.drag<SVGGElement, GraphNode>()
    .on('start', dragstarted)
    .on('drag', dragged)
    .on('end', dragended)
}

// 关联节点名解析
function targetName(id: string): string {
  return data.value?.nodes.find((n) => n.id === id)?.label || id
}

onMounted(async () => {
  await load()
  // 容器尺寸变化时重绘
  const container = svgRef.value?.parentElement
  if (container && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => renderGraph())
    resizeObserver.observe(container)
  }
})

onBeforeUnmount(() => {
  if (sim) sim.stop()
  resizeObserver?.disconnect()
})
</script>

<template>
  <div class="graph-view">
    <div class="head">
      <h2>知识图谱</h2>
      <div class="filters">
        <label class="limit-label">场景上限
          <input v-model.number="sceneLimit" type="number" min="10" max="1000" class="num-input" />
        </label>
        <label class="limit-label">Wiki 上限
          <input v-model.number="wikiLimit" type="number" min="10" max="1000" class="num-input" />
        </label>
        <button class="btn btn-primary btn-sm" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="stats" class="stat-row">
      <span class="stat-chip">🗂 场景 {{ stats.scenes }}</span>
      <span class="stat-chip">📖 记忆 {{ stats.memories }}</span>
      <span class="stat-chip">📚 Wiki {{ stats.wiki }}</span>
      <span class="stat-chip">🔗 场景-记忆 {{ stats.scene_links }}</span>
      <span class="stat-chip">🔗 Wiki 关联 {{ stats.wiki_links }}</span>
    </div>

    <div class="graph-split">
      <!-- 左：图谱 -->
      <div class="graph-pane">
        <p v-if="loading" class="empty">加载中…</p>
        <p v-else-if="!data?.nodes.length" class="empty">暂无图谱数据（场景/Wiki 关联）。</p>
        <svg v-else ref="svgRef" class="graph-svg"></svg>
        <div class="legend">
          <span v-for="(meta, t) in TYPE_META" :key="t" class="legend-item">
            <span class="legend-dot" :style="{ background: meta.color }"></span>
            {{ meta.ico }} {{ meta.label }}
          </span>
          <span class="legend-item legend-hint">点击记忆节点跳详情，场景/Wiki 节点看关联</span>
        </div>
      </div>

      <!-- 右：详情 -->
      <div class="detail-pane">
        <div v-if="!selected" class="empty">点击场景 / Wiki 节点查看关联</div>
        <div v-else class="detail-card">
          <h3>{{ typeIco(selected.type) }} {{ selected.label }}</h3>
          <div class="meta">
            <span class="type-badge" :style="{ background: typeColor(selected.type) }">{{ typeLabel(selected.type) }}</span>
            <template v-if="selected.type === 'scene'">
              <span>🔥 热度 {{ selected.heat }}</span>
              <span>{{ selected.memories_count }} 记忆</span>
            </template>
            <template v-else-if="selected.type === 'wiki'">
              <span class="dim-tag">{{ selected.category || '未分类' }}</span>
            </template>
          </div>
          <p v-if="selected.content" class="content">{{ selected.content }}</p>
          <div v-if="selected.dimensions?.length" class="dims">
            <span v-for="d in selected.dimensions" :key="d" class="dim-tag">{{ d }}</span>
          </div>
          <section v-if="selectedLinks.length" class="links-block">
            <h4>关联（{{ selectedLinks.length }}）</h4>
            <ul class="rel-list">
              <li v-for="(l, i) in selectedLinks" :key="i" class="rel-item">
                <span class="rel-type">{{ l.rel_type || 'contains' }}</span>
                <RouterLink :to="l.target.startsWith('page-') ? '/wiki' : '/memories/' + l.target">
                  {{ targetName(l.target) }}
                </RouterLink>
              </li>
            </ul>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.graph-split {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.graph-pane {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow: hidden;
}
.graph-svg {
  display: block;
  width: 100%;
  height: 620px;
  background: var(--surface);
}
.detail-pane {
  width: 320px;
  min-width: 280px;
  position: sticky;
  top: 0;
}
.detail-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.detail-card h3 {
  margin: 0 0 8px;
  font-size: var(--fs-lg);
  font-weight: 600;
  word-break: break-all;
}
.detail-card h4 {
  margin: 0 0 8px;
  font-size: var(--fs-md);
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: var(--fs-sm);
  color: var(--text-muted);
  margin-bottom: 8px;
}
.type-badge {
  color: #fff;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: var(--fs-sm);
}
.content {
  margin: 8px 0;
  color: var(--text);
}
.dims {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.dim-tag {
  background: var(--brand-soft);
  color: var(--brand);
  padding: 1px 8px;
  border-radius: 10px;
  font-size: var(--fs-sm);
}
.links-block {
  margin-top: 10px;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.rel-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}
.rel-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
  font-size: var(--fs-sm);
  border-bottom: 1px dashed var(--border);
}
.rel-type {
  color: var(--text-faint);
  font-size: 11px;
  white-space: nowrap;
}
.stat-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.stat-chip {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 2px 10px;
  font-size: var(--fs-sm);
  background: var(--surface);
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.legend-hint {
  margin-left: auto;
}
.filters {
  display: flex;
  gap: 12px;
  align-items: center;
}
.limit-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.num-input {
  width: 70px;
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
}
</style>
