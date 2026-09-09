import { Empty, Table, Tabs, Tag } from 'antd'
import type { Comparison, DrawerScope } from '../types'
import Amount from './Amount'
import { lookupValue } from './compareUtils'

const SCOPE_LABELS: Record<DrawerScope, string> = {
  process_domain: '按工艺域',
  process_stage: '按工艺阶段',
  process_class: '按工艺类别',
}

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
        label: SCOPE_LABELS[drawer.scope] ?? drawer.scope,
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
                render: (name: string, row) => (
                  <span>
                    {name}
                    {row.is_fallback_bucket && (
                      <Tag style={{ marginLeft: 6 }}>兜底桶</Tag>
                    )}
                  </span>
                ),
              },
              ...suppliers.map((s) => ({
                title: s.supplier_name,
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
