import { useMemo, useState } from 'react'
import { Alert, Button, Card, DatePicker, Empty, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import dayjs from 'dayjs'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { errorDetail, getSupplierHistory, httpStatus, listMasterSuppliers } from '../api/client'
import type { SupplierHistoryPoint } from '../types'

/** 曲线配色：前 8 条曲线固定取色，超过后由 recharts 自行分配 */
const SERIES_COLORS = [
  '#1677ff',
  '#fa541c',
  '#52c41a',
  '#722ed1',
  '#eb2f96',
  '#13c2c2',
  '#faad14',
  '#2f54eb',
  '#a0d911',
  '#f5222d',
]

/** 与「元/pcs」量纲差异过大的指标：同选时给出提示（共用一条 Y 轴） */
const MIXED_SCALE_HINT = new Set(['tooling_total'])

interface ChartRow {
  date: string
  /** 同一日期可能有多份报价，用于 tooltip 区分（零件/方案） */
  detail?: string | null
  /** `${supplier_code}|${metric_key}` -> 金额（缺失为 null，曲线断开而不补 0） */
  [key: string]: string | number | null | undefined
}

/** 每份报价对应曲线上的一个点：同一天多份报价（多产品/多方案）各自成点，避免互相覆盖丢数 */
function toChartRows(points: SupplierHistoryPoint[], metricKeys: string[]): ChartRow[] {
  return points.map((point) => {
    const row: ChartRow = { date: point.date, detail: point.part_name ?? point.scheme ?? '' }
    for (const key of metricKeys) {
      const value = point.metrics[key]
      if (value === null || value === undefined) continue
      row[`${point.supplier_code}|${key}`] = value
    }
    return row
  })
}

export default function SupplierHistoryPage() {
  const { code = '' } = useParams<{ code: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const initialCategory = searchParams.get('category') ?? undefined
  const [category, setCategory] = useState<string | undefined>(initialCategory)
  const [metricKeys, setMetricKeys] = useState<string[]>(['final_unit_price_taxed'])
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs] | null>(null)
  const [compareCode, setCompareCode] = useState<string | undefined>()

  const { data, isLoading, isError, error, isFetching } = useQuery({
    queryKey: ['supplier-history', code, category, metricKeys, range?.[0]?.format('YYYY-MM-DD'), range?.[1]?.format('YYYY-MM-DD'), compareCode],
    queryFn: () =>
      getSupplierHistory(code, {
        metrics: metricKeys.join(','),
        category_code: category,
        date_from: range?.[0]?.format('YYYY-MM-DD'),
        date_to: range?.[1]?.format('YYYY-MM-DD'),
        compare_code: compareCode,
      }),
    enabled: Boolean(code) && metricKeys.length > 0,
  })

  const { data: suppliers } = useQuery({
    queryKey: ['master', 'suppliers', '__options__'],
    queryFn: () => listMasterSuppliers(''),
  })

  const metrics = useMemo(() => data?.metrics ?? [], [data])
  const rows = useMemo(
    () => toChartRows(data?.points ?? [], metrics.map((m) => m.key)),
    [data, metrics],
  )

  const series = useMemo(() => {
    const list: { key: string; name: string }[] = []
    const owners = [data?.supplier, data?.compare_supplier].filter(
      (s): s is { code: string; name: string } => Boolean(s),
    )
    for (const supplier of owners) {
      for (const metric of metrics) {
        list.push({ key: `${supplier.code}|${metric.key}`, name: `${supplier.name}·${metric.label}` })
      }
    }
    return list
  }, [data, metrics])

  if (!code) {
    return <Alert type="error" message="无效的供应商编码" showIcon />
  }
  if (isError) {
    const notFound = httpStatus(error) === 404
    return (
      <Alert
        type="error"
        message={notFound ? '供应商不存在或已被删除' : '加载供应商历史报价失败'}
        description={errorDetail(error)}
        showIcon
        action={
          <Button size="small" onClick={() => navigate('/suppliers')}>
            返回供应商管理
          </Button>
        }
      />
    )
  }

  const points = data?.points ?? []
  const comparePointCount = data?.compare_supplier
    ? points.filter((p) => p.supplier_code === data.compare_supplier?.code).length
    : 0
  const metricLabel = (key: string) => metrics.find((m) => m.key === key)?.label ?? key
  const mixedScale = metricKeys.length > 1 && metricKeys.some((k) => MIXED_SCALE_HINT.has(k))

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <Link to="/suppliers">
              <ArrowLeftOutlined /> 供应商管理
            </Link>
            <Typography.Text strong>{data?.supplier.name ?? code}</Typography.Text>
            <Typography.Text type="secondary">
              {isLoading
                ? ''
                : `历史报价 ${data?.quote_count ?? 0} 份${
                    (data?.filtered_count ?? 0) !== (data?.quote_count ?? 0)
                      ? `（当前筛选 ${data?.filtered_count ?? 0} 份）`
                      : ''
                  }`}
            </Typography.Text>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap size={8}>
            <Select
              allowClear
              style={{ width: 200 }}
              placeholder="品类（单选）"
              value={category}
              onChange={(value) => {
                setCategory(value)
                const next = new URLSearchParams(searchParams)
                if (value) next.set('category', value)
                else next.delete('category')
                setSearchParams(next, { replace: true })
              }}
              options={(data?.categories ?? []).map((c) => ({
                value: c.code,
                label: `${c.name}（${c.code}）`,
              }))}
            />
            <Select
              mode="multiple"
              style={{ minWidth: 320, maxWidth: 520 }}
              placeholder="费用细项（可多选）"
              value={metricKeys}
              onChange={(values) =>
                setMetricKeys(values.length > 0 ? values : ['final_unit_price_taxed'])
              }
              options={(data?.metric_options ?? []).map((m) => ({ value: m.key, label: m.label }))}
            />
            <DatePicker.RangePicker
              value={range}
              onChange={(values) => setRange(values as [dayjs.Dayjs, dayjs.Dayjs] | null)}
            />
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              style={{ width: 220 }}
              placeholder="叠加对比供应商"
              value={compareCode}
              onChange={setCompareCode}
              options={(suppliers ?? [])
                .filter((s) => s.code !== code)
                .map((s) => ({ value: s.code, label: `${s.name}（${s.code}）` }))}
            />
          </Space>

          {mixedScale && (
            <Alert
              type="warning"
              showIcon
              message="所选指标量纲差异较大（模/治具费为整单金额，其余为元/pcs），曲线共用一条纵轴，读数请留意量级。"
            />
          )}

          {isLoading ? (
            <div style={{ textAlign: 'center', padding: 60 }}>
              <Spin />
            </div>
          ) : points.length === 0 ? (
            <Empty description="该筛选条件下暂无历史报价（未识别到的指标不会补 0）" />
          ) : (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                共 {points.filter((p) => p.supplier_code === data?.supplier.code).length} 个报价点
                {data?.compare_supplier &&
                  `（含对比供应商 ${data.compare_supplier.name} ${comparePointCount} 个报价点）`}
                {data?.compare_supplier && comparePointCount === 0 && (
                  <Typography.Text type="warning" style={{ fontSize: 12 }}>
                    {' '}
                    · 对比供应商在当前品类/指标/时间筛选下无报价点，曲线上不可见
                  </Typography.Text>
                )}
                {points.some((p) => p.date_source === 'created') &&
                  ' · 未识别报价日期的按录入日期落点'}
                {isFetching ? ' · 刷新中…' : ''}
              </Typography.Text>
              <ResponsiveContainer width="100%" height={360}>
                <LineChart data={rows} margin={{ top: 8, right: 24, bottom: 8, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" />
                  <YAxis width={70} />
                  <Tooltip
                    labelFormatter={(label: unknown, payload) => {
                      const detail = (payload?.[0]?.payload as ChartRow | undefined)?.detail
                      return detail ? `${String(label)} · ${detail}` : String(label)
                    }}
                  />
                  <Legend />
                  {series.map((item, index) => (
                    <Line
                      key={item.key}
                      type="monotone"
                      dataKey={item.key}
                      name={item.name}
                      stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
                      connectNulls
                      dot={{ r: 3 }}
                      activeDot={{ r: 5 }}
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </>
          )}
        </Space>
      </Card>

      <Card title="报价明细（曲线数据来源）">
        <Table<SupplierHistoryPoint>
          rowKey={(record) => `${record.supplier_code}-${record.quote_id}-${record.date}`}
          size="small"
          dataSource={points}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: '—' }}
          columns={[
            {
              title: '报价日期',
              dataIndex: 'date',
              width: 160,
              fixed: 'left',
              render: (date: string, record) => (
                <Space size={4}>
                  {date || '—'}
                  {record.date_source === 'created' && <Tag color="default">录入日期</Tag>}
                </Space>
              ),
            },
            { title: '供应商', dataIndex: 'supplier_name', width: 200 },
            {
              title: '品类',
              dataIndex: 'category_name',
              width: 120,
              render: (v: string | null) => v ?? '—',
            },
            {
              title: '项目',
              dataIndex: 'project_name',
              width: 120,
              render: (v: string | null) => v ?? '—',
            },
            {
              title: '零件',
              dataIndex: 'part_name',
              width: 120,
              render: (v: string | null) => v ?? '—',
            },
            {
              title: '方案',
              dataIndex: 'scheme',
              width: 120,
              render: (v: string | null) => v ?? '—',
            },
            ...metrics.map((metric) => ({
              title: metricLabel(metric.key),
              key: metric.key,
              width: 130,
              render: (_: unknown, record: SupplierHistoryPoint) => {
                const value = record.metrics[metric.key]
                return value === null || value === undefined ? (
                  <Typography.Text type="secondary">—</Typography.Text>
                ) : (
                  value.toFixed(2)
                )
              },
            })),
          ]}
        />
      </Card>
    </Space>
  )
}
