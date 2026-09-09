import { useMemo, useRef, useState } from 'react'
import {
  App,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Transfer,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createMasterAlias,
  createMasterAtom,
  createMasterCategory,
  createMasterDimGroup,
  createMasterSupplier,
  deleteMasterAlias,
  deleteMasterAtom,
  deleteMasterCategory,
  deleteMasterDimGroup,
  deleteMasterSupplier,
  errorDetail,
  getAtoms,
  getCategories,
  getClasses,
  getDomains,
  getStages,
  listMasterAliases,
  listMasterAtoms,
  listMasterCategories,
  listMasterDimGroups,
  listMasterSuppliers,
  patchMasterAtom,
  patchMasterCategory,
  patchMasterDimGroup,
  patchMasterSupplier,
} from '../api/client'
import type {
  DimGroupScope,
  MasterAlias,
  MasterAtom,
  MasterCategory,
  MasterDimGroup,
  MasterSupplier,
} from '../types'

const SCOPE_LABELS: Record<DimGroupScope, string> = {
  process_domain: '工艺域',
  process_stage: '工艺阶段',
  process_class: '工艺类别',
  custom: '自定义',
}

const ALIAS_SOURCE_LABELS: Record<MasterAlias['source'], string> = {
  initial: '初始导入',
  manual_feedback: '人工反馈',
  new_process: '新工艺回流',
}

export default function MasterPage() {
  return (
    <Tabs
      defaultActiveKey="atoms"
      items={[
        { key: 'atoms', label: '原子', children: <AtomTab /> },
        { key: 'aliases', label: '别名', children: <AliasTab /> },
        { key: 'categories', label: '品类', children: <CategoryTab /> },
        { key: 'dim_groups', label: '抽屉分组', children: <DimGroupTab /> },
        { key: 'suppliers', label: '供应商', children: <SupplierTab /> },
      ]}
    />
  )
}

// ---------- 通用 ----------

function useShowError() {
  const { message } = App.useApp()
  return (e: unknown, action: string) => message.error(`${action}失败：${errorDetail(e)}`)
}

/** 原子远程搜索 Select（防抖调 /api/atoms） */
function AtomSearchSelect({
  value,
  onChange,
  style,
}: {
  value?: string
  onChange?: (code: string) => void
  style?: React.CSSProperties
}) {
  const [options, setOptions] = useState<{ code: string; name: string }[]>([])
  const timer = useRef<number | undefined>(undefined)

  const search = (q: string) => {
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      getAtoms(q)
        .then(setOptions)
        .catch(() => setOptions([]))
    }, 300)
  }

  return (
    <Select
      showSearch
      allowClear={false}
      value={value}
      style={style}
      placeholder="输入编码或名称搜索原子"
      filterOption={false}
      onSearch={search}
      onChange={onChange}
      options={options.map((a) => ({ value: a.code, label: `${a.code} ${a.name}` }))}
    />
  )
}

// ---------- 原子 ----------

function AtomTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const showError = useShowError()
  const [q, setQ] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<MasterAtom | null>(null)
  const [addForm] = Form.useForm()
  const [editForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['master', 'atoms', q],
    queryFn: () => listMasterAtoms(q),
  })
  const { data: domains } = useQuery({ queryKey: ['domains'], queryFn: getDomains })
  const { data: stages } = useQuery({ queryKey: ['stages'], queryFn: getStages })
  const { data: classes } = useQuery({ queryKey: ['classes'], queryFn: getClasses })
  const { data: categories } = useQuery({ queryKey: ['categories'], queryFn: getCategories })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['master', 'atoms'] })
    void queryClient.invalidateQueries({ queryKey: ['master', 'aliases'] })
    void queryClient.invalidateQueries({ queryKey: ['master', 'dimGroups'] })
  }

  const createMut = useMutation({
    mutationFn: createMasterAtom,
    onSuccess: () => {
      message.success('原子已创建')
      setAddOpen(false)
      addForm.resetFields()
      invalidate()
    },
    onError: (e) => showError(e, '创建原子'),
  })
  const patchMut = useMutation({
    mutationFn: ({ code, patch }: { code: string; patch: Record<string, string> }) =>
      patchMasterAtom(code, patch),
    onSuccess: () => {
      message.success('原子已更新')
      setEditing(null)
      invalidate()
    },
    onError: (e) => showError(e, '更新原子'),
  })
  const deleteMut = useMutation({
    mutationFn: deleteMasterAtom,
    onSuccess: () => {
      message.success('原子已删除')
      invalidate()
    },
    onError: (e) => showError(e, '删除原子'),
  })

  const columns: ColumnsType<MasterAtom> = [
    { title: '编码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name' },
    { title: '工艺域', dataIndex: 'domain_name', width: 100 },
    { title: '阶段', dataIndex: 'stage_name', width: 90 },
    { title: '类别', dataIndex: 'class_name', width: 90 },
    {
      title: '常用品类',
      dataIndex: 'categories',
      render: (cats: string[]) =>
        cats.length > 0 ? cats.map((c) => <Tag key={c}>{c}</Tag>) : '—',
    },
    { title: '备注', dataIndex: 'remark', render: (v: string | null) => v ?? '—' },
    {
      title: '操作',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            onClick={() => {
              setEditing(record)
              editForm.setFieldsValue({
                name: record.name,
                stage_name: record.stage_name,
                class_name: record.class_name,
                remark: record.remark ?? '',
              })
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该原子？"
            description="被别名/品类/报价行引用时将被拒绝"
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

  const enumSelect = (options: string[], placeholder: string) => (
    <Select
      placeholder={placeholder}
      options={options.map((s) => ({ value: s, label: s }))}
    />
  )

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Input.Search
          allowClear
          placeholder="按编码/名称搜索"
          style={{ width: 260 }}
          onSearch={setQ}
        />
        <Button type="primary" onClick={() => setAddOpen(true)}>
          新增原子
        </Button>
      </Space>
      <Table<MasterAtom>
        rowKey="code"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={{ pageSize: 20, showSizeChanger: true }}
      />

      <Modal
        title="新增原子"
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
              name: values.name,
              domain_code: values.domain_code,
              stage_name: values.stage_name,
              class_name: values.class_name,
              remark: values.remark || undefined,
              category_codes: values.category_codes || undefined,
            })
          }
        >
          <Form.Item name="name" label="原子名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="domain_code" label="工艺域" rules={[{ required: true }]}>
            <Select
              placeholder="选择工艺域"
              options={(domains ?? []).map((d) => ({ value: d.code, label: `${d.code} ${d.name}` }))}
            />
          </Form.Item>
          <Form.Item name="stage_name" label="工艺阶段" rules={[{ required: true }]}>
            {enumSelect(stages ?? [], '选择工艺阶段')}
          </Form.Item>
          <Form.Item name="class_name" label="工艺类别" rules={[{ required: true }]}>
            {enumSelect(classes ?? [], '选择工艺类别')}
          </Form.Item>
          <Form.Item name="category_codes" label="常用品类">
            <Select
              mode="multiple"
              allowClear
              placeholder="选择品类"
              options={(categories ?? []).map((c) => ({ value: c.code, label: c.name }))}
            />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`编辑原子 ${editing?.code ?? ''}`}
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
                  name: values.name,
                  stage_name: values.stage_name,
                  class_name: values.class_name,
                  remark: values.remark,
                },
              })
            }
          }}
        >
          <Form.Item name="name" label="原子名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="stage_name" label="工艺阶段" rules={[{ required: true }]}>
            {enumSelect(stages ?? [], '选择工艺阶段')}
          </Form.Item>
          <Form.Item name="class_name" label="工艺类别" rules={[{ required: true }]}>
            {enumSelect(classes ?? [], '选择工艺类别')}
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}

// ---------- 别名 ----------

function AliasTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const showError = useShowError()
  const [q, setQ] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [addForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['master', 'aliases', q],
    queryFn: () => listMasterAliases(q),
  })

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['master', 'aliases'] })

  const createMut = useMutation({
    mutationFn: createMasterAlias,
    onSuccess: () => {
      message.success('别名已创建')
      setAddOpen(false)
      addForm.resetFields()
      invalidate()
    },
    onError: (e) => showError(e, '创建别名'),
  })
  const deleteMut = useMutation({
    mutationFn: deleteMasterAlias,
    onSuccess: () => {
      message.success('别名已删除')
      invalidate()
    },
    onError: (e) => showError(e, '删除别名'),
  })

  const columns: ColumnsType<MasterAlias> = [
    { title: '别名', dataIndex: 'alias_text' },
    { title: '原子', dataIndex: 'atom_name', render: (v: string, r) => `${v}（${r.atom_code}）` },
    {
      title: '命中次数',
      dataIndex: 'hit_count',
      width: 100,
      sorter: (a, b) => a.hit_count - b.hit_count,
      defaultSortOrder: 'descend',
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 110,
      render: (v: MasterAlias['source']) => <Tag>{ALIAS_SOURCE_LABELS[v] ?? v}</Tag>,
    },
    { title: '创建时间', dataIndex: 'created_at', width: 160 },
    {
      title: '操作',
      width: 90,
      render: (_, record) => (
        <Popconfirm title="删除该别名？" onConfirm={() => deleteMut.mutate(record.id)}>
          <Button size="small" danger>
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Input.Search
          allowClear
          placeholder="按别名搜索"
          style={{ width: 260 }}
          onSearch={setQ}
        />
        <Button type="primary" onClick={() => setAddOpen(true)}>
          新增别名
        </Button>
      </Space>
      <Table<MasterAlias>
        rowKey="id"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={{ pageSize: 20, showSizeChanger: true }}
      />

      <Modal
        title="新增别名"
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
            createMut.mutate({ atom_code: values.atom_code, alias_text: values.alias_text })
          }
        >
          <Form.Item name="atom_code" label="所属原子" rules={[{ required: true }]}>
            <AtomSearchSelect style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="alias_text" label="别名文本" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}

// ---------- 品类 ----------

function CategoryTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const showError = useShowError()
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<MasterCategory | null>(null)
  const [addForm] = Form.useForm()
  const [editForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['master', 'categories'],
    queryFn: listMasterCategories,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['master', 'categories'] })
    void queryClient.invalidateQueries({ queryKey: ['master', 'atoms'] })
  }

  const createMut = useMutation({
    mutationFn: createMasterCategory,
    onSuccess: () => {
      message.success('品类已创建')
      setAddOpen(false)
      addForm.resetFields()
      invalidate()
    },
    onError: (e) => showError(e, '创建品类'),
  })
  const patchMut = useMutation({
    mutationFn: ({ code, patch }: { code: string; patch: { name: string } }) =>
      patchMasterCategory(code, patch),
    onSuccess: () => {
      message.success('品类已更新')
      setEditing(null)
      invalidate()
    },
    onError: (e) => showError(e, '更新品类'),
  })
  const deleteMut = useMutation({
    mutationFn: deleteMasterCategory,
    onSuccess: () => {
      message.success('品类已删除')
      invalidate()
    },
    onError: (e) => showError(e, '删除品类'),
  })

  const columns: ColumnsType<MasterCategory> = [
    { title: '编码', dataIndex: 'code', width: 140 },
    { title: '名称', dataIndex: 'name' },
    { title: '原子数', dataIndex: 'atom_count', width: 90 },
    { title: '创建时间', dataIndex: 'created_at', width: 160 },
    {
      title: '操作',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            onClick={() => {
              setEditing(record)
              editForm.setFieldsValue({ name: record.name })
            }}
          >
            改名
          </Button>
          <Popconfirm
            title="删除该品类？"
            description="被原子/任务/报价引用时将被拒绝"
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
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button type="primary" onClick={() => setAddOpen(true)}>
        新增品类
      </Button>
      <Table<MasterCategory>
        rowKey="code"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={{ pageSize: 20 }}
      />

      <Modal
        title="新增品类"
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
            createMut.mutate({ code: values.code.trim(), name: values.name.trim() })
          }
        >
          <Form.Item
            name="code"
            label="品类编码"
            rules={[{ required: true, pattern: /^CAT-[A-Z0-9]+$/, message: '格式应为 CAT-XXX（大写字母/数字）' }]}
          >
            <Input placeholder="如 CAT-SJ" />
          </Form.Item>
          <Form.Item name="name" label="品类名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`品类改名 ${editing?.code ?? ''}`}
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
            if (editing) patchMut.mutate({ code: editing.code, patch: { name: values.name } })
          }}
        >
          <Form.Item name="name" label="品类名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}

// ---------- 抽屉分组 ----------

interface TransferRecord {
  key: string
  title: string
  description: string
}

/** 成员穿梭框：全量原子（主数据量小，一次拉取） */
function MemberTransfer({
  dataSource,
  targetKeys,
  onChange,
}: {
  dataSource: TransferRecord[]
  targetKeys: string[]
  onChange: (keys: string[]) => void
}) {
  return (
    <Transfer<TransferRecord>
      dataSource={dataSource}
      targetKeys={targetKeys}
      onChange={(keys) => onChange(keys as string[])}
      render={(item) => item.title}
      titles={['全部原子', '组成员']}
      listStyle={{ width: 260, height: 320 }}
      filterOption={(input, item) => item.title.includes(input)}
      showSearch
    />
  )
}

function DimGroupTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const showError = useShowError()
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<MasterDimGroup | null>(null)
  const [memberEditing, setMemberEditing] = useState<MasterDimGroup | null>(null)
  const [memberKeys, setMemberKeys] = useState<string[]>([])
  const [addForm] = Form.useForm()
  const [editForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['master', 'dimGroups'],
    queryFn: listMasterDimGroups,
  })
  /** 全量原子作为成员编辑候选（GET /api/master/atoms 不分页） */
  const { data: atoms } = useQuery({
    queryKey: ['master', 'atoms', ''],
    queryFn: () => listMasterAtoms(''),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['master', 'dimGroups'] })
    void queryClient.invalidateQueries({ queryKey: ['master', 'atoms'] })
  }

  const createMut = useMutation({
    mutationFn: createMasterDimGroup,
    onSuccess: () => {
      message.success('分组已创建')
      setAddOpen(false)
      addForm.resetFields()
      invalidate()
    },
    onError: (e) => showError(e, '创建分组'),
  })
  const patchMut = useMutation({
    mutationFn: ({
      groupCode,
      patch,
    }: {
      groupCode: string
      patch: { group_name?: string; parent_code?: string | null; member_atoms?: string[] }
    }) => patchMasterDimGroup(groupCode, patch),
    onSuccess: () => {
      message.success('分组已更新')
      setEditing(null)
      setMemberEditing(null)
      invalidate()
    },
    onError: (e) => showError(e, '更新分组'),
  })
  const deleteMut = useMutation({
    mutationFn: deleteMasterDimGroup,
    onSuccess: () => {
      message.success('分组已删除')
      invalidate()
    },
    onError: (e) => showError(e, '删除分组'),
  })

  const atomOptions: TransferRecord[] = useMemo(
    () =>
      (atoms ?? []).map((a) => ({
        key: a.code,
        title: `${a.code} ${a.name}`,
        description: a.name,
      })),
    [atoms],
  )

  // 树形层级：按 parent_code 计算深度并排序（父在前），无环假设下 DFS
  const rows = useMemo(() => {
    const groups = data ?? []
    const byParent = new Map<string | null, MasterDimGroup[]>()
    for (const g of groups) {
      const list = byParent.get(g.parent_code) ?? []
      list.push(g)
      byParent.set(g.parent_code, list)
    }
    const depths = new Map<string, number>()
    const ordered: { group: MasterDimGroup; depth: number }[] = []
    const visit = (parent: string | null, depth: number) => {
      for (const g of byParent.get(parent) ?? []) {
        if (depths.has(g.group_code)) continue // 防环
        depths.set(g.group_code, depth)
        ordered.push({ group: g, depth })
        visit(g.group_code, depth + 1)
      }
    }
    visit(null, 0)
    // 循环引用兜底：未访问到的直接追加
    for (const g of groups) {
      if (!depths.has(g.group_code)) ordered.push({ group: g, depth: 0 })
    }
    return ordered
  }, [data])

  const columns: ColumnsType<{ group: MasterDimGroup; depth: number }> = [
    {
      title: '分组名称',
      dataIndex: ['group', 'group_name'],
      render: (name: string, { group, depth }) => (
        <Space size={4}>
          <span style={{ paddingLeft: depth * 20 }}>{name}</span>
          {group.is_builtin && <Tag color="blue">内置</Tag>}
        </Space>
      ),
    },
    { title: '编码', dataIndex: ['group', 'group_code'], width: 200 },
    {
      title: '维度',
      dataIndex: ['group', 'scope'],
      width: 100,
      render: (v: DimGroupScope) => <Tag>{SCOPE_LABELS[v] ?? v}</Tag>,
    },
    {
      title: '父分组',
      dataIndex: ['group', 'parent_name'],
      width: 120,
      render: (v: string | null, { group }) => v ?? group.parent_code ?? '—',
    },
    { title: '成员数', dataIndex: ['group', 'member_count'], width: 80 },
    {
      title: '操作',
      width: 200,
      render: (_, { group }) => (
        <Space>
          <Button
            size="small"
            onClick={() => {
              setMemberEditing(group)
              setMemberKeys(group.member_atoms)
            }}
          >
            成员
          </Button>
          {!group.is_builtin && (
            <>
              <Button
                size="small"
                onClick={() => {
                  setEditing(group)
                  editForm.setFieldsValue({
                    group_name: group.group_name,
                    parent_code: group.parent_code,
                  })
                }}
              >
                编辑
              </Button>
              <Popconfirm
                title="删除该分组？"
                description="内置分组或有子分组时将被拒绝"
                onConfirm={() => deleteMut.mutate(group.group_code)}
              >
                <Button size="small" danger>
                  删除
                </Button>
              </Popconfirm>
            </>
          )}
        </Space>
      ),
    },
  ]

  const groupOptions = (exclude?: string) =>
    (data ?? [])
      .filter((g) => g.group_code !== exclude)
      .map((g) => ({ value: g.group_code, label: `${g.group_name}（${g.group_code}）` }))

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button type="primary" onClick={() => setAddOpen(true)}>
        新增分组
      </Button>
      <Table<{ group: MasterDimGroup; depth: number }>
        rowKey={({ group }) => group.group_code}
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20 }}
      />

      <Modal
        title="新增抽屉分组"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => addForm.submit()}
        confirmLoading={createMut.isPending}
        destroyOnClose
        width={720}
      >
        <Form
          form={addForm}
          layout="vertical"
          onFinish={(values) =>
            createMut.mutate({
              group_code: values.group_code.trim(),
              group_name: values.group_name.trim(),
              scope: values.scope,
              parent_code: values.parent_code ?? null,
              member_atoms: values.member_atoms ?? [],
            })
          }
        >
          <Form.Item name="group_code" label="分组编码" rules={[{ required: true }]}>
            <Input placeholder="如 custom:高精密组" />
          </Form.Item>
          <Form.Item name="group_name" label="分组名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="scope" label="维度" rules={[{ required: true }]} initialValue="custom">
            <Select
              options={(Object.keys(SCOPE_LABELS) as DimGroupScope[]).map((s) => ({
                value: s,
                label: SCOPE_LABELS[s],
              }))}
            />
          </Form.Item>
          <Form.Item name="parent_code" label="父分组">
            <Select allowClear options={groupOptions()} placeholder="不选则为顶层分组" />
          </Form.Item>
          <Form.Item name="member_atoms" label="组成员" initialValue={[]}>
            <MemberPicker options={atomOptions} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`编辑分组 ${editing?.group_code ?? ''}`}
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
                groupCode: editing.group_code,
                patch: {
                  group_name: values.group_name,
                  parent_code: values.parent_code ?? null,
                },
              })
            }
          }}
        >
          <Form.Item name="group_name" label="分组名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="parent_code" label="父分组">
            <Select
              allowClear
              options={groupOptions(editing?.group_code)}
              placeholder="不选则为顶层分组"
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`编辑成员 ${memberEditing?.group_name ?? ''}`}
        open={memberEditing !== null}
        onCancel={() => setMemberEditing(null)}
        onOk={() => {
          if (memberEditing) {
            patchMut.mutate({
              groupCode: memberEditing.group_code,
              patch: { member_atoms: memberKeys },
            })
          }
        }}
        confirmLoading={patchMut.isPending}
        width={640}
        destroyOnClose
      >
        <MemberTransfer
          dataSource={atomOptions}
          targetKeys={memberKeys}
          onChange={setMemberKeys}
        />
      </Modal>
    </Space>
  )
}

/** Form 包装的成员穿梭框（用于新增分组的 Form.Item） */
function MemberPicker({
  value,
  onChange,
  options,
}: {
  value?: string[]
  onChange?: (keys: string[]) => void
  options: TransferRecord[]
}) {
  return <MemberTransfer dataSource={options} targetKeys={value ?? []} onChange={onChange ?? (() => {})} />
}

// ---------- 供应商 ----------

function SupplierTab() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const showError = useShowError()
  const [q, setQ] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<MasterSupplier | null>(null)
  const [addForm] = Form.useForm()
  const [editForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['master', 'suppliers', q],
    queryFn: () => listMasterSuppliers(q),
  })

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ['master', 'suppliers'] })

  const createMut = useMutation({
    mutationFn: createMasterSupplier,
    onSuccess: () => {
      message.success('供应商已创建')
      setAddOpen(false)
      addForm.resetFields()
      invalidate()
    },
    onError: (e) => showError(e, '创建供应商'),
  })
  const patchMut = useMutation({
    mutationFn: ({ code, patch }: { code: string; patch: { name?: string; alias?: string } }) =>
      patchMasterSupplier(code, patch),
    onSuccess: () => {
      message.success('供应商已更新')
      setEditing(null)
      invalidate()
    },
    onError: (e) => showError(e, '更新供应商'),
  })
  const deleteMut = useMutation({
    mutationFn: deleteMasterSupplier,
    onSuccess: () => {
      message.success('供应商已删除')
      invalidate()
    },
    onError: (e) => showError(e, '删除供应商'),
  })

  const columns: ColumnsType<MasterSupplier> = [
    { title: '编码', dataIndex: 'code', width: 140 },
    { title: '名称', dataIndex: 'name' },
    { title: '别名', dataIndex: 'alias', render: (v: string | null) => v ?? '—' },
    { title: '创建时间', dataIndex: 'created_at', width: 160 },
    {
      title: '操作',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            onClick={() => {
              setEditing(record)
              editForm.setFieldsValue({ name: record.name, alias: record.alias ?? '' })
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title="删除该供应商？"
            description="被报价单引用时将被拒绝"
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
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Input.Search
          allowClear
          placeholder="按编码/名称/别名搜索"
          style={{ width: 260 }}
          onSearch={setQ}
        />
        <Button type="primary" onClick={() => setAddOpen(true)}>
          新增供应商
        </Button>
      </Space>
      <Table<MasterSupplier>
        rowKey="code"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={{ pageSize: 20 }}
      />

      <Modal
        title="新增供应商"
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
              code: values.code.trim(),
              name: values.name.trim(),
              alias: values.alias || undefined,
            })
          }
        >
          <Form.Item name="code" label="供应商编码" rules={[{ required: true }]}>
            <Input placeholder="如 SUP-001" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="alias" label="别名">
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`编辑供应商 ${editing?.code ?? ''}`}
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
                patch: { name: values.name, alias: values.alias },
              })
            }
          }}
        >
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="alias" label="别名">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
