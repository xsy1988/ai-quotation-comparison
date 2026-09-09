import type { Comparison, HierarchyKey, Supplier, WarningCategory } from '../types'
import { NA_TEXT } from './Amount'

export type CellValue = number | null

/** 从 hierarchy 取某行的 quote_id -> 值 映射 */
export function hierarchyValues(
  comparison: Comparison,
  key: HierarchyKey,
): Record<string, CellValue> {
  const row = comparison.hierarchy.find((r) => r.key === key)
  return row ? row.values : {}
}

export function lookupValue(values: Record<string, CellValue>, quoteId: number): CellValue {
  const v = values[String(quoteId)]
  return v === undefined ? null : v
}

/** 低置信度 / 未匹配 / 新工艺候选 按维度聚合到供应商 */
export function warningSuppliers(
  comparison: Comparison,
  category: WarningCategory,
): Supplier[] {
  const byQuote = new Map(comparison.warnings.map((w) => [w.quote_id, w]))
  return comparison.suppliers.filter((s) => {
    const w = byQuote.get(s.quote_id)
    if (!w) return false
    if (category === 'low_confidence') return w.line_counts.low_confidence > 0
    if (category === 'unmatched') return w.line_counts.unmatched > 0
    if (category === 'new_process') return w.line_counts.new_process > 0
    return false
  })
}

/** 勾稽异常：calc_check 为 failed / warning（calc_check 通过则应为 "passed"，null = 未校验） */
export const CALC_CHECK_BAD = new Set(['failed', 'warning'])

export function calcAbnormalSuppliers(comparison: Comparison): Supplier[] {
  return comparison.suppliers.filter(
    (s) => s.calc_check !== null && CALC_CHECK_BAD.has(s.calc_check),
  )
}

/** 该供应商某模块是否有勾稽异常标记（徽标点击后高亮列用） */
export function supplierHasFlag(supplier: Supplier, flag: string): boolean {
  return supplier.flags.includes(flag)
}

export { NA_TEXT }
