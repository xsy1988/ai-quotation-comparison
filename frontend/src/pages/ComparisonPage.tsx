import { useMemo, useState } from 'react'
import { Alert, Badge, Card, Empty, Space, Spin, Table, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { getComparison } from '../api/client'
import type { Comparison, Supplier, WarningCategory } from '../types'
import HierarchyTable from '../components/HierarchyTable'
import DrawerTabs from '../components/DrawerTabs'
import FingerprintTable from '../components/FingerprintTable'
import AiAnalysisSection from '../components/AiAnalysisSection'
import NewProcessCard from '../components/NewProcessCard'
import { calcAbnormalSuppliers, warningSuppliers } from '../components/compareUtils'
import SupplierCategorySelect from '../components/SupplierCategorySelect'

const BADGE_DEFS: {
  category: WarningCategory
  label: string
  color: string
  count: (c: Comparison) => number
  suppliers: (c: Comparison) => Supplier[]
}[] = [
  {
    category: 'calc',
    label: '勾稽异常',
    color: 'red',
    count: (c) => calcAbnormalSuppliers(c).length,
    suppliers: calcAbnormalSuppliers,
  },
  {
    category: 'unmatched',
    label: '未匹配',
    color: 'volcano',
    count: (c) =>
      c.warnings.reduce((acc, w) => acc + (w.line_counts.unmatched > 0 ? 1 : 0), 0),
    suppliers: (c) => warningSuppliers(c, 'unmatched'),
  },
  {
    category: 'low_confidence',
    label: '低置信',
    color: 'orange',
    count: (c) =>
      c.warnings.reduce((acc, w) => acc + (w.line_counts.low_confidence > 0 ? 1 : 0), 0),
    suppliers: (c) => warningSuppliers(c, 'low_confidence'),
  },
  {
    category: 'new_process',
    label: '新工艺候选',
    color: 'purple',
    count: (c) =>
      c.warnings.reduce((acc, w) => acc + (w.line_counts.new_process > 0 ? 1 : 0), 0),
    suppliers: (c) => warningSuppliers(c, 'new_process'),
  },
]

export default function ComparisonPage() {
  const { id } = useParams<{ id: string }>()
  const taskId = Number(id)
  const [activeBadge, setActiveBadge] = useState<WarningCategory | null>(null)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['comparison', taskId],
    queryFn: () => getComparison(taskId),
    enabled: Number.isFinite(taskId),
  })

  // 点击徽标 -> 高亮该维度涉及的供应商列（Tag 提示）
  const highlighted = useMemo(() => {
    if (!data || !activeBadge) return new Set<number>()
    const def = BADGE_DEFS.find((d) => d.category === activeBadge)
    return new Set((def?.suppliers(data) ?? []).map((s) => s.quote_id))
  }, [data, activeBadge])

  // 供应商列顺序的唯一来源：比价表格（「报价对比」模块）的列顺序，AI 分析模块必须与之一致
  const supplierOrder = useMemo(() => data?.suppliers.map((s) => s.quote_id), [data])

  if (!Number.isFinite(taskId)) {
    return <Alert type="error" message="无效的任务 ID" showIcon />
  }
  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
        <div style={{ marginTop: 16 }}>加载对比结果…</div>
      </div>
    )
  }
  if (isError || !data) {
    return (
      <Alert
        type="error"
        message="加载对比结果失败"
        description={error instanceof Error ? error.message : String(error)}
        showIcon
      />
    )
  }

  const noData =
    data.suppliers.length === 0 &&
    data.hierarchy.length === 0 &&
    data.drawers.length === 0

  const activeDef = BADGE_DEFS.find((d) => d.category === activeBadge)

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {noData ? (
        <Card>
          <Empty description="该任务暂无对比数据" />
        </Card>
      ) : (
        <>
          <AiAnalysisSection taskId={taskId} supplierOrder={supplierOrder} />

          <Card title="报价单对比">
            {data.price_tree.length === 0 ? (
              <Empty />
            ) : (
              <HierarchyTable comparison={data} highlighted={highlighted} />
            )}
          </Card>

          <Card
            title={`任务 #${data.task_id} 比价结果`}
            extra={
              <Space wrap size={4}>
                {BADGE_DEFS.map((def) => (
                  <Badge
                    key={def.category}
                    count={def.count(data)}
                    size="small"
                    color={def.color}
                    offset={[-2, 2]}
                  >
                    <Tag
                      color={activeBadge === def.category ? def.color : 'default'}
                      style={{ cursor: 'pointer', marginRight: 10 }}
                      onClick={() =>
                        setActiveBadge(activeBadge === def.category ? null : def.category)
                      }
                    >
                      {def.label}
                    </Tag>
                  </Badge>
                ))}
              </Space>
            }
          >
            <Table
              size="small"
              rowKey="quote_id"
              pagination={false}
              dataSource={data.suppliers}
              locale={{ emptyText: '—' }}
              columns={[
                { title: '供应商', dataIndex: 'supplier_name' },
                {
                  title: '品类',
                  key: 'category',
                  width: 280,
                  render: (_: unknown, s: Supplier) => (
                    <SupplierCategorySelect supplier={s} />
                  ),
                },
              ]}
            />
            <Typography.Text
              type="secondary"
              style={{ fontSize: 12, display: 'block', marginTop: 8 }}
            >
              共 {data.suppliers.length} 份报价单 · “/” = 该供应商未报此行，未按 0 处理
              {activeDef &&
                ` · 已高亮${activeDef.label}涉及供应商：${
                  activeDef
                    .suppliers(data)
                    .map((s) => s.supplier_name)
                    .join('、') || '无'
                }（再点一次徽标取消）`}
            </Typography.Text>
          </Card>

          <Card title="加工费专区（维度抽屉）">
            <DrawerTabs comparison={data} />
          </Card>

          <Card title="指纹对齐（打包口径对齐）">
            {data.fingerprint_groups.length === 0 ? (
              <Empty description="无打包指纹数据" />
            ) : (
              <FingerprintTable comparison={data} />
            )}
          </Card>

          <NewProcessCard taskId={taskId} />
        </>
      )}
    </Space>
  )
}
