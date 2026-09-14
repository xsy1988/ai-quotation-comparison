import { useMemo } from 'react'
import { Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { Comparison } from '../types'
import Amount from './Amount'

interface Props {
  comparison: Comparison
}

export default function FingerprintTable({ comparison }: Props) {
  const { fingerprint_groups, suppliers } = comparison

  // 行顺序与「报价对比」的供应商列顺序对齐：同一供应商的行必须相邻（suppliers 已按此排序）
  const rank = useMemo(
    () => new Map(suppliers.map((s, i) => [s.quote_id, i])),
    [suppliers],
  )

  const byQuoteName = new Map(
    suppliers.map((s) => [
      s.quote_id,
      [s.supplier_name, s.part_name, s.scheme].filter(Boolean).join(' · '),
    ]),
  )

  return (
    <>
      {fingerprint_groups.map((group) => {
        const rows = [...group.rows].sort(
          (a, b) =>
            (rank.get(a.quote_id) ?? Number.MAX_SAFE_INTEGER) -
            (rank.get(b.quote_id) ?? Number.MAX_SAFE_INTEGER),
        )
        const bySupplier = new Map<number, typeof group.rows>()
        for (const row of rows) {
          const list = bySupplier.get(row.quote_id) ?? []
          list.push(row)
          bySupplier.set(row.quote_id, list)
        }
        return (
          <Table
            key={group.fingerprint}
            size="small"
            rowKey={(row) => `${row.quote_id}-${row.item_name}-${row.amount ?? 'na'}`}
            pagination={false}
            bordered
            style={{ marginBottom: 16 }}
            title={() => (
              <div>
                <Space size={4} wrap>
                  <span>指纹</span>
                  {group.atoms.map((atom) => (
                    <Tooltip key={atom.code} title={atom.code}>
                      <Tag color="blue" style={{ marginRight: 0 }}>
                        {atom.name ?? atom.code}
                      </Tag>
                    </Tooltip>
                  ))}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    编码 {group.fingerprint}
                  </Typography.Text>
                </Space>
                <div style={{ color: '#999', fontSize: 12 }}>
                  同一指纹 = 各供应商对这些工艺项的打包/拆分口径一致，可横向对齐
                </div>
              </div>
            )}
            dataSource={rows}
            rowClassName={(row, index) =>
              index > 0 && rows[index - 1].quote_id !== row.quote_id
                ? 'supplier-group-start-row'
                : ''
            }
            columns={[
              {
                title: '供应商',
                dataIndex: 'quote_id',
                key: 'supplier',
                width: 200,
                render: (quoteId: number) => byQuoteName.get(quoteId) ?? quoteId,
              },
              {
                title: '条目名',
                dataIndex: 'item_name',
                key: 'item_name',
              },
              {
                title: '金额',
                dataIndex: 'amount',
                key: 'amount',
                align: 'right',
                render: (amount: number | null) => <Amount value={amount} />,
              },
              {
                title: '打包/拆分',
                key: 'bundle',
                width: 120,
                render: (_: unknown, row) =>
                  (bySupplier.get(row.quote_id)?.length ?? 0) > 1 ? (
                    <Tag color="purple">与其他家合并项同指纹</Tag>
                  ) : (
                    <Tag>单条</Tag>
                  ),
              },
            ]}
          />
        )
      })}
    </>
  )
}
