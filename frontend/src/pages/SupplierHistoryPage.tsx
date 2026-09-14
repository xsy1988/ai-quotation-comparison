import { useMemo, useState } from 'react'
import { Alert, Button, Card, DatePicker, Empty, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import dayjs from 'dayjs'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  ResponsiveContainer,
  Scatter,
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

const DAY_MS = 24 * 60 * 60 * 1000

/** 一条带状曲线 = 一家供应商的一个指标（横轴日粒度，纵轴当日最高/最低价） */
interface SeriesMeta {
  key: string
  name: string
  metricKey: string
  color: string
}

/** 曲线上的原始点：一份报价单当天的该指标取值 */
interface QuoteDot {
  ts: number
  value: number
  quoteId: number
  detail: string | null
  supplier: string
}

interface DaySeries {
  key: string
  name: string
  color: string
  min: number
  max: number
  points: QuoteDot[]
}

interface DayBucket {
  ts: number
  date: string
  quoteCount: number
  series: DaySeries[]
}

/** 带状曲线的一行：某天某系列的上下边界（同日多份报价 → 区间；仅一份 → 上下界相同，收成一条线） */
type BandRow = { ts: number; date: string } & Record<string, number | string | null>

interface ChartData {
  rows: BandRow[]
  buckets: Map<number, DayBucket>
  scatter: Record<string, QuoteDot[]>
  ticks: number[]
  domain: [number, number]
  /** 日期缺失（既无报价日期也无录入日期）而无法落点的报价单数 */
  missingDate: number
}

/** 把报价点折成「日期 → 当日上下边界 + 全部原始点」的结构，供带状曲线 + 散点共用 */
function buildChart(points: SupplierHistoryPoint[], series: SeriesMeta[]): ChartData {
  const buckets = new Map<number, DayBucket>()
  const quoteIds = new Map<number, Set<number>>()
  const scatter: Record<string, QuoteDot[]> = {}
  let missingDate = 0

  for (const point of points) {
    const day = dayjs(point.date, 'YYYY-MM-DD')
    if (!day.isValid()) {
      missingDate += 1
      continue
    }
    const ts = day.startOf('day').valueOf()
    let bucket = buckets.get(ts)
    if (!bucket) {
      bucket = { ts, date: day.format('YYYY-MM-DD'), quoteCount: 0, series: [] }
      buckets.set(ts, bucket)
    }
    const ids = quoteIds.get(ts) ?? new Set<number>()
    ids.add(point.quote_id)
    quoteIds.set(ts, ids)

    for (const item of series) {
      const value = point.metrics[item.metricKey]
      if (value === null || value === undefined) continue
      let hit = bucket.series.find((s) => s.key === item.key)
      if (!hit) {
        hit = { key: item.key, name: item.name, color: item.color, min: value, max: value, points: [] }
        bucket.series.push(hit)
      }
      hit.min = Math.min(hit.min, value)
      hit.max = Math.max(hit.max, value)
      const dot: QuoteDot = {
        ts,
        value,
        quoteId: point.quote_id,
        detail: point.part_name ?? point.scheme ?? null,
        supplier: point.supplier_name ?? point.supplier_code,
      }
      hit.points.push(dot)
      ;(scatter[item.key] ??= []).push(dot)
    }
  }

  const ordered = [...buckets.values()].sort((a, b) => a.ts - b.ts)
  for (const bucket of ordered) {
    bucket.quoteCount = quoteIds.get(bucket.ts)?.size ?? 0
    for (const item of bucket.series) {
      item.points.sort((a, b) => b.value - a.value)
    }
    bucket.series.sort((a, b) => a.name.localeCompare(b.name))
  }

  const rows: BandRow[] = ordered.map((bucket) => {
    const row: BandRow = { ts: bucket.ts, date: bucket.date }
    for (const item of series) {
      const hit = bucket.series.find((s) => s.key === item.key)
      row[`${item.key}__min`] = hit ? hit.min : null
      row[`${item.key}__max`] = hit ? hit.max : null
    }
    return row
  })

  const now = dayjs().startOf('day').valueOf()
  return {
    rows,
    buckets,
    scatter,
    ticks: pickTicks(ordered.map((bucket) => bucket.ts)),
    // 左右各留半天，单日只剩一份报价时点也能落在中间而不是贴边
    domain: ordered.length
      ? [ordered[0].ts - DAY_MS / 2, ordered[ordered.length - 1].ts + DAY_MS / 2]
      : [now - DAY_MS, now + DAY_MS],
    missingDate,
  }
}

/** 日期刻度：每天都有数据时全画，点多时均匀抽稀（首尾必留），避免横轴糊成一片 */
function pickTicks(timestamps: number[], max = 12): number[] {
  if (timestamps.length <= max) return timestamps
  const step = (timestamps.length - 1) / (max - 1)
  const picked = new Set<number>()
  for (let i = 0; i < max; i += 1) picked.add(timestamps[Math.round(i * step)])
  return [...picked].sort((a, b) => a - b)
}

/** tooltip 定位到当天：数值坐标轴给 label=时间戳，散点给 payload.ts，两条路都试 */
function bucketTsOf(label: unknown, payload: readonly { payload?: unknown }[] | undefined): number | undefined {
  const direct = Number(label)
  if (label !== undefined && label !== null && label !== '' && Number.isFinite(direct)) return direct
  for (const entry of payload ?? []) {
    const ts = Number((entry.payload as { ts?: unknown } | undefined)?.ts)
    if (Number.isFinite(ts)) return ts
  }
  return undefined
}

function DayTooltip({
  active,
  label,
  payload,
  buckets,
}: {
  active?: boolean
  label?: unknown
  payload?: readonly { payload?: unknown }[]
  buckets: Map<number, DayBucket>
}) {
  if (!active) return null
  const ts = bucketTsOf(label, payload)
  const bucket = ts === undefined ? undefined : buckets.get(ts)
  if (!bucket) return null
  return (
    <div
      style={{
        background: '#fff',
        border: '1px solid #f0f0f0',
        borderRadius: 6,
        boxShadow: '0 2px 8px rgba(0, 0, 0, 0.12)',
        padding: '8px 10px',
        fontSize: 12,
        maxWidth: 420,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        {bucket.date}
        <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
          {' '}
          · 当日 {bucket.quoteCount} 份报价
        </Typography.Text>
      </div>
      {bucket.series.map((item) => (
        <div key={item.key} style={{ marginTop: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: 4,
                background: item.color,
                display: 'inline-block',
              }}
            />
            <span>{item.name}</span>
            <span style={{ color: '#8c8c8c' }}>
              {item.min === item.max
                ? item.min.toFixed(2)
                : `${item.min.toFixed(2)} ~ ${item.max.toFixed(2)}`}
            </span>
          </div>
          {item.points.map((point) => (
            <div key={`${item.key}-${point.quoteId}`} style={{ paddingLeft: 14, color: '#595959' }}>
              {point.value.toFixed(2)}
              <span style={{ color: '#bfbfbf' }}>{` #${point.quoteId}`}</span>
              {point.detail ? ` · ${point.detail}` : ''}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

export default function SupplierHistoryPage() {
  const { code = '' } = useParams<{ code: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const initialCategory = searchParams.get('category') ?? undefined
  const [category, setCategory] = useState<string | undefined>(initialCategory)
  const [metricKeys, setMetricKeys] = useState<string[]>(['final_unit_price_taxed'])
  const [domainCodes, setDomainCodes] = useState<string[]>([])
  const [atomCodes, setAtomCodes] = useState<string[]>([])
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs] | null>(null)
  const [compareCode, setCompareCode] = useState<string | undefined>()

  const { data, isLoading, isError, error, isFetching } = useQuery({
    queryKey: ['supplier-history', code, category, metricKeys, domainCodes, atomCodes, range?.[0]?.format('YYYY-MM-DD'), range?.[1]?.format('YYYY-MM-DD'), compareCode],
    queryFn: () =>
      getSupplierHistory(code, {
        metrics: metricKeys.join(','),
        category_code: category,
        domain_codes: domainCodes.join(','),
        atom_codes: atomCodes.join(','),
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

  const series = useMemo(() => {
    const list: SeriesMeta[] = []
    const owners = [data?.supplier, data?.compare_supplier].filter(
      (s): s is { code: string; name: string } => Boolean(s),
    )
    for (const supplier of owners) {
      for (const metric of metrics) {
        list.push({
          key: `${supplier.code}|${metric.key}`,
          name: `${supplier.name}·${metric.label}`,
          metricKey: metric.key,
          color: SERIES_COLORS[list.length % SERIES_COLORS.length],
        })
      }
    }
    return list
  }, [data, metrics])

  const chart = useMemo(() => buildChart(data?.points ?? [], series), [data, series])

  // 工艺域是原子工艺的父级：选了域就只看该域下的原子，避免出现「域选中但列出的原子不属于它」的错觉
  const atomOptions = useMemo(() => {
    const atoms = data?.atom_options ?? []
    if (domainCodes.length === 0) return atoms
    return atoms.filter((a) => a.domain_code && domainCodes.includes(a.domain_code))
  }, [data, domainCodes])

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
            <Select
              mode="multiple"
              allowClear
              style={{ minWidth: 200, maxWidth: 360 }}
              placeholder="工艺域（默认全部）"
              value={domainCodes}
              onChange={(values: string[]) => {
                setDomainCodes(values)
                if (values.length > 0) {
                  const allowed = new Set(
                    (data?.atom_options ?? [])
                      .filter((a) => values.includes(a.domain_code))
                      .map((a) => a.code),
                  )
                  setAtomCodes((prev) => prev.filter((atom) => allowed.has(atom)))
                }
              }}
              options={(data?.domain_options ?? []).map((d) => ({
                value: d.code,
                label: `${d.name}（${d.code}）`,
              }))}
            />
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              style={{ minWidth: 220, maxWidth: 380 }}
              placeholder="原子工艺（默认全部）"
              value={atomCodes}
              onChange={setAtomCodes}
              options={atomOptions.map((a) => ({ value: a.code, label: `${a.name}（${a.code}）` }))}
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
                {(data?.process_filter.domain_codes.length ?? 0) > 0 ||
                (data?.process_filter.atom_codes.length ?? 0) > 0 ? (
                  ` · 已按工艺筛选（只看命中该工艺的报价单）`
                ) : (
                  ''
                )}
                {points.some((p) => p.date_source === 'created') &&
                  ' · 未识别报价日期的按录入日期落点'}
                {chart.missingDate > 0 && ` · ${chart.missingDate} 个报价点无可用日期，未上图`}
                {isFetching ? ' · 刷新中…' : ''}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                横轴为报价时间（日），纵轴为价格；带（阴影区）＝当日各份报价的最高~最低价，
                点＝每一份报价（同一天多份报价各自成点）；同一天只有一份报价时上下界重合为一条线。
              </Typography.Text>
              <ResponsiveContainer width="100%" height={380}>
                <ComposedChart data={chart.rows} margin={{ top: 8, right: 24, bottom: 8, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="ts"
                    type="number"
                    scale="time"
                    domain={chart.domain}
                    ticks={chart.ticks}
                    tickFormatter={(value: number) => dayjs(value).format('MM-DD')}
                    minTickGap={8}
                  />
                  <YAxis width={70} />
                  <Tooltip content={(props) => <DayTooltip {...props} buckets={chart.buckets} />} />
                  <Legend />
                  {series.map((item) => (
                    <Area<BandRow>
                      key={item.key}
                      dataKey={(row) => [row[`${item.key}__min`], row[`${item.key}__max`]]}
                      name={item.name}
                      stroke={item.color}
                      fill={item.color}
                      fillOpacity={0.18}
                      strokeWidth={2}
                      legendType="plainline"
                      activeDot={false}
                      isAnimationActive={false}
                    />
                  ))}
                  {series.map((item) => (
                    <Scatter
                      key={`${item.key}-dots`}
                      data={chart.scatter[item.key] ?? []}
                      dataKey="value"
                      fill={item.color}
                      legendType="none"
                      isAnimationActive={false}
                    />
                  ))}
                </ComposedChart>
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
            {
              title: '命中工艺',
              key: 'matched_process',
              width: 240,
              render: (_: unknown, record: SupplierHistoryPoint) =>
                record.matched_atoms.length === 0 ? (
                  <Typography.Text type="secondary">—</Typography.Text>
                ) : (
                  <Space size={4} wrap>
                    {record.matched_atoms.map((atom) => (
                      <Tag key={atom.code} color="blue">
                        {atom.name}
                      </Tag>
                    ))}
                  </Space>
                ),
            },
          ]}
        />
      </Card>
    </Space>
  )
}
