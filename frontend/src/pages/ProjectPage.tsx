import { useState } from 'react'
import {
  App,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createMasterProject,
  deleteMasterProject,
  errorDetail,
  listMasterCategories,
  listMasterProjects,
  patchMasterProject,
} from '../api/client'
import type { MasterProject } from '../types'

/** 项目管理：项目 = 公司内部的一个具体 SKU，报价单识别出的项目名绑定到这里 */
export default function ProjectPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [q, setQ] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<MasterProject | null>(null)
  const [addForm] = Form.useForm()
  const [editForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['master', 'projects', q],
    queryFn: () => listMasterProjects(q),
  })
  const { data: categories } = useQuery({
    queryKey: ['categories'],
    queryFn: listMasterCategories,
  })

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['master', 'projects'] })
  const categoryName = (code: string | null) =>
    code ? (categories ?? []).find((c) => c.code === code)?.name ?? code : '—'

  const createMut = useMutation({
    mutationFn: createMasterProject,
    onSuccess: (project) => {
      message.success(`项目已创建：${project.name}（${project.code}）`)
      setAddOpen(false)
      addForm.resetFields()
      invalidate()
    },
    onError: (e) => message.error(errorDetail(e, '创建项目')),
  })
  const patchMut = useMutation({
    mutationFn: ({ code, patch }: { code: string; patch: { name?: string; category_code?: string; remark?: string } }) =>
      patchMasterProject(code, patch),
    onSuccess: () => {
      message.success('项目已更新')
      setEditing(null)
      invalidate()
    },
    onError: (e) => message.error(errorDetail(e, '更新项目')),
  })
  const deleteMut = useMutation({
    mutationFn: deleteMasterProject,
    onSuccess: () => {
      message.success('项目已删除')
      invalidate()
    },
    // 被报价单引用时后端返回 409 + 中文提示
    onError: (e) => message.error(errorDetail(e, '删除项目')),
  })

  const columns: ColumnsType<MasterProject> = [
    { title: '编码', dataIndex: 'code', width: 140 },
    { title: '项目名称', dataIndex: 'name' },
    {
      title: '品类',
      dataIndex: 'category_code',
      width: 200,
      render: (code: string | null) =>
        code ? <Tag color="blue">{categoryName(code)}</Tag> : <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: '已绑定报价单',
      dataIndex: 'quote_count',
      width: 130,
      render: (n?: number) => `${n ?? 0} 份`,
    },
    {
      title: '备注',
      dataIndex: 'remark',
      render: (v: string | null) => v ?? <Typography.Text type="secondary">—</Typography.Text>,
    },
    { title: '创建时间', dataIndex: 'created_at', width: 160 },
    {
      title: '操作',
      width: 130,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            onClick={() => {
              setEditing(record)
              editForm.setFieldsValue({
                name: record.name,
                category_code: record.category_code ?? undefined,
                remark: record.remark ?? '',
              })
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该项目？"
            description="仍被报价单引用时将被拒绝"
            onConfirm={() => deleteMut.mutate(record.code)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="项目管理"
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          项目 = 公司内部的一个具体 SKU，报价单识别出的项目名会匹配到这里
        </Typography.Text>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Space>
          <Input.Search
            allowClear
            placeholder="按编码 / 名称 / 备注搜索"
            style={{ width: 260 }}
            onSearch={setQ}
          />
          <Button type="primary" onClick={() => setAddOpen(true)}>
            新增项目
          </Button>
        </Space>
        <Table<MasterProject>
          rowKey="code"
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={data ?? []}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
        />
      </Space>

      <Modal
        title="新增项目"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => addForm.submit()}
        confirmLoading={createMut.isPending}
        destroyOnClose
      >
        <Form
          form={addForm}
          layout="vertical"
          onFinish={(values) =>
            createMut.mutate({
              name: values.name.trim(),
              category_code: values.category_code || undefined,
              remark: values.remark?.trim() || undefined,
            })
          }
        >
          <Form.Item name="name" label="项目名称" rules={[{ required: true, message: '请输入项目名称' }]}>
            <Input placeholder="如 主壳" />
          </Form.Item>
          <Form.Item label="项目编码" extra="留空自动分配（PRJ-xxx）">
            <Input value="自动生成" disabled />
          </Form.Item>
          <Form.Item name="category_code" label="品类">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="可选"
              options={(categories ?? []).map((c) => ({
                value: c.code,
                label: `${c.name}（${c.code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`编辑项目 ${editing?.code ?? ''}`}
        open={editing !== null}
        onCancel={() => setEditing(null)}
        onOk={() => editForm.submit()}
        confirmLoading={patchMut.isPending}
        destroyOnClose
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={(values) => {
            if (editing) {
              patchMut.mutate({
                code: editing.code,
                patch: {
                  name: values.name.trim(),
                  category_code: values.category_code || '',
                  remark: values.remark ?? '',
                },
              })
            }
          }}
        >
          <Form.Item name="name" label="项目名称" rules={[{ required: true, message: '请输入项目名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="category_code" label="品类">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              options={(categories ?? []).map((c) => ({
                value: c.code,
                label: `${c.name}（${c.code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
