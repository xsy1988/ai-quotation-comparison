import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Segmented,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { errorDetail, getQuote, httpStatus } from '../api/client'
import type { QuoteLine, QuoteToolingLine } from '../types'
import Amount from '../components/Amount'
import { CollapsibleText } from '../components/MarkdownLite'

const MODULE_LABEL: Record<string, string> = {
  materials: '材料费',
  processing: '加工费',
  inspection: '检验费',
  packaging_transport: '包装运输费',
  sga_tax: '损管利税',
  other: '其它费用',
}

const PARSE_STATUS: Record<string, { text: string; color: string }> = {
  pending: { text: '待解析', color: 'default' },
  parsed: { text: '已解析', color: 'green' },
  in_comparison: { text: '已入比价', color: 'blue' },
  reviewed: { text: '已复核', color: 'cyan' },
  failed: { text: '解析失败', color: 'red' },
}

const CONFIRM_LABEL: Record<string, { text: string; color: string }> = {
  auto: { text: '自动', color: 'default' },
  pending: { text: '待确认', color: 'gold' },
  confirmed: { text: '已确认', color: 'green' },
  corrected: { text: '已修正', color: 'cyan' },
}

const BASIC_LABELS: [string, string][] = [
  ['project_name', '项目名称'],
  ['part_name', '零件名称'],
  ['scheme', '方案'],
  ['material_spec', '材料规格'],
  ['quote_date', '报价日期'],
  ['currency', '币种'],
  ['moq', '起订量'],
  ['quote_no', '报价单号'],
  ['source_file', '来源文件'],
]

function basicEntries(basic: Record<string, unknown>) {
  return BASIC_LABELS.filter(([key]) => basic[key] !== null && basic[key] !== undefined).map(
    ([key, label]) => ({ label, value: String(basic[key]) }),
  )
}

export default function QuoteDetailPage() {
  const { id } = useParams<{ id: string }>()
  const quoteId = Number(id)
  const navigate = useNavigate()
  const [activeModule, setActiveModule] = useState('all')

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['quote', quoteId],
    queryFn: () => getQuote(quoteId),
    enabled: Number.isFinite(quoteId),
  })

  const moduleOptions = useMemo(
    () => [
      { label: '全部', value: 'all' },
      ...(data?.modules ?? [])
        .filter((m) => m.total !== null)
        .map((m) => ({ label: m.name, value: m.module })),
    ],
    [data],
  )

  const lines = useMemo(() => {
    const all = data?.lines ?? []
    return activeModule === 'all' ? all : all.filter((l) => l.module === activeModule)
  }, [data, activeModule])

  if (isError) {
    const notFound = httpStatus(error) === 404
    return (
      <Alert
        type="error"
        message={notFound ? '报价单不存在或已被删除' : '加载报价单解析结果失败'}
        description={errorDetail(error)}
        showIcon
        action={
          <Button size="small" onClick={() => navigate('/quotes')}>
            返回报价单数据
          </Button>
        }
      />
    )
  }
  if (isLoading || !data) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
      </div>
    )
  }

  const lineColumns: ColumnsType<QuoteLine> = [
    {
      title: '模块',
      dataIndex: 'module',
      width: 104,
      render: (m: string) => MODULE_LABEL[m] ?? m,
    },
    { title: '条目', dataIndex: 'item_name', render: (v: string | null) => v ?? '—' },
    { title: '类型', dataIndex: 'item_type', width: 88, render: (v: string | null) => v ?? '—' },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 100,
      align: 'right',
      render: (v: number | null) => <Amount value={v} />,
    },
    {
      title: '原子工艺',
      dataIndex: 'atom_name',
      width: 140,
      render: (name: string | null, record) => {
        if (name) {
          return (
            <Tooltip title={record.atom_code ?? ''}>
              <Tag color="blue">{name}</Tag>
            </Tooltip>
          )
        }
        return record.is_new_process ? <Tag color="magenta">新工艺待建</Tag> : '—'
      },
    },
    {
      title: '校验',
      width: 180,
      render: (_, record) => (
        <Space size={4} wrap>
          {record.bundle_flag && <Tag>打包项</Tag>}
          {record.cross_check?.verdict && (
            <Tag color={record.cross_check.verdict === 'pass' ? 'green' : 'orange'}>
              {record.cross_check.verdict === 'pass' ? '勾稽通过' : '勾稽异常'}
            </Tag>
          )}
          {record.confirm_status && (
            <Tag color={CONFIRM_LABEL[record.confirm_status]?.color}>
              {CONFIRM_LABEL[record.confirm_status]?.text ?? record.confirm_status}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: '匹配路径',
      dataIndex: 'match_path',
      width: 160,
      responsive: ['xl'],
      render: (v: string | null) => v ?? '—',
    },
    {
      title: '备注',
      dataIndex: 'note',
      responsive: ['xl'],
      render: (v: string | null) => v ?? '—',
    },
  ]

  const toolingColumns: ColumnsType<QuoteToolingLine> = [
    { title: '类别', dataIndex: 'type_name', width: 100 },
    { title: '名称', dataIndex: 'item_name', render: (v: string | null) => v ?? '—' },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 120,
      align: 'right',
      render: (v: number | null) => <Amount value={v} />,
    },
    { title: '穴数', dataIndex: 'cavity_count', width: 90, render: (v: number | null) => v ?? '—' },
    { title: '寿命', dataIndex: 'lifespan', width: 120, render: (v: number | null) => v ?? '—' },
    { title: '备注', dataIndex: 'note', render: (v: string | null) => v ?? '—' },
  ]

  const status = PARSE_STATUS[data.parse_status]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Space wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          报价单 #{data.quote_id} · {data.supplier_name}
        </Typography.Title>
        <Tag color={status?.color ?? 'default'}>{status?.text ?? data.parse_status}</Tag>
        {data.calc_check === 'fail' && <Tag color="orange">勾稽异常</Tag>}
        {data.flags.map((f) => (
          <Tag key={f} color="gold">
            {f}
          </Tag>
        ))}
      </Space>
      <Space wrap>
        <Link to="/quotes">
          <Button size="small">返回列表</Button>
        </Link>
        <Link to={`/tasks/${data.task_id}/comparison`}>
          <Button size="small" type="primary">
            进入比价界面
          </Button>
        </Link>
      </Space>

      <Card title="基本信息" size="small">
        <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} bordered>
          {basicEntries(data.basic).map((entry) => (
            <Descriptions.Item
              key={entry.label}
              label={entry.label}
              span={entry.value.length > 20 ? 3 : 1}
            >
              {entry.value}
            </Descriptions.Item>
          ))}
          {data.category_code && (
            <Descriptions.Item label="品类编码" span={1}>
              {data.category_code}
            </Descriptions.Item>
          )}
        </Descriptions>
      </Card>

      <Card title="单价结构" size="small">
        <Descriptions size="small" column={6} bordered>
          {data.modules
            .filter((m) => m.total !== null)
            .map((m) => (
              <Descriptions.Item key={m.module} label={m.name}>
                <Amount value={m.total} />
              </Descriptions.Item>
            ))}
        </Descriptions>
        <Descriptions size="small" column={5} style={{ marginTop: 12 }}>
          <Descriptions.Item label="未税合计">
            <Amount value={data.summary.untaxed_total} />
          </Descriptions.Item>
          <Descriptions.Item label="税额">
            <Amount value={data.summary.tax_amount} />
          </Descriptions.Item>
          <Descriptions.Item label="折扣">
            <Amount value={data.summary.discount} />
          </Descriptions.Item>
          <Descriptions.Item label="含税单价">
            <Amount value={data.summary.final_unit_price_taxed} strong />
          </Descriptions.Item>
          <Descriptions.Item label="模具合计">
            <Amount value={data.summary.tooling_total} />
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card
        title="费用明细"
        size="small"
        extra={
          <Segmented
            size="small"
            options={moduleOptions}
            value={activeModule}
            onChange={(v) => setActiveModule(String(v))}
          />
        }
      >
        <Table<QuoteLine>
          rowKey="id"
          size="small"
          columns={lineColumns}
          dataSource={lines}
          scroll={{ x: 'max-content' }}
          pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: false }}
          locale={{ emptyText: '无明细条目（供应商仅报模块总价）' }}
        />
      </Card>

      <Card title="模具/治具费用" size="small">
        <Table<QuoteToolingLine>
          rowKey={(r) => `${r.tooling_type}-${r.item_name ?? ''}-${String(r.amount ?? '')}`}
          size="small"
          columns={toolingColumns}
          dataSource={data.tooling}
          pagination={false}
          locale={{ emptyText: '未识别到模具/治具费用' }}
        />
      </Card>

      <Card title="其它信息" size="small">
        {data.other_info ? (
          <CollapsibleText text={data.other_info} />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="解析时未识别到额外信息" />
        )}
      </Card>
    </Space>
  )
}
