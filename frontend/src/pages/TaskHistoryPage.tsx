import { useMemo, useState } from 'react'
import { Alert, Button, Input, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { getTasks } from '../api/client'
import type { TaskListItem } from '../types'

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  pending: { text: '待解析', color: 'default' },
  parsing: { text: '解析中', color: 'processing' },
  parsed: { text: '已解析', color: 'green' },
  reviewed: { text: '已复核', color: 'cyan' },
  failed: { text: '失败', color: 'red' },
}

export default function TaskHistoryPage() {
  const [q, setQ] = useState('')
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['tasks'],
    queryFn: getTasks,
  })

  const rows = useMemo(() => {
    const all = data ?? []
    if (!q.trim()) return all
    return all.filter((t) =>
      [t.project_name, String(t.id)].some((v) => v?.includes(q.trim())),
    )
  }, [data, q])

  const columns: ColumnsType<TaskListItem> = [
    { title: '任务', dataIndex: 'id', width: 90, render: (id: number) => `#${id}` },
    { title: '项目名称', dataIndex: 'project_name', render: (v: string) => v || '—' },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (status: string) => {
        const def = STATUS_LABEL[status]
        return <Tag color={def?.color ?? 'default'}>{def?.text ?? status}</Tag>
      },
    },
    { title: '报价单数', dataIndex: 'quote_count', width: 100 },
    { title: '创建时间', dataIndex: 'created_at', width: 180 },
    {
      title: '操作',
      width: 220,
      render: (_, record) => (
        <Space>
          <Link to={`/tasks/${record.id}/comparison`}>
            <Button size="small" type="primary">
              查看比价结果
            </Button>
          </Link>
          <Link to={`/tasks/${record.id}/progress`}>
            <Button size="small">解析进度</Button>
          </Link>
        </Space>
      ),
    },
  ]

  if (isError) {
    return (
      <Alert
        type="error"
        message="加载报价对比历史失败"
        description={error instanceof Error ? error.message : String(error)}
        showIcon
      />
    )
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Typography.Title level={4} style={{ margin: 0 }}>
          报价对比历史
        </Typography.Title>
        <Input.Search
          allowClear
          placeholder="按项目名称/任务号筛选"
          style={{ width: 260 }}
          onSearch={setQ}
          onChange={(e) => {
            if (e.target.value === '') setQ('')
          }}
        />
      </Space>
      <Table<TaskListItem>
        rowKey="id"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        locale={{ emptyText: '暂无对比任务，先去「发起报价对比」上传报价单' }}
      />
    </Space>
  )
}
