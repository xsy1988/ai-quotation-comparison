import { useState } from 'react'
import { App, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Spin, Tag, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { NewAtomSuggestion } from '../types'
import {
  errorDetail,
  getAtoms,
  getCategories,
  getClasses,
  getDomains,
  getStages,
  getSuggestions,
  resolveSuggestion,
} from '../api/client'
import type { ResolveSuggestionPayload } from '../api/client'
import Amount from './Amount'

interface Props {
  taskId: number
}

type ModalKind = 'create' | 'merge'

/** 新工艺决策卡：清单外工艺（兜底原子 + is_new_process）聚合为建议队列，逐条决策。 */
export default function NewProcessCard({ taskId }: Props) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [active, setActive] = useState<{ suggestion: NewAtomSuggestion; kind: ModalKind } | null>(null)
  const [atomSearch, setAtomSearch] = useState('')
  const [createForm] = Form.useForm()
  const [mergeForm] = Form.useForm()

  const { data: suggestions, isLoading } = useQuery({
    queryKey: ['suggestions', taskId],
    queryFn: () => getSuggestions(taskId),
    enabled: Number.isFinite(taskId),
  })

  const domainsQuery = useQuery({
    queryKey: ['domains'],
    queryFn: getDomains,
    enabled: active?.kind === 'create',
  })
  const stagesQuery = useQuery({
    queryKey: ['stages'],
    queryFn: getStages,
    enabled: active?.kind === 'create',
  })
  const classesQuery = useQuery({
    queryKey: ['classes'],
    queryFn: getClasses,
    enabled: active?.kind === 'create',
  })
  const categoriesQuery = useQuery({
    queryKey: ['categories'],
    queryFn: getCategories,
    enabled: active?.kind === 'create',
  })
  const atomsQuery = useQuery({
    queryKey: ['atoms', atomSearch],
    queryFn: () => getAtoms(atomSearch),
    enabled: active?.kind === 'merge',
  })

  const mutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: ResolveSuggestionPayload }) =>
      resolveSuggestion(id, payload),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['comparison', taskId] })
      queryClient.invalidateQueries({ queryKey: ['suggestions', taskId] })
      if (result.action === 'create') {
        message.success(`已新增原子 ${result.atom_code} 并回流别名`)
      } else if (result.action === 'merge') {
        message.success(`已归并至 ${result.atom_code} 并回流别名`)
      } else {
        message.success('已忽略该新工艺建议')
      }
      setActive(null)
    },
    onError: (e) => message.error(`操作失败：${errorDetail(e)}`),
  })

  if (!isLoading && (!suggestions || suggestions.length === 0)) return null

  const openModal = (suggestion: NewAtomSuggestion, kind: ModalKind) => {
    if (kind === 'create') {
      createForm.setFieldsValue({
        name: suggestion.suggested_name ?? suggestion.source_text,
        domain_code: suggestion.suggested_domain_code ?? undefined,
        stage_name: suggestion.suggested_stage_name ?? undefined,
      })
    } else {
      mergeForm.resetFields()
      setAtomSearch(suggestion.source_text)
    }
    setActive({ suggestion, kind })
  }

  const submitCreate = async () => {
    if (!active) return
    const values = await createForm.validateFields()
    mutation.mutate({
      id: active.suggestion.id,
      payload: { action: 'create', ...values },
    })
  }

  const submitMerge = async () => {
    if (!active) return
    const values = await mergeForm.validateFields()
    mutation.mutate({
      id: active.suggestion.id,
      payload: { action: 'merge', atom_code: values.atom_code },
    })
  }

  return (
    <>
      <Card size="small" title="新工艺决策">
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 12 }}>
          以下写法未命中原子清单（当前挂在兜底原子上）。可新增原子并回流别名、归并到现有原子，或忽略。
        </Typography.Paragraph>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {(suggestions ?? []).map((s) => (
            <Card
              key={s.id}
              size="small"
              type="inner"
              title={
                <Space size={8}>
                  <Typography.Text strong>{s.source_text}</Typography.Text>
                  <Tag color="purple">出现 {s.occurrence_count} 次</Tag>
                  <Tag color="blue">
                    <Amount value={s.amount} />
                  </Tag>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {s.supplier_name ? `报价单 #${s.quote_id}（${s.supplier_name}）` : `报价单 #${s.quote_id}`}
                  </Typography.Text>
                </Space>
              }
              extra={
                <Space>
                  <Button size="small" type="primary" onClick={() => openModal(s, 'create')}>
                    新增原子
                  </Button>
                  <Button size="small" onClick={() => openModal(s, 'merge')}>
                    归并现有
                  </Button>
                  <Popconfirm
                    title="忽略该新工艺建议？"
                    description="条目保留在兜底原子上，不再提示。"
                    okText="忽略"
                    cancelText="取消"
                    onConfirm={() =>
                      mutation.mutate({ id: s.id, payload: { action: 'ignore' } })
                    }
                  >
                    <Button size="small">忽略</Button>
                  </Popconfirm>
                </Space>
              }
            >
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                跨报价单按原文写法聚合，决策后所有同名条目一并改挂。
              </Typography.Text>
            </Card>
          ))}
        </Space>
      </Card>

      <Modal
        title={`新增原子：${active?.suggestion.source_text ?? ''}`}
        open={active?.kind === 'create'}
        onOk={submitCreate}
        onCancel={() => setActive(null)}
        confirmLoading={mutation.isPending}
        okText="创建并改挂"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={createForm} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="name"
            label="原子名称"
            rules={[{ required: true, message: '请输入原子名称' }]}
          >
            <Input placeholder="如：电火花加工" />
          </Form.Item>
          <Form.Item
            name="domain_code"
            label="工艺域"
            rules={[{ required: true, message: '请选择工艺域' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择工艺域（决定编码前缀）"
              loading={domainsQuery.isLoading}
              options={(domainsQuery.data ?? []).map((d) => ({
                value: d.code,
                label: `${d.code} ${d.name}`,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="stage_name"
            label="工艺阶段"
            rules={[{ required: true, message: '请选择工艺阶段' }]}
          >
            <Select
              showSearch
              placeholder="选择工艺阶段"
              loading={stagesQuery.isLoading}
              options={(stagesQuery.data ?? []).map((s) => ({ value: s, label: s }))}
            />
          </Form.Item>
          <Form.Item
            name="class_name"
            label="工艺类别"
            rules={[{ required: true, message: '请选择工艺类别' }]}
          >
            <Select
              showSearch
              placeholder="选择工艺类别"
              loading={classesQuery.isLoading}
              options={(classesQuery.data ?? []).map((c) => ({ value: c, label: c }))}
            />
          </Form.Item>
          <Form.Item name="category_code" label="适用品类（可选）">
            <Select
              showSearch
              optionFilterProp="label"
              allowClear
              placeholder="选择适用品类"
              loading={categoriesQuery.isLoading}
              options={(categoriesQuery.data ?? []).map((c) => ({
                value: c.code,
                label: `${c.code} ${c.name}`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`归并现有原子：${active?.suggestion.source_text ?? ''}`}
        open={active?.kind === 'merge'}
        onOk={submitMerge}
        onCancel={() => setActive(null)}
        confirmLoading={mutation.isPending}
        okText="归并并改挂"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={mergeForm} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="atom_code"
            label="目标原子"
            rules={[{ required: true, message: '请选择要归并到的原子' }]}
          >
            <Select
              showSearch
              filterOption={false}
              placeholder="搜索原子编码/名称"
              onSearch={setAtomSearch}
              notFoundContent={atomsQuery.isLoading ? <Spin size="small" /> : null}
              options={(atomsQuery.data ?? []).map((a) => ({
                value: a.code,
                label: `${a.code} ${a.name}`,
              }))}
            />
          </Form.Item>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            归并后原文写法将作为别名回流到词库（source=new_process），同名条目全部改挂该原子。
          </Typography.Text>
        </Form>
      </Modal>
    </>
  )
}
