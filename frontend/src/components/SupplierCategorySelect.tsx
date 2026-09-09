import { App, Select } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Supplier } from '../types'
import { errorDetail, getCategories, patchQuote } from '../api/client'

interface Props {
  supplier: Supplier
}

/** 供应商卡片上的品类就地修改：选项来自 /api/categories，提交 PATCH /api/quotes/{id}。 */
export default function SupplierCategorySelect({ supplier }: Props) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const categories = useQuery({ queryKey: ['categories'], queryFn: getCategories })

  const mutation = useMutation({
    mutationFn: (categoryCode: string) =>
      patchQuote(supplier.quote_id, { category_code: categoryCode }),
    onSuccess: () => {
      message.success('品类已更新，映射已重跑')
      queryClient.invalidateQueries({ queryKey: ['comparison'] })
    },
    onError: (e) => message.error(`品类更新失败：${errorDetail(e)}`),
  })

  return (
    <Select
      size="small"
      loading={categories.isLoading}
      value={supplier.category_code ?? undefined}
      placeholder="未设品类"
      onChange={(code) => mutation.mutate(code)}
      options={(categories.data ?? []).map((c) => ({
        value: c.code,
        label: `${c.code} ${c.name}`,
      }))}
      style={{ minWidth: 180 }}
    />
  )
}
