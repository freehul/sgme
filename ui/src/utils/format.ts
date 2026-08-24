// 时间戳格式化：空值返回占位符，否则本地化显示
export function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}
