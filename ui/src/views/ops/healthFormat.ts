// 运维健康总览展示辅助（纯函数，便于独立校验）
// 数据契约：GET /v1/admin/ops/health-overview（storage 段为必需；scenes 段已退役）
import type { OpsHealthOverview } from '../../api/ops'

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

// 字节数 → 人类可读单位（1024 进制）：0 → '0 B'；<1KB 取整；≥1KB 保留 1 位小数（≥100 时取整）
export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const text = value >= 100 ? value.toFixed(0) : value.toFixed(1)
  return `${text.replace(/\.0$/, '')} ${BYTE_UNITS[unit]}`
}

// 计数 → 千分位（null/NaN → '—'）
export function formatCount(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—'
  return n.toLocaleString('zh-CN')
}

// 响应是否可用：storage 段必须存在，否则走空态（不显示一堆 0 误导）
export function isOverviewUsable(
  data: OpsHealthOverview | null | undefined,
): data is OpsHealthOverview {
  return (
    !!data &&
    typeof data.storage === 'object' &&
    data.storage !== null
  )
}
