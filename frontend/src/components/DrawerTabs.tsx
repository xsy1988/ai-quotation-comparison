import { useMemo } from 'react'
import { Empty, Table, Tabs } from 'antd'
import type { Comparison } from '../types'
import Amount from './Amount'
import {
  SUPPLIER_COL_MIN_WIDTH,
  lookupValue,
  supplierColumnGroups,
  supplierSeparatorStyle,
} from './compareUtils'

interface Props {
  comparison: Comparison
}

export default function DrawerTabs({ comparison }: Props) {
  const { drawers, suppliers } = comparison
  // 与「报价对比」一致：同一供应商的报价列相邻，组间画分隔线并保证列最小宽度
  const groups = useMemo(() => supplierColumnGroups(suppliers), [suppliers])

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
            scroll={{ x: 'max-content' }}
            columns={[
              {
                title: '抽屉组',
                dataIndex: 'group_name',
                key: 'group_name',
                width: 200,
                fixed: 'left',
              },
              ...suppliers.map((s) => ({
                title: [s.supplier_name, s.part_name, s.scheme].filter(Boolean).join(' · '),
                key: `q${s.quote_id}`,
                align: 'right' as const,
                minWidth: SUPPLIER_COL_MIN_WIDTH,
                onHeaderCell: () => ({ style: supplierSeparatorStyle(groups.get(s.quote_id)) }),
                onCell: () => ({ style: supplierSeparatorStyle(groups.get(s.quote_id)) }),
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
