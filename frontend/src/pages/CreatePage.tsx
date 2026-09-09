import { useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  List,
  message,
  Modal,
  Tag,
  Typography,
  Upload,
} from 'antd'
import { HistoryOutlined, InboxOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import dayjs from 'dayjs'
import { createTask, getTasks } from '../api/client'

const DONE_STATUSES = new Set(['parsed', 'reviewed'])

export default function CreatePage() {
  const navigate = useNavigate()
  const [form] = Form.useForm<{ project_name: string }>()
  const [fileList, setFileList] = useState<import('antd').UploadFile[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)

  const { data: tasks, isFetching: tasksLoading } = useQuery({
    queryKey: ['tasks'],
    queryFn: getTasks,
    enabled: historyOpen,
  })

  const mutation = useMutation({
    mutationFn: ({ files, projectName }: { files: File[]; projectName: string }) =>
      createTask(files, projectName),
    onSuccess: ({ task_id }) => {
      message.success('对比任务已创建，开始解析')
      navigate(`/tasks/${task_id}/progress`)
    },
    onError: (err) => {
      message.error(`任务创建失败：${err instanceof Error ? err.message : String(err)}`)
    },
  })

  const submit = async () => {
    const values = await form.validateFields()
    const rawFiles = fileList
      .map((f) => f.originFileObj)
      .filter((f): f is NonNullable<typeof f> => f instanceof File)
    if (rawFiles.length === 0) {
      message.warning('请至少上传一个 .xlsx 报价文件')
      return
    }
    mutation.mutate({ files: rawFiles, projectName: values.project_name })
  }

  return (
    <Card style={{ maxWidth: 720, margin: '40px auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          发起报价对比
        </Typography.Title>
        <Button icon={<HistoryOutlined />} onClick={() => setHistoryOpen(true)}>
          历史任务
        </Button>
      </div>
      <Form form={form} layout="vertical">
        <Form.Item
          name="project_name"
          label="项目名称"
          rules={[{ required: true, message: '请填写项目名称' }]}
        >
          <Input placeholder="例如：新能源汽车电控壳体" />
        </Form.Item>
        <Form.Item label="报价文件（.xlsx，可多选多家供应商）" required>
          <Upload.Dragger
            multiple
            accept=".xlsx"
            fileList={fileList}
            beforeUpload={(file) => {
              if (!file.name.toLowerCase().endsWith('.xlsx')) {
                message.error(`仅支持 .xlsx 文件：${file.name}`)
                return Upload.LIST_IGNORE
              }
              return false // 阻止自动上传，提交时手动上传
            }}
            onChange={({ fileList: list }) => setFileList(list)}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽报价文件到此区域</p>
            <p className="ant-upload-hint">每个文件视为一家供应商的报价单</p>
          </Upload.Dragger>
        </Form.Item>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          品类由 Agent 依据报价内容自动判定，无需手动选择。
        </Typography.Paragraph>
        <Button
          type="primary"
          size="large"
          block
          loading={mutation.isPending}
          onClick={submit}
        >
          提交并开始解析
        </Button>
      </Form>

      <Modal
        title="历史任务"
        open={historyOpen}
        footer={null}
        onCancel={() => setHistoryOpen(false)}
      >
        <List
          loading={tasksLoading}
          dataSource={tasks ?? []}
          locale={{ emptyText: '暂无历史任务' }}
          renderItem={(task) => (
            <List.Item
              style={{ cursor: 'pointer' }}
              onClick={() => {
                setHistoryOpen(false)
                navigate(
                  DONE_STATUSES.has(task.status)
                    ? `/tasks/${task.id}/comparison`
                    : `/tasks/${task.id}/progress`,
                )
              }}
            >
              <List.Item.Meta
                title={
                  <span>
                    #{task.id} {task.project_name}{' '}
                    <Tag color={DONE_STATUSES.has(task.status) ? 'green' : 'processing'}>
                      {task.status}
                    </Tag>
                  </span>
                }
                description={`${task.quote_count} 份报价 · ${dayjs(task.created_at).format('YYYY-MM-DD HH:mm')}`}
              />
            </List.Item>
          )}
        />
      </Modal>
    </Card>
  )
}
