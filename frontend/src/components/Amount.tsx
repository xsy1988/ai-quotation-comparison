// 统一金额渲染：两位小数、千分位、右对齐；null 渲染空值语义，绝不显示 0 或空白
export const NA_TEXT = '路线未含此工序'

export function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined) return NA_TEXT
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export default function Amount({ value, strong }: { value: number | null; strong?: boolean }) {
  if (value === null) {
    return <span className="na-cell">{NA_TEXT}</span>
  }
  return (
    <span className="amount-cell" style={strong ? { fontWeight: 600 } : undefined}>
      {formatAmount(value)}
    </span>
  )
}
