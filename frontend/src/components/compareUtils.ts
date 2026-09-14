import type { CSSProperties } from 'react'
import type { Comparison, HierarchyKey, Supplier, WarningCategory } from '../types'
import { NA_TEXT } from './Amount'

export type CellValue = number | null

/** 供应商报价列的最小宽度：一行可容纳 10 个汉字（14px × 10）加单元格内边距 */
export const SUPPLIER_COL_MIN_WIDTH = 160

/** 供应商分组口径：与后端 _task_quotes 一致，优先 supplier_code，缺失时用名称 */
function supplierKey(s: Supplier): string {
  return s.supplier_code || s.supplier_name || `q${s.quote_id}`
}

export interface SupplierColumnGroup {
  /** 分组序号：同一供应商一组，按首次出现顺序（0 起，首组无左侧分隔线） */
  groupIndex: number
  /** 该供应商在本任务中的报价份数 */
  groupSize: number
  /** 组内第一列 */
  isGroupStart: boolean
}

/** 供应商列分组信息：后端已保证同一供应商的报价列相邻，这里标出组序号与组内首列，
 *  供表格设置列最小宽度、画组间分隔线、按供应商顺序排列数据行使用。 */
export function supplierColumnGroups(
  suppliers: Supplier[],
): Map<number, SupplierColumnGroup> {
  const size = new Map<string, number>()
  const order: string[] = []
  for (const s of suppliers) {
    const key = supplierKey(s)
    size.set(key, (size.get(key) ?? 0) + 1)
    if (!order.includes(key)) order.push(key)
  }
  const started = new Set<string>()
  const out = new Map<number, SupplierColumnGroup>()
  for (const s of suppliers) {
    const key = supplierKey(s)
    const isGroupStart = !started.has(key)
    started.add(key)
    out.set(s.quote_id, {
      groupIndex: order.indexOf(key),
      groupSize: size.get(key) ?? 1,
      isGroupStart,
    })
  }
  return out
}

/** 供应商分隔线：组首列（首组除外）左侧画竖线，组内相邻列不画 */
export function supplierSeparatorStyle(group?: SupplierColumnGroup): CSSProperties {
  return group && group.isGroupStart && group.groupIndex > 0
    ? { borderLeft: '2px solid #bfbfbf' }
    : {}
}

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

/** 勾稽异常：后端 calc_check 只写 pass/fail（unchecked = 未校验），另兼容 failed/warning 历史值 */
export const CALC_CHECK_BAD = new Set(['fail', 'failed', 'warning'])

export function calcAbnormalSuppliers(comparison: Comparison): Supplier[] {
  return comparison.suppliers.filter(
    (s) =>
      (s.calc_check !== null && CALC_CHECK_BAD.has(s.calc_check)) ||
      // 派生重算发现的勾稽问题（如明细金额未印出导致模块合计只是下限）也会打 calc_abnormal 标
      s.flags.includes('calc_abnormal'),
  )
}

/** 该供应商某模块是否有勾稽异常标记（徽标点击后高亮列用） */
export function supplierHasFlag(supplier: Supplier, flag: string): boolean {
  return supplier.flags.includes(flag)
}

export { NA_TEXT }
