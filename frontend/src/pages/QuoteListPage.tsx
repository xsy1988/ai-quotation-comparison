import { useState } from 'react'
import { Alert, Button, Card, Input, Select, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { listQuotes } from '../api/client'
import type { QuoteListItem } from '../types'
import Amount from '../components/Amount'

const PARSE_STATUS: Record<string, { text: string; color: string }> = {
  pending: { text: '待解析', color: 'default' },
  parsed: { text: '已解析', color: 'green' },
  in_comparison: { text: '已入比价', color: 'blue' },
  reviewed: { text: '已复核', color: 'cyan' },
  failed: { text: '解析失败', color: 'red' },
}

/** 列宽即最小宽度：内容更宽时整表横向滚动，不把中文挤成一列一字 */
/** 列宽：11 列合计约 1290px，1440 宽的屏幕不出现横向滚动，更窄时才滚动 */
const COL_WIDTH = {
  quote: 84,
  supplier: 220,
  part: 130,
  category: 86,
  price: 110,
  status: 136,
  lines: 80,
  other: 88,
  task: 88,
  created: 130,
  action: 120,
}

/** 创建时间只到分钟：秒对排查无意义，反而撑宽列 */
function shortTime(value: string): string {
  return value ? value.slice(0, 16) : '—'
}

export default function QuoteListPage() {
  const [q, setQ] = useState('')
  const [parseStatus, setParseStatus] = useState<string | undefined>(undefined)

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['quotes', q, parseStatus],
    queryFn: () => listQuotes({ q: q || undefined, parse_status: parseStatus }),
  })

  const columns: ColumnsType<QuoteListItem> = [
    {
      title: '报价单',
      dataIndex: 'quote_id',
      width: COL_WIDTH.quote,
      render: (id: number) => <Typography.Text type="secondary">#{id}</Typography.Text>,
    },
    {
      title: '供应商',
      dataIndex: 'supplier_name',
      width: COL_WIDTH.supplier,
    },
    {
      title: '零件名称',
      dataIndex: 'part_name',
      width: COL_WIDTH.part,
      render: (v: string | null) => v ?? <span className="na-cell">—</span>,
    },
    {
      title: '品类',
      dataIndex: 'category_name',
      width: COL_WIDTH.category,
      render: (v: string | null) => v ?? <span className="na-cell">—</span>,
    },
    {
      title: '含税单价',
      dataIndex: 'final_unit_price_taxed',
      width: COL_WIDTH.price,
      align: 'right',
      sorter: (a, b) => (a.final_unit_price_taxed ?? -1) - (b.final_unit_price_taxed ?? -1),
      render: (v: number | null) => <Amount value={v} strong />,
    },
    {
      title: '解析状态',
      dataIndex: 'parse_status',
      width: COL_WIDTH.status,
      render: (status: string, record) => {
        const def = PARSE_STATUS[status]
        return (
          <Space size={4} wrap={false}>
            <Tag color={def?.color ?? 'default'} style={{ marginRight: 0 }}>
              {def?.text ?? status}
            </Tag>
            {record.calc_check === 'fail' && (
              <Tooltip title="分项加总与合计不一致，需核对明细">
                <Tag color="orange" style={{ marginRight: 0 }}>
                  勾稽异常
                </Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: '明细',
      dataIndex: 'line_count',
      width: COL_WIDTH.lines,
      align: 'right',
      render: (n: number) => (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {n} 条
        </Typography.Text>
      ),
    },
    {
      title: '补充信息',
      dataIndex: 'has_other_info',
      width: COL_WIDTH.other,
      align: 'center',
      render: (has: boolean) =>
        has ? (
          <Tooltip title="解析时额外识别到的信息（已剔除个人信息）">
            <Tag color="purple" style={{ marginRight: 0 }}>
              有
            </Tag>
          </Tooltip>
        ) : (
          <span className="na-cell">—</span>
        ),
    },
    {
      title: '归属任务',
      dataIndex: 'task_id',
      width: COL_WIDTH.task,
      render: (id: number, record) => (
        <Tooltip title={record.project_name ?? '对比任务'}>
          <Link to={`/tasks/${id}/comparison`}>#{id}</Link>
        </Tooltip>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: COL_WIDTH.created,
      sorter: (a, b) => a.created_at.localeCompare(b.created_at),
      render: (v: string) => (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {shortTime(v)}
        </Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: COL_WIDTH.action,
      fixed: 'right',
      render: (_, record) => (
        <Link to={`/quotes/${record.quote_id}`}>
          <Button size="small" type="link" style={{ padding: 0 }}>
            查看解析结果
          </Button>
        </Link>
      ),
    },
  ]

  if (isError) {
    return (
      <Alert
        type="error"
        message="加载报价单数据失败"
        description={error instanceof Error ? error.message : String(error)}
        showIcon
      />
    )
  }

  const total = data?.length ?? 0

  return (
    <Card
      title={
        <Space size={8} align="baseline">
          <span>报价单数据</span>
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            已解析报价单共 {total} 份
          </Typography.Text>
        </Space>
      }
      extra={
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="按供应商/零件名称搜索"
            style={{ width: 240 }}
            onSearch={setQ}
            onChange={(e) => {
              if (e.target.value === '') setQ('')
            }}
          />
          <Select
            allowClear
            placeholder="解析状态"
            style={{ width: 132 }}
            value={parseStatus}
            onChange={setParseStatus}
            options={Object.entries(PARSE_STATUS).map(([value, def]) => ({
              value,
              label: def.text,
            }))}
          />
          <Button icon={<ReloadOutlined />} loading={isFetching} onClick={() => refetch()} />
        </Space>
      }
    >
      <Table<QuoteListItem>
        rowKey="quote_id"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: false }}
        scroll={{ x: 'max-content' }}
        locale={{ emptyText: '暂无已解析报价单' }}
      />
    </Card>
  )
}
