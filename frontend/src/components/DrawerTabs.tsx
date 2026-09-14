import { useMemo } from 'react'
import { Empty, Table, Tabs } from 'antd'
import type { Comparison } from '../types'
import Amount from './Amount'
import {
  lookupValue,
  supplierColumnGroups,
  supplierSeparatorStyle,
  useSupplierColumnFit,
} from './compareUtils'

/** 「抽屉组」固定列宽度 */
const LABEL_COL_WIDTH = 200

interface Props {
  comparison: Comparison
}

export default function DrawerTabs({ comparison }: Props) {
  const { drawers, suppliers } = comparison
  // 与「报价对比」一致：同一供应商的报价列相邻，组间画分隔线并保证列最小宽度
  const groups = useMemo(() => supplierColumnGroups(suppliers), [suppliers])
  const fit = useSupplierColumnFit(suppliers.length, LABEL_COL_WIDTH)

  if (drawers.length === 0) {
    return <Empty description="无加工费维度抽屉数据" />
  }

  return (
    <div ref={fit.ref}>
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
              scroll={{ x: fit.scrollX }}
              columns={[
                {
                  title: '抽屉组',
                  dataIndex: 'group_name',
                  key: 'group_name',
                  width: LABEL_COL_WIDTH,
                  fixed: 'left',
                },
                ...suppliers.map((s, index) => ({
                  title: [s.supplier_name, s.part_name, s.scheme].filter(Boolean).join(' · '),
                  key: `q${s.quote_id}`,
                  align: 'right' as const,
                  width: fit.widthOf(index),
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
    </div>
  )
}
