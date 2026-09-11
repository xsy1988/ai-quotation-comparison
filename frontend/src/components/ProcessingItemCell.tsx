import { useState } from 'react'
import { App, InputNumber, Select, Spin, Tag, Tooltip } from 'antd'
import { SwapOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ProcessingItem } from '../types'
import { errorDetail, getAtoms, patchQuoteLine } from '../api/client'
import Amount from './Amount'

interface Props {
  item: ProcessingItem
}

/** 加工费明细条目：金额与原子映射就地编辑（点击切换为输入控件，blur/回车提交）。 */
export default function ProcessingItemCell({ item }: Props) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [editingAmount, setEditingAmount] = useState(false)
  const [amountDraft, setAmountDraft] = useState<number | null>(null)
  const [editingAtom, setEditingAtom] = useState(false)
  const [atomSearch, setAtomSearch] = useState('')

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['comparison'] })

  const mutation = useMutation({
    mutationFn: (patch: { amount?: number; atom_code?: string }) =>
      patchQuoteLine(item.id, patch),
    onSuccess: () => {
      message.success('已保存')
      invalidate()
    },
    onError: (e) => message.error(`保存失败：${errorDetail(e)}`),
  })

  const atomsQuery = useQuery({
    queryKey: ['atoms', atomSearch],
    queryFn: () => getAtoms(atomSearch),
    enabled: editingAtom,
  })

  const submitAmount = () => {
    setEditingAmount(false)
    if (amountDraft !== null && amountDraft !== item.amount) {
      mutation.mutate({ amount: amountDraft })
    }
  }

  return (
    <span className="proc-cell" style={{ display: 'block' }}>
      {item.is_shared ? (
        // 共享单元格去重置零副本：金额只计一次，置零副本显示"/"，备注见 tooltip
        <Tooltip title={item.note ?? '与共享单元格金额只计一次'}>
          <span style={{ color: '#999', cursor: 'help' }}>/</span>
        </Tooltip>
      ) : editingAmount ? (
        <InputNumber
          size="small"
          autoFocus
          value={amountDraft}
          onChange={(v) => setAmountDraft(v)}
          onBlur={submitAmount}
          onPressEnter={submitAmount}
          style={{ width: 110 }}
        />
      ) : (
        <span
          onClick={() => {
            setAmountDraft(item.amount)
            setEditingAmount(true)
          }}
          style={{ cursor: 'pointer' }}
          title="点击修改金额"
        >
          <Amount value={item.amount} />
        </span>
      )}
      {item.bundle_flag && (
        <Tooltip title="打包报价未拆分明细，整行金额计入加工费">
          <Tag color="blue" style={{ marginLeft: 4 }}>
            含打包
          </Tag>
        </Tooltip>
      )}
      {item.is_new_process && <Tag color="purple">新工艺</Tag>}
      {/* 原子映射默认隐藏，hover 单元格出现 icon，点击搜索更换工艺（match_path 将变为 manual） */}
      <span className="proc-cell-atom" style={{ marginLeft: 6 }}>
        {editingAtom ? (
          <Select
            size="small"
            autoFocus
            showSearch
            filterOption={false}
            loading={atomsQuery.isLoading}
            defaultOpen
            value={item.atom_code ?? undefined}
            placeholder="搜索原子编码/名称"
            onSearch={setAtomSearch}
            onBlur={() => setEditingAtom(false)}
            onSelect={(code: string) => {
              setEditingAtom(false)
              if (code !== item.atom_code) mutation.mutate({ atom_code: code })
            }}
            notFoundContent={atomsQuery.isLoading ? <Spin size="small" /> : null}
            options={(atomsQuery.data ?? []).map((a) => ({
              value: a.code,
              label: `${a.code} ${a.name}`,
            }))}
            style={{ minWidth: 220 }}
          />
        ) : (
          <Tooltip title={item.atom_code ? '搜索更换工艺' : '未匹配，点击搜索匹配工艺'}>
            <SwapOutlined
              style={{ cursor: 'pointer', color: item.atom_code ? undefined : '#faad14' }}
              onClick={() => {
                setAtomSearch(item.atom_code ?? item.name)
                setEditingAtom(true)
              }}
            />
          </Tooltip>
        )}
      </span>
    </span>
  )
}
