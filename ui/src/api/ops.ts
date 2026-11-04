// 运维看板 API 封装（/v1/admin/ops/*）—— TTL 处置审计 + LLM 成本账本
// 后端可能尚未实现：调用方需捕获错误并显示空态（不得白屏）
import { api } from './client'

// ---------- TTL 处置（D-52 五档处置审计，ttl_disposition 表） ----------

// 五档处置：valid_expired 过期降档 / renewed 续期 / anchor 锚点 / sleeping 休眠 / archived 归档
export interface TtlDispositionItem {
  day: string
  valid_expired: number
  renewed: number
  anchor: number
  sleeping: number
  archived: number
}

export interface TtlDispositionTotal {
  valid_expired: number
  renewed: number
  anchor: number
  sleeping: number
  archived: number
}

export interface TtlDispositionResponse {
  items: TtlDispositionItem[]
  total: TtlDispositionTotal
}

export function getTtlDisposition(days: number) {
  return api.get<TtlDispositionResponse>(`/v1/admin/ops/ttl-disposition?days=${days}`)
}

// ---------- LLM 成本（llm_usage_daily 账本） ----------

export interface LlmCostItem {
  day: string
  provider: string
  model: string
  stage: string
  prompt_tokens: number
  completion_tokens: number
  calls: number
}

export interface LlmCostTotals {
  prompt_tokens: number
  completion_tokens: number
  calls: number
}

// 成本告警档位（P1-9，2026-11-04）：budget 字段来自
// sgme/engine/cost_alert.py::budget_status——只读评估，不改配置、不触发告警。
// available=false 表示 llm_usage_daily 账本尚未创建（本月无记账）。
export type LlmBudgetLevel = 'none' | 'warn' | 'alert'
export type LlmBudgetBaselineSource = 'config' | 'rolling_median'

export interface LlmBudget {
  available: boolean
  /** 配置是否启用成本告警（cost_alert.enabled） */
  enabled?: boolean
  /** 统计月份（UTC，YYYY-MM）；账本未就绪时可能为 null */
  month: string | null
  /** 本月 prompt+completion 合计 tokens */
  month_tokens: number
  /** 告警基线 tokens（config 估算或滚动中位数 ×1.1） */
  baseline_tokens: number
  /** 基线来源：config=首月配置估算；rolling_median=近 3 个完整月中位数 ×1.1 */
  baseline_source: LlmBudgetBaselineSource | string
  /** 本月 tokens / 基线（1.0 = 100%） */
  ratio: number
  /** 告警档位：none=正常 / warn=提示线 / alert=告警线 */
  level: LlmBudgetLevel | string
  /** 提示线倍数（默认 1.2） */
  warn_ratio: number
  /** 告警线倍数（默认 2.0） */
  alert_ratio: number
}

export interface LlmCostResponse {
  items: LlmCostItem[]
  totals: LlmCostTotals
  /** 窗口天数（后端回显，可选） */
  days?: number
  /** 成本告警档位（旧后端不返回该字段 → 页面走空态） */
  budget?: LlmBudget
  /** 后端说明（表未创建时给出提示文案） */
  note?: string
}

export function getLlmCost(days: number) {
  return api.get<LlmCostResponse>(`/v1/admin/ops/llm-cost?days=${days}`)
}

// ---------- 运维健康总览（存储体检 + 9.6 就绪，只读） ----------
// GET /v1/admin/ops/health-overview（Admin Key）：
//   storage = 磁盘/文件体检（DB/WAL/wiki.db/raw/备份）；scenes = 9.6 场景层下线就绪度。
// 后端未就绪时调用方捕获错误显示空态（不得白屏）。

export interface OpsStorageHealth {
  /** 数据目录（展示用；含本机绝对路径，仅管理员可见） */
  data_dir: string
  /** memory.db 主库字节数 */
  memory_db_bytes: number
  /** memory.db WAL 字节数（未开 WAL 时为 0） */
  memory_wal_bytes: number
  /** wiki.db 字节数 */
  wiki_db_bytes: number
  /** raw 原始会话文件数 */
  raw_files: number
  /** raw 原始会话总字节数 */
  raw_bytes: number
  /** 备份快照数 */
  backup_snapshots: number
  /** 备份快照总字节数 */
  backup_bytes: number
}

export interface OpsScenesReadiness {
  /** scenes 表行数 */
  scenes: number
  /** scene_vectors 表行数 */
  scene_vectors: number
  /** scenes_fts 索引行数 */
  scenes_fts: number
  /** GONE 规则数（已下线端点清单，D-64 ③） */
  gone_rules: number
  /** 冲突规则数（语义变更端点清单，D-64 ②） */
  conflict_rules: number
  /** 兼容期阶段：pre_stable / active / expired / unknown（compat.compat_phase） */
  compat_phase: string | null
  /** 稳定期起算日（compat.stable_since；未设置时 null，可选字段） */
  stable_since?: string | null
  /** 兼容期剩余天数（负值=已过期；未设置时 null，可选字段） */
  days_left?: number | null
  /** 语义变更端点是否强制 409 */
  enforce: boolean
  /** 已下线端点是否强制 410 */
  enforce_gone: boolean
  /** 9.6 场景层下线就绪徽标 */
  ready: boolean
  /** 阻塞项（人可读；空数组 = 无阻塞） */
  blockers: string[]
}

export interface OpsHealthOverview {
  /** 生成时刻（ISO 8601） */
  generated_at: string
  storage: OpsStorageHealth
  scenes: OpsScenesReadiness
  /** 人工动作清单（场景层下线操作步骤；旧后端可能不返回） */
  manual_actions?: string[]
}

export function getHealthOverview() {
  return api.get<OpsHealthOverview>('/v1/admin/ops/health-overview')
}
