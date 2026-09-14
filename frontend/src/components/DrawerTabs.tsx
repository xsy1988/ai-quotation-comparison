import { Empty, Table, Tabs } from 'antd'
import type { Comparison } from '../types'
import Amount from './Amount'
import { lookupValue } from './compareUtils'

interface Props {
  comparison: Comparison
}

export default function DrawerTabs({ comparison }: Props) {
  const { drawers, suppliers } = comparison

  if (drawers.length === 0) {
    return <Empty description="无加工费维度抽屉数据" />
  }

  return (
    <Tabs
      items={drawers.map((drawer) => ({
        key: drawer.scope,
        label: drawer.name || drawer.scope,
        children: (
          <Table
            size="small"
            rowKey="group_code"
            pagination={false}
            bordered
            dataSource={drawer.groups}
            columns={[
              {
                title: '抽屉组',
                dataIndex: 'group_name',
                key: 'group_name',
                width: 200,
              },
              ...suppliers.map((s) => ({
                title: [s.supplier_name, s.part_name, s.scheme].filter(Boolean).join(' · '),
                key: `q${s.quote_id}`,
                align: 'right' as const,
                render: (_: unknown, row: (typeof drawer.groups)[number]) => (
                  <Amount value={lookupValue(row.values, s.quote_id)} />
                ),
              })),
            ]}
          />
        ),
      }))}
    />
  )
}
