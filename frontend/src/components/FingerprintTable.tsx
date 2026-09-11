import { Table, Tag } from 'antd'
import type { Comparison } from '../types'
import Amount from './Amount'

interface Props {
  comparison: Comparison
}

export default function FingerprintTable({ comparison }: Props) {
  const { fingerprint_groups, suppliers } = comparison

  const byQuoteName = new Map(
    suppliers.map((s) => [
      s.quote_id,
      [s.supplier_name, s.part_name, s.scheme].filter(Boolean).join(' · '),
    ]),
  )

  return (
    <>
      {fingerprint_groups.map((group) => {
        const bySupplier = new Map<number, typeof group.rows>()
        for (const row of group.rows) {
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
              <span>
                指纹 <Tag>{group.fingerprint}</Tag>
                <span style={{ color: '#999', fontSize: 12 }}>
                  同一指纹 = 各供应商对这些工艺项的打包/拆分口径一致，可横向对齐
                </span>
              </span>
            )}
            dataSource={group.rows}
            columns={[
              {
                title: '供应商',
                dataIndex: 'quote_id',
                key: 'supplier',
                width: 160,
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
