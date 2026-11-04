// 运维健康总览展示辅助（纯函数，便于独立校验）
// 数据契约：GET /v1/admin/ops/health-overview（storage / scenes 两段）。
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

// 兼容期阶段 → 中文标注（值来自 sgme/compat.py::compat_phase 的 phase 字段）
const COMPAT_PHASE_LABELS: Record<string, string> = {
  pre_stable: '未定稳定期（pre_stable）',
  active: '兼容期内（active）',
  expired: '兼容期已过期（expired）',
  unknown: '未知（stable_since 非法）',
}

export function compatPhaseLabel(phase: string | null | undefined): string {
  const key = String(phase || '').trim()
  if (!key) return '—'
  return COMPAT_PHASE_LABELS[key] || key
}

// 响应是否可用：storage / scenes 两段都必须存在，否则走空态（不显示一堆 0 误导）
export function isOverviewUsable(
  data: OpsHealthOverview | null | undefined,
): data is OpsHealthOverview {
  return (
    !!data &&
    typeof data.storage === 'object' &&
    data.storage !== null &&
    typeof data.scenes === 'object' &&
    data.scenes !== null
  )
}
