import { useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  bindMasterProject,
  bindMasterSupplier,
  createMasterProject,
  errorDetail,
  getMasterMatch,
  listMasterCategories,
  listMasterProjects,
  listMasterSuppliers,
  registerMasterSupplier,
  unbindMasterProject,
} from '../api/client'
import type { MasterMatchProject, MasterMatchSupplier } from '../types'

interface Props {
  taskId: number
}

/**
 * 比价页「供应商 / 项目管理」模块：把解析出的供应商/项目纳入主数据管理。
 * 只刷新本模块自己的 query（master-match），不动页面其它模块。
 */
export default function MasterMatchCard({ taskId }: Props) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [supplierTarget, setSupplierTarget] = useState<MasterMatchSupplier | null>(null)
  const [bindTarget, setBindTarget] = useState<MasterMatchSupplier | null>(null)
  const [projectTarget, setProjectTarget] = useState<MasterMatchProject | null>(null)
  const [matchProjectTarget, setMatchProjectTarget] = useState<MasterMatchProject | null>(null)
  const [addForm] = Form.useForm()
  const [bindForm] = Form.useForm()
  const [matchForm] = Form.useForm()
  const [projectForm] = Form.useForm()

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['master-match', taskId],
    queryFn: () => getMasterMatch(taskId),
    enabled: Number.isFinite(taskId),
  })

  // 关联已有供应商/项目：列表都很小，一次取回后本地搜索
  const { data: allSuppliers } = useQuery({
    queryKey: ['master', 'suppliers', '__options__'],
    queryFn: () => listMasterSuppliers(''),
    enabled: bindTarget !== null,
  })
  const { data: allProjects } = useQuery({
    queryKey: ['master', 'projects', '__options__'],
    queryFn: () => listMasterProjects(''),
    enabled: matchProjectTarget !== null,
  })
  const { data: categories } = useQuery({
    queryKey: ['categories'],
    queryFn: listMasterCategories,
    enabled: projectTarget !== null,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['master-match', taskId] })

  const registerMut = useMutation({
    mutationFn: registerMasterSupplier,
    onSuccess: (result) => {
      message.success(
        result.created
          ? `已加入管理：${result.name}（${result.code}），绑定 ${result.bound_quotes} 张报价单`
          : `已复用已有供应商：${result.name}（${result.code}）`,
      )
      setSupplierTarget(null)
      addForm.resetFields()
      void refresh()
    },
    onError: (e) => message.error(errorDetail(e, '加入管理失败')),
  })
  const bindSupplierMut = useMutation({
    mutationFn: ({ code, quoteIds }: { code: string; quoteIds: number[] }) =>
      bindMasterSupplier(code, quoteIds),
    onSuccess: (result) => {
      message.success(`已关联，绑定 ${result.bound_quotes} 张报价单`)
      setBindTarget(null)
      bindForm.resetFields()
      void refresh()
    },
    onError: (e) => message.error(errorDetail(e, '关联供应商失败')),
  })
  const createProjectMut = useMutation({
    mutationFn: createMasterProject,
    onSuccess: (project) => {
      message.success(`项目已创建并绑定：${project.name}（${project.code}）`)
      setProjectTarget(null)
      projectForm.resetFields()
      void refresh()
      void queryClient.invalidateQueries({ queryKey: ['master', 'projects'] })
    },
    onError: (e) => message.error(errorDetail(e, '创建项目失败')),
  })
  const bindProjectMut = useMutation({
    mutationFn: ({ code, quoteIds }: { code: string; quoteIds: number[] }) =>
      bindMasterProject(code, quoteIds),
    onSuccess: (result) => {
      message.success(`项目已匹配，绑定 ${result.bound_quotes} 张报价单`)
      setMatchProjectTarget(null)
      matchForm.resetFields()
      void refresh()
    },
    onError: (e) => message.error(errorDetail(e, '匹配项目失败')),
  })
  const unbindProjectMut = useMutation({
    mutationFn: ({ code, quoteIds }: { code: string; quoteIds: number[] }) =>
      unbindMasterProject(code, quoteIds),
    onSuccess: () => {
      message.success('项目已解绑')
      void refresh()
    },
    onError: (e) => message.error(errorDetail(e, '解绑失败')),
  })

  const supplierColumns = [
    {
      title: '识别到的供应商',
      dataIndex: 'name',
      render: (name: string, record: MasterMatchSupplier) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{name}</Typography.Text>
          {record.part_names.length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              零件：{record.part_names.join('、')}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '报价单',
      dataIndex: 'quote_count',
      width: 90,
      render: (n: number) => `${n} 份`,
    },
    {
      title: '管理状态',
      key: 'status',
      width: 210,
      render: (_: unknown, record: MasterMatchSupplier) =>
        record.managed ? (
          <Space size={4}>
            <Tag color="green">已管理</Tag>
            <Typography.Text type="secondary">{record.supplier_code}</Typography.Text>
            <Link
              to={`/suppliers/${record.supplier_code}${
                record.categories.length === 1 ? `?category=${record.categories[0]}` : ''
              }`}
            >
              历史报价
            </Link>
          </Space>
        ) : (
          <Tag color="orange">未管理</Tag>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 190,
      render: (_: unknown, record: MasterMatchSupplier) =>
        record.managed ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Space>
            <Button
              size="small"
              type="primary"
              onClick={() => {
                setSupplierTarget(record)
                addForm.setFieldsValue({ name: record.name, alias: '' })
              }}
            >
              加入管理
            </Button>
            <Button
              size="small"
              onClick={() => {
                setBindTarget(record)
                bindForm.setFieldsValue({ code: undefined })
              }}
            >
              关联已有
            </Button>
          </Space>
        ),
    },
  ]

  const projectColumns = [
    {
      title: '识别到的项目',
      dataIndex: 'name',
      render: (name: string) => <Typography.Text strong>{name}</Typography.Text>,
    },
    {
      title: '报价单',
      dataIndex: 'quote_count',
      width: 90,
      render: (n: number) => `${n} 份`,
    },
    {
      title: '匹配状态',
      key: 'status',
      width: 210,
      render: (_: unknown, record: MasterMatchProject) =>
        record.bound ? (
          <Space size={4}>
            <Tag color="green">已匹配</Tag>
            <Typography.Text type="secondary">{record.project_code}</Typography.Text>
            {record.project_name && record.project_name !== record.name && (
              <Typography.Text type="secondary">（{record.project_name}）</Typography.Text>
            )}
          </Space>
        ) : (
          <Tag color="orange">未匹配</Tag>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 230,
      render: (_: unknown, record: MasterMatchProject) =>
        record.bound ? (
          <Button
            size="small"
            onClick={() =>
              unbindProjectMut.mutate({
                code: record.project_code!,
                quoteIds: record.quote_ids,
              })
            }
            loading={unbindProjectMut.isPending}
          >
            解绑
          </Button>
        ) : (
          <Space>
            <Button
              size="small"
              type="primary"
              onClick={() => {
                setProjectTarget(record)
                projectForm.setFieldsValue({ name: record.name, category_code: undefined, remark: '' })
              }}
            >
              新建并绑定
            </Button>
            <Button
              size="small"
              onClick={() => {
                setMatchProjectTarget(record)
                matchForm.setFieldsValue({ code: undefined })
              }}
            >
              匹配已有
            </Button>
          </Space>
        ),
    },
  ]

  return (
    <Card
      title="供应商 / 项目管理"
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => void refetch()}
          loading={isFetching}
        >
          刷新
        </Button>
      }
    >
      {isError ? (
        <Alert type="error" message="加载供应商 / 项目匹配信息失败" description={errorDetail(error)} showIcon />
      ) : !data || (data.suppliers.length === 0 && data.projects.length === 0) ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={isLoading ? '加载中…' : '本任务暂无可管理的供应商或项目'}
        />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <div>
            <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 8 }}>
              新供应商
              <Typography.Text type="secondary" style={{ fontSize: 13, fontWeight: 400 }}>
                （{data.unmanaged_supplier_count} 家未管理）
              </Typography.Text>
            </Typography.Title>
            <Table<MasterMatchSupplier>
              rowKey="normalized"
              size="small"
              pagination={false}
              loading={isLoading}
              columns={supplierColumns}
              dataSource={data.suppliers}
              locale={{ emptyText: '本任务没有识别到供应商' }}
            />
          </div>
          <div>
            <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 8 }}>
              项目
              <Typography.Text type="secondary" style={{ fontSize: 13, fontWeight: 400 }}>
                （{data.unbound_project_count} 个未匹配）
              </Typography.Text>
            </Typography.Title>
            <Table<MasterMatchProject>
              rowKey="normalized"
              size="small"
              pagination={false}
              loading={isLoading}
              columns={projectColumns}
              dataSource={data.projects}
              locale={{ emptyText: '本任务没有识别到项目名' }}
            />
          </div>
        </Space>
      )}

      <Modal
        title={`加入供应商管理：${supplierTarget?.name ?? ''}`}
        open={supplierTarget !== null}
        onCancel={() => setSupplierTarget(null)}
        onOk={() => addForm.submit()}
        confirmLoading={registerMut.isPending}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="将自动分配供应商编码，并把同名报价单（含其它任务）一并挂到该供应商下。"
        />
        <Form
          form={addForm}
          layout="vertical"
          onFinish={(values) =>
            registerMut.mutate({
              name: values.name.trim(),
              alias: values.alias?.trim() || undefined,
              quote_ids: supplierTarget?.quote_ids ?? [],
            })
          }
        >
          <Form.Item name="name" label="供应商名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="alias"
            label="别名"
            extra="识别出的其它写法，多个用「、」分隔，后续解析可自动归到这家供应商"
          >
            <Input placeholder="如 鸿图、东莞鸿图" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`关联到已有供应商：${bindTarget?.name ?? ''}`}
        open={bindTarget !== null}
        onCancel={() => setBindTarget(null)}
        onOk={() => bindForm.submit()}
        confirmLoading={bindSupplierMut.isPending}
        destroyOnClose
      >
        <Form
          form={bindForm}
          layout="vertical"
          onFinish={(values) =>
            bindSupplierMut.mutate({
              code: values.code,
              quoteIds: bindTarget?.quote_ids ?? [],
            })
          }
        >
          <Form.Item name="code" label="已有供应商" rules={[{ required: true, message: '请选择供应商' }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="按编码 / 名称 / 别名搜索"
              options={(allSuppliers ?? []).map((s) => ({
                value: s.code,
                label: `${s.name}（${s.code}）`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`新建项目并绑定：${projectTarget?.name ?? ''}`}
        open={projectTarget !== null}
        onCancel={() => setProjectTarget(null)}
        onOk={() => projectForm.submit()}
        confirmLoading={createProjectMut.isPending}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="项目 = 公司内部的一个 SKU 产品；同名报价单（含其它任务）会一并绑定。"
        />
        <Form
          form={projectForm}
          layout="vertical"
          onFinish={(values) =>
            createProjectMut.mutate({
              name: values.name.trim(),
              category_code: values.category_code || undefined,
              remark: values.remark?.trim() || undefined,
              quote_ids: projectTarget?.quote_ids ?? [],
            })
          }
        >
          <Form.Item name="name" label="项目名称" rules={[{ required: true }]}>
            <Input />
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
        title={`匹配已有项目：${matchProjectTarget?.name ?? ''}`}
        open={matchProjectTarget !== null}
        onCancel={() => setMatchProjectTarget(null)}
        onOk={() => matchForm.submit()}
        confirmLoading={bindProjectMut.isPending}
        destroyOnClose
      >
        <Form
          form={matchForm}
          layout="vertical"
          onFinish={(values) =>
            bindProjectMut.mutate({
              code: values.code,
              quoteIds: matchProjectTarget?.quote_ids ?? [],
            })
          }
        >
          <Form.Item name="code" label="已有项目" rules={[{ required: true, message: '请选择项目' }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="按编码 / 名称搜索"
              options={(allProjects ?? []).map((p) => ({
                value: p.code,
                label: `${p.name}（${p.code}）`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
